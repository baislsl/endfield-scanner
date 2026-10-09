"""命令行入口。

子命令
------
* ``login``    —— 获取 / 校验 / 清除 account_token（引导式浏览器登录）
* ``sniff``    —— 主命令：盯着屏幕或直播流，识别到扫码登录码就立刻抢占
* ``selftest`` —— 用自造二维码把整条链路跑通（不消耗任何真实码）
* ``doctor``   —— 环境自检：依赖、屏幕录制权限、网络连通性、token 状态
* ``status``   —— 快速查看当前 token 是否仍然可用
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Iterator

from . import __version__
from .core import api as api_mod
from .core import auth as auth_mod
from .core.api import ApiError, HypergryphAuth
from .core.frames import (
    FileSource,
    Frame,
    FrameSourceError,
    ScreenSource,
    StreamSource,
    parse_region,
)
from .core.qrdecode import QRDecoder
from .core.sniper import ConsoleReporter, Outcome, Sniper

# --------------------------------------------------------------------------- #
# 输出小工具
# --------------------------------------------------------------------------- #


def setup_console() -> None:
    """让在 Windows 控制台里输出中文与符号不会崩。

    Windows 控制台默认代码页是 **GBK（cp936）**，而 CLI 会输出
    ✔ ✘ ⚠ ★ 这类字符，直接 `print` 会抛::

        UnicodeEncodeError: 'gbk' codec can't encode character '\u2714'

    这个问题真的发生过：`doctor` 在 Windows 上一点就崩。
    修法是双管齐下：

    * 把控制台代码页切到 65001（UTF-8）—— 只对真实控制台有效；
    * 把 stdout/stderr 重新包成 UTF-8，并用 ``errors="replace"`` 兜底，
      这样即使输出被重定向、或代码页切换失败，也不会抛异常。
    """
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _print_banner() -> None:
    print(f"endfield-scanner v{__version__} —— 终末地扫码登录抢码")
    print("提示：本工具只读屏幕画面，不读取游戏内存、不修改游戏文件。")
    print()


# --------------------------------------------------------------------------- #
# 帧源构造
# --------------------------------------------------------------------------- #


def resolve_screen_region(args: argparse.Namespace) -> tuple[int, int, int, int] | None:
    """定出截屏区域。返回 None 表示**扫全屏**。

    默认就是全屏。理由很直接：实测全屏首帧解码中位 40ms、最高 53ms
    （1280x720 真实直播内容），而裁到中间条带只省十几毫秒 ——
    这点差距不值得让用户去框选、去猜像素坐标。命令跑起来、切到游戏/直播页、
    剩下交给它就完了。

    ``--region`` 仍然保留，给想压榨延迟的人用。
    """
    explicit = parse_region(getattr(args, "region", None))
    if explicit is not None:
        return explicit
    return None


def build_frame_source(args: argparse.Namespace) -> Iterator[Frame]:
    """把 ``--input`` 参数变成帧源迭代器。"""
    spec: str = args.input
    kind, _, rest = spec.partition(":")

    if kind in ("screen", "display", ""):
        region = resolve_screen_region(args)
        source = ScreenSource(
            monitor=args.monitor, region=region, fps=args.fps, band=args.band
        )
        return source.frames()

    if kind in ("image", "img", "file", "video"):
        return FileSource(rest, fps=args.fps, loop=args.loop).frames()

    if kind == "stream":
        # 直播取流尚未实现（原因与恢复要点见 sources/__init__.py 的模块说明）。
        # 这里保留 `stream:<平台>:<房间号>` 的语法解析，将来补上 resolver 即可启用。
        platform, _, room = rest.partition(":")
        if platform not in ("bilibili", "bili", "douyin", "dy"):
            raise SystemExit(
                f"未知直播平台 {platform!r}。支持的写法："
                f"stream:bilibili:<房间号> / stream:douyin:<web_rid>"
                f"（注意：直播取流目前未启用，请用 --input screen）"
            )
        if not room:
            raise SystemExit("缺少直播间号。写法：stream:bilibili:<房间号>")

        from .sources import resolve_platform  # noqa: PLC0415

        try:
            resolver = resolve_platform(platform)
        except NotImplementedError as exc:
            raise SystemExit(f"✘ {exc}") from None

        source = StreamSource(
            resolver.resolve,
            on_status=lambda msg: print(f"[stream] {msg}", flush=True),
        )
        return source.frames()

    raise SystemExit(f"无法识别的 --input: {spec!r}")


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #


def cmd_login(args: argparse.Namespace) -> int:
    _print_banner()
    if getattr(args, "use_env_proxy", False):
        api_mod.USE_ENV_PROXY = True
        print("注意：已启用环境变量代理，抢码延迟可能变高。")

    if args.logout:
        removed = auth_mod.delete_token()
        print("已清除保存的 account_token。" if removed else "没有已保存的 account_token。")
        return 0

    from .core import qrlogin  # noqa: PLC0415

    try:
        if args.paste:
            token = auth_mod.prompt_for_token()
        elif args.token:
            token = args.token
        elif args.browser:
            # 实验路径：打开网页旁听网络请求抓 token。
            # ⚠️ 这条路径未能验证 —— account_token 主要落在森空岛 App 侧，
            # 网页上未必会出现。优先用扫码（默认）或 --paste。
            print("⚠ 这是实验路径，未必能抓到 token。推荐用默认的扫码方式。")
            print()
            token = auth_mod.guided_browser_login(
                login_url=args.login_url, timeout_s=args.login_timeout, headless=False
            )
        else:
            token = qrlogin.login_by_qr(
                timeout_s=args.login_timeout,
                open_browser=not args.no_browser,
                verbose=args.verbose,
            )
    except auth_mod.TokenNotFound as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 2
    except qrlogin.QrLoginError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130

    print()
    print("正在向服务端校验 token…")
    try:
        info = asyncio.run(auth_mod.validate_token(token))
    except ApiError as exc:
        print(f"✘ 校验失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"✘ 校验出错：{exc}", file=sys.stderr)
        return 2

    print(f"✔ token 可用：{auth_mod.describe_account(info)}")
    where = auth_mod.save_token(token, allow_plaintext=args.allow_plaintext_token)
    print(f"✔ 已保存到：{where}")
    print()
    print("下一步：endfield-scanner sniff        # 打开游戏登录页，让二维码显示在屏幕上")
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def cmd_status(args: argparse.Namespace) -> int:
    _print_banner()
    try:
        source, info = asyncio.run(auth_mod.load_and_validate())
    except auth_mod.TokenNotFound as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    except auth_mod.TokenInvalid as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 2

    print(f"token 来源：{source.origin}")
    print(f"token 指纹：{source.masked()}")
    print(f"账号信息：{auth_mod.describe_account(info)}")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    """打印已保存的 account_token。

    token 等价于账号，所以这里刻意做了几件事：
    * 默认只打指纹，要看全量必须显式加 --show；
    * 输出到非终端（被重定向/管道）时拒绝打印全量，避免不小心写进日志文件。
    """
    _print_banner()
    source = auth_mod.load_token()
    if source is None:
        print(
            f"没有找到 account_token。请先运行 `endfield-scanner login`，"
            f"或设置环境变量 {auth_mod.ENV_VAR}。",
            file=sys.stderr,
        )
        return 1

    print(f"来源：{source.origin}")
    print(f"长度：{len(source.value)}")
    print(f"指纹：{source.masked()}")

    if not args.show:
        print()
        print("要查看完整 token，加 --show。")
        print("⚠ token 等价于你的账号，别粘贴到聊天/截图/日志里。")
        return 0

    if not sys.stdout.isatty() and not args.force:
        print(
            "\n✘ 标准输出不是终端（可能正在重定向到文件），已拒绝打印完整 token。\n"
            "  确实要打印请加 --force。",
            file=sys.stderr,
        )
        return 2

    print()
    print("完整 token（谨慎处理）：")
    print(source.value)
    return 0


# --------------------------------------------------------------------------- #
# sniff
# --------------------------------------------------------------------------- #


async def _run_sniff(args: argparse.Namespace) -> int:
    if getattr(args, "use_env_proxy", False):
        api_mod.USE_ENV_PROXY = True

    if args.skip_token_check:
        source = auth_mod.load_token()
        if source is None:
            print(
                f"✘ --skip-token-check 也需要一个 token 占位。"
                f"请设置 {auth_mod.ENV_VAR} 或先运行 `endfield-scanner login`。",
                file=sys.stderr,
            )
            return 1
        reporter0 = ConsoleReporter(color=_supports_color(), verbose=args.verbose)
        reporter0.warn("已跳过 token 校验（仅用于链路自测，真跑抢码不要加这个参数）")
        info: dict[str, Any] | None = None
    else:
        try:
            source, info = await auth_mod.load_and_validate()
        except (auth_mod.TokenNotFound, auth_mod.TokenInvalid) as exc:
            print(f"✘ {exc}", file=sys.stderr)
            return 1

    reporter = ConsoleReporter(color=_supports_color(), verbose=args.verbose)
    account_desc = (
        auth_mod.describe_account(info) if info is not None else "未校验（--skip-token-check）"
    )
    reporter.info(f"账号：{account_desc}（token 来自 {source.origin}）")
    reporter.info(f"帧源：{args.input}  轮询间隔：{args.poll_interval}ms")
    reporter.info("开始监视…（Ctrl-C 退出）")

    decoder = QRDecoder(
        enable_roi_reuse=not args.no_roi_reuse,
        enable_cv2_fallback=args.cv2_fallback,
        huge_frames_fast_path=not args.full_cascade,
        wechat_models_dir=args.wechat_model,
    )
    sniper = Sniper(
        account_token=source.value,
        decoder=decoder,
        reporter=reporter,
        poll_interval_ms=args.poll_interval,
        max_scan_id_age_s=args.scan_ttl,
        keep_going=args.keep_going,
        confirm_grace_s=args.confirm_grace,
        heartbeat_s=args.heartbeat,
        auto_scan=not args.no_auto_scan,
        auto_confirm=not args.no_confirm,
        confirm_delay_ms=args.confirm_delay,
        confirm_delay_max_ms=args.confirm_delay_max,
        redeem=args.redeem,
        save_capture=args.save_capture,
        verbose_timing=args.verbose,
    )

    frames = build_frame_source(args)
    try:
        result = await sniper.run(frames, timeout_s=args.timeout)
    except KeyboardInterrupt:
        reporter.info("已手动停止。")
        return 130

    print()
    print("—" * 60)
    print(f"解码统计：{decoder.stats.summary()}")
    print("—" * 60)

    if result.ok:
        reporter.success(f"抢码成功，延迟 {result.latency_ms:.0f}ms")
        reporter.success("登录成功，客户端应该已经登上你的账号了。")
        if args.dump_result:
            # token 属于敏感信息，默认不打印
            safe = {k: v for k, v in result.payload.items() if k != "token"}
            print(json.dumps(safe, ensure_ascii=False, indent=2))
        return 0

    messages = {
        Outcome.LOST: "被其他观众抢先了。可以调小 --poll-interval 再试。",
        Outcome.EXPIRED: "二维码已失效且未等到扫码。",
        Outcome.TIMEOUT: "超时未出现可用的扫码二维码。",
        Outcome.ERROR: "发生错误。",
    }
    reporter.warn(f"未抢到：{result.outcome.value} —— {result.detail}")
    reporter.info(messages.get(result.outcome, ""))
    return 3


def cmd_sniff(args: argparse.Namespace) -> int:
    _print_banner()
    if getattr(args, "verbose", False):
        # -v 打开全量 HTTP 日志：把发往服务端的每个请求都打出来
        from .core.httplog import ENABLED as _HTTP_LOG  # noqa: PLC0415

        _HTTP_LOG.value = True
    try:
        return asyncio.run(_run_sniff(args))
    except KeyboardInterrupt:
        print("\n已手动停止。")
        return 130
    except FrameSourceError as exc:
        print(f"✘ 帧源错误：{exc}", file=sys.stderr)
        return 4


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #


def cmd_selftest(args: argparse.Namespace) -> int:
    """用自造二维码验证「解码 → 抽参 → 轮询」链路，不需要真实码。"""
    _print_banner()
    try:
        import qrcode  # noqa: PLC0415
    except Exception:
        print("需要 qrcode 依赖：pip install 'endfield-scanner[dev]'", file=sys.stderr)
        return 1

    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    from .core.api import extract_scan_id

    scan_id = args.scan_id
    payload = f"hypergryph://scan_login?scanId={scan_id}"
    print(f"构造测试二维码：{payload}")

    # 生成一个「像直播里那样小」的二维码贴到深色背景上，检验多策略级联
    qr = qrcode.QRCode(border=2, box_size=4)
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = np.array(qr.make_image(fill_color="black", back_color="white").convert("RGB"))
    qr_bgr = cv2.cvtColor(qr_img, cv2.COLOR_RGB2BGR)

    scale = args.scale
    if scale != 1.0:
        qr_bgr = cv2.resize(
            qr_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        )

    canvas = np.full((900, 1600, 3), 32, dtype=np.uint8)
    height, width = qr_bgr.shape[:2]
    top = (canvas.shape[0] - height) // 2
    left = (canvas.shape[1] - width) // 2
    canvas[top : top + height, left : left + width] = qr_bgr

    # 模拟直播编码损失
    if args.blur > 0:
        canvas = cv2.GaussianBlur(canvas, (args.blur | 1, args.blur | 1), 0)
    if args.jpeg_quality < 100:
        ok, encoded = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), args.jpeg_quality])
        if ok:
            canvas = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if args.noise > 0:
        noise = np.random.default_rng(0).normal(0, args.noise, canvas.shape)
        canvas = np.clip(canvas.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    if args.save:
        cv2.imwrite(args.save, canvas)
        print(f"测试帧已保存：{args.save}")

    decoder = QRDecoder()
    result = decoder.decode(canvas)
    print(f"解码统计：{decoder.stats.summary()}")

    if result is None:
        print("✘ 解码失败 —— 多策略级联还需要加强。", file=sys.stderr)
        return 1

    print(f"✔ 解码成功：策略={result.method} 耗时={result.elapsed_ms:.1f}ms")
    print(f"  payload = {result.payload}")

    got = extract_scan_id(result.payload)
    if got != scan_id:
        print(f"✘ scanId 抽取有误：期望 {scan_id}，得到 {got}", file=sys.stderr)
        return 1
    print(f"✔ scanId 抽取正确：{got}")

    if args.offline:
        print()
        print("（--offline：跳过网络部分）")
        return 0

    # 再验证一下协议客户端确实能走通 scan_status（用这个假 scanId，预期「未扫码」或失效）
    async def probe() -> int:
        async with HypergryphAuth() as auth:
            try:
                state = await auth.get_scan_state(scan_id)
            except ApiError as exc:
                print(f"· scan_status 返回业务错误（假 scanId 的正常表现）：{exc}")
                return 0
            print(f"✔ scan_status 可达：status={state.raw_status} msg={state.raw_message!r}")
            return 0

    return asyncio.run(probe())


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
    _print_banner()
    ok = True

    def check(label: str, fn: Any) -> None:
        nonlocal ok
        try:
            detail = fn()
        except Exception as exc:
            ok = False
            print(f"  ✘ {label}: {exc}")
        else:
            print(f"  ✔ {label}{f': {detail}' if detail else ''}")

    print("依赖：")
    check("Python", lambda: sys.version.split()[0])

    def _decoder() -> str:
        # 用真实解码器自报家门，顺带跑一次自造二维码，确认它真的能干活
        import cv2  # noqa: PLC0415
        import qrcode  # noqa: PLC0415

        from .core.qrdecode import QRDecoder  # noqa: PLC0415

        decoder = QRDecoder()
        try:
            import numpy as np  # noqa: PLC0415

            payload = "hypergryph://scan_login?scanId=" + "0" * 32
            qr = qrcode.QRCode(border=2, box_size=8)
            qr.add_data(payload)
            qr.make(fit=True)
            image = np.array(qr.make_image(fill_color="black", back_color="white").convert("RGB"))
            frame = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            got = decoder.decode(frame)
            selfcheck = "自检通过" if (got and got.scan_id) else "自检失败"
        except Exception as exc:  # pragma: no cover
            selfcheck = f"自检异常: {exc}"
        has_wechat = "有" if hasattr(cv2, "wechat_qrcode") else "无"
        return f"{decoder.backend}，{selfcheck}（cv2 {cv2.__version__}，微信模型 {has_wechat}）"

    check("QR 解码器", _decoder)
    check("numpy", lambda: __import__("numpy").__version__)
    check("httpx", lambda: __import__("httpx").__version__)

    # PyAV 现在是可选依赖（只有直播取流需要，而直播取流已搁置），
    # 所以缺失只提示、不算失败。
    def _av() -> str:
        try:
            import av  # noqa: PLC0415
        except ImportError:
            return "未安装（可选，仅直播取流需要；直播取流已搁置）"
        return f"PyAV {av.__version__}（直播流解码）"

    check("av", _av)
    check("mss", lambda: __import__("mss").__version__)

    def _keyring() -> str:
        import keyring  # noqa: PLC0415

        return keyring.get_keyring().__class__.__name__

    check("keyring", _keyring)

    print()
    if sys.platform == "darwin":
        print("屏幕录制（macOS 需要授权）：")
    elif sys.platform == "win32":
        print("截屏能力（Windows 需在已登录的桌面会话里运行）：")
    else:
        print("截屏能力：")

    def _screen() -> str:
        import numpy as np  # noqa: PLC0415

        from .core.frames import _screen_capture_error, new_mss_instance  # noqa: PLC0415

        try:
            with new_mss_instance() as sct:
                shot = sct.grab(sct.monitors[1])
                image = np.asarray(shot)
        except Exception as exc:
            # 复用与帧源相同的平台化提示，别把 mss 的原始消息直接丢给用户
            raise _screen_capture_error(exc) from exc
        if image.size == 0:
            raise RuntimeError("截到空画面")
        return f"可截屏 {image.shape[1]}x{image.shape[0]}"

    check("截屏", _screen)
    if sys.platform == "darwin":
        print("    （若此项失败：系统设置 → 隐私与安全性 → 屏幕录制，勾选你的终端并重启）")

    print()
    print("网络连通性：")
    proxy_mode = "沿用环境变量代理" if api_mod.USE_ENV_PROXY else "直连（忽略环境变量代理）"
    print(f"  · 代理策略：{proxy_mode}")

    def _api() -> str:
        import httpx  # noqa: PLC0415

        with httpx.Client(timeout=10.0, trust_env=api_mod.USE_ENV_PROXY) as client:
            r = client.post(
                api_mod.GEN_SCAN_LOGIN,
                json={"appCode": api_mod.SKLAND_APP_CODE},
            )
        payload = r.json()
        if payload.get("status") != 0:
            raise RuntimeError(f"status={payload.get('status')} msg={payload.get('msg')}")
        scan_url = payload["data"]["scanUrl"]
        return f"gen_scan/login 正常，样例 {scan_url[:48]}…"

    check("鹰角接口", _api)

    print()
    print("account_token：")
    source = auth_mod.load_token()
    if source is None:
        print("  · 尚未配置。运行 `endfield-scanner login`（扫码登录）或 `login --paste`。")
    else:
        print(f"  · 来源：{source.origin}")
        print(f"  · 指纹：{source.masked()}")

        async def _validate() -> str:
            info = await auth_mod.validate_token(source.value)
            return auth_mod.describe_account(info)

        check("服务端校验", lambda: asyncio.run(_validate()))

    print()
    print("结论：" + ("一切就绪 ✔" if ok else "存在问题，请先修复上面标 ✘ 的项。"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="endfield-scanner",
        description="明日方舟：终末地 扫码登录抢码工具（从直播流/屏幕抢扫码登录码）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  endfield-scanner login            # 扫码登录，拿到 account_token\n"
            "  endfield-scanner sniff            # 全屏扫描游戏登录二维码（主力命令）\n"
            "  endfield-scanner sniff -v         # 同上，带逐次状态输出（排障用）\n"
            "  endfield-scanner token --show     # 查看已保存的 token\n"
            "  endfield-scanner selftest         # 自造二维码验证解码链路\n"
            "  endfield-scanner doctor           # 环境自检\n"
            "\n"
            "注意：工具只能抢「扫码之后」的提交，扫码本身仍需人来完成。\n"
            "      直播取流尚未实现，原因见 sources/__init__.py 的模块说明。\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- login ---
    p_login = sub.add_parser("login", help="获取/校验/清除 account_token")
    p_login.add_argument(
        "--paste", action="store_true", help="手动粘贴 token（已经有了 token 就用这个，最快）"
    )
    p_login.add_argument("--token", help="直接提供 token（会写入 shell 历史，慎用）")
    p_login.add_argument("--logout", action="store_true", help="清除已保存的 token")
    p_login.add_argument(
        "--browser",
        action="store_true",
        help="[实验] 打开网页旁听请求抓 token；未能验证有效性，优先用默认扫码",
    )
    p_login.add_argument(
        "--no-browser", action="store_true", help="扫码模式下不自动打开浏览器"
    )
    p_login.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="逐次打印扫码状态（排障用：能看出服务端有没有收到扫码）",
    )
    p_login.add_argument(
        "--login-url", default="https://www.skland.com/", help="引导登录使用的页面"
    )
    p_login.add_argument(
        "--login-timeout", type=float, default=300.0, help="等待手动登录的秒数"
    )
    p_login.add_argument(
        "--use-env-proxy",
        action="store_true",
        help="沿用环境变量里的代理（默认直连，抢占延迟更低）",
    )
    p_login.add_argument(
        "--allow-plaintext-token",
        action="store_true",
        help="系统钥匙串不可用时允许明文落盘（权限会设为 0600）",
    )
    p_login.set_defaults(func=cmd_login)

    # --- sniff ---
    p_sniff = sub.add_parser("sniff", help="监视屏幕并抢码（扫到二维码就自动登录）")
    p_sniff.add_argument(
        "--input",
        default="screen",
        help=(
            "帧源。可用：screen（默认，截屏）；image:<路径>；video:<路径>。"
            "注：stream:<平台>:<房间号> 的语法还在，但直播取流已搁置未启用"
            "（直播取流尚未实现，见 sources/__init__.py）"
        ),
    )
    p_sniff.add_argument(
        "--region",
        help="可选：只扫屏幕的某个矩形 left,top,width,height（默认扫全屏，一般用不上）",
    )
    p_sniff.add_argument("--monitor", type=int, default=1, help="显示器编号，1 为主屏")
    p_sniff.add_argument(
        "--band",
        type=float,
        default=1.0,
        help="扫描屏幕竖向中间的比例，1.0 = 全屏（默认）",
    )
    p_sniff.add_argument("--fps", type=float, default=20.0, help="采集帧率上限")
    p_sniff.add_argument(
        "--poll-interval",
        type=int,
        default=100,
        help="scan_status 轮询间隔（毫秒）。这是抢码胜负手，默认 100ms",
    )
    p_sniff.add_argument(
        "--scan-ttl", type=float, default=180.0, help="单个 scanId 最长盯多久（秒）"
    )
    p_sniff.add_argument(
        "--confirm-grace",
        type=float,
        default=60.0,
        help="看到「已扫码待确认」后额外多等的秒数（给用户点手机授权弹窗的时间）",
    )
    p_sniff.add_argument(
        "--no-auto-scan",
        action="store_true",
        help="关掉「代扫码」：默认工具会用你的 token 自动完成扫码+确认，无需人掏手机",
    )
    p_sniff.add_argument(
        "--save-capture",
        nargs="?",
        const="./captures",
        default=None,
        metavar="目录",
        help="识别到二维码时把那一帧存成图片（默认目录 ./captures）。"
        "排障用：能直接看到当时屏幕上是什么",
    )
    p_sniff.add_argument(
        "--confirm-delay",
        type=int,
        default=2000,
        help="代确认前随机等待的下限毫秒数（默认 2000）",
    )
    p_sniff.add_argument(
        "--confirm-delay-max",
        type=int,
        default=5000,
        help="代确认前随机等待的上限毫秒数（默认 5000）。与下限组成区间，随机取值",
    )
    p_sniff.add_argument(
        "--redeem",
        action="store_true",
        help="抢着由我们消费 scanCode 换 token（默认关闭：这一跳应交给游戏客户端，"
        "抢着消费会让客户端显示二维码已过期）",
    )
    p_sniff.add_argument(
        "--no-confirm",
        action="store_true",
        help="代扫码后不代确认，停在「已扫码待确认」（排障：看客户端有没有反应）",
    )
    p_sniff.add_argument(
        "--heartbeat",
        type=float,
        default=10.0,
        help="每隔多少秒汇报一次「服务端当前怎么看这个码」（默认 10s）",
    )
    p_sniff.add_argument("--timeout", type=float, default=None, help="整体超时（秒），默认不限")
    p_sniff.add_argument("--keep-going", action="store_true", help="抢到后继续盯着下一个码")
    p_sniff.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="文件帧源循环播放（--no-loop 只跑一遍）",
    )
    p_sniff.add_argument(
        "--wechat-model",
        nargs="?",
        const="models/wechat_qrcode",
        default=None,
        metavar="目录",
        help="可选：启用 OpenCV 微信二维码模型（对极小/模糊的码更强，但慢很多，"
        "且只有 Windows 能用）。需自备 4 个模型文件（从 opencv_3rdparty 的 "
        "wechat_qrcode 分支获取），仓库不附带",
    )
    p_sniff.add_argument(
        "--full-cascade",
        action="store_true",
        help="超大画面（高 DPI 屏）也跑完整解码级联：识别下限从约 80px 降到 60px，"
        "但无码帧从约 142ms 涨到 500~800ms。默认关闭（走快路径）",
    )
    p_sniff.add_argument(
        "--no-roi-reuse", action="store_true", help="关闭跨帧 RoI 复用（会明显变慢，仅排障用）"
    )
    p_sniff.add_argument(
        "--cv2-fallback",
        action="store_true",
        help="额外启用 cv2 兜底解码（默认关闭：实测每帧多花 ~650ms，且小码识别率不如 zxing）",
    )
    p_sniff.add_argument(
        "--skip-token-check",
        action="store_true",
        help="跳过启动时的 token 校验（仅用于链路自测；实战会让你在抢码那一刻才发现 token 失效）",
    )
    p_sniff.add_argument(
        "--use-env-proxy",
        action="store_true",
        help="沿用环境变量里的代理（默认直连，抢占延迟更低）",
    )
    p_sniff.add_argument("-v", "--verbose", action="store_true", help="打印轮询细节")
    p_sniff.add_argument("--dump-result", action="store_true", help="成功后打印返回结构（自动剔除 token）")
    p_sniff.set_defaults(func=cmd_sniff)

    # --- selftest ---
    p_self = sub.add_parser("selftest", help="用自造二维码验证整条链路（不消耗真实码）")
    p_self.add_argument(
        "--scan-id",
        default="d0549b45e5002c2cf8e5c302deb1dca6",
        help="用于构造测试二维码的 scanId（纯 32 位 hex）",
    )
    p_self.add_argument("--scale", type=float, default=0.6, help="把二维码缩到多小以模拟直播画面")
    p_self.add_argument("--blur", type=int, default=0, help="高斯模糊核大小（奇数），模拟码率损失")
    p_self.add_argument("--jpeg-quality", type=int, default=100, help="JPEG 质量，模拟画面压缩")
    p_self.add_argument("--noise", type=float, default=0.0, help="高斯噪声标准差")
    p_self.add_argument("--save", help="把构造出的测试帧写到该路径")
    p_self.add_argument("--offline", action="store_true", help="只测解码与抽参，不碰网络")
    p_self.set_defaults(func=cmd_selftest)

    # --- doctor ---
    p_doc = sub.add_parser("doctor", help="环境自检")
    p_doc.set_defaults(func=cmd_doctor)

    # --- status ---
    p_status = sub.add_parser("status", help="查看当前 token 是否可用")
    p_status.set_defaults(func=cmd_status)

    # --- token ---
    p_token = sub.add_parser("token", help="查看已保存的 account_token")
    p_token.add_argument("--show", action="store_true", help="打印完整 token（默认只显示指纹）")
    p_token.add_argument(
        "--force", action="store_true", help="即使输出被重定向也打印完整 token"
    )
    p_token.set_defaults(func=cmd_token)

    return parser


def main(argv: list[str] | None = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
