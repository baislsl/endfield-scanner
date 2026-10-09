"""Hypergryph（鹰角网络）通行证扫码登录接口客户端。

所有端点均已在 2026-10 实测确认。

关键事实（决定了整个工具的抢码策略）::

    1. POST /general/v1/gen_scan/login  -> scanId, scanUrl
       scanUrl = "hypergryph://scan_login?scanId=<32位hex>"
       => 二维码内容是明文，scanId 可以直接从二维码字符串里切出来，无需任何额外请求。

    2. GET  /general/v1/scan_status?scanId=  -> "未扫码"(100) / scanCode
       => 这个「未扫码 -> 已扫码」的翻转就是发令枪。

    3. POST /user/auth/v1/token_by_scan_code {"scanCode": <base64>}
       => 实测「完全无鉴权」：不带任何 token / cookie / appCode 都能进入业务逻辑
          （用合法 base64 但无效的 scanCode 打进去，返回的是业务错误 "登录失败"
          而非鉴权错误）。因此这一跳是纯时序竞争：谁先提交有效 scanCode，谁赢。

    4. POST /user/info/v1/scan_login        {"scanId", "token"}
       => 用 account_token 代做「扫码」：100 -> 101。
          这是本工具能"抢"的前提 —— 否则必须等人先扫码。

    5. POST /user/info/v1/update_scan_status {"scanId", "token"}
       => 代做「确认」：101 -> 0，之后 scan_status 返回 scanCode。

    端点 4/5 不在任何公开文档里：它们是在加固过的 APK（梆梆加固，
    classes.dex 只有 5 个壳类）里直接扫描字符串常量捞到的 ——
    代码被加密，但 API 路径往往仍是明文。字段名则靠 Go/gin 的必填校验报错
    反推（传空 body 会回 "Key: 'ScanLoginReq.token' ... 'required'"）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Final

import httpx

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

AS_BASE: Final = "https://as.hypergryph.com"
ZONAI_BASE: Final = "https://zonai.skland.com"

GEN_SCAN_LOGIN: Final = f"{AS_BASE}/general/v1/gen_scan/login"
SCAN_STATUS: Final = f"{AS_BASE}/general/v1/scan_status"
TOKEN_BY_SCAN_CODE: Final = f"{AS_BASE}/user/auth/v1/token_by_scan_code"
#: ★ 逆向 App 得到（APK 加固，从 dex 字符串 + 接口探测确认）：
#: 这两个接口用 account_token 代为完成「用森空岛 App 扫码并确认」，
#: 从此抢码不需要人掏手机。注意它们在 ``/user/info/v1/`` 下 ——
#: 光靠猜是猜不到的（我试遍了 /general/v1/scan* 与 /user/auth/v1/scan* 全 404）。
SCAN_LOGIN: Final = f"{AS_BASE}/user/info/v1/scan_login"
UPDATE_SCAN_STATUS: Final = f"{AS_BASE}/user/info/v1/update_scan_status"
USER_BASIC: Final = f"{AS_BASE}/user/info/v1/basic"

#: 森空岛 App 的 appCode，也是 gen_scan/login 唯一接受的取值
SKLAND_APP_CODE: Final = "4ca99fa6b56cc2ba"

#: 扫码登录二维码里承载的 URL 协议头
SCAN_URL_SCHEME: Final = "hypergryph://scan_login"

#: scanId 形态：32 位十六进制（服务端发的是小写，但接受大小写输入更稳）
SCAN_ID_RE: Final = re.compile(r"^[0-9a-fA-F]{32}$")

# --------------------------------------------------------------------------- #
# scan_status 的状态码
# --------------------------------------------------------------------------- #
#
# 实测（2026-10）：
#   100 未扫码
#   101 已扫码待确认   ← 用户扫了码，但还没点 App 里的授权弹窗
#     0 已确认，此时返回 scanCode，可以换 token
#   102 已失效
#
# ⚠️ 踩过的坑：最初只把 100 当作"继续等"，把 101 当成异常。结果用户扫完码、
# 正要去点授权弹窗的那一刻程序就报错退出了 —— 而这个中间态恰恰是最需要
# 耐心等待的一步。抢码场景下这个 bug 更致命：等于主动放弃已经贴脸的码。
SCAN_STATUS_NOT_SCANNED: Final = 100
SCAN_STATUS_PENDING_CONFIRM: Final = 101
SCAN_STATUS_CONFIRMED: Final = 0
SCAN_STATUS_EXPIRED: Final = 102

#: 这些状态表示"码还有效，继续等"，都不应视为错误
SCAN_STATUS_WAITING: Final = frozenset({SCAN_STATUS_NOT_SCANNED, SCAN_STATUS_PENDING_CONFIRM})

#: 从任意文本（二维码内容 / 粘贴内容）里提取 scanId。
#:
#: 末尾的空白否定断言是必要的：没有它的话 ``scanId=<33位>`` 会被截取出
#: 前 32 位当成合法 ID。而 scanId 恰好是定长 32 位，所以一旦后面还跟着
#: 十六进制字符，就说明这压根不是我们要的东西。
SCAN_ID_EXTRACT_RE: Final = re.compile(
    r"scanId=([0-9a-fA-F]{32})(?![0-9a-fA-F])", re.IGNORECASE
)

_USER_AGENT: Final = (
    "Skland/1.32.1 (com.hypergryph.skland; build:103201004; Android 33; ) Okhttp/4.11.0"
)

#: 全局默认：不沿用环境变量里的代理，直连鹰角接口。
#:
#: 抢码场景下这是刻意选择：国内直连延迟最低，而 ``all_proxy=socks5://...``
#: 这类环境变量既会加一跳、又可能让 httpx 解析端口失败。
#: 需要走代理的用户可以设 ``ENDFIELD_USE_ENV_PROXY=1``，或用 ``--use-env-proxy``。
USE_ENV_PROXY: bool = os.environ.get("ENDFIELD_USE_ENV_PROXY", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class ApiError(RuntimeError):
    """接口返回了非成功状态。"""

    def __init__(self, message: str, *, status: int | None = None, raw: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.raw = raw


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScanSession:
    """由 gen_scan/login 创建的一次扫码会话。"""

    scan_id: str
    scan_url: str

    @property
    def qr_payload(self) -> str:
        """应当被编码进二维码的字符串。"""
        return self.scan_url


@dataclass(frozen=True, slots=True)
class ScanState:
    """scan_status 的一次返回。

    ``scan_code`` 非空即表示「已扫码」，此时必须立刻抢提交。
    """

    scanned: bool
    scan_code: str | None
    raw_status: int
    raw_message: str

    @property
    def is_ready(self) -> bool:
        return self.scanned and bool(self.scan_code)


# --------------------------------------------------------------------------- #
# scanId 提取
# --------------------------------------------------------------------------- #


def extract_scan_id(text: str) -> str | None:
    """从二维码内容或任意粘贴文本中提取 scanId。

    实测终末地/森空岛二维码内容形如::

        hypergryph://scan_login?scanId=d0549b45e5002c2cf8e5c302deb1dca6

    为稳健起见同时接受：纯 32 位 hex、以及 query 中带 scanId 的任意 URL。
    """
    if not text:
        return None
    candidate = text.strip()
    if SCAN_ID_RE.match(candidate):
        return candidate.lower()
    match = SCAN_ID_EXTRACT_RE.search(candidate)
    if match:
        return match.group(1).lower()
    return None


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


def build_client(
    *,
    timeout: float = 5.0,
    max_keepalive: int = 64,
    use_env_proxy: bool | None = None,
) -> httpx.AsyncClient:
    """构造一个为「低延迟轮询」调优的异步客户端。

    抢码的胜负手是轮询 RTT，因此这里刻意：
      * 开启 keep-alive 并放大连接池，避免每次请求都重新握手；
      * 关掉 HTTP/2（服务端未必支持，多一条协商路径反而添乱）；
      * 默认超时压到 5s，避免单次卡死拖垮整个轮询循环。

    :param use_env_proxy: 是否沿用环境变量里的代理设置。
        ``None`` 表示按 :data:`USE_ENV_PROXY` 的全局默认。

        **为什么需要这个开关**：很多用户（尤其是开过科学上网工具的人）
        shell 里会有 ``all_proxy=socks5://127.0.0.1:7891`` 这类变量。
        鹰角接口在国内是直连可达的，一旦被塞进代理，轻则多一跳延迟
        （抢码大忌），重则像 ``socks5://host:7891`` 这种写法直接把 httpx
        的端口解析搞崩。所以默认**绕过环境代理直连**。
    """
    limits = httpx.Limits(
        max_keepalive_connections=max_keepalive,
        max_connections=max_keepalive,
        keepalive_expiry=30.0,
    )
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if use_env_proxy is None:
        use_env_proxy = USE_ENV_PROXY
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        limits=limits,
        headers=headers,
        http2=False,
        trust_env=use_env_proxy,
    )
    # 挂上请求日志钩子：一次覆盖所有走这个客户端的接口
    from .httplog import install as _install_http_log  # noqa: PLC0415

    _install_http_log(client)
    return client


def _parse_envelope(payload: Any) -> tuple[int, str, Any]:
    """解析鹰角接口的统一信封 ``{status, msg, data}``。

    实测注意：错误响应可能是 ``{"msg": ..., "status": 100, "type": "A"}``，
    也可能是 ``{"message": ...}``（无 status）。两种都要能吃。
    """
    if not isinstance(payload, dict):
        raise ApiError(f"响应不是 JSON 对象: {payload!r}", raw=payload)

    if "status" not in payload:
        message = payload.get("message") or payload.get("msg") or "未知错误"
        raise ApiError(str(message), raw=payload)

    status = int(payload.get("status", -1))
    message = str(payload.get("msg") or payload.get("message") or "")
    return status, message, payload.get("data")


class HypergryphAuth:
    """扫码登录链路的最小客户端。"""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "HypergryphAuth":
        if self._client is None:
            self._client = build_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("客户端未初始化，请使用 async with HypergryphAuth() as auth")
        return self._client

    # -------------------------------------------------------------- 轮询 --

    async def get_scan_state(self, scan_id: str) -> ScanState:
        """查询扫码状态。**这是抢码循环里被调用最多的接口，必须保持轻量。**"""
        response = await self.client.get(SCAN_STATUS, params={"scanId": scan_id})
        response.raise_for_status()
        status, message, data = _parse_envelope(response.json())

        # ⚠ 关键：**101「已扫码待确认」阶段也会给出 scanCode**（实测 + 用户验证）。
        # 早期版本只认 status == 0，把 101 的 scanCode 直接丢掉，
        # 于是工具被迫等到用户点完确认（1~2 秒）才动手 ——
        # 那是它唯一的速度优势，白送给了人手。
        scan_code: str | None = None
        if status in (SCAN_STATUS_CONFIRMED, SCAN_STATUS_PENDING_CONFIRM) and isinstance(data, dict):
            raw_code = data.get("scanCode")
            if isinstance(raw_code, str) and raw_code:
                scan_code = raw_code

        return ScanState(
            scanned=scan_code is not None,
            scan_code=scan_code,
            raw_status=status,
            raw_message=message,
        )

    # ------------------------------------------------- 代做扫码（自动） --

    async def scan_login(self, scan_id: str, account_token: str) -> dict[str, Any]:
        """用 account_token 把 ``scanId`` 登记为「已扫码」。

        等价于用森空岛 App 扫这个码。成功后 ``scan_status`` 会从 ``100`` 变成 ``101``。

        返回体含 ``phone`` / ``loginRegion`` 等信息，可用于确认登的是哪个账号。
        """
        response = await self.client.post(
            SCAN_LOGIN, json={"scanId": scan_id, "token": account_token}
        )
        response.raise_for_status()
        status, message, data = _parse_envelope(response.json())
        if status != 0:
            raise ApiError(f"代扫码失败: {message}", status=status, raw=data)
        return data if isinstance(data, dict) else {}

    async def confirm_scan(self, scan_id: str, account_token: str) -> dict[str, Any]:
        """用 account_token 代为「确认登录」。

        等价于在森空岛 App 里点「确认」。成功后 ``scan_status`` 会变成 ``0``
        并给出 ``scanCode``。
        """
        response = await self.client.post(
            UPDATE_SCAN_STATUS, json={"scanId": scan_id, "token": account_token}
        )
        response.raise_for_status()
        status, message, data = _parse_envelope(response.json())
        if status != 0:
            raise ApiError(f"代确认失败: {message}", status=status, raw=data)
        return data if isinstance(data, dict) else {}

    # -------------------------------------------------------------- 抢码 --

    async def redeem_scan_code(self, scan_code: str) -> dict[str, Any]:
        """用 scanCode 换取鹰角通行证 token —— 全流程的唯一胜负手。

        实测该接口无需鉴权 header，纯靠 scanCode 的有效性与提交时序判定。
        """
        response = await self.client.post(TOKEN_BY_SCAN_CODE, json={"scanCode": scan_code})
        response.raise_for_status()
        status, message, data = _parse_envelope(response.json())
        if status != 0:
            raise ApiError(f"换取 token 失败: {message}", status=status, raw=data)
        if not isinstance(data, dict):
            raise ApiError(f"换取 token 返回体异常: {data!r}", raw=data)
        # 必须校验 token 真的在 —— 否则会把"没换到 token"当成成功，
        # 一路谎报下去（早期版本就是 self 用了 data.get("token") or ""，
        # 空字符串照样算成功，非常难查）。
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ApiError(f"换取 token 失败：返回体里没有 token 字段（{sorted(data)}）", raw=data)
        return data

    # -------------------------------------------------------- token 探测 --

    async def probe_account_token(self, account_token: str) -> dict[str, Any]:
        """校验 account_token 是否有效，并顺带取回账号基本信息。

        用 ``/user/info/v1/basic``（token 走 query，实测匿名 400、带有效 token 返回 hgId）。
        注意：本函数**不验证 token 的剩余有效期**，那是服务端的权限。
        """
        response = await self.client.get(
            USER_BASIC, params={"token": account_token}
        )
        if response.status_code >= 400:
            raise ApiError(
                f"account_token 校验失败 (HTTP {response.status_code})，可能已失效",
                status=response.status_code,
            )
        status, message, data = _parse_envelope(response.json())
        if status != 0:
            raise ApiError(f"account_token 无效: {message}", status=status, raw=data)
        if not isinstance(data, dict):
            raise ApiError(f"account 信息返回体异常: {data!r}", raw=data)
        return data

__all__ = [
    "AS_BASE",
    "SCAN_STATUS_CONFIRMED",
    "SCAN_STATUS_EXPIRED",
    "SCAN_STATUS_NOT_SCANNED",
    "SCAN_STATUS_PENDING_CONFIRM",
    "SCAN_STATUS_WAITING",
    "SCAN_LOGIN",
    "UPDATE_SCAN_STATUS",
    "GEN_SCAN_LOGIN",
    "SCAN_ID_EXTRACT_RE",
    "SCAN_ID_RE",
    "SCAN_STATUS",
    "SCAN_URL_SCHEME",
    "SKLAND_APP_CODE",
    "TOKEN_BY_SCAN_CODE",
    "USER_BASIC",
    "ApiError",
    "HypergryphAuth",
    "ScanSession",
    "ScanState",
    "USE_ENV_PROXY",
    "build_client",
    "extract_scan_id",
]
