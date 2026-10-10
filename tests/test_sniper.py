"""用本地假服务器验证抢码状态机 —— 不触碰任何真实接口。

这里刻意把三条路径都覆盖到：
  * 抢到（成功换到 token）
  * 被别人抢先（同一个 scanCode 第二次提交必定失败）
  * 码已失效

并且验证「竞速」这件事本身：``_redeem`` 一发现可换就立刻提交，
不会等到下一轮轮询。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np
import pytest

from endfield_scanner.core import api as api_mod
from endfield_scanner.core.frames import Frame
from endfield_scanner.core.qrdecode import QRDecoder
from endfield_scanner.core.sniper import Outcome, Sniper

SCAN_ID = "d0549b45e5002c2cf8e5c302deb1dca6"
PAYLOAD = f"hypergryph://scan_login?scanId={SCAN_ID}"
SCAN_CODE = "ZmFrZS1zY2FuLWNvZGUtMDAwMDAwMDA="
TOKEN = "fake-account-token-abcdefghijklmnop"


class FakeHypergryph:
    """一个只实现抢码所需三个端点的假服务器。"""

    def __init__(
        self,
        *,
        scanned_after_s: float = 0.0,
        confirmed_after_s: float | None = None,
        initially_expired: bool = False,
        scan_code_at_pending: bool = False,
        scanned_after_polls: int | None = None,
    ) -> None:
        self.scanned_after_s = scanned_after_s
        #: 扫码后多久才"确认授权"。设成非 None 就会先经过 101「已扫码待确认」中间态。
        self.confirmed_after_s = confirmed_after_s
        self.initially_expired = initially_expired
        #: 前多少次 scan_status 返回「未扫码」。
        #:
        #: 与 ``scanned_after_s`` 的区别：那个依赖**墙钟**，而墙钟从 ``start()``
        #: 就开始走 —— 机器一空闲（CI 上很常见）客户端还没开始轮询，时间就过去了，
        #: 于是"轮询了 N 次"的断言随机器快慢漂移。要断言轮询次数就该用次数驱动。
        self.scanned_after_polls = scanned_after_polls
        self._polls_seen = 0
        #: True = 101「已扫码待确认」阶段就返回 scanCode（实测真实服务端就是这样）。
        #: 这正是"抢先提交"能赢的前提：scanCode 在用户点确认之前就已可用。
        self.scan_code_at_pending = scan_code_at_pending
        self.started_at = time.monotonic()
        self.status_polls = 0
        self.redeem_attempts = 0
        self.redeem_successes = 0
        self.scan_login_calls = 0
        self.confirm_calls = 0
        self.confirmed = False
        #: 在"尚未落成 101"时就发确认的次数 —— 应当恒为 0
        self.premature_confirms = 0
        #: 代扫码后是否把状态推进到「已扫」。False 用来模拟无 auto_scan 的场景。
        self.scanned = scanned_after_s <= 0.0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # -- 生命周期 ---------------------------------------------------------- #

    def start(self) -> "FakeHypergryph":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静音
                pass

            def _send(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/general/v1/scan_status"):
                    with outer._lock:
                        outer.status_polls += 1
                    if outer.initially_expired:
                        self._send(200, {"status": 102, "msg": "已失效", "type": "A"})
                        return
                    # 代确认过 -> 直接 0 + scanCode
                    if outer.confirmed:
                        self._send(200, {"status": 0, "msg": "OK", "type": "A",
                                         "data": {"scanCode": SCAN_CODE}})
                        return
                    # 代扫码过（但未确认）-> 101
                    if outer.scanned and outer.scan_code_at_pending:
                        self._send(200, {"status": 101, "msg": "已扫码待确认", "type": "A",
                                         "data": {"scanCode": SCAN_CODE}})
                        return
                    if outer.scanned:
                        self._send(200, {"status": 101, "msg": "已扫码待确认", "type": "A"})
                        return
                    with outer._lock:
                        outer._polls_seen += 1
                        polls_seen = outer._polls_seen
                        # 轮询次数到了就把状态**真的**推进为「已扫」。
                        #
                        # 为什么不能只影响这一次的返回值：`scanned` 是个状态位，
                        # 后续分支都看它。只改返回值的话，下一次轮询又走回旧状态，
                        # 永远推不动（这个坑我踩过）。
                        if (
                            outer.scanned_after_polls is not None
                            and polls_seen > outer.scanned_after_polls
                        ):
                            outer.scanned = True

                    elapsed = time.monotonic() - outer.started_at
                    if elapsed < outer.scanned_after_s:
                        # 100 未扫码
                        self._send(200, {"status": 100, "msg": "未扫码", "type": "A"})
                        return
                    if outer.confirmed_after_s is not None:
                        confirm_at = outer.scanned_after_s + outer.confirmed_after_s
                        if elapsed < confirm_at:
                            # 101 已扫码待确认 —— 用户正在点手机上的授权弹窗
                            payload = {"status": 101, "msg": "已扫码待确认", "type": "A"}
                            if outer.scan_code_at_pending:
                                # 真实服务端在这个阶段就已经给出 scanCode 了
                                payload["data"] = {"scanCode": SCAN_CODE}
                            self._send(200, payload)
                            return
                    # 0 已确认
                    self._send(
                        200,
                        {"status": 0, "msg": "OK", "type": "A", "data": {"scanCode": SCAN_CODE}},
                    )
                    return
                self._send(404, {"status": -1, "msg": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except Exception:
                    body = {}

                if self.path.startswith("/user/info/v1/scan_login"):
                    # 代扫码：用 token 登记「已扫码」
                    with outer._lock:
                        outer.scan_login_calls += 1
                        outer.scanned = True
                    self._send(200, {"status": 0, "msg": "OK", "type": "A",
                                     "data": {"phone": "139****0000", "loginRegion": "中国-测试市"}})
                    return

                if self.path.startswith("/user/info/v1/update_scan_status"):
                    # 代确认：进入「已确认」，scan_status 才会给 scanCode。
                    # 但真实服务端只在"已扫码(101)"之后才接受确认 ——
                    # 背靠背连发（还没落成 101 就确认）会被拒。
                    with outer._lock:
                        outer.confirm_calls += 1
                        premature = not outer.scanned
                        if not premature:
                            outer.confirmed = True
                    if premature:
                        outer.premature_confirms += 1
                        self._send(200, {"status": 100,
                                         "msg": "尚未扫码，无法确认", "type": "B"})
                        return
                    self._send(200, {"status": 0, "msg": "OK", "type": "A", "data": {}})
                    return

                if self.path.startswith("/user/auth/v1/token_by_scan_code"):
                    with outer._lock:
                        outer.redeem_attempts += 1
                        # 一个 scanCode 只能被换走一次 —— 这就是抢码的全部残酷之处
                        first = outer.redeem_successes == 0 and body.get("scanCode") == SCAN_CODE
                        if first:
                            outer.redeem_successes += 1
                    if first:
                        self._send(200, {"status": 0, "msg": "OK", "type": "A", "data": {"token": TOKEN}})
                    else:
                        self._send(200, {"status": 100, "msg": "登录失败", "type": "B"})
                    return

                if self.path.startswith("/user/info/v1/basic"):
                    self._send(
                        200,
                        {
                            "status": 0,
                            "msg": "OK",
                            "data": {"hgId": "12345", "nickname": "测试管理员", "email": "a@b.c"},
                        },
                    )
                    return
                self._send(404, {"status": -1, "msg": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeHypergryph":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


@pytest.fixture()
def fake_api(monkeypatch: pytest.MonkeyPatch):
    """把 api 模块里的端点常量改指到假服务器。"""
    servers: list[FakeHypergryph] = []

    def _install(**kwargs: Any) -> FakeHypergryph:
        server = FakeHypergryph(**kwargs).start()
        servers.append(server)
        monkeypatch.setattr(api_mod, "AS_BASE", server.base)
        monkeypatch.setattr(api_mod, "GEN_SCAN_LOGIN", f"{server.base}/general/v1/gen_scan/login")
        monkeypatch.setattr(api_mod, "SCAN_STATUS", f"{server.base}/general/v1/scan_status")
        monkeypatch.setattr(
            api_mod, "TOKEN_BY_SCAN_CODE", f"{server.base}/user/auth/v1/token_by_scan_code"
        )
        monkeypatch.setattr(api_mod, "USER_BASIC", f"{server.base}/user/info/v1/basic")
        # 代扫码的两个端点也必须指向假服务器，否则测试会打到真实服务端 ——
        # 那既不安全（会真的消费二维码），结果也不可复现。
        monkeypatch.setattr(api_mod, "SCAN_LOGIN", f"{server.base}/user/info/v1/scan_login")
        monkeypatch.setattr(
            api_mod, "UPDATE_SCAN_STATUS", f"{server.base}/user/info/v1/update_scan_status"
        )
        return server

    yield _install
    for server in servers:
        server.stop()


def _qr_frames(count: int = 400) -> Any:
    """产出一个带真实二维码的帧序列（模拟直播画面里一直挂着码）。"""
    import cv2
    import qrcode

    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(PAYLOAD)
    qr.make(fit=True)
    code = np.array(qr.make_image(fill_color="black", back_color="white").convert("RGB"))
    code = cv2.cvtColor(code, cv2.COLOR_RGB2BGR)

    canvas = np.full((720, 1280, 3), 32, dtype=np.uint8)
    h, w = code.shape[:2]
    canvas[40 : 40 + h, 40 : 40 + w] = code

    def generator():
        for _ in range(count):
            yield Frame(image=canvas, timestamp=time.monotonic())
            time.sleep(0.01)

    return generator()


class _SilentReporter:
    """实现完整的 Reporter 协议（含可选方法），并把输出留档供断言。"""

    def __init__(self) -> None:
        self.successes: list[str] = []
        self.warnings: list[str] = []
        self.events: list[str] = []
        self.debugs: list[str] = []
        self.hits: list[str] = []

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def info(self, message: str) -> None:
        self.events.append(message)

    def hit(self, message: str) -> None:
        self.hits.append(message)
        self.events.append(message)

    def success(self, message: str) -> None:
        self.successes.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


# --------------------------------------------------------------------------- #
# 测试
# --------------------------------------------------------------------------- #


class TestSniperWins:
    async def test_redeems_on_scan_code_flip(self, fake_api) -> None:
        server = fake_api(scanned_after_s=0.3)
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            redeem=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )

        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)

        assert result.outcome is Outcome.SUCCESS, result.detail
        assert result.account_token == TOKEN
        assert result.scan_id == SCAN_ID
        # 端到端延迟应当贴近「可换」那一刻，而不是整个超时时间
        assert result.latency_ms is not None and result.latency_ms < 5000
        assert server.redeem_successes == 1
        # 关键：翻转后只提交一次就赢，不是靠反复轰炸
        assert server.redeem_attempts == 1
        assert reporter.successes, "应当报告成功"

    async def test_polls_repeatedly_while_unscanned(self, fake_api) -> None:
        """未扫码期间必须在反复轮询，而不是碰一次运气就放弃。

        用 ``scanned_after_polls`` 而不是墙钟来驱动：墙钟从服务端 ``start()``
        就开始走，机器一空闲（CI 上很常见）客户端还没开始轮询时间就过去了，
        这个断言会随机器快慢漂移 —— 曾经就因此在 CI 上偶发失败。
        """
        # 注意三个参数的配合：
        #   scanned_after_s 必须给正值 —— 构造时 `scanned = scanned_after_s <= 0`，
        #     给 0 会让它一开始就是「已扫」，直接走 101 分支，第一次轮询就结束。
        #   然后由 scanned_after_polls 在前 5 次返回 100，第 6 次推进为已扫。
        #   scan_code_at_pending=True 贴近真实服务端：101 阶段就给出 scanCode。
        server = fake_api(
            scanned_after_s=60.0, scanned_after_polls=5, scan_code_at_pending=True
        )
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=10,
            auto_scan=False,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        assert result.ok
        # 前 5 次是 100，第 6 次起才是 101 —— 所以至少轮询了 6 次
        assert server.status_polls >= 6, f"只轮询了 {server.status_polls} 次"

    async def test_does_not_reprocess_same_scan_id(self, fake_api) -> None:
        server = fake_api(scanned_after_s=0.2)
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            redeem=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        # 一个 scanId 只应触发一个轮询任务、一次提交
        assert server.redeem_attempts == 1


class TestPendingConfirmState:
    """回归：101「已扫码待确认」绝不能被当成异常/失效。

    这是真实踩到的 bug —— 观众扫码后正要去点 App 里的授权弹窗，
    程序却因为不认识 101 而直接放弃了这个码。
    """

    async def test_waits_through_pending_confirm(self, fake_api) -> None:
        # 扫码后要再过 0.6s 才确认，期间服务端返回 101
        fake_api(scanned_after_s=0.1, confirmed_after_s=0.6)
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            confirm_grace_s=10.0,
            redeem=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)

        assert result.outcome is Outcome.SUCCESS, result.detail
        assert result.account_token == TOKEN
        # 若 101 阶段拿不到 scanCode，必须明确提示过"在等确认"，
        # 否则用户不知道卡在哪
        assert any("确认" in msg for msg in reporter.hits), reporter.hits

    async def test_pending_confirm_extends_deadline(self, fake_api) -> None:
        """scan_ttl 很短，但一旦进入「已扫码待确认」就应当被宽限救回来。"""
        fake_api(scanned_after_s=0.1, confirmed_after_s=1.2)
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            # TTL 比"确认"还早到：没有宽限机制的话必然失败
            max_scan_id_age_s=0.5,
            confirm_grace_s=10.0,
            redeem=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        assert result.outcome is Outcome.SUCCESS, result.detail

    async def test_reports_clearly_when_user_never_confirms(self, fake_api) -> None:
        """用户扫了但一直没点确认：报错要说清是"没确认"，而不是含糊的"未扫描"。"""
        fake_api(scanned_after_s=0.05, confirmed_after_s=9999.0)
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            auto_scan=False,
            max_scan_id_age_s=0.3,
            confirm_grace_s=0.5,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=8), timeout=25)
        assert result.outcome is Outcome.EXPIRED
        assert "确认" in result.detail or "确认" in " ".join(reporter.events)


class TestAutoScan:
    """核心能力回归：用 account_token 代为完成「扫码 + 确认」。

    这两个接口是逆向 App 得到的（APK 有梆梆加固，真实代码不可读，
    端点是在 dex 字符串里挖出来、再用 gin 的参数校验错误逼出字段名的）：

        POST /user/info/v1/scan_login          {scanId, token}  → 登记「已扫码」
        POST /user/info/v1/update_scan_status  {scanId, token}  → 「确认登录」

    它们让工具**完全不需要人掏手机** —— 这是整个项目的转折点。
    """

    async def test_auto_scan_registers_and_confirms_with_token(self, fake_api) -> None:
        server = fake_api(scanned_after_s=9999.0, scan_code_at_pending=True)
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="MY-ACCOUNT-TOKEN",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            auto_scan=True,
            confirm_grace_s=5.0,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)

        assert result.outcome is Outcome.SUCCESS, result.detail
        # 必须真的调了那两个接口 —— 否则就还是"等人扫"
        assert server.scan_login_calls == 1, "没有调用 scan_login"
        assert server.confirm_calls == 1, "没有调用 update_scan_status"
        # 而且用的是我们自己的 account_token
        assert any("已代为扫码" in m for m in reporter.hits), reporter.hits
        assert any("已代为确认" in m for m in reporter.hits), reporter.hits

    async def test_without_auto_scan_waits_for_human(self, fake_api) -> None:
        """关掉 auto_scan 后不应调用代扫码接口 —— 退回"等人扫"的原行为。"""
        server = fake_api(scanned_after_s=0.2, scan_code_at_pending=True)
        sniper = Sniper(
            account_token="MY-ACCOUNT-TOKEN",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            auto_scan=False,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        assert result.outcome is Outcome.SUCCESS, result.detail
        assert server.scan_login_calls == 0
        assert server.confirm_calls == 0

    async def test_auto_scan_failure_is_not_fatal(self, fake_api) -> None:
        """代扫码失败（例如 token 失效）必须退化为"等人扫"，而不是让整轮崩掉。"""
        server = fake_api(scanned_after_s=0.2, scan_code_at_pending=True)
        sniper = Sniper(
            account_token="BAD-TOKEN",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            auto_scan=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        # 让 scan_login 返回业务错误
        from endfield_scanner.core import api as api_mod

        original = api_mod.HypergryphAuth.scan_login

        async def broken(self, scan_id, account_token):  # noqa: ANN001
            raise api_mod.ApiError("token 无效", status=100)

        api_mod.HypergryphAuth.scan_login = broken  # type: ignore[method-assign]
        try:
            result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        finally:
            api_mod.HypergryphAuth.scan_login = original  # type: ignore[method-assign]

        # 代扫码失败了，但人扫仍能赢 —— 工具不能因此失效
        assert result.outcome is Outcome.SUCCESS, result.detail
        assert server.scan_login_calls == 0


class TestNoConfirmStopsAtPending:
    """`--no-confirm` 必须停在「已扫码待确认」，这是排障的关键手段。

    它用来回答一个具体问题：**客户端（游戏）到底有没有看到这次扫码**。
    如果停在 101 时游戏显示"已扫码待确认"，说明 scan_login 是对的、
    问题在确认那一步；如果游戏直接跳到"过期"，说明请求没作用在这个码的会话上。
    """

    async def test_stops_before_confirm(self, fake_api) -> None:
        server = fake_api(scanned_after_s=9999.0)
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="TOKEN",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            auto_scan=True,
            auto_confirm=False,
            confirm_grace_s=0.5,
            max_scan_id_age_s=1.0,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=8), timeout=20)

        # 扫码做了，确认**没做**
        assert server.scan_login_calls == 1, "应当代扫码"
        assert server.confirm_calls == 0, "不该代确认（--no-confirm）"
        assert any("已停在「已扫码待确认」" in m for m in reporter.events), reporter.events

    async def test_confirm_enabled_reaches_success(self, fake_api) -> None:
        """对照组：开着 auto_confirm 时应正常拿到 token。"""
        server = fake_api(scanned_after_s=9999.0, scan_code_at_pending=True)
        sniper = Sniper(
            account_token="TOKEN",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            auto_scan=True,
            auto_confirm=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        assert result.outcome is Outcome.SUCCESS, result.detail
        assert server.confirm_calls == 1


class TestConfirmWaitsForScannedState:
    """回归：代确认必须等状态落成 101 之后再发。

    真实踩到的 bug：`scan_login` 与 `update_scan_status` 背靠背连发，
    中间没有任何间隔。服务端把"确认"算在一个**尚未建立的扫码状态**上，
    客户端（游戏）那边只显示"请在手机上确认登录"、永远等不到登录。

    真实 App 里这两步隔着"人看到提示 → 点确认"的时间，所以这里补回来。
    """

    async def test_never_confirms_before_scanned(self, fake_api) -> None:
        server = fake_api(scanned_after_s=9999.0, scan_code_at_pending=True)
        sniper = Sniper(
            account_token="TOKEN",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            auto_scan=True,
            auto_confirm=True,
            confirm_delay_ms=400,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)

        assert result.outcome is Outcome.SUCCESS, result.detail
        assert server.premature_confirms == 0, "在落成 101 之前就发了确认"
        assert server.confirm_calls == 1

    async def test_skips_confirm_when_still_unscanned(self, fake_api) -> None:
        """若延迟后服务端仍显示未扫码，应当**跳过**确认并给出警告，而不是硬发。"""
        server = fake_api(scanned_after_s=9999.0)
        reporter = _SilentReporter()
        # 让 scan_login 不改变 scanned 状态 —— 模拟"扫码未被服务端接受"
        from endfield_scanner.core import api as api_mod

        original = api_mod.HypergryphAuth.scan_login

        async def noop_scan(self, scan_id, account_token):  # noqa: ANN001
            return {"phone": "1****"}

        api_mod.HypergryphAuth.scan_login = noop_scan  # type: ignore[method-assign]
        sniper = Sniper(
            account_token="TOKEN",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            auto_scan=True,
            auto_confirm=True,
            confirm_delay_ms=200,
            max_scan_id_age_s=1.0,
            confirm_grace_s=0.3,
        )
        try:
            await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=6), timeout=20)
        finally:
            api_mod.HypergryphAuth.scan_login = original  # type: ignore[method-assign]

        assert server.confirm_calls == 0, "服务端说未扫码时不该硬发确认"
        assert any("跳过代确认" in m for m in reporter.warnings), reporter.warnings


class TestConfirmDelayIsHumanLike:
    """代确认的等待要像真人，且区间随机。

    实测依据：正常手机扫码后**不是立刻确认** —— App 要跳一个确认页，
    人得看清内容再点一下，这个间隔在 4 秒量级。
    几百毫秒是在模拟机器，既不像人，也可能因此被风控盯上。
    """

    def test_default_range_is_two_to_five_seconds(self) -> None:
        """默认 2~5 秒：秒级才像真人（App 要跳确认页），但 10 秒上限又太长。"""
        sniper = Sniper(account_token="x")
        assert sniper.confirm_delay_ms == 2000
        assert sniper.confirm_delay_max_ms == 5000

    def test_default_max_is_absolute_not_derived(self) -> None:
        """上限是绝对默认值，不随下限按比例放大。

        否则 `--confirm-delay 5000` 会推出 12.5 秒的上限，用户预期之外。
        """
        sniper = Sniper(account_token="x", confirm_delay_ms=9000)
        # 上限仍是默认 5000，但因为小于下限会被顶到下限
        assert sniper.confirm_delay_max_ms == 9000

    def test_range_is_respected_and_actually_random(self) -> None:
        import random as _random

        sniper = Sniper(account_token="x", confirm_delay_ms=4000, confirm_delay_max_ms=6000)
        seen = {
            _random.randint(sniper.confirm_delay_ms, sniper.confirm_delay_max_ms)
            for _ in range(200)
        }
        assert min(seen) >= 4000 and max(seen) <= 6000
        # 200 次里应当出现多于一个取值 —— 否则就不是随机
        assert len(seen) > 50, f"取值不够分散，可能没在随机：{len(seen)}"

    def test_max_never_below_min(self) -> None:
        """上限被传得比下限小的时候，不应产生非法区间。"""
        sniper = Sniper(account_token="x", confirm_delay_ms=5000, confirm_delay_max_ms=1000)
        assert sniper.confirm_delay_max_ms >= sniper.confirm_delay_ms

    async def test_actual_wait_uses_random_delay(self, fake_api) -> None:
        """真正等待的时间应落在区间内，而不是固定值。"""
        import time as _time

        fake_api(scanned_after_s=9999.0, scan_code_at_pending=True)
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="TOKEN",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            auto_scan=True,
            auto_confirm=True,
            confirm_delay_ms=300,
            confirm_delay_max_ms=900,
        )
        started = _time.monotonic()
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        waited_ms = (_time.monotonic() - started) * 1000

        assert result.outcome is Outcome.SUCCESS, result.detail
        # 至少等了下限；且不该超过上限太多（留出解码与网络开销的余量）
        assert waited_ms >= 300, f"等得太短：{waited_ms:.0f}ms"
        assert waited_ms < 300 + 5000, f"等得太久：{waited_ms:.0f}ms"
        assert any("随机等待" in m for m in reporter.debugs), reporter.debugs


class TestScanTtlActuallyExpires:
    """回归：scanId 的 TTL 必须真的生效。

    曾经的 bug：``_first_seen`` 存 ``time.time()``（Unix 墙钟，约 1.79e9），
    而 deadline 用 ``time.monotonic()``（开机秒数，很小）去算，两者相减得到
    17.9 亿秒的"剩余时间"，实际效果是 **TTL 永远不会到期**。
    心跳日志里那串天文数字就是这个 bug 的指纹。
    """

    async def test_abandons_scan_id_after_ttl(self, fake_api) -> None:
        """watcher 必须在 TTL 附近放弃该 scanId。

        注意：放弃这个码**不等于**结束整轮监视 —— 屏幕上可能还挂着旧码，
        新码往往马上出现，所以 ``run()`` 会继续等（这是刻意的行为）。
        因此这里断言的是"该 scanId 被放弃"这个事件，而不是 run 的返回值。
        """
        fake_api(scanned_after_s=9999.0)
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=30,
            auto_scan=False,
            max_scan_id_age_s=0.8,
            heartbeat_s=0.2,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )

        run_task = asyncio.create_task(sniper.run(_qr_frames(), timeout_s=30))
        try:
            # 轮询直到某个码被明确标记为已放弃
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if any("放弃" in m for m in reporter.debugs):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(f"TTL 没生效，从未放弃该码。debugs={reporter.debugs[:5]}")
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

        # 放弃时的轮询次数应当匹配 ~0.8s / 30ms（约 27 次），而不是几百次
        detail = next(m for m in reporter.debugs if "放弃" in m)
        polls = int(re.search(r"(\d+) 次", detail).group(1))
        assert 5 <= polls <= 60, f"轮询次数与 TTL 不符：{polls}（详情：{detail}）"

    async def test_heartbeat_reports_sane_remaining_time(self, fake_api) -> None:
        """心跳里的"剩余约 Ns"必须是正常秒数，不能是天文数字。"""
        fake_api(scanned_after_s=9999.0)
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=30,
            auto_scan=False,
            max_scan_id_age_s=2.0,
            heartbeat_s=0.2,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=6), timeout=20)

        remaining_values: list[float] = []
        for message in reporter.events:
            match = re.search(r"剩余约 ([\d.]+)s", message)
            if match:
                remaining_values.append(float(match.group(1)))

        assert remaining_values, f"没有产生任何心跳：{reporter.events[:3]}"
        for value in remaining_values:
            # 剩余时间不可能超过 TTL 本身，更不该是 1.79e9
            assert 0 <= value <= 5.0, f"剩余时间不合理：{value}"


class TestGrabAtPendingConfirm:
    """核心胜负手回归：101「已扫码待确认」阶段就要抢先提交。

    实测真实服务端在 101 时 scanCode **已经可用**，而 `token_by_scan_code`
    是无鉴权的 —— 谁先提交算谁的。
    早期版本死等状态 0（用户点完确认）才提交，等于白送人手 1~2 秒，
    把工具唯一的速度优势丢掉了。
    """

    async def test_submits_during_pending_without_waiting_for_confirm(
        self, fake_api
    ) -> None:
        # 扫码后 scanCode 立刻可用，但"确认"要再等很久 ——
        # 工具必须在这段时间内就赢下来，而不是干等
        server = fake_api(
            scanned_after_s=0.1, confirmed_after_s=9999.0, scan_code_at_pending=True
        )
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            redeem=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        started = time.monotonic()
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        elapsed = time.monotonic() - started

        assert result.outcome is Outcome.SUCCESS, result.detail
        assert result.account_token == TOKEN
        # 必须在"确认"到来之前就赢（confirmed_after_s 是 9999）
        assert elapsed < 5.0, f"等太久了（{elapsed:.1f}s），说明还在死等确认"
        assert server.redeem_successes == 1
        # 关键是"在确认之前就动手"。至于走的是哪条分支（101 带 code 会被
        # 前面的 is_ready 分支接住，那是更早更好的路径）不作硬性要求。
        assert any("提交" in m for m in reporter.hits), reporter.hits

    async def test_falls_back_to_waiting_when_pending_has_no_code(
        self, fake_api
    ) -> None:
        """若 101 阶段拿不到 scanCode，必须能优雅退回"等确认"，而不是放弃。"""
        fake_api(
            scanned_after_s=0.1, confirmed_after_s=0.8, scan_code_at_pending=False
        )
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            auto_scan=False,
            confirm_grace_s=10.0,
            redeem=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        assert result.outcome is Outcome.SUCCESS, result.detail
        assert any("还换不出 scanCode" in m for m in reporter.hits), reporter.hits

    async def test_only_tries_pending_grab_once(self, fake_api) -> None:
        """早提交只试一次，不要每个轮询周期都空打一发。"""
        server = fake_api(
            scanned_after_s=0.1, confirmed_after_s=0.8, scan_code_at_pending=False
        )
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            confirm_grace_s=10.0,
            redeem=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=15), timeout=30)
        # 只有最终确认后的那一次成功提交
        assert server.redeem_attempts == 1


class TestNeverSubmitsEmptyScanCode:
    """回归：服务端 status 0 但没给 token 时，必须显式报错。

    早期版本写的是 ``data.get("token") or ""``，空字符串照样算成功 ——
    工具会打印"抢到码了"，而你账号根本没登上。这类谎报最难查。
    """

    async def test_raises_when_response_has_no_token(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from endfield_scanner.core import api as api_mod
        from endfield_scanner.core.api import ApiError, HypergryphAuth

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a): pass

            def do_POST(self):
                import json as _json
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                # status 0 = 成功，但**故意不给 token**
                body = _json.dumps({"status": 0, "msg": "OK", "data": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = api_mod.TOKEN_BY_SCAN_CODE
        api_mod.TOKEN_BY_SCAN_CODE = f"http://127.0.0.1:{server.server_address[1]}/token"
        try:
            async with HypergryphAuth() as auth:
                with pytest.raises(ApiError) as excinfo:
                    await auth.redeem_scan_code("anything")
            assert "token" in str(excinfo.value)
        finally:
            api_mod.TOKEN_BY_SCAN_CODE = original
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TestSniperLoses:
    async def test_reports_expired_not_timeout(self, fake_api) -> None:
        """二维码已失效时必须报 expired，不能误报成 timeout（曾经的 bug）。"""
        fake_api(initially_expired=True)
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=3), timeout=20)
        assert result.outcome is Outcome.EXPIRED, result.detail

    async def test_lost_when_scan_code_already_taken(self, fake_api) -> None:
        """模拟被人抢先：服务端对同一个 scanCode 的第二次提交返回失败。"""
        server = fake_api(scanned_after_s=0.1)
        server.redeem_successes = 1  # 假装已经有人换过了

        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            redeem=True,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=10), timeout=25)
        assert result.outcome is Outcome.LOST, result.detail
        assert reporter.warnings

    async def test_timeout_when_no_qr_at_all(self) -> None:
        def blank_frames():
            blank = np.zeros((720, 1280, 3), dtype=np.uint8)
            for _ in range(400):
                yield Frame(image=blank, timestamp=time.monotonic())
                time.sleep(0.01)

        sniper = Sniper(
            account_token="unused",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        result = await asyncio.wait_for(sniper.run(blank_frames(), timeout_s=1.5), timeout=20)
        assert result.outcome is Outcome.TIMEOUT


class TestFakeServerContract:
    """顺带锁住假服务器本身的行为，避免它悄悄漂移导致上面的测试变成空转。"""

    def test_scan_code_only_redeemable_once(self, fake_api) -> None:
        import httpx

        server = fake_api()
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            first = client.post(
                f"{server.base}/user/auth/v1/token_by_scan_code", json={"scanCode": SCAN_CODE}
            ).json()
            second = client.post(
                f"{server.base}/user/auth/v1/token_by_scan_code", json={"scanCode": SCAN_CODE}
            ).json()
        assert first["status"] == 0
        assert second["status"] != 0


class TestSaveCapture:
    """识别到二维码时留一张截图。

    "当时屏幕上到底是什么"是排障时最难复现、也最值钱的信息 ——
    尤其是多显示器、高 DPI 缩放这类只能人工核对的情况，
    留图比任何日志都直接。
    """

    async def test_saves_frame_when_code_found(self, fake_api, tmp_path) -> None:
        fake_api(scanned_after_s=9999.0, scan_code_at_pending=True)
        out_dir = tmp_path / "shots"
        reporter = _SilentReporter()
        sniper = Sniper(
            account_token="T",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
            save_capture=str(out_dir),
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=10), timeout=20)
        assert result.outcome is Outcome.SUCCESS, result.detail

        files = sorted(out_dir.glob("*.png"))
        assert len(files) == 1, f"应当正好存一张，实际 {len(files)}"
        assert any("已保存识别到的截图" in m for m in reporter.events), reporter.events
        # 文件名里带 scanId 前缀，便于和多张截图对应
        assert SCAN_ID[:8] in files[0].name

        # 图片内容应当能读回，且尺寸与帧一致
        import cv2

        image = cv2.imread(str(files[0]))
        assert image is not None and image.shape[2] == 3

    async def test_no_capture_by_default(self, fake_api, tmp_path) -> None:
        """默认不落图 —— 别在用户没要求时往磁盘写东西。"""
        fake_api(scanned_after_s=9999.0, scan_code_at_pending=True)
        sniper = Sniper(
            account_token="T",
            decoder=QRDecoder(),
            reporter=_SilentReporter(),
            poll_interval_ms=40,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
        )
        await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=10), timeout=20)
        assert list(tmp_path.glob("**/*.png")) == []

    async def test_capture_failure_does_not_break_sniping(
        self, fake_api, tmp_path, monkeypatch
    ) -> None:
        """存图失败不能影响抢码本身。

        这里**注入**失败而不是构造一个非法路径：跨平台的"非法路径"很难找 ——
        例如 `/dev/null/x` 在 POSIX 上失败，在 Windows 上会被解析成
        `C:/dev/null/x` 然后**创建成功**。直接让 imwrite 返回 False 更可靠。
        """
        import cv2

        fake_api(scanned_after_s=9999.0, scan_code_at_pending=True)
        reporter = _SilentReporter()
        monkeypatch.setattr(cv2, "imwrite", lambda *a, **k: False)
        sniper = Sniper(
            account_token="T",
            decoder=QRDecoder(),
            reporter=reporter,
            poll_interval_ms=40,
            confirm_delay_ms=0,
            confirm_delay_max_ms=0,
            save_capture=str(tmp_path / "shots"),
        )
        result = await asyncio.wait_for(sniper.run(_qr_frames(), timeout_s=10), timeout=20)
        assert result.outcome is Outcome.SUCCESS, result.detail
        assert any("保存截图失败" in m for m in reporter.warnings), reporter.warnings


class TestFrameTimestampIsMonotonic:
    """`Frame.timestamp` 必须用单调钟。

    它的唯一用途是和"当前时刻"做差算采集耗时。用 `time.time()`
    （Unix 墙钟，约 1.79e9）去减 `time.monotonic()`（开机秒数）会得到
    -1.79e12 这种天文数字 —— 这个坑在本项目里犯过两次（TTL 一次、耗时埋点一次）。
    """

    def test_file_source_timestamp_is_monotonic(self, tmp_path) -> None:
        import cv2
        import numpy as np

        from endfield_scanner.core.frames import FileSource

        path = tmp_path / "f.png"
        cv2.imwrite(str(path), np.zeros((20, 20, 3), dtype=np.uint8))
        frame = next(FileSource(str(path), loop=False).frames())

        now_mono = time.monotonic()
        # 与单调钟同量级（毫秒级误差内），而不是与墙钟同量级
        assert abs(frame.timestamp - now_mono) < 60, (
            f"timestamp={frame.timestamp} 与 monotonic={now_mono} 差距过大，"
            "可能又混用了墙钟"
        )

    def test_screen_source_timestamp_is_monotonic(self) -> None:
        """ScreenSource 的构造代码里也必须是单调钟（不实际抓屏，只看源码）。"""
        import inspect

        from endfield_scanner.core.frames import ScreenSource

        source = inspect.getsource(ScreenSource.frames)
        assert "time.monotonic()" in source
        assert "time.time()" not in source
