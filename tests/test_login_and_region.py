"""扫码登录出码、token 查看安全阀的单测。

这些测试刻意**不依赖终端大小、不弹窗**，只覆盖可确定性验证的部分：
出码的正确性用「自产 PNG → 自己的解码器读回」闭环来验证。
（区域框选功能曾被实现两次后移除：它要求交互式拖拽窗口，与命令行工具的定位冲突，而且全屏扫描并不慢。）
"""

from __future__ import annotations

import urllib.request

import pytest

from endfield_scanner.core import qrlogin

PAYLOAD = "hypergryph://scan_login?scanId=" + "a" * 32


class TestUnicodeQr:
    def test_shape_is_square_in_terminal(self) -> None:
        text, columns = qrlogin.render_unicode_qr(PAYLOAD)
        lines = text.splitlines()
        assert columns > 20
        # 终端字符高约为宽两倍，半块压缩后行数应约为列数的一半（否则会被拉长扫不出）
        assert abs(len(lines) - columns / 2) <= 1

    def test_uses_half_block_glyphs(self) -> None:
        text, _ = qrlogin.render_unicode_qr(PAYLOAD)
        assert set(text) <= {" ", "▀", "▄", "█", "\n"}

    def test_different_payloads_differ(self) -> None:
        first, _ = qrlogin.render_unicode_qr(PAYLOAD)
        second, _ = qrlogin.render_unicode_qr(PAYLOAD.replace("a", "b"))
        assert first != second


class TestPngRoundTrip:
    def test_png_is_decodable_by_our_own_decoder(self, tmp_path) -> None:
        """自产二维码 → 自己的解码器读回，形成闭环。"""
        import cv2

        from endfield_scanner.core.qrdecode import QRDecoder

        scan_id = "b" * 32
        payload = f"hypergryph://scan_login?scanId={scan_id}"
        path = qrlogin.render_png(payload, tmp_path / "qr.png")

        image = cv2.imread(str(path))
        assert image is not None
        result = QRDecoder().decode(image)
        assert result is not None, "自己生成的二维码自己读不出来"
        assert result.scan_id == scan_id

    def test_creates_parent_directory(self, tmp_path) -> None:
        nested = tmp_path / "deep" / "deeper" / "qr.png"
        qrlogin.render_png(PAYLOAD, nested)
        assert nested.exists()


class TestLocalQrPage:
    def test_serves_svg_qr(self) -> None:
        url, server = qrlogin.serve_qr_page(PAYLOAD)
        try:
            assert url.startswith("http://127.0.0.1:")
            html = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
            assert "<svg" in html
            assert PAYLOAD in html
        finally:
            server.shutdown()
            server.server_close()

    def test_each_call_gets_its_own_port(self) -> None:
        first_url, first = qrlogin.serve_qr_page(PAYLOAD)
        second_url, second = qrlogin.serve_qr_page(PAYLOAD)
        try:
            assert first_url != second_url
        finally:
            first.shutdown()
            first.server_close()
            second.shutdown()
            second.server_close()
class _FakeLoginServer:
    """只实现扫码登录三个端点的假服务器。

    ``pending_s`` 用来模拟「用户扫了码、但隔一会儿才点授权弹窗」——
    这期间服务端返回 101，正是曾经让程序误判退出的中间态。
    """

    def __init__(self, *, pending_s: float = 0.0, expire: bool = False, token: str = "fake-token") -> None:
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.pending_s = pending_s
        self.expire = expire
        self.token = token
        self.token_requests = 0
        outer = self
        self.started_at = time.monotonic()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a): pass

            def _send(self, payload):
                import json as _json
                body = _json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                import json as _json
                n = int(self.headers.get("Content-Length") or 0)
                body = _json.loads(self.rfile.read(n) or b"{}")
                if self.path.endswith("/gen_scan/login"):
                    self._send({"status": 0, "msg": "OK", "type": "A",
                                "data": {"scanId": "a" * 32,
                                         "scanUrl": "hypergryph://scan_login?scanId=" + "a" * 32}})
                    return
                if self.path.endswith("/token_by_scan_code"):
                    outer.token_requests += 1
                    if body.get("scanCode") == "GOODCODE":
                        self._send({"status": 0, "msg": "OK", "type": "A", "data": {"token": outer.token}})
                    else:
                        self._send({"status": 100, "msg": "登录失败", "type": "B"})
                    return
                self._send({"status": -1, "msg": "not found"})

            def do_GET(self):
                import time as _t
                if "/scan_status" in self.path:
                    if outer.expire:
                        self._send({"status": 102, "msg": "已失效", "type": "A"})
                        return
                    elapsed = _t.monotonic() - outer.started_at
                    if elapsed < outer.pending_s:
                        # 101 已扫码待确认
                        self._send({"status": 101, "msg": "已扫码待确认", "type": "A"})
                    else:
                        self._send({"status": 0, "msg": "OK", "type": "A",
                                    "data": {"scanCode": "GOODCODE"}})
                    return
                self._send({"status": -1, "msg": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def endpoints(self) -> dict[str, str]:
        return {
            "gen": f"{self.base}/general/v1/gen_scan/login",
            "status": f"{self.base}/general/v1/scan_status",
            "token": f"{self.base}/user/auth/v1/token_by_scan_code",
        }

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class TestQrLoginStateMachine:
    def test_completes_after_pending_confirm(self) -> None:
        """回归：必须能穿过 101「已扫码待确认」拿到 token。

        曾经的 bug：只认识 100 和 0，看到 101 直接抛
        「二维码状态异常：已扫码待确认」退出 —— 而那正是用户去点授权弹窗的一刻。
        """
        import httpx as _httpx

        server = _FakeLoginServer(pending_s=0.8, token="token-after-confirm")
        try:
            with _httpx.Client(timeout=5.0, trust_env=False) as client:
                token = qrlogin.login_by_qr(
                    timeout_s=15,
                    open_browser=False,
                    quiet=True,
                    endpoints=server.endpoints(),
                    client=client,
                )
            assert token == "token-after-confirm"
            assert server.token_requests == 1
        finally:
            server.stop()

    def test_immediate_confirm_also_works(self) -> None:
        import httpx as _httpx

        server = _FakeLoginServer(pending_s=0.0, token="instant")
        try:
            with _httpx.Client(timeout=5.0, trust_env=False) as client:
                token = qrlogin.login_by_qr(
                    timeout_s=15, open_browser=False, quiet=True,
                    endpoints=server.endpoints(), client=client,
                )
            assert token == "instant"
        finally:
            server.stop()

    def test_expired_qr_raises(self) -> None:
        import httpx as _httpx

        server = _FakeLoginServer(expire=True)
        try:
            with _httpx.Client(timeout=5.0, trust_env=False) as client:
                with pytest.raises(qrlogin.QrLoginError) as excinfo:
                    qrlogin.login_by_qr(
                        timeout_s=5, open_browser=False, quiet=True,
                        endpoints=server.endpoints(), client=client,
                    )
            assert "失效" in str(excinfo.value)
        finally:
            server.stop()

    def test_timeout_mentions_pending_confirm_when_scanned(self) -> None:
        """扫了但一直不确认：报错要说清是「没确认」，不能含糊成「没扫到」。"""
        import httpx as _httpx

        server = _FakeLoginServer(pending_s=9999.0)
        try:
            with _httpx.Client(timeout=5.0, trust_env=False) as client:
                with pytest.raises(qrlogin.QrLoginError) as excinfo:
                    qrlogin.login_by_qr(
                        timeout_s=2, open_browser=False, quiet=True,
                        endpoints=server.endpoints(), client=client,
                    )
            assert "--paste" in str(excinfo.value)
        finally:
            server.stop()


# --------------------------------------------------------------------------- #
# 帧源暂停闸门
# --------------------------------------------------------------------------- #


class TestScreenCaptureErrorIsActionable:
    """截屏失败必须给出可照做的提示，而不是把 mss 的原始消息丢给用户。

    真实的坑：Windows 上通过 OpenSSH 服务远程执行时，进程落在 **Session 0**
    （服务会话），那里没有可捕获的桌面，``mss`` 抛的是
    ``BitBlt: Access is denied`` —— 这句话对用户毫无指导意义。
    """

    def test_windows_hint_mentions_session0_and_desktop(self, monkeypatch) -> None:
        import sys

        from endfield_scanner.core.frames import FrameSourceError, _screen_capture_error

        monkeypatch.setattr(sys, "platform", "win32")
        err = _screen_capture_error(RuntimeError("BitBlt: Access is denied"))
        assert isinstance(err, FrameSourceError)
        text = str(err)
        assert "Session 0" in text
        assert "桌面会话" in text
        # 原始错误要保留，便于进一步排查
        assert "BitBlt" in text

    def test_macos_hint_mentions_screen_recording_permission(self, monkeypatch) -> None:
        import sys

        from endfield_scanner.core.frames import _screen_capture_error

        monkeypatch.setattr(sys, "platform", "darwin")
        text = str(_screen_capture_error(RuntimeError("no display")))
        assert "屏幕录制" in text
        assert "重启" in text

    def test_other_platforms_get_a_generic_hint(self, monkeypatch) -> None:
        import sys

        from endfield_scanner.core.frames import _screen_capture_error

        monkeypatch.setattr(sys, "platform", "linux")
        text = str(_screen_capture_error(RuntimeError("boom")))
        assert "桌面" in text or "显示" in text


# --------------------------------------------------------------------------- #
# 框选窗口的坐标换算与预览渲染（不需要真的点鼠标）
# --------------------------------------------------------------------------- #


def _synthetic_selection(full, x: int, y: int, w: int, h: int, scale: float):
    """模拟原生选择器的产出：从全屏图裁一块，并按 scale 输出。

    ``scale=2.0`` 对应 Retina 物理像素输出（本机就是这种），``1.0`` 对应普通屏。
    """
    import cv2

    crop = full[y : y + h, x : x + w]
    if scale != 1.0:
        crop = cv2.resize(
            crop, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_NEAREST
        )
    return crop
