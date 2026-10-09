"""扫码方式登录：出码给用户扫，换回 account_token。

为什么把这条做成默认
--------------------
原来默认的 ``guided_browser_login``（打开网页、旁听网络请求抓 token）是**猜测**：
我没有证据表明 ``account_token`` 会出现在网页的任何一个响应体里。
而下面这条链路和抢码用的是**同一组接口**，每个环节都实测过：

    ① POST /general/v1/gen_scan/login          → scanId + scanUrl（二维码内容）
    ② 你用手机森空岛扫这个码（App 里就是干这个的）
    ③ GET  /general/v1/scan_status?scanId=     → 「未扫码」翻转为 scanCode
    ④ POST /user/auth/v1/token_by_scan_code    → account_token

② 必须由人来完成（就是要用你已登录的森空岛 App），这正是我们绕开
极验验证码的办法 —— 不做人机对抗，而是复用你已经登好的 App。

出码方式给两种，按终端能力自动选：
  * 终端足够大 → 直接打 Unicode 半块二维码，拿手机扫屏幕
  * 否则 → 起一个本地 HTTP 服务显示放大的二维码，并打开浏览器

同时都会落一张 PNG 到磁盘兜底。
"""

from __future__ import annotations

import contextlib
import http.server
import socket
import socketserver
import threading
import time
import webbrowser
from pathlib import Path

import httpx

from .api import (
    SCAN_STATUS_CONFIRMED,
    SCAN_STATUS_NOT_SCANNED,
    SCAN_STATUS_PENDING_CONFIRM,
)

SKLAND_APP_CODE = "4ca99fa6b56cc2ba"
GEN_SCAN_LOGIN = "https://as.hypergryph.com/general/v1/gen_scan/login"
SCAN_STATUS = "https://as.hypergryph.com/general/v1/scan_status"
TOKEN_BY_SCAN_CODE = "https://as.hypergryph.com/user/auth/v1/token_by_scan_code"

#: scanId 失效得很快（实测 ≤30s），所以默认只等这么久一轮
DEFAULT_TIMEOUT_S = 300.0


class QrLoginError(RuntimeError):
    """扫码登录失败。"""


# --------------------------------------------------------------------------- #
# 二维码渲染
# --------------------------------------------------------------------------- #


def render_unicode_qr(text: str) -> tuple[str, int]:
    """把内容渲染成终端可扫的 Unicode 二维码。返回 (文本, 列数)。

    用半块字符（▀▄█）把两行像素压成一行，这样终端里二维码是"方"的 ——
    终端字符通常高约为宽的两倍，不压缩会被拉长到扫不出来。
    """
    try:
        import qrcode  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        raise QrLoginError("需要 qrcode 依赖：pip install 'endfield-scanner[dev]'") from exc

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    width = len(matrix[0])
    # 补一行，保证行数为偶数
    if len(matrix) % 2:
        matrix = list(matrix) + [[False] * width]

    lines: list[str] = []
    for y in range(0, len(matrix), 2):
        row_top, row_bottom = matrix[y], matrix[y + 1]
        chars = []
        for top, bottom in zip(row_top, row_bottom):
            if top and bottom:
                chars.append("█")
            elif top and not bottom:
                chars.append("▀")
            elif bottom and not top:
                chars.append("▄")
            else:
                chars.append(" ")
        lines.append("".join(chars))
    return "\n".join(lines), width


def render_png(text: str, path: Path, *, box_size: int = 10) -> Path:
    """落一张 PNG 兜底（终端渲染可能因字号/缩放而扫不出来）。"""
    try:
        import qrcode  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        raise QrLoginError("需要 qrcode 依赖") from exc

    qr = qrcode.QRCode(border=3, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(path))
    return path


# --------------------------------------------------------------------------- #
# 本地出码页
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _QrPageHandler(http.server.BaseHTTPRequestHandler):
    """只服务一个页面：一张尽可能大的二维码。"""

    html: bytes = b""
    server_version = "endfield-scanner"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # 静音
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = type(self).html
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_qr_page(scan_url: str) -> tuple[str, socketserver.TCPServer]:
    """起一个本地页面展示二维码，返回 (url, server)。用完记得 shutdown。"""
    try:
        import qrcode  # noqa: PLC0415
        import qrcode.image.svg  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        raise QrLoginError("需要 qrcode 依赖") from exc

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(scan_url)
    qr.make(fit=True)
    svg = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).to_string().decode("utf-8")

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>扫码登录 · endfield-scanner</title>
<style>
  body {{ margin:0; background:#111; color:#eee;
         font-family:-apple-system,"PingFang SC",sans-serif;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; min-height:100vh; gap:18px; }}
  .card {{ background:#fff; padding:24px; border-radius:16px; }}
  svg {{ width:min(62vw,62vh); height:min(62vw,62vh); display:block; }}
  h1 {{ font-size:22px; font-weight:600; margin:0; }}
  p {{ margin:0; color:#aaa; font-size:15px; }}
  code {{ background:#222; padding:2px 8px; border-radius:6px; font-size:13px; }}
</style></head>
<body>
  <h1>用「森空岛」App 扫这个码</h1>
  <div class="card">{svg}</div>
  <p>扫完页面上会出现提示，本窗口可以关掉</p>
  <p><code>{scan_url}</code></p>
</body></html>"""

    handler = type("Handler", (_QrPageHandler,), {"html": html.encode("utf-8")})
    port = _free_port()
    server = socketserver.TCPServer(("127.0.0.1", port), handler)
    server.allow_reuse_address = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}/", server


@contextlib.contextmanager
def _client_ctx(client: httpx.Client | None):
    """用外部客户端就复用，否则自己建一个（并且负责关掉）。"""
    if client is not None:
        yield client
        return
    with httpx.Client(timeout=httpx.Timeout(10.0), trust_env=False) as owned:
        yield owned


def _terminal_is_big_enough(columns: int, qr_columns: int) -> bool:
    try:
        import shutil  # noqa: PLC0415

        width, height = shutil.get_terminal_size((80, 24))
    except Exception:
        return False
    # 留出两侧留白，并保证高度也放得下（半块压缩后行数约为列数的一半）
    return width - 4 >= qr_columns and height - 4 >= qr_columns // 2


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def login_by_qr(
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    open_browser: bool = True,
    quiet: bool = False,
    verbose: bool = False,
    endpoints: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> str:
    """出码 → 等用户扫 → 换 account_token。返回 token。

    :param endpoints: 覆盖三个接口地址，仅用于测试（默认走真实的鹰角接口）。
    :param client: 复用一个外部 httpx 客户端，仅用于测试。
    """
    def say(message: str) -> None:
        """面向用户的正常输出（quiet 时静音）。"""
        if not quiet:
            print(message, flush=True)

    def debug(message: str) -> None:
        """排障输出。故意不受 quiet 影响 —— quiet 是给测试用的，
        不该把"服务端到底有没有收到扫码"这种关键信号一起压掉。"""
        if verbose:
            print(message, flush=True)

    urls = {
        "gen": GEN_SCAN_LOGIN,
        "status": SCAN_STATUS,
        "token": TOKEN_BY_SCAN_CODE,
    }
    if endpoints:
        urls.update(endpoints)

    with _client_ctx(client) as client:
        # ① 建码
        try:
            response = client.post(urls["gen"], json={"appCode": SKLAND_APP_CODE})
            payload = response.json()
        except Exception as exc:
            raise QrLoginError(f"无法连接鹰角接口：{exc}") from exc
        if payload.get("status") != 0:
            raise QrLoginError(f"建码失败：{payload.get('msg')}")
        data = payload["data"]
        scan_id = str(data["scanId"])
        scan_url = str(data["scanUrl"])

        # ② 出码：终端优先，同时落 PNG，必要时起本地页面
        qr_text, qr_columns = render_unicode_qr(scan_url)
        png_path = Path.cwd() / "login-qr.png"
        try:
            render_png(scan_url, png_path)
        except Exception:
            png_path = None  # type: ignore[assignment]

        server: socketserver.TCPServer | None = None
        page_url: str | None = None
        if not _terminal_is_big_enough(0, qr_columns):
            try:
                page_url, server = serve_qr_page(scan_url)
            except Exception:
                page_url = None

        say("")
        say("=" * 60)
        say("  用手机「森空岛」App 扫下面的二维码完成登录")
        say("  （App 里：个人中心右上角 → 扫一扫）")
        say("=" * 60)
        say("")
        if not quiet:
            print(qr_text, flush=True)
        say("")
        if page_url:
            say(f"  二维码太小扫不到？浏览器打开： {page_url}")
            if open_browser:
                try:
                    webbrowser.open(page_url)
                except Exception:
                    pass
        if png_path:
            say(f"  也可以打开图片扫： {png_path}")
        say("")
        say(f"  scanId = {scan_id}")
        say("  等待扫码…（Ctrl-C 取消）")
        say("")

        # ③ 轮询等扫
        #
        # 状态机（实测）：
        #   100 未扫码        → 继续等
        #   101 已扫码待确认   → 继续等（用户正在点 App 里的授权弹窗，**这一步最不能催**）
        #     0 已确认        → 拿到 scanCode，去换 token
        #   102 / 其它        → 失效，报错
        started = time.monotonic()
        scan_code: str | None = None
        scanned_at: float | None = None
        warned_pending = False
        last_status: int | None = None
        polls = 0
        try:
            while time.monotonic() - started < timeout_s:
                try:
                    state = client.get(urls["status"], params={"scanId": scan_id}).json()
                except Exception:
                    time.sleep(0.5)
                    continue

                polls += 1
                status = state.get("status")

                # 逐次状态上报：这是"服务端有没有收到扫码"的唯一可信来源。
                # 排障时别靠猜 —— 状态停在 100 就是压根没收到扫码。
                if status != last_status:
                    last_status = status
                    elapsed_now = time.monotonic() - started
                    debug(
                        f"  [状态] {status} {state.get('msg')!r} "
                        f"（{elapsed_now:.1f}s，第 {polls} 次轮询）"
                    )
                if status == SCAN_STATUS_CONFIRMED:
                    scan_code = (state.get("data") or {}).get("scanCode")
                    if scan_code:
                        break
                    # status 0 但没给 scanCode：当成还在处理，继续等
                elif status == SCAN_STATUS_PENDING_CONFIRM:
                    if scanned_at is None:
                        scanned_at = time.monotonic()
                        say("… 已扫码，等待你在手机上确认授权")
                    elif not warned_pending and time.monotonic() - scanned_at > 15:
                        warned_pending = True
                        say("… 还在等你点手机上的「确认」按钮")
                elif status != SCAN_STATUS_NOT_SCANNED:
                    raise QrLoginError(
                        f"二维码状态异常：{state.get('msg')} (status={status})。"
                        "二维码失效很快，重新运行一次即可。"
                    )
                time.sleep(0.5)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()

        if not scan_code:
            if last_status == SCAN_STATUS_PENDING_CONFIRM:
                hint = "你扫了码，但没在手机上点「确认」授权。"
            elif last_status == SCAN_STATUS_NOT_SCANNED:
                hint = (
                    "服务端始终停在「未扫码」——说明它**从没收到过扫码**。"
                    "请确认用手机【森空岛】App 的扫一扫（个人中心右上角）扫的是这个二维码。"
                )
            else:
                hint = f"二维码状态停在 {last_status}（{scan_id}）。"
            raise QrLoginError(
                f"等了 {timeout_s:.0f} 秒没等到扫码确认（共轮询 {polls} 次）。{hint}\n"
                "二维码失效很快，重新运行一次即可；也可以改用 login --paste 手动粘贴 token。"
            )

        elapsed = time.monotonic() - started
        say(f"✔ 已确认授权（用时 {elapsed:.1f}s），正在换取 token…")

        # ④ 换 token
        try:
            result = client.post(urls["token"], json={"scanCode": scan_code}).json()
        except Exception as exc:
            raise QrLoginError(f"换取 token 请求失败：{exc}") from exc
        if result.get("status") != 0:
            raise QrLoginError(f"换取 token 失败：{result.get('msg')}")
        token = (result.get("data") or {}).get("token")
        if not token:
            raise QrLoginError(f"接口没返回 token：{result}")
        return str(token)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "QrLoginError",
    "login_by_qr",
    "render_png",
    "render_unicode_qr",
    "serve_qr_page",
]
