"""抢码核心：识别 → 提取 scanId → 高频轮询 → 抢占提交。

为什么这样能抢赢
----------------
人工流程是「打开森空岛 → 扫码 → 点确认」，前后要几秒。
本工具的流程是：

    解码出二维码（约 20~60ms）
      └─ 直接从字符串里切出 scanId（0 请求，参考 MHY_Scanner 切 ticket 的做法）
           └─ 以 ~100ms 间隔轮询 /general/v1/scan_status
                └─ 一旦「未扫码 → 已扫码」翻转，立刻复用同一条 keep-alive 连接
                   POST /user/auth/v1/token_by_scan_code

实测该提交接口**无鉴权**（不需要 token / cookie / appCode / 签名），
所以胜负完全取决于「翻转检测延迟 + 一次 POST 的 RTT」。
我们把它压到几十毫秒量级，这就是相对人手的全部优势。

刻意不做的事
------------
* **不用 scanId 抢跑**：``token_by_scan_code`` 只认最终的 ``scanCode``，
  拿 scanId 猛刷是无效请求，只会把自己送进风控名单。
* **不并发轰炸**：轮询用固定节奏 + 指数退避，靠时序赢，不靠流量赢。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Protocol

import httpx

from .api import (
    SCAN_STATUS_CONFIRMED,
    SCAN_STATUS_NOT_SCANNED,
    SCAN_STATUS_PENDING_CONFIRM,
    ApiError,
    HypergryphAuth,
)
from .frames import Frame
from .qrdecode import QRDecoder


class Outcome(str, Enum):
    """一次抢码任务的最终结果。"""

    SUCCESS = "success"
    LOST = "lost"  # scanCode 被别人先换走了
    EXPIRED = "expired"  # 码过期了也没等到扫码
    TIMEOUT = "timeout"  # 全局超时
    ERROR = "error"


@dataclass(slots=True)
class SnipeResult:
    outcome: Outcome
    scan_id: str | None = None
    account_token: str | None = None
    #: 从「第一次看到这个二维码」到「提交成功」的毫秒数
    latency_ms: float | None = None
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS


class Reporter(Protocol):
    """输出回调。CLI 打印、以及未来 GUI 都实现这个协议。

    ``debug`` 是可选的：只做必须的实现，其余用 :meth:`Sniper._debug` 安全转发。
    """

    def info(self, message: str) -> None: ...
    def hit(self, message: str) -> None: ...
    def success(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def debug(self, message: str) -> None: ...


class ConsoleReporter:
    """带颜色和时间戳的终端输出。"""

    COLORS = {
        "info": "\033[36m",
        "hit": "\033[33m",
        "success": "\033[32m",
        "warn": "\033[31m",
        "dim": "\033[90m",
        "reset": "\033[0m",
    }

    def __init__(self, *, color: bool = True, verbose: bool = False) -> None:
        self.color = color
        self.verbose = verbose
        self._t0 = time.time()

    def _emit(self, kind: str, message: str) -> None:
        elapsed = time.time() - self._t0
        stamp = f"[{elapsed:7.2f}s]"
        if self.color:
            c = self.COLORS.get(kind, "")
            r = self.COLORS["reset"]
            prefix = f"{self.COLORS['dim']}{stamp}{r} {c}{kind.upper():<7}{r}"
        else:
            prefix = f"{stamp} {kind.upper():<7}"
        print(f"{prefix} {message}", flush=True)

    def info(self, message: str) -> None:
        self._emit("info", message)

    def hit(self, message: str) -> None:
        self._emit("hit", message)

    def success(self, message: str) -> None:
        self._emit("success", message)

    def warn(self, message: str) -> None:
        self._emit("warn", message)

    def debug(self, message: str) -> None:
        if self.verbose:
            self._emit("dim", message)


class Sniper:
    """把一个帧源变成「抢到就退出」的循环。"""

    def __init__(
        self,
        *,
        account_token: str,
        decoder: QRDecoder | None = None,
        reporter: Reporter | None = None,
        poll_interval_ms: int = 100,
        max_scan_id_age_s: float = 180.0,
        max_processed_memory: int = 64,
        keep_going: bool = False,
        confirm_grace_s: float = 60.0,
        heartbeat_s: float = 10.0,
        grab_at_pending: bool = True,
        auto_scan: bool = True,
        auto_confirm: bool = True,
        confirm_delay_ms: int = 2000,
        confirm_delay_max_ms: int | None = 5000,
        redeem: bool = False,
        save_capture: str | None = None,
        verbose_timing: bool = False,
    ) -> None:
        self.account_token = account_token
        self.decoder = decoder or QRDecoder()
        self.reporter: Reporter = reporter or ConsoleReporter()
        self.poll_interval_ms = max(30, poll_interval_ms)
        self.max_scan_id_age_s = max_scan_id_age_s
        self.max_processed_memory = max_processed_memory
        #: True = 抢到一个后继续盯下一个码（主播通常会让多人轮流上号）
        self.keep_going = keep_going
        #: 看到「已扫码待确认」后额外给的等待秒数 —— 用户得腾出手点手机上的授权弹窗
        self.confirm_grace_s = confirm_grace_s
        #: 心跳间隔：定期汇报"服务端当前怎么看这个码"，免得用户以为卡死
        self.heartbeat_s = heartbeat_s
        #: True = 一看到「已扫码待确认」(101) 就抢先提交，不等用户点确认。
        #: 这是全部速度优势所在：人点确认要 1~2 秒，而提交只需几十毫秒。
        self.grab_at_pending = grab_at_pending
        #: ★ True = 连「扫码」都代做：用 account_token 调 scan_login +
        #: update_scan_status，等价于森空岛 App 扫码并点确认。
        #: 打开后完全不需要人掏手机 —— 见 api.SCAN_LOGIN 的注释。
        self.auto_scan = auto_scan
        #: True = 代扫码之后紧接着代「确认」。关掉它可以停在「已扫码待确认」，
        #: 用来观察客户端（游戏）到底有没有反应 —— 排障时非常有用。
        self.auto_confirm = auto_confirm
        #: 代扫码之后、代确认之前的最小间隔（毫秒）。
        #:
        #: 实测教训：一开始这两步是背靠背连发的，中间没有任何间隔。
        #: 结果服务端把"确认"算在一个**还没落成 101 的扫码**上，
        #: 客户端（游戏）那边只显示"请确认"、永远等不到登录。
        #: 真实 App 里这两步隔着"人看到提示 → 点确认"的时间，所以这里补回来。
        self.confirm_delay_ms = max(0, confirm_delay_ms)
        #: 延迟上限，与下限组成随机区间（默认 2~5 秒）。
        #:
        #: 为什么是秒级而不是几百毫秒：正常手机扫码后**不是立刻确认**的 ——
        #: App 要跳一个确认页，人得看清内容再点一下。用几百毫秒是在模拟
        #: "机器"，而固定间隔本身就是可识别的机器特征。
        #: 上限默认绝对取值 5000ms；若被传成小于下限，则退化为取下限本身
        #: （避免区间非法）。
        self.confirm_delay_max_ms = max(
            self.confirm_delay_ms,
            confirm_delay_max_ms if confirm_delay_max_ms is not None else 5000,
        )
        #: ★ 是否由**我们**去调 token_by_scan_code 消费 scanCode。
        #:
        #: 默认 **False**：扫码+确认之后，`scan_status` 变 0 并给出 scanCode，
        #: 这一跳应该由**游戏客户端自己**完成 —— 那才是正常登录流程。
        #: 我们去调它会把 scanCode 抢先消费掉，客户端随后就拿不到登录结果，
        #: 表现出来就是客户端显示"二维码已过期"。
        self.redeem = redeem
        #: 识别到扫码码时把那一帧存下来（目录路径）。None = 不存。
        #:
        #: 用途：出了问题时，"当时屏幕上到底是什么"是最难复现的信息。
        #: 留一张截图比任何日志都直接 —— 尤其是多显示器、高 DPI 缩放这类
        #: 只能人工核对的情况。
        self.save_capture = save_capture
        #: 逐帧打印"采集 + 解码"耗时。用来定位"每秒只处理几帧"到底卡在哪一段。
        self.verbose_timing = verbose_timing

        #: 已处理过的 scanId（成功或彻底失败），避免同一个码反复触发
        self._processed: OrderedDict[str, float] = OrderedDict()
        #: 正在轮询的 scanId -> 任务
        self._watchers: dict[str, asyncio.Task[SnipeResult | None]] = {}
        #: 首次见到某个 scanId 的**墙钟**时刻，用于统计端到端延迟
        self._first_seen: dict[str, float] = {}
        #: 已结算的轮询结论。注意任务完成后会被 done_callback 从 _watchers 移除，
        #: 所以必须单独记账，否则仲裁时什么都看不到（会把"已失效"误报成"超时"）。
        self._settled: list[SnipeResult] = []
        self._result: asyncio.Future[SnipeResult] | None = None

    def _debug(self, message: str) -> None:
        """安全转发 debug 日志。

        不能直接 ``self.reporter.debug(...)``：Reporter 是 Protocol，
        调用方很容易只实现必需的那几个方法。一旦这里抛 AttributeError，
        异常会被轮询任务的 done_callback 当成"任务失败"吞掉，
        结果是**整个轮询结论丢失**、最终误报成"超时"——排查代价极高。
        所以对可选方法一律做存在性检查。
        """
        debug = getattr(self.reporter, "debug", None)
        if callable(debug):
            try:
                debug(message)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 去重
    # ------------------------------------------------------------------ #

    def _note_processed(self, scan_id: str) -> None:
        self._processed[scan_id] = time.time()
        while len(self._processed) > self.max_processed_memory:
            self._processed.popitem(last=False)

    def _is_processed(self, scan_id: str) -> bool:
        return scan_id in self._processed

    def _grab_first_seen(self, scan_id: str) -> None:
        """记录首次见到该 scanId 的**墙钟**时刻，供延迟统计使用。

        注意这里刻意只存墙钟：TTL 的截止时间要用 ``time.monotonic()`` 算，
        但那个起点是"开始轮询"，在 :meth:`_watch_scan_id` 里现取即可。
        早期版本把两种时间混用，导致"剩余时间"打印出 17.9 亿秒、TTL 实际失效。
        """
        self._first_seen.setdefault(scan_id, time.time())

    # ------------------------------------------------------------------ #
    # 轮询与抢占
    # ------------------------------------------------------------------ #

    async def _run_for_scan_id(
        self, auth: HypergryphAuth, scan_id: str
    ) -> SnipeResult | None:
        """发现一个码之后要做的全部事：先代扫码（如果开启），再盯着抢提交。

        代扫码就是把"人掏手机扫"换成两次 HTTP 请求：
        ``scan_login``（登记扫码）→ ``confirm_scan``（点确认）。
        两次都在毫秒级完成，所以整条链路没有人的参与。
        """
        if self.auto_scan:
            await self._auto_scan(auth, scan_id)
        return await self._watch_scan_id(auth, scan_id)

    async def _auto_scan(self, auth: HypergryphAuth, scan_id: str) -> None:
        """用 account_token 代为扫码 + 确认。失败不致命 —— 退回原行为继续等人扫。"""
        try:
            info = await auth.scan_login(scan_id, self.account_token)
        except (httpx.HTTPError, ApiError) as exc:
            self.reporter.warn(
                f"代扫码失败（{exc}）；改为等待他人扫码。"
                "若提示 token 失效，请重新运行 login。"
            )
            return

        who = info.get("phone") or info.get("nickname") or "?"
        region = info.get("loginRegion") or ""
        self.reporter.hit(
            f"scanId {scan_id[:8]}… 已代为扫码（账号 {who}{' / ' + region if region else ''}）"
        )

        if not self.auto_confirm:
            self.reporter.info(
                "已停在「已扫码待确认」（--no-confirm）。"
                "此时去看客户端：如果它显示已扫码/等待确认，说明这一步是对的。"
            )
            return

        # ★ 关键：先等服务端把状态落成 101，再发「确认」。
        #   背靠背连发会让确认落在一个尚未建立的扫码状态上，客户端因此不认。
        # 随机等待，模拟真人：固定间隔是可识别的机器特征
        delay_ms = random.randint(self.confirm_delay_ms, self.confirm_delay_max_ms)
        if delay_ms:
            self._debug(f"代确认前随机等待 {delay_ms}ms（模拟真人点击）")
            await asyncio.sleep(delay_ms / 1000.0)

        try:
            state = await auth.get_scan_state(scan_id)
        except (httpx.HTTPError, ApiError):
            state = None

        if state is not None and state.raw_status == SCAN_STATUS_NOT_SCANNED:
            self.reporter.warn(
                f"scanId {scan_id[:8]}… 服务端仍显示未扫码，跳过代确认"
                "（确认可能因此不被采纳）"
            )
        elif state is not None and state.raw_status not in (
            SCAN_STATUS_PENDING_CONFIRM,
            SCAN_STATUS_CONFIRMED,
        ):
            self._debug(f"状态 {state.raw_status}，跳过代确认")
        else:
            try:
                await auth.confirm_scan(scan_id, self.account_token)
                self.reporter.hit(f"scanId {scan_id[:8]}… 已代为确认登录")
            except (httpx.HTTPError, ApiError) as exc:
                self.reporter.warn(f"代确认失败（{exc}），交给后续轮询等待")

    async def _watch_scan_id(self, auth: HypergryphAuth, scan_id: str) -> SnipeResult | None:
        """盯着一个 scanId，一旦可换就抢提交。None 表示没抢到且不值得再等。

        状态机（实测）：100 未扫码 → 101 已扫码待确认 → 0 已确认（返回 scanCode）。

        **100 和 101 都必须继续等**，尤其是 101 —— 那是观众正在点手机授权弹窗的时刻。
        早期版本把 101 当异常，等于在码已经贴到脸上时主动放弃。
        """
        # 首次见到这个码的时刻（用于延迟统计），以及"开始盯着它"的时刻。
        self._grab_first_seen(scan_id)
        first_seen_wall = self._first_seen.get(scan_id, time.time())
        # ⚠ TTL 必须从**开始轮询**算起，而不是从"首次看到码"算起。
        #   中间隔着代扫码 + 随机等待（可能几百毫秒到几秒）。若用后者，
        #   当 TTL 比那段耗时还短时，轮询任务一启动就已过期 —— 一次都不轮询。
        watch_start = time.monotonic()
        deadline = watch_start + self.max_scan_id_age_s
        interval = self.poll_interval_ms / 1000.0
        consecutive_errors = 0
        polls = 0
        pending_since: float | None = None
        last_status: int | None = None
        last_heartbeat = time.monotonic()
        announced_no_code = False

        while time.monotonic() < deadline:
            polls += 1
            try:
                state = await auth.get_scan_state(scan_id)
                consecutive_errors = 0
            except (httpx.HTTPError, ApiError) as exc:
                consecutive_errors += 1
                # 退避，但不退出：网络抖一下不该让我们放弃这个码
                backoff = min(interval * (2**consecutive_errors), 1.5)
                self._debug(f"轮询异常({consecutive_errors}): {exc}，退避 {backoff:.2f}s")
                await asyncio.sleep(backoff)
                continue

            # 把状态迁移记下来：排障时"到底有没有被扫"全靠它，
            # 而不是靠"最后报了失效"去猜
            if state.raw_status != last_status:
                last_status = state.raw_status
                self._debug(
                    f"scanId {scan_id[:8]}… 状态 {state.raw_status} "
                    f"({state.raw_message}) 第 {polls} 次轮询"
                )

            if state.is_ready and state.scan_code:
                if not self.redeem:
                    # 正常流程：确认完成，剩下那一跳交给**游戏客户端自己**。
                    # 我们绝不能去消费 scanCode —— 那是客户端的事。抢着消费掉，
                    # 客户端就拿不到登录结果，表现正是"二维码已过期"。
                    self.reporter.hit(
                        f"scanId {scan_id[:8]}… 已确认，登录交由客户端完成"
                        f"（未消费 scanCode）"
                    )
                    return SnipeResult(
                        outcome=Outcome.SUCCESS,
                        scan_id=scan_id,
                        latency_ms=(time.time() - first_seen_wall) * 1000.0,
                        detail="已代扫码并确认，登录由客户端完成",
                    )
                self.reporter.hit(
                    f"scanId {scan_id[:8]}… 拿到 scanCode，正在抢占提交"
                )
                return await self._redeem(auth, scan_id, state.scan_code, first_seen_wall)

            if state.raw_status == SCAN_STATUS_PENDING_CONFIRM:
                # ★ 关键：状态 101「已扫码待确认」时 scanCode 往往已经可用。
                #   `token_by_scan_code` 是无鉴权的 —— 谁先提交算谁的，
                #   所以**绝不能等用户点确认**：那 1~2 秒是白送给人手的，
                #   而我们全部的速度优势就在这一跳上。
                if pending_since is None:
                    pending_since = time.monotonic()
                    extended = pending_since + self.confirm_grace_s
                    if extended > deadline:
                        deadline = extended
                if self.grab_at_pending and self.redeem:
                    if state.scan_code:
                        self.reporter.hit(
                            f"scanId {scan_id[:8]}… 待确认阶段已拿到 scanCode，抢先提交"
                        )
                        return await self._redeem(
                            auth, scan_id, state.scan_code, first_seen_wall
                        )
                    if not announced_no_code:
                        announced_no_code = True
                        self.reporter.hit(
                            f"scanId {scan_id[:8]}… 已扫码，但待确认阶段还换不出 scanCode；"
                            f"继续等（最多 {self.confirm_grace_s:.0f}s）"
                        )
                await asyncio.sleep(interval)
                continue

            if state.raw_status != SCAN_STATUS_NOT_SCANNED:
                # 既不是"未扫码"也不是"待确认"，通常是码在我们轮询到之前就失效了
                self._debug(
                    f"scanId {scan_id[:8]}… 状态 {state.raw_status} "
                    f"({state.raw_message})，共轮询 {polls} 次"
                )
                return SnipeResult(
                    outcome=Outcome.EXPIRED,
                    scan_id=scan_id,
                    detail=(
                        f"{state.raw_message}（轮询 {polls} 次都未见到扫码）"
                    ),
                )

            # 心跳：盯一个还没被扫的码时，长时间毫无输出会让人以为卡死了。
            # 更要紧的是它把**服务端看到的状态**讲出来 —— "服务端有没有收到扫码"
            # 是排障时最关键、也最容易靠猜搞错的一件事。
            now = time.monotonic()
            if now - last_heartbeat >= self.heartbeat_s:
                last_heartbeat = now
                remaining = max(0.0, deadline - now)
                if last_status == SCAN_STATUS_NOT_SCANNED:
                    self.reporter.info(
                        f"· 仍在等扫码（scanId {scan_id[:8]}…，已轮询 {polls} 次，"
                        f"服务端状态：未扫码，剩余约 {remaining:.0f}s）"
                    )
                else:
                    self.reporter.info(
                        f"· 监视中（scanId {scan_id[:8]}…，已轮询 {polls} 次，"
                        f"剩余约 {remaining:.0f}s）"
                    )

            await asyncio.sleep(interval)

        detail = (
            f"已扫码但未在手机上确认授权（轮询 {polls} 次）"
            if pending_since is not None
            else f"盯了 {self.max_scan_id_age_s:.0f}s 未被扫描（轮询 {polls} 次）"
        )
        # 这条诊断必须有：TTL 到期走的是"正常退出循环"的路径，
        # 不打日志的话，"这个码被放弃了"在输出里完全不可见，排障时会误以为还在盯。
        self._debug(
            f"scanId {scan_id[:8]}… 放弃：{detail}"
            f"（从开始盯到放弃共 {time.monotonic() - watch_start:.1f}s）"
        )
        return SnipeResult(outcome=Outcome.EXPIRED, scan_id=scan_id, detail=detail)

    async def _redeem(
        self,
        auth: HypergryphAuth,
        scan_id: str,
        scan_code: str,
        first_seen_wall: float,
    ) -> SnipeResult:
        """抢占提交 —— 全流程唯一决定胜负的一跳。"""
        latency_ms = max(0.0, (time.time() - first_seen_wall) * 1000.0)
        try:
            data = await auth.redeem_scan_code(scan_code)
        except ApiError as exc:
            # 最常见的是被别人抢先：服务端会说"登录失败"
            outcome = Outcome.LOST
            detail = str(exc)
            if "过期" in detail or "expire" in detail.lower():
                outcome = Outcome.EXPIRED
            return SnipeResult(
                outcome=outcome, scan_id=scan_id, detail=detail, latency_ms=latency_ms
            )
        except httpx.HTTPError as exc:
            return SnipeResult(
                outcome=Outcome.ERROR, scan_id=scan_id, detail=f"提交请求失败: {exc}"
            )

        self._debug(f"token_by_scan_code 返回字段: {sorted(data.keys())}")
        return SnipeResult(
            outcome=Outcome.SUCCESS,
            scan_id=scan_id,
            account_token=str(data.get("token") or ""),
            latency_ms=latency_ms,
            detail="已抢到并提交成功",
            payload=data,
        )

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    async def run(
        self,
        frames: Iterator[Frame],
        *,
        timeout_s: float | None = None,
    ) -> SnipeResult:
        """消费帧源直到抢到码（或超时）。

        帧源的阻塞读取与解码被放到工作线程，保证 asyncio 事件循环
        能准时发出轮询请求 —— 这是延迟上的关键，不能省。
        """
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        self._settled.clear()

        async with HypergryphAuth() as auth:
            decode_task = asyncio.create_task(self._decode_loop(frames, auth))

            try:
                done, _pending = await asyncio.wait(
                    {decode_task, self._result},
                    timeout=timeout_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # 让出一轮事件循环：轮询任务可能刚刚完成，但它的 done_callback
                # 还没被调度。不 yield 就直接仲裁，会把「码已失效」误报成「超时」——
                # 这个 bug 只在事件循环调度节奏不同时暴露（单独跑不复现，跑全量用例才现形）。
                await asyncio.sleep(0)

                if self._result in done:
                    return self._result.result()

                if decode_task in done:
                    exc = decode_task.exception()
                    if exc is not None:
                        return SnipeResult(outcome=Outcome.ERROR, detail=f"帧源异常: {exc}")

                # 走到这里意味着：超时了，或者帧源耗尽。两种情况下都不能直接
                # 断言"没出现过码" —— 先看已经跑完的轮询任务有没有更准确的结论
                # （比如"码已失效"或"被别人抢先"），有就用它，别用误导性的 TIMEOUT。
                settled = self._collect_settled(final=True)
                if settled is not None:
                    return settled

                # 还有轮询在跑，给它们一点时间收尾
                if self._watchers:
                    await asyncio.wait(
                        set(self._watchers.values()),
                        timeout=5.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    settled = self._collect_settled(final=True)
                    if settled is not None:
                        return settled

                if decode_task in done:
                    return SnipeResult(outcome=Outcome.EXPIRED, detail="帧源结束且未抢到码")
                return SnipeResult(
                    outcome=Outcome.TIMEOUT,
                    detail=f"超过 {timeout_s:.0f}s 未出现可用的二维码" if timeout_s else "已结束",
                )
            finally:
                decode_task.cancel()
                for task in list(self._watchers.values()):
                    task.cancel()
                await asyncio.gather(
                    decode_task, *self._watchers.values(), return_exceptions=True
                )

    def _collect_settled(self, *, final: bool = False) -> SnipeResult | None:
        """从已完成的轮询任务里挑一个有信息量的结论。

        :param final: 是否是「最终裁决」（超时/帧源结束时的收尾）。

        这里有个必须区分清楚的地方：**二维码失效不该终止整轮监视**。
        实测中 ``scanId`` 存活很短（≤30 秒），而且屏幕上可能残留着早已失效的
        旧二维码 —— 抓到它就报错退出，等于后面真正的新码全错过了。

        所以：

        * ``final=False``（运行中）：只有"抢到了"或"被别人抢先"才算定论；
          单纯失效只是这个码没戏，继续等下一个。
        * ``final=True``（收尾）：失效也要拿来当结论报，比含糊的"超时"有用。
        """
        candidates = [r for r in self._settled if r is not None]
        if not candidates:
            return None

        for result in candidates:  # 抢到了永远最优先
            if result.outcome is Outcome.SUCCESS:
                return result

        if self.keep_going and not final:
            return None

        if not final:
            # 运行中：被抢先算定论（说明确实有人在抢，值得停），失效不算
            for result in candidates:
                if result.outcome is Outcome.LOST:
                    return result
            return None

        for outcome in (Outcome.LOST, Outcome.EXPIRED, Outcome.ERROR):
            for result in candidates:
                if result.outcome is outcome:
                    return result
        return candidates[0]

    async def _decode_loop(self, frames: Iterator[Frame], auth: HypergryphAuth) -> None:
        """在工作线程里解码，识别到新 scanId 就开一个轮询任务。"""
        import itertools

        counter = itertools.count()
        while True:
            frame = await asyncio.to_thread(self._next_frame, frames)
            if frame is None:
                return

            index = next(counter)

            # 耗时分两段量：抓帧（含 mss 抓屏与通道处理）与解码。
            # 高 DPI 屏上"每秒只处理几帧"可能是任一段造成的，不量就只能在猜。
            t_before = time.perf_counter()
            result = await asyncio.to_thread(self.decoder.decode, frame.image)
            decode_ms = (time.perf_counter() - t_before) * 1000.0
            # 抓帧耗时由帧源时间戳与"取到帧"的时刻之差近似
            grab_ms = (t_before - frame.timestamp) * 1000.0
            if self.verbose_timing:
                height, width = frame.image.shape[:2]
                self._debug(
                    f"  帧{index} 采集 {grab_ms:6.1f}ms + 解码 {decode_ms:6.1f}ms"
                    f"  ({width}x{height})"
                )

            if result is None or not result.scan_id:
                if result is not None:
                    self._debug(f"识别到二维码但非扫码登录码: {result.payload[:80]}")
                continue

            scan_id = result.scan_id
            if self._is_processed(scan_id) or scan_id in self._watchers:
                continue

            self._grab_first_seen(scan_id)
            self.reporter.hit(
                f"发现扫码登录码 scanId={scan_id} "
                f"(策略={result.method}, 解码={result.elapsed_ms:.0f}ms, 第{index}帧)"
            )
            # 把**原始 payload** 打出来。之前只打切出来的 32 位片段，
            # 谁也没法核对"工具读到的到底是哪个码"。排障必须看全貌。
            if self.save_capture:
                self._save_frame(frame.image, scan_id, index)

            # 完整打印，不截断 —— 二维码里可能带着 appCode 之类的额外参数，
            # 而"我们的确认为什么对不上客户端"这类问题，答案往往就藏在这里。
            payload = result.payload
            self._debug(f"  ← 二维码原文(长度 {len(payload)}): {payload}")
            # 顺带把里面所有 query 参数列出来，便于逐项比对
            if "?" in payload:
                query = payload.split("?", 1)[1]
                self._debug(f"  ← 二维码参数: {query}")
            task = asyncio.create_task(self._run_for_scan_id(auth, scan_id))
            self._watchers[scan_id] = task
            task.add_done_callback(lambda t, sid=scan_id: self._on_watcher_done(sid, t))

    def _save_frame(self, image: Any, scan_id: str, index: int) -> None:
        """把识别到二维码的那一帧存成图片。失败不影响主流程。"""
        try:
            import cv2  # noqa: PLC0415

            out_dir = Path(self.save_capture).expanduser()
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            name = f"capture-{stamp}-{scan_id[:8]}-frame{index}.png"
            path = out_dir / name
            if not cv2.imwrite(str(path), image):
                raise RuntimeError("cv2.imwrite 返回 False")
            height, width = image.shape[:2]
            self.reporter.info(f"已保存识别到的截图: {path}  ({width}x{height})")
        except Exception as exc:
            self.reporter.warn(f"保存截图失败（不影响抢码）: {type(exc).__name__}: {exc}")

    @staticmethod
    def _next_frame(frames: Iterator[Frame]) -> Frame | None:
        try:
            return next(frames)
        except StopIteration:
            return None

    def _on_watcher_done(self, scan_id: str, task: asyncio.Task[SnipeResult | None]) -> None:
        self._watchers.pop(scan_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.reporter.warn(f"scanId {scan_id[:8]}… 轮询任务异常: {exc}")
            self._note_processed(scan_id)
            return

        result = task.result()
        if result is None:
            return
        self._settled.append(result)

        if result.ok:
            self._note_processed(scan_id)
            self.reporter.success(
                f"✔ 抢到码！scanId={scan_id} 端到端延迟={result.latency_ms:.0f}ms"
            )
            if not self.keep_going and self._result is not None and not self._result.done():
                self._result.set_result(result)
            return

        if result.outcome is Outcome.SUCCESS:
            return  # 已在上面处理

        self._note_processed(scan_id)
        if result.outcome is Outcome.LOST:
            self.reporter.warn(f"✘ scanId {scan_id[:8]}… 被别人抢先了（{result.detail}）")
        else:
            # 失效 ≠ 收工：屏幕上可能还挂着旧码，新码往往马上就出
            self.reporter.info(
                f"· scanId {scan_id[:8]}… {result.detail}；继续等下一个码"
            )


def build_reporter(*, color: bool = True, verbose: bool = False) -> ConsoleReporter:
    return ConsoleReporter(color=color, verbose=verbose)


__all__ = [
    "ConsoleReporter",
    "Outcome",
    "Reporter",
    "SnipeResult",
    "Sniper",
    "build_reporter",
]
