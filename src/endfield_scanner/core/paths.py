"""跨平台的路径解析。

为什么需要这个
--------------
早期版本把配置目录写成 ``Path.home() / ".config" / "endfield-scanner"``，
这是 **Unix 约定**。在 Windows 上它会落到 ``C:\\Users\\<你>\\.config\\...`` ——
能用，但不符合平台习惯，用户按惯例去 ``%APPDATA%`` 找是找不到的。

各平台的惯例：

===========  ==========================================
平台          位置
===========  ==========================================
Windows      ``%APPDATA%\\endfield-scanner``
macOS        ``~/Library/Application Support/endfield-scanner``
Linux/其它    ``$XDG_CONFIG_HOME/endfield-scanner`` 或 ``~/.config/endfield-scanner``
===========  ==========================================

用 ``ENDFIELD_SCANNER_HOME`` 环境变量可以强制覆盖（测试与便携部署都靠它）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 覆盖配置目录的环境变量
HOME_ENV = "ENDFIELD_SCANNER_HOME"

APP_NAME = "endfield-scanner"


def config_dir() -> Path:
    """返回配置目录（按平台惯例，可用环境变量覆盖）。

    这个函数**不创建目录**，只在需要写入时由调用方 ``mkdir``。
    """
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        # 极端情况（服务账户可能没有 APPDATA）退回用户目录下的常规位置
        return Path.home() / "AppData" / "Roaming" / APP_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".config" / APP_NAME


__all__ = ["APP_NAME", "HOME_ENV", "config_dir"]
