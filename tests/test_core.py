"""不碰真实接口的纯逻辑单测：scanId 抽取、二维码解码、参数解析。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from endfield_scanner.core.api import extract_scan_id
from endfield_scanner.core.frames import FileSource, crop_region, parse_region, Frame
from endfield_scanner.core.qrdecode import QRDecoder

# 用户提供的真实二维码解析结果，作为回归基准
REAL_SCAN_ID = "d0549b45e5002c2cf8e5c302deb1dca6"
REAL_PAYLOAD = f"hypergryph://scan_login?scanId={REAL_SCAN_ID}"


# --------------------------------------------------------------------------- #
# scanId 抽取
# --------------------------------------------------------------------------- #


class TestExtractScanId:
    def test_real_payload(self) -> None:
        assert extract_scan_id(REAL_PAYLOAD) == REAL_SCAN_ID

    def test_bare_hex(self) -> None:
        assert extract_scan_id(REAL_SCAN_ID) == REAL_SCAN_ID

    def test_uppercase_is_normalised(self) -> None:
        assert extract_scan_id(REAL_PAYLOAD.upper()) == REAL_SCAN_ID

    def test_whitespace_tolerated(self) -> None:
        assert extract_scan_id(f"  {REAL_PAYLOAD}\n") == REAL_SCAN_ID

    def test_query_order_independent(self) -> None:
        payload = f"hypergryph://scan_login?foo=1&scanId={REAL_SCAN_ID}&bar=2"
        assert extract_scan_id(payload) == REAL_SCAN_ID

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "hypergryph://scan_login",
            "https://example.com/",
            "scanId=tooshort",
            "scanId=" + "z" * 32,  # 非 hex
            "scanId=" + "a" * 31,  # 少一位
            "scanId=" + "a" * 33,  # 多一位（不能把前 32 位当成合法 ID）
        ],
    )
    def test_rejects_garbage(self, bad: str) -> None:
        assert extract_scan_id(bad) is None


# --------------------------------------------------------------------------- #
# 二维码解码
# --------------------------------------------------------------------------- #


def _render_qr(payload: str, *, qr_px: int, canvas: tuple[int, int] = (1080, 1920), jpeg: int = 100):
    """把一个二维码渲染到指定尺寸的深色画布中央（模拟直播画面）。"""
    import cv2
    import qrcode

    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(payload)
    qr.make(fit=True)
    image = np.array(qr.make_image(fill_color="black", back_color="white").convert("RGB"))
    bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    bgr = cv2.resize(bgr, (qr_px, qr_px), interpolation=cv2.INTER_AREA)

    canvas_h, canvas_w = canvas
    frame = np.full((canvas_h, canvas_w, 3), 32, dtype=np.uint8)
    top, left = (canvas_h - qr_px) // 2, (canvas_w - qr_px) // 2
    frame[top : top + qr_px, left : left + qr_px] = bgr

    if jpeg < 100:
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg])
        if ok:
            frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    return frame


class TestQRDecoder:
    def test_decodes_real_payload(self) -> None:
        decoder = QRDecoder()
        result = decoder.decode(_render_qr(REAL_PAYLOAD, qr_px=300, jpeg=85))
        assert result is not None
        assert result.payload == REAL_PAYLOAD
        assert result.scan_id == REAL_SCAN_ID

    @pytest.mark.parametrize("qr_px", [400, 300, 200, 150, 100, 80])
    def test_decodes_across_sizes(self, qr_px: int) -> None:
        # 直播里二维码被缩放是常态，必须覆盖到相当小的尺寸
        decoder = QRDecoder()
        result = decoder.decode(_render_qr(REAL_PAYLOAD, qr_px=qr_px, jpeg=75))
        assert result is not None, f"{qr_px}px 的二维码未能识别"
        assert result.scan_id == REAL_SCAN_ID

    def test_roi_reuse_is_faster_than_cold(self) -> None:
        frame = _render_qr(REAL_PAYLOAD, qr_px=300, jpeg=85)
        decoder = QRDecoder(enable_roi_reuse=True)

        first = decoder.decode(frame)
        assert first is not None and first.bbox is not None

        warm = [decoder.decode(frame) for _ in range(5)]
        assert all(r is not None for r in warm)
        # RoI 命中后应当走 roi 策略，且明显快于首帧全图搜索
        assert any("roi" in (r.method or "") for r in warm)
        assert min(r.elapsed_ms for r in warm) < first.elapsed_ms

    def test_roi_disabled_still_decodes(self) -> None:
        decoder = QRDecoder(enable_roi_reuse=False)
        result = decoder.decode(_render_qr(REAL_PAYLOAD, qr_px=300))
        assert result is not None and result.scan_id == REAL_SCAN_ID

    def test_no_qr_returns_none(self) -> None:
        decoder = QRDecoder()
        assert decoder.decode(np.zeros((240, 320, 3), dtype=np.uint8)) is None

    def test_empty_frame_is_safe(self) -> None:
        decoder = QRDecoder()
        assert decoder.decode(np.zeros((0, 0, 3), dtype=np.uint8)) is None

    def test_records_stats(self) -> None:
        decoder = QRDecoder()
        decoder.decode(_render_qr(REAL_PAYLOAD, qr_px=300))
        assert decoder.stats.frames >= 1
        assert decoder.stats.hits == 1
        assert "zxing" in decoder.backend or "cv2" in decoder.backend

    def test_ignores_unrelated_qr(self) -> None:
        # 画面上出现无关二维码时不应被当成登录码
        decoder = QRDecoder()
        result = decoder.decode(_render_qr("https://example.com/hello", qr_px=300))
        assert result is not None
        assert result.scan_id is None


# --------------------------------------------------------------------------- #
# 帧源工具
# --------------------------------------------------------------------------- #


class TestRegionParsing:
    def test_parses(self) -> None:
        assert parse_region("100,200,800,400") == (100, 200, 800, 400)

    def test_tolerates_spaces(self) -> None:
        assert parse_region(" 10 , 20 , 30 , 40 ") == (10, 20, 30, 40)

    def test_none_and_empty(self) -> None:
        assert parse_region(None) is None
        assert parse_region("") is None

    @pytest.mark.parametrize("bad", ["1,2,3", "a,b,c,d", "1,2,3,4,5", "10 20 30 40"])
    def test_rejects_bad(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_region(bad)


class TestCropRegion:
    def test_crops_and_records_origin(self) -> None:
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        frame = Frame(image=image, timestamp=0.0)
        cropped = crop_region(frame, (10, 20, 50, 40))
        assert cropped.image.shape == (40, 50, 3)
        assert cropped.region == (10, 20, 50, 40)

    def test_clamps_out_of_bounds(self) -> None:
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        frame = Frame(image=image, timestamp=0.0)
        # 故意给一个超出画面的区域，应当被夹到画面内而不是崩掉
        cropped = crop_region(frame, (190, 90, 100, 100))
        assert cropped.image.shape[0] >= 1 and cropped.image.shape[1] >= 1


class TestFileSource:
    def test_reads_image_once_without_loop(self, tmp_path) -> None:
        import cv2

        path = tmp_path / "qr.png"
        cv2.imwrite(str(path), _render_qr(REAL_PAYLOAD, qr_px=300))

        frames = list(FileSource(str(path), loop=False).frames())
        assert len(frames) == 1
        assert frames[0].image.ndim == 3


# --------------------------------------------------------------------------- #
# HTTP 请求日志（-v 的行为）
# --------------------------------------------------------------------------- #


class TestHttpLog:
    """`-v` 应当把发往服务端的每个请求都打出来，且凭证必须打码。

    日志会被贴进 issue / 聊天窗口 / 终端历史，所以凭证泄露在这里是真实风险，
    必须有测试守着。
    """

    @pytest.fixture(autouse=True)
    def _enable_logging(self):
        """这些用例要验证"开着日志时的输出"，所以先把全局开关打开。"""
        from endfield_scanner.core import httplog

        was = httplog.ENABLED.value
        httplog.ENABLED.value = True
        yield
        httplog.ENABLED.value = was

    def _logger(self):
        from endfield_scanner.core.httplog import HttpLog

        lines: list[str] = []
        return HttpLog(color=False, sink=lines.append), lines

    def test_masks_secret_values(self) -> None:
        from endfield_scanner.core.httplog import _scrub

        scrubbed = _scrub(
            {"scanId": "a" * 32, "token": "SECRET-TOKEN-VALUE", "cred": "SECRET-CRED"}
        )
        text = json.dumps(scrubbed, ensure_ascii=False)
        assert "SECRET-TOKEN-VALUE" not in text
        assert "SECRET-CRED" not in text
        # scanId 不是凭证，保留原文便于核对
        assert "a" * 32 in text

    def test_masks_nested_and_case_sensitive_keys(self) -> None:
        from endfield_scanner.core.httplog import _scrub

        text = json.dumps(
            _scrub({"data": {"account_token": "X" * 20}, "list": [{"cred": "Y" * 20}]}),
            ensure_ascii=False,
        )
        assert "X" * 20 not in text and "Y" * 20 not in text

    def test_short_secret_fully_masked(self) -> None:
        from endfield_scanner.core.httplog import mask

        assert mask("abc") == "***"
        assert "abcd" in mask("abcdefghij")

    def test_request_line_contains_method_and_full_url(self) -> None:
        import httpx

        logger, lines = self._logger()
        request = httpx.Request(
            "POST",
            "https://as.hypergryph.com/user/info/v1/scan_login?scanId=" + "b" * 32,
            json={"token": "SHOULD-NOT-LEAK", "scanId": "b" * 32},
        )
        logger.on_request(request)
        out = "\n".join(lines)
        assert "POST" in out
        assert "https://as.hypergryph.com/user/info/v1/scan_login" in out
        assert "scanId=" + "b" * 32 in out
        assert "SHOULD-NOT-LEAK" not in out

    def test_response_line_shows_status_and_business_code(self) -> None:
        import httpx

        logger, lines = self._logger()
        request = httpx.Request("GET", "https://as.hypergryph.com/general/v1/scan_status")
        response = httpx.Response(
            200, request=request, json={"status": 100, "msg": "未扫码", "type": "A"}
        )
        logger.on_response(response)
        out = "\n".join(lines)
        assert "200" in out
        assert "status=100" in out
        assert "未扫码" in out

    def test_disabled_logger_stays_silent(self) -> None:
        import httpx

        from endfield_scanner.core import httplog

        httplog.ENABLED.value = False
        logger, lines = self._logger()
        request = httpx.Request("GET", "https://example.com/")
        logger.on_request(request)
        logger.on_response(httpx.Response(200, request=request))
        assert lines == []

    def test_async_client_hooks_are_coroutines(self) -> None:
        """httpx 的 AsyncClient 要求钩子必须是协程，挂同步函数会直接崩。

        这个 bug 真的发生过：日志一开，所有请求都报
        "object NoneType can't be used in 'await' expression"。
        """
        import inspect

        from endfield_scanner.core import api as api_mod

        client = api_mod.build_client()
        try:
            hooks = client.event_hooks
            assert hooks["request"], "没有挂上请求钩子"
            for hook in hooks["request"] + hooks["response"]:
                assert inspect.iscoroutinefunction(hook), f"异步客户端的钩子必须是协程: {hook}"
        finally:
            import asyncio

            # 不用 get_event_loop_policy()（Python 3.14 起弃用）
            asyncio.run(client.aclose())


# --------------------------------------------------------------------------- #
# 跨平台配置目录
# --------------------------------------------------------------------------- #


class TestConfigDir:
    """配置目录必须遵循各平台惯例。

    早期版本写死 `~/.config`（Unix 约定），在 Windows 上会落到
    `C:\\Users\\<你>\\.config\\...` —— 能用，但用户按惯例去 %APPDATA% 找不到。
    """

    @pytest.fixture(autouse=True)
    def _clear_override(self, monkeypatch):
        monkeypatch.delenv("ENDFIELD_SCANNER_HOME", raising=False)
        yield

    def test_env_override_wins(self, monkeypatch, tmp_path) -> None:
        from endfield_scanner.core.paths import config_dir

        monkeypatch.setenv("ENDFIELD_SCANNER_HOME", str(tmp_path / "custom"))
        assert config_dir() == tmp_path / "custom"

    def test_windows_uses_appdata(self, monkeypatch, tmp_path) -> None:
        import sys

        from endfield_scanner.core.paths import config_dir

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        assert config_dir() == tmp_path / "Roaming" / "endfield-scanner"

    def test_windows_without_appdata_falls_back_under_home(self, monkeypatch, tmp_path) -> None:
        import sys

        from endfield_scanner.core.paths import config_dir

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        # 不能落到 Unix 风格的 ~/.config（用 Path 逐段比较，避免分隔符差异）
        assert config_dir().parts[-2:] != (".config", "endfield-scanner")
        assert config_dir() == tmp_path / "AppData" / "Roaming" / "endfield-scanner"

    def test_macos_uses_application_support(self, monkeypatch, tmp_path) -> None:
        import sys

        from endfield_scanner.core.paths import config_dir

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        # 用 Path 拼接来断言，这样在 Windows 上跑测试也不会因反斜杠而误判
        assert config_dir() == tmp_path / "Library" / "Application Support" / "endfield-scanner"

    def test_linux_prefers_xdg_config_home(self, monkeypatch, tmp_path) -> None:
        import sys

        from endfield_scanner.core.paths import config_dir

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config_dir() == tmp_path / "xdg" / "endfield-scanner"

    def test_linux_defaults_to_dot_config(self, monkeypatch, tmp_path) -> None:
        import sys

        from endfield_scanner.core.paths import config_dir

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        assert config_dir() == tmp_path / ".config" / "endfield-scanner"

    def test_does_not_create_directory(self, monkeypatch, tmp_path) -> None:
        """只解析路径，不该有副作用（由调用方在写入时创建）。"""
        from endfield_scanner.core.paths import config_dir

        target = tmp_path / "not-created"
        monkeypatch.setenv("ENDFIELD_SCANNER_HOME", str(target))
        config_dir()
        assert not target.exists()


class TestSaveTokenReportsKeyringFailure:
    """钥匙串写入失败不能静默 —— 那意味着 token 明文落盘，用户有权知道。"""

    def test_warns_and_falls_back(self, monkeypatch, tmp_path, capsys) -> None:
        from endfield_scanner.core import auth as auth_mod

        monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(auth_mod, "FALLBACK_TOKEN_FILE", tmp_path / "token.json")

        class BrokenKeyring:
            @staticmethod
            def get_keyring():
                return BrokenKeyring()

            @staticmethod
            def set_password(*args, **kwargs):
                raise RuntimeError("A specified logon session does not exist")

        monkeypatch.setattr(auth_mod, "_keyring", lambda: BrokenKeyring)

        where = auth_mod.save_token("SECRET-TOKEN-VALUE")
        captured = capsys.readouterr()
        assert "钥匙串写入失败" in captured.err
        assert "明文" in captured.err
        # 仍然回退成功，功能不受影响
        assert "明文文件" in where
        assert (tmp_path / "token.json").read_text("utf-8").count("SECRET-TOKEN-VALUE") == 1

    def test_refuses_fallback_when_plaintext_disallowed(self, monkeypatch, tmp_path) -> None:
        from endfield_scanner.core import auth as auth_mod

        monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(auth_mod, "FALLBACK_TOKEN_FILE", tmp_path / "token.json")

        class BrokenKeyring:
            @staticmethod
            def get_keyring():
                return BrokenKeyring()

            @staticmethod
            def set_password(*args, **kwargs):
                raise RuntimeError("no logon session")

        monkeypatch.setattr(auth_mod, "_keyring", lambda: BrokenKeyring)
        with pytest.raises(auth_mod.TokenNotFound):
            auth_mod.save_token("x", allow_plaintext=False)


# --------------------------------------------------------------------------- #
# Windows 控制台编码
# --------------------------------------------------------------------------- #


class TestSetupConsole:
    """CLI 必须能在 Windows 控制台里输出中文与符号而不崩。

    真实踩到的坑：Windows 控制台默认代码页是 GBK（cp936），
    `doctor` 输出 `✔` 时直接抛::

        UnicodeEncodeError: 'gbk' codec can't encode character '\\u2714'

    整条命令就崩了。修法是切代码页 + 把 stdout/stderr 重包成 UTF-8 并
    用 errors="replace" 兜底（重定向输出时也不会抛）。
    """

    def test_reconfigures_streams_with_replacement(self) -> None:
        import io

        from endfield_scanner import cli

        class FakeStream(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[dict] = []

            def reconfigure(self, **kwargs):  # type: ignore[override]
                self.calls.append(kwargs)

        fake_out, fake_err = FakeStream(), FakeStream()
        import sys

        orig_out, orig_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = fake_out, fake_err
        try:
            cli.setup_console()
        finally:
            sys.stdout, sys.stderr = orig_out, orig_err

        for stream in (fake_out, fake_err):
            assert stream.calls, "没有调用 reconfigure"
            call = stream.calls[-1]
            assert call.get("encoding") == "utf-8"
            # 关键：必须有 errors 兜底，否则个别字符仍会抛
            assert call.get("errors") == "replace"

    def test_survives_streams_without_reconfigure(self) -> None:
        """输出被重定向到不支持 reconfigure 的对象时也不能崩。"""
        import sys

        from endfield_scanner import cli

        orig_out, orig_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = object(), object()  # 没有 reconfigure
        try:
            cli.setup_console()  # 不应抛异常
        finally:
            sys.stdout, sys.stderr = orig_out, orig_err

    def test_symbols_are_encodable_after_replacement(self) -> None:
        """即便退化成 GBK，用 replace 也不该抛异常。"""
        text = "✔ ✘ ⚠ ★ 中文"
        text.encode("gbk", errors="replace")  # 不应抛


class TestNoDisplayVsPermission:
    """「枚举不到显示器」与「没有权限」必须给出不同指引。

    真实踩到：无 GUI 上下文里 mss 抛 `IndexError`，但提示却说成
    「macOS 屏幕录制权限」——把用户引向了完全错误的排查方向。
    """

    def test_index_error_reports_no_desktop_not_permission(self, monkeypatch) -> None:
        import sys

        from endfield_scanner.core.frames import _screen_capture_error

        monkeypatch.setattr(sys, "platform", "darwin")
        text = str(_screen_capture_error(IndexError("list index out of range")))
        assert "找不到可截屏的显示器" in text
        assert "桌面会话" in text
        # 不能误导成权限问题
        assert "屏幕录制" not in text

    def test_shot_error_on_macos_reports_permission(self, monkeypatch) -> None:
        import sys

        from endfield_scanner.core.frames import _screen_capture_error

        monkeypatch.setattr(sys, "platform", "darwin")
        text = str(_screen_capture_error(RuntimeError("capture failed")))
        assert "屏幕录制" in text

    def test_shot_error_on_windows_reports_session0(self, monkeypatch) -> None:
        import sys

        from endfield_scanner.core.frames import _screen_capture_error

        monkeypatch.setattr(sys, "platform", "win32")
        text = str(_screen_capture_error(RuntimeError("BitBlt: Access is denied")))
        assert "Session 0" in text
        assert "屏幕录制" not in text


class TestHugeFrameFastPath:
    """高 DPI 屏（物理像素巨大）的解码快路径。

    背景：用户在 200% 缩放屏上，mss 给出 3456x2168 而逻辑分辨率只有 1728x1084。
    实测该尺寸下 zxing 单次全图搜索约 400ms，走完整级联（含两次放大救援）
    无码帧要 500~1200ms —— 每秒只处理一两帧，抢码时可能等好几秒才看见码。

    取舍（3456x2168 实测）::

        场景            快路径     完整级联
        无码（最常见）   144ms      1185ms
        码 >=100px       ~140ms     ~140ms
        码 60px          漏         543ms

    所以默认走快路径，把"要不要为极小的码牺牲 8 倍帧率"留给用户决定。
    """

    def _frame(self, *, qr_px: int | None, size=(2168, 3456)):
        import cv2
        import numpy as np
        import qrcode

        rng = np.random.default_rng(0)
        canvas = rng.integers(0, 255, (*size, 3), dtype=np.uint8)
        if qr_px:
            payload = "hypergryph://scan_login?scanId=" + "a" * 32
            qr = qrcode.QRCode(border=2, box_size=10)
            qr.add_data(payload)
            qr.make(fit=True)
            img = cv2.cvtColor(
                np.array(qr.make_image(fill_color="black", back_color="white").convert("RGB")),
                cv2.COLOR_RGB2BGR,
            )
            img = cv2.resize(img, (qr_px, qr_px), interpolation=cv2.INTER_AREA)
            h, w = size
            canvas[h // 2 - qr_px // 2 : h // 2 + qr_px // 2,
                   w // 2 - qr_px // 2 : w // 2 + qr_px // 2] = img
        return canvas

    def test_fast_path_is_much_cheaper_than_full_cascade(self) -> None:
        """断言**相对提升**，不断言绝对耗时。

        绝对耗时完全取决于机器：同一张 3456x2168 空画面，
        zxing 0.6x 在 macOS 上约 139ms，在某台同时跑着游戏的 Windows 上约 1700ms
        （整机慢约 12 倍）。把绝对毫秒写进断言，等于把机器性能当成契约 ——
        真机上一跑就假失败。这个优化真正承诺的是"快路径比完整级联快得多"。
        """
        import time

        from endfield_scanner.core.qrdecode import QRDecoder

        frame = self._frame(qr_px=None)

        def measure(fast: bool) -> float:
            decoder = QRDecoder(huge_frames_fast_path=fast)
            t = time.perf_counter()
            decoder.decode(frame)
            return (time.perf_counter() - t) * 1000

        fast_ms = measure(True)
        full_ms = measure(False)

        # 实测 macOS 8x、Windows 8.5x，取 2x 作为保守下限
        assert fast_ms * 2 < full_ms, (
            f"快路径没有明显更快：快={fast_ms:.0f}ms 完整={full_ms:.0f}ms"
        )

    def test_fast_path_still_finds_realistic_codes(self) -> None:
        """快路径不能牺牲现实尺寸的码 —— 实测 100px 以上都该认得。"""
        from endfield_scanner.core.qrdecode import QRDecoder

        for qr_px in (300, 150, 100):
            decoder = QRDecoder(huge_frames_fast_path=True)
            result = decoder.decode(self._frame(qr_px=qr_px))
            assert result is not None, f"快路径漏掉了 {qr_px}px 的码"
            assert result.scan_id is not None

    def test_full_cascade_finds_very_small_code(self) -> None:
        """完整级联能认出快路径漏掉的极小码（60px），这是它存在的理由。"""
        from endfield_scanner.core.qrdecode import QRDecoder

        frame = self._frame(qr_px=60)
        assert QRDecoder(huge_frames_fast_path=True).decode(frame) is None, (
            "前提变了：快路径现在也能认出 60px，那这个开关就没必要了"
        )
        full = QRDecoder(huge_frames_fast_path=False)
        assert full.decode(frame) is not None, "完整级联应当能认出 60px 的码"

    def test_normal_sized_frames_unaffected(self) -> None:
        """普通分辨率（1080p 及以下）不该受这个开关影响。"""
        from endfield_scanner.core.qrdecode import QRDecoder

        frame = self._frame(qr_px=200, size=(1080, 1920))
        a = QRDecoder(huge_frames_fast_path=True).decode(frame)
        b = QRDecoder(huge_frames_fast_path=False).decode(frame)
        assert a is not None and b is not None
        assert a.scan_id == b.scan_id


# --------------------------------------------------------------------------- #
# 打包脚本
# --------------------------------------------------------------------------- #


class TestPackagingBuildScript:
    """打包脚本里可确定性验证的部分。

    完整的打包过程需要 PyInstaller 且耗时约 30 秒，不适合放进单测；
    这里只锁住「产物命名」和「必须排除的模块」这类容易改错的地方。
    """

    def _module(self):
        import importlib.util
        import sys
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "packaging" / "build.py"
        spec = importlib.util.spec_from_file_location("_pkg_build", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_pkg_build"] = mod
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod

    def test_release_name_has_version_platform_and_arch(self) -> None:
        """外层目录 / zip 的名字要带版本+平台，便于区分不同版本的下发。"""
        name = self._module()._release_name()
        assert name.startswith("endfield-scanner-v")
        parts = name.split("-")
        # 例如 endfield-scanner-v0.1.0-macos-arm64
        assert parts[2].startswith("v"), name
        assert parts[3] in ("macos", "windows", "linux"), name
        assert parts[4] in ("arm64", "x64"), name

    def test_inner_executable_keeps_a_plain_name(self) -> None:
        """**可执行文件本体必须是朴素名**，不带版本与平台。

        这样用户升级后，自己的脚本 / 快捷方式 / 文档里的路径都不用改。
        曾经把两者混用一个名字，导致可执行文件也变成了
        `endfield-scanner-v0.1.0-macos-arm64`。
        """
        from pathlib import Path

        mod = self._module()
        src = (
            Path(__file__).resolve().parent.parent / "packaging" / "build.py"
        ).read_text("utf-8")
        # PyInstaller 那一步必须用朴素名
        assert '"--name", exe_name' in src
        assert "exe_name = args.name or NAME" in src
        assert mod.NAME == "endfield-scanner"

    def test_zip_is_named_like_the_release_directory(self) -> None:
        """zip 名与目录名一致，解压后不会多一层奇怪的前缀。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent / "packaging" / "build.py"
        ).read_text("utf-8")
        assert "str(dist / release_name), \"zip\", root_dir=dist, base_dir=release_name" in src

    def test_version_is_normalized_to_single_v(self) -> None:
        """`0.1.0` / `v0.1.0` / `v v0.1.0` 都要归一成同一种写法。

        否则同一个版本会因为传参写法不同而产出两种文件名 —— CI 传的是 tag 名
        （带 v），本地默认读 pyproject（不带 v），两边必须一致。
        """
        mod = self._module()
        for raw in ("0.1.0", "v0.1.0", "  0.1.0  ", "vv0.1.0"):
            assert mod._normalize_version(raw) == "v0.1.0", raw
        assert mod._normalize_version("2.3.4") == "v2.3.4"

    def test_version_has_a_single_source(self) -> None:
        """版本号只能有一处来源：``src/endfield_scanner/__init__.py``。

        曾经 ``pyproject.toml`` 里也硬写了一份，改一处忘另一处就会出现
        "CLI 显示 0.1.0、产物名却是 v0.2.0"。现在 pyproject 用
        ``dynamic = ["version"]`` + ``[tool.hatch.version]`` 指回该文件。
        """
        from pathlib import Path

        mod = self._module()
        root = Path(__file__).resolve().parent.parent

        # build.py 读到的版本 = 运行时 __version__
        sys_path_added = False
        import sys

        if str(root / "src") not in sys.path:
            sys.path.insert(0, str(root / "src"))
            sys_path_added = True
        try:
            from endfield_scanner import __version__ as runtime_version

            assert mod._project_version() == runtime_version
        finally:
            if sys_path_added:
                sys.path.remove(str(root / "src"))

        assert mod._normalize_version(None) == f"v{runtime_version}"

    def test_pyproject_declares_dynamic_version(self) -> None:
        """pyproject 里不能再出现静态 version，否则又变成两份。"""
        from pathlib import Path

        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8")
        assert 'dynamic = ["version"]' in pyproject
        assert "[tool.hatch.version]" in pyproject
        # 顶格的 `version = ` 说明又写死了
        import re

        assert not re.search(r"^version\s*=", pyproject, re.MULTILINE), "pyproject 里又出现静态 version"

    def test_av_is_excluded(self) -> None:
        """`av` 必须排除：它自带一套 ffmpeg，会和 opencv 的 ffmpeg 撞 dylib。

        这是实打实踩过的坑 —— 两者同时存在时会刷一堆 objc 警告，严重时崩溃。
        """
        assert "av" in self._module().EXCLUDES

    def test_big_unused_modules_are_excluded(self) -> None:
        excludes = set(self._module().EXCLUDES)
        for heavy in ("tkinter", "matplotlib", "pandas", "scipy"):
            assert heavy in excludes, f"{heavy} 应被排除以减小体积"

    def test_entry_script_uses_absolute_import(self) -> None:
        """入口脚本必须用绝对导入。

        用 ``src/endfield_scanner/__main__.py`` 当入口会报
        ``attempted relative import with no known parent package`` ——
        那里面是 ``from .cli import main``，被当成独立脚本执行时没有父包。
        """
        from pathlib import Path

        entry = Path(__file__).resolve().parent.parent / "packaging" / "entry.py"
        # 只看代码行：docstring 里会把"错误的写法"当反例引用，
        # 整文件搜索会误命中，那是测试写法的问题而不是代码的问题。
        code_lines = [
            line.strip()
            for line in entry.read_text("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        assert "from endfield_scanner.cli import main" in code
        # 相对导入不能出现在真正的 import 语句里
        import_lines = [ln for ln in code_lines if ln.startswith(("import ", "from "))]
        assert not any(ln.startswith("from .") for ln in import_lines), import_lines

    def test_entry_script_is_runnable_as_script(self) -> None:
        """``python packaging/entry.py --version`` 必须能起来（不依赖父包）。"""
        import subprocess
        import sys
        from pathlib import Path

        entry = Path(__file__).resolve().parent.parent / "packaging" / "entry.py"
        r = subprocess.run(
            [sys.executable, str(entry), "--version"], capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0, r.stderr
        assert "endfield-scanner" in r.stdout


class TestBuildScriptZipsOnedir:
    """目录形式必须额外产出 zip。

    为什么需要：``--onedir`` 启动快（实测稳态 0.14s vs ``--onefile`` 的 5s），
    但分发的是一个目录。zip 就是它的分发形态，所以打包时必须一并产出。
    """

    def _source(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parent.parent / "packaging" / "build.py").read_text("utf-8")

    def test_default_is_onedir_not_onefile(self) -> None:
        src = self._source()
        # 默认分支应当是 onedir：只有显式 --onefile 才走 onefile
        assert '"--onefile" if args.onefile else "--onedir"' in src

    def test_creates_zip_for_onedir(self) -> None:
        src = self._source()
        assert "make_archive" in src, "缺少打 zip 的步骤"
        # 用标准库而不调 zip/Compress-Archive：跨平台行为一致
        assert "shutil.make_archive" in src

    def test_zip_skipped_for_onefile(self) -> None:
        """单文件模式本身就是单文件，不需要再套 zip。"""
        src = self._source()
        assert "not args.onefile and not args.no_zip" in src
