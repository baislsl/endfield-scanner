"""HTTP 请求日志：把发往服务端的每一个请求都打出来。

为什么做在客户端层而不是每个接口里
----------------------------------
抢码要 debug 的东西几乎都是"到底发了什么请求"。如果在每个接口方法里手写
日志，一来容易漏，二来新增接口就会忘记加。挂在 httpx 的 ``event_hooks``
上就一次覆盖全部 —— 包括：
  * ``as.hypergryph.com`` 的扫码登录接口
  * ``api.live.bilibili.com`` 的直播取流接口
  * ``www.skland.com`` 的引导登录页
  * 本地出码页

用法
----
全局开关（CLI 的 ``-v`` 会打开它）::

    from .httplog import HttpLog, ENABLED
    ENABLED.value = True

敏感信息
--------
``token`` / ``cred`` / ``authorization`` 这类值**绝不能**进日志 ——
日志会被贴进 issue、聊天窗口、终端历史。所以这里只打印**字段名**，
值一律打码。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

class Flag:
    """一个简单的全局布尔开关（可被多处读写）。"""

    def __init__(self, value: bool = False) -> None:
        self.value = value

    def __bool__(self) -> bool:
        return self.value


#: 全局开关。CLI 的 ``-v`` 打开它；测试里可临时打开。
ENABLED = Flag(False)

#: 这些字段名对应的值必须打码
SECRET_KEYS = {
    "token",
    "cred",
    "authorization",
    "password",
    "scanCode",
    "scan_code",
    "cookie",
    "credential",
    "account_token",
    "device_token",
}

_COLOR = {
    "req": "\033[36m",
    "ok": "\033[32m",
    "warn": "\033[33m",
    "err": "\033[31m",
    "dim": "\033[90m",
    "reset": "\033[0m",
}


def mask(value: str, *, keep: int = 6) -> str:
    """把敏感值打码，但保留头部便于比对是不是同一个。"""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}…({len(value)}字符)"


def _scrub(payload: Any, depth: int = 0) -> Any:
    """递归地把敏感字段的值替换成打码形式。"""
    if depth > 6:
        return "…"
    if isinstance(payload, dict):
        return {
            k: (mask(str(v)) if k in SECRET_KEYS and isinstance(v, str) else _scrub(v, depth + 1))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_scrub(item, depth + 1) for item in payload[:8]]
    if isinstance(payload, str) and len(payload) > 200:
        return payload[:200] + "…"
    return payload


@dataclass
class HttpLog:
    """记录每个请求的 URL / 方法 / 参数 / 响应状态与耗时。

    :param color: 是否上色（输出到终端时用）。
    :param sink: 输出函数，默认 print。便于测试捕获。
    """

    color: bool = True
    sink: Any = None
    #: 请求开始时刻，key 是 httpx 的 request
    _started: dict[int, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.sink is None:
            self.sink = print

    # ------------------------------------------------------------------ #

    def _emit(self, kind: str, message: str) -> None:
        text = f"[http] {message}"
        if self.color:
            c = _COLOR.get(kind, "")
            text = f"{c}{text}{_COLOR['reset']}"
        self.sink(text)

    @staticmethod
    def _describe_request(request: Any) -> str:
        """把请求渲染成 `METHOD url?params body` 一行。"""
        url = str(request.url)
        # httpx 的 URL 已经把 query 拼进去了，这里原样保留便于直接复制粘贴
        parts = [f"{request.method} {url}"]

        body = getattr(request, "content", b"") or b""
        if body:
            try:
                parsed = json.loads(body)
                parts.append(f"body={json.dumps(_scrub(parsed), ensure_ascii=False)}")
            except Exception:
                text = body.decode("utf-8", "replace")
                parts.append(f"body={text[:200]}")
        return "  ".join(parts)

    def on_request(self, request: Any) -> None:
        if not ENABLED:
            return
        with self._lock:
            self._started[id(request)] = time.monotonic()
        self._emit("req", "→ " + self._describe_request(request))

    def on_response(self, response: Any) -> None:
        if not ENABLED:
            return
        with self._lock:
            started = self._started.pop(id(response.request), None)
        elapsed = f"{(time.monotonic() - started) * 1000:.0f}ms" if started else "?"

        status = response.status_code
        kind = "ok" if status < 400 else ("warn" if status < 500 else "err")

        # 顺带把业务状态码/消息打出来：鹰角接口的错误常常是 HTTP 200 + status!=0
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict) and "status" in payload:
                sc = payload.get("status")
                msg = payload.get("msg") or payload.get("message") or ""
                detail = f"  status={sc} msg={msg!r}"
        except Exception:
            pass

        self._emit(kind, f"← {status} {response.request.method} {response.request.url}  ({elapsed}){detail}")


#: 默认实例。``build_client`` 会把它挂到 httpx 上。
LOGGER = HttpLog()


# --------------------------------------------------------------------------- #
# httpx 钩子适配
# --------------------------------------------------------------------------- #
#
# httpx 对钩子有硬性要求：
#   * ``AsyncClient`` 的钩子**必须是协程函数**（同步函数会报
#     "object NoneType can't be used in 'await' expression"）
#   * ``Client`` 的钩子必须是同步函数
# 所以同一个 HttpLog 要提供两套入口，按客户端类型挂对应的那套。


async def _async_on_request(request: Any) -> None:
    LOGGER.on_request(request)


async def _async_on_response(response: Any) -> None:
    LOGGER.on_response(response)


def install(client: Any, *, color: bool = True, sink: Any = None) -> None:
    """把日志钩子挂到一个 httpx 客户端上（自动区分同步/异步客户端）。

    :param color: 是否上色。传 sink 时会忽略 color（供测试捕获纯文本）。
    :param sink: 输出函数，默认 print。
    """
    logger = LOGGER if (color is True and sink is None) else HttpLog(color=color, sink=sink)

    # 异步客户端（我们主力用这个）：钩子必须是协程
    if hasattr(client, "aclose"):
        async def on_req(request: Any) -> None:
            logger.on_request(request)

        async def on_resp(response: Any) -> None:
            logger.on_response(response)

        client.event_hooks["request"].append(on_req)
        client.event_hooks["response"].append(on_resp)
        return

    # 同步客户端（直播取流 / 引导登录页用）
    client.event_hooks["request"].append(logger.on_request)
    client.event_hooks["response"].append(logger.on_response)


__all__ = ["ENABLED", "LOGGER", "SECRET_KEYS", "Flag", "HttpLog", "install", "mask"]
