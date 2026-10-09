"""account_token 的获取与安全存储。

为什么需要 account_token
------------------------
抢码的最后一跳 ``POST /user/auth/v1/token_by_scan_code`` 是**无鉴权**的（实测），
但它换出来的是「谁的账号」。所以你必须先证明自己是谁 —— 这就需要一个
鹰角通行证 ``account_token``，它相当于「用森空岛 App 扫码」这一步的身份。

实测结论：``token_by_scan_code`` 本身**不需要过极验验证码**。
整个链路里唯一需要人机校验的只有「登录（获取 account_token）」这一步，
而它一生只需要做一次。

三种获取方式（本模块都支持）
----------------------------
1. ``guided_browser_login()`` —— 引导式（推荐）。
   打开官方登录页，你手动过一次验证码；程序旁听网络请求，
   自动捕获 token，校验后写入系统 keyring。
2. ``import_token()`` —— 手动导入。你从别的工具/抓包里拿到 token 直接粘进来。
3. 环境变量 ``ENDFIELD_ACCOUNT_TOKEN`` —— 适合 CI 或临时覆盖。

token 的解析口径
----------------
国内：``as.hypergryph.com/user/auth/v1/token_by_phone_password`` 等接口
返回 ``data.token``，即 account_token。
海外：``as.gryphline.com`` 同结构（工具不内置海外链路，但 token 存储口径一致）。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .api import ApiError, HypergryphAuth
from .paths import config_dir

if TYPE_CHECKING:  # pragma: no cover
    pass

SERVICE_NAME = "endfield-scanner"
KEY_ACCOUNT = "hypergryph-account-token"
ENV_VAR = "ENDFIELD_ACCOUNT_TOKEN"

#: 配置目录按平台惯例解析（Windows 用 %APPDATA%，macOS 用 Application Support）。
#: 之所以不再写死 ``~/.config``：那是 Unix 约定，Windows 用户按惯例去 %APPDATA% 找不到。
CONFIG_DIR = config_dir()
FALLBACK_TOKEN_FILE = CONFIG_DIR / "token.json"

#: 官方登录/通行证相关的域名，用于在浏览会话中定位 token
AUTH_HOSTS = (
    "as.hypergryph.com",
    "user.hypergryph.com",
    "www.skland.com",
    "zonai.skland.com",
    "as.gryphline.com",
)

#: token 在 JSON 里常见的字段名
TOKEN_FIELD_NAMES = ("token", "account_token", "accountToken", "hgToken", "user_token")


class TokenNotFound(RuntimeError):
    """没能拿到可用的 account_token。"""


class TokenInvalid(RuntimeError):
    """拿到了 token，但服务端不认。"""


@dataclass(slots=True)
class TokenSource:
    """token 及其来源，用于日志与排障。"""

    value: str
    origin: str

    def masked(self) -> str:
        if len(self.value) <= 8:
            return "*" * len(self.value)
        return f"{self.value[:4]}...{self.value[-4:]} (len={len(self.value)}, 来自 {self.origin})"


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #


def _keyring() -> Any | None:
    try:
        import keyring  # noqa: PLC0415

        return keyring
    except Exception:  # pragma: no cover - 环境缺失时降级
        return None


def _load_fallback_file() -> str | None:
    if not FALLBACK_TOKEN_FILE.exists():
        return None
    try:
        payload = json.loads(FALLBACK_TOKEN_FILE.read_text("utf-8"))
    except Exception:
        return None
    token = payload.get("account_token")
    return str(token) if token else None


def _store_fallback_file(token: str) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    FALLBACK_TOKEN_FILE.write_text(
        json.dumps({"account_token": token}, ensure_ascii=False, indent=2), "utf-8"
    )
    # 明文 token 等价于账号，权限必须收紧到 0600
    FALLBACK_TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return FALLBACK_TOKEN_FILE


def save_token(token: str, *, allow_plaintext: bool = True) -> str:
    """保存 token，返回实际使用的存储位置描述。

    写入系统钥匙串失败时**不会静默吞掉** —— 那意味着 token 会以明文落盘，
    用户有权知道。失败原因会一并说明（Windows 上常见的是凭据管理器需要
    交互式登录会话，例如通过 SSH 服务远程执行时就没有）。
    """
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE_NAME, KEY_ACCOUNT, token)
            return f"系统钥匙串 ({kr.get_keyring().__class__.__name__})"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if not allow_plaintext:
                raise TokenNotFound(
                    f"系统钥匙串不可用（{reason}），且未允许明文落盘。"
                    "请加 --allow-plaintext-token，或改用环境变量。"
                ) from exc
            print(
                f"⚠ 系统钥匙串写入失败（{reason}）；\n"
                f"  已回退到明文文件存储 —— token 等同于账号，请自行评估风险。",
                file=sys.stderr,
            )
    if not allow_plaintext:
        raise TokenNotFound(
            "系统钥匙串不可用，且未允许明文落盘。请安装 keyring，或加 --allow-plaintext-token。"
        )
    path = _store_fallback_file(token)
    return f"明文文件 {path} (已设为 0600)"


def delete_token() -> bool:
    """删除已保存的 token。返回是否真的删掉了什么。"""
    removed = False
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE_NAME, KEY_ACCOUNT)
            removed = True
        except Exception:
            pass
    if FALLBACK_TOKEN_FILE.exists():
        FALLBACK_TOKEN_FILE.unlink()
        removed = True
    return removed


def load_token() -> TokenSource | None:
    """按优先级读取 token：环境变量 > 系统钥匙串 > 明文文件。"""
    env = os.environ.get(ENV_VAR)
    if env:
        return TokenSource(env.strip(), f"环境变量 {ENV_VAR}")

    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE_NAME, KEY_ACCOUNT)
            if value:
                return TokenSource(value, "系统钥匙串")
        except Exception:
            pass

    value = _load_fallback_file()
    if value:
        return TokenSource(value, f"明文文件 {FALLBACK_TOKEN_FILE}")
    return None


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


async def validate_token(token: str) -> dict[str, Any]:
    """向服务端确认 token 可用，并取回账号基本信息。

    这一步很重要：token 会过期。与其在抢码那一刻才发现自己没登录，
    不如启动时就校验 —— 抢码窗口只有几十秒，没有重试余地。
    """
    async with HypergryphAuth() as auth:
        return await auth.probe_account_token(token)


async def load_and_validate() -> tuple[TokenSource, dict[str, Any]]:
    """读取并校验 token。"""
    source = load_token()
    if source is None:
        raise TokenNotFound(
            "没有找到 account_token。请先运行 `endfield-scanner login` 完成一次引导式登录，"
            f"或设置环境变量 {ENV_VAR}。"
        )
    try:
        info = await validate_token(source.value)
    except ApiError as exc:
        raise TokenInvalid(f"已保存的 account_token 已失效（{exc}）。请重新运行 `endfield-scanner login`。") from exc
    return source, info


def describe_account(info: dict[str, Any]) -> str:
    """把 /user/info/v1/basic 的返回渲染成一行人类可读描述。"""
    nickname = info.get("nickname") or info.get("nickName") or "?"
    hg_id = info.get("hgId") or "?"
    email = info.get("email") or ""
    parts = [f"昵称={nickname}", f"hgId={hg_id}"]
    if email:
        parts.append(f"邮箱={email}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 引导式浏览器登录
# --------------------------------------------------------------------------- #


def _iter_token_candidates(payload: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    """从任意 JSON 结构里挖出疑似 token 的字符串字段。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, str) and key in TOKEN_FIELD_NAMES and len(value) >= 16:
                yield path, value
            else:
                yield from _iter_token_candidates(value, path)
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            yield from _iter_token_candidates(item, f"{prefix}[{index}]")


class _TokenCollector:
    """旁听浏览会话，收集候选 token。"""

    def __init__(self) -> None:
        self.candidates: list[tuple[str, str]] = []
        self.seen: set[str] = set()

    def add(self, origin: str, token: str) -> bool:
        if not token or token in self.seen:
            return False
        self.seen.add(token)
        self.candidates.append((origin, token))
        return True

    def feed_json(self, origin: str, payload: Any) -> None:
        for path, token in _iter_token_candidates(payload):
            self.add(f"{origin} [{path}]", token)

    def feed_text(self, origin: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            self.feed_json(origin, json.loads(text))
        except Exception:
            pass


def guided_browser_login(
    *,
    login_url: str = "https://www.skland.com/",
    timeout_s: float = 300.0,
    headless: bool = False,
) -> str:
    """打开官方登录页，让用户手动完成一次登录，程序自动捕获 token。

    实现方式：用 Playwright 驱动真实浏览器（因此极验看到的是正常人类环境，
    不需要我们对抗它），旁听所有网络请求/响应，从中提取候选 token。

    需要可选依赖：``pip install playwright && playwright install chromium``

    返回捕获到的 token；失败抛 TokenNotFound。
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        raise TokenNotFound(
            "引导式登录需要 Playwright：\n"
            "    pip install playwright\n"
            "    playwright install chromium\n"
            "或者改用 `endfield-scanner login --paste` 手动粘贴 token。"
        ) from exc

    collector = _TokenCollector()
    print(f"[login] 正在打开 {login_url}")
    print("[login] 请在弹出的浏览器里完成登录（含验证码）。完成后本窗口会自动继续。")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        def on_response(response: Any) -> None:
            try:
                if not any(host in response.url for host in AUTH_HOSTS):
                    return
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype:
                    return
                collector.feed_json(response.url, response.json())
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            # 轮询等待：一旦捕获到候选就停；或用户手动结束后关闭窗口
            waited_ms = 0
            step_ms = 500
            while waited_ms < timeout_s * 1000:
                if collector.candidates:
                    break
                page.wait_for_timeout(step_ms)
                waited_ms += step_ms
                # 顺带从 web storage 里捞一把（部分站点把 token 放这里）
                try:
                    storage = page.evaluate(
                        "() => ({...window.localStorage, ...window.sessionStorage})"
                    )
                    if isinstance(storage, dict):
                        collector.feed_json("web-storage", storage)
                except Exception:
                    pass
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    if not collector.candidates:
        raise TokenNotFound(
            "浏览会话里没有捕获到 token。可以改用 `endfield-scanner login --paste` 手动粘贴。"
        )

    # 逐个用服务端校验，第一个通过的即为真 token
    print(f"[login] 捕获到 {len(collector.candidates)} 个候选，正在逐个校验…")
    import httpx  # noqa: PLC0415

    from .api import USE_ENV_PROXY, USER_BASIC  # noqa: PLC0415

    for origin, token in collector.candidates:
        try:
            with httpx.Client(timeout=8.0, trust_env=USE_ENV_PROXY) as client:
                info = client.get(USER_BASIC, params={"token": token})
            payload = info.json()
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("status") == 0 and payload.get("data"):
            print(f"[login] ✔ 校验通过（来源：{origin}）")
            print(f"[login]   {describe_account(payload['data'])}")
            return token

    raise TokenNotFound(
        "捕获到的候选都无法通过服务端校验。请改用 `endfield-scanner login --paste` 手动粘贴 token。"
    )


def prompt_for_token() -> str:
    """从标准输入读取 token（支持整段 JSON 粘贴）。"""
    print("请粘贴 account_token（可直接粘贴一行，或粘贴包含 token 字段的 JSON 片段）：")
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else input("token> ").strip()
    if not raw:
        raise TokenNotFound("没有输入任何内容。")

    # 允许用户直接粘贴一段 JSON
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    if payload is not None:
        found = list(_iter_token_candidates(payload))
        if found:
            print(f"从 JSON 中提取到 token（字段：{found[0][0]}）")
            return found[0][1]

    # 也允许粘贴形如 `token=xxx` 或整段复制的内容
    for line in raw.splitlines():
        line = line.strip().strip(",;")
        if line.startswith('"token"'):
            try:
                return str(json.loads("{" + line + "}")["token"])
            except Exception:
                pass
        if line.lower().startswith("token="):
            return line.split("=", 1)[1].strip().strip('"')
    return raw.split()[0] if raw.split() else raw


__all__ = [
    "AUTH_HOSTS",
    "ENV_VAR",
    "FALLBACK_TOKEN_FILE",
    "KEY_ACCOUNT",
    "SERVICE_NAME",
    "TOKEN_FIELD_NAMES",
    "TokenInvalid",
    "TokenNotFound",
    "TokenSource",
    "delete_token",
    "describe_account",
    "guided_browser_login",
    "load_and_validate",
    "load_token",
    "prompt_for_token",
    "save_token",
    "validate_token",
]
