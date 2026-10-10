#!/usr/bin/env python3
"""跨平台打包脚本：一条命令产出当前平台的可执行文件。

为什么需要"跨平台 CI"而不是靠这个脚本交叉编译
----------------------------------------------
**PyInstaller 不能交叉编译。** 它的工作方式是把**当前正在运行的解释器**
及其依赖一起打包进产物，所以：

* 在 macOS 上跑，只能得到 macOS 的可执行文件；
* 在 Apple Silicon 上跑，只能得到 arm64；想要 x86_64 得在 Intel Mac 上跑
  （或者装 Rosetta 2 + 用 x86_64 wheel 重建整个依赖环境，成本高且脆弱）；
* 想要 Windows 产物，**必须**在 Windows 上跑。

32 位就更不用考虑了：macOS 从 Catalina（2019）起彻底不支持 32 位；
Windows 的 32 位用户占比极低，且 32 位进程有 4GB 地址空间限制，
而本工具依赖的 numpy/opencv 早已不提供 32 位 wheel。

所以正确做法是：**本脚本负责"在某个平台上打包"，CI 的矩阵负责"在所有平台上都打包一遍"。**
见 `.github/workflows/build.yml`。

为什么默认 ``--onedir`` 而不是 ``--onefile``
--------------------------------------------
实测（macOS arm64，本机）::

    --onefile   启动 5.46 / 5.12 / 4.86 秒   ← 恒定 5 秒，永不改善
    --onedir    启动 19.9 / 0.15 / 0.14 秒   ← 首次慢是 OS 页缓存，之后极快
    源码运行     启动        0.13 秒

``--onefile`` 会把压缩包附在可执行文件尾部，**每次运行都解压到临时目录**，
所以那个 5 秒是每次都要付的。对一个交互式 CLI 来说这足以让人以为程序卡死。
``--onedir`` 只是一堆普通文件，由操作系统页缓存接管，第二次起和源码一样快。

代价是要分发一个目录 —— 所以本脚本会**额外打一个 zip**，
分发时给用户那个 zip 即可。

用法
----
::

    python packaging/build.py              # 目录形式 + 同名 zip（默认）
    python packaging/build.py --onefile    # 单文件（启动慢，仅在必须单文件时用）
    python packaging/build.py --clean      # 先清掉 build/dist
    python packaging/build.py --no-zip     # 只出目录，不打包

产物落在 ``dist/``。

⚠️ 本脚本**打印到终端的内容必须全部是 ASCII**
---------------------------------------------
docstring 和注释用中文没问题（它们不会被编码），但 ``print`` / argparse 文案不行。

原因：Windows CI 上 Python 的 stdout 编码是 ``cp1252``，打印中文会直接抛
``UnicodeEncodeError: 'charmap' codec can't encode characters`` 并让构建失败。
CI 里没有人设 ``PYTHONIOENCODING``，所以不能指望运行环境的 locale。
本脚本会打印中文曾导致过 CI 打包失败 —— 改回去之前请先想清楚这一点。
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "packaging" / "entry.py"
NAME = "endfield-scanner"

#: 明确排除的模块：显著减小体积，且与功能无关。
#:
#: * ``av``  —— 直播取流用（当前搁置）。**必须排除**：它自带一套 ffmpeg，
#:   会和 opencv 自带的 ffmpeg 撞 dylib（重复的 libavdevice），
#:   启动时刷一堆 objc 警告，严重时直接崩。
#: * 其余是常见的"被间接拉进来但其实用不到"的大件。
EXCLUDES = [
    "av",
    "tkinter",
    "matplotlib",
    "pytest",
    "PyQt5",
    "PySide6",
    "IPython",
    "pandas",
    "scipy",
    "setuptools",
    "pip",
]


#: 版本号的唯一来源。
#:
#: ``pyproject.toml`` 里配的是 ``dynamic = ["version"]`` + ``[tool.hatch.version]``，
#: 同样读这个文件 —— 全项目只有这一处写版本号。
#: 这样 CLI 显示的版本、包的版本、产物名里的版本永远一致。
VERSION_FILE = ROOT / "src" / "endfield_scanner" / "__init__.py"


def _project_version() -> str:
    """读版本号（唯一来源：``src/endfield_scanner/__init__.py``）。

    手写正则而不用 ``tomllib``/import：前者是 TOML 专用，后者会把整个包
    import 进来（打包脚本不该依赖被构建的代码能正常运行）。正则足够且无副作用。
    读不到就返回 "0.0.0"，让构建照常进行而不是报错中断。
    """
    try:
        text = VERSION_FILE.read_text("utf-8")
    except OSError:
        return "0.0.0"
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def _normalize_version(raw: str | None) -> str:
    """统一版本号写法：总是带一个 ``v`` 前缀。

    这样 ``--version 0.1.0``、``--version v0.1.0`` 和从 pyproject 读到的
    ``0.1.0`` 会得到同样的产物名，避免同一个版本出现两种文件名。
    """
    v = (raw or _project_version()).strip()
    # 用 lstrip 而不是去掉一个字符：这样 `vv0.1.0` 也能归一到 `v0.1.0`，
    # 函数对"已经带 v"和"带了多个 v"的输入都幂等。
    v = v.lstrip("v")
    return f"v{v}" if v else ""


def _arch_tag(version: str | None = None) -> str:
    """给产物起个带版本与架构的名字，方便同时分发多架构、多版本。

    形如 ``endfield-scanner-v0.1.0-macos-arm64``。
    带上版本号是为了让用户下载后能区分不同版本 —— 否则多个版本的 zip
    下载下来文件名一模一样。
    """
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        os_name = "macos"
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    elif sys.platform == "win32":
        os_name = "windows"
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    else:
        os_name = "linux"
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    ver = _normalize_version(version)
    return f"{NAME}-{ver}-{os_name}-{arch}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a standalone executable for the current platform")
    parser.add_argument("--onefile", action="store_true",
                        help="single-file output (about 5s startup; use only when a single file is required)")
    parser.add_argument("--no-zip", action="store_true",
                        help="skip the extra zip (by default onedir output is also zipped)")
    parser.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    parser.add_argument("--name", default=None,
                        help="artifact name (defaults to endfield-scanner-<version>-<os>-<arch>)")
    parser.add_argument("--version", default=None,
                        help="version to embed in the artifact name; "
                             "defaults to the version in pyproject.toml. "
                             "A leading 'v' is optional and always normalized in.")
    args = parser.parse_args()

    if not ENTRY.exists():
        print(f"entry script not found: {ENTRY}", file=sys.stderr)
        return 1

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)

    # 只算一次：产物名在命令行、打印、打 zip 三处都要用，分散计算容易不一致
    name = args.name or _arch_tag(args.version)

    # PyInstaller 默认把缓存写到 ~/Library/Application Support 或 %APPDATA%。
    # 在受限环境（沙箱、CI 的干净账户）里那可能不可写，所以固定到项目内。
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(ROOT / ".pyinstaller"))

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if args.onefile else "--onedir",
        "--name", name,
        *[f"--exclude-module={m}" for m in EXCLUDES],
        str(ENTRY),
    ]

    print(f"platform : {sys.platform} / {platform.machine()}")
    print(f"python   : {sys.executable}")
    src_label = "--version" if args.version else VERSION_FILE.name
    print(f"version  : {_normalize_version(args.version)}  (from {src_label})")
    print(f"artifact : {name}")
    print("command  :", " ".join(cmd))
    print()

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\nbuild failed.", file=sys.stderr)
        return result.returncode

    dist = ROOT / "dist"

    # 目录形式额外打一个 zip：分发时给用户那个 zip，解压即用。
    # 用 shutil.make_archive 而不是 zip/Compress-Archive：三大平台行为一致，
    # 且不会像 PowerShell 的 Compress-Archive 那样遇到长路径就出问题。
    if not args.onefile and not args.no_zip:
        target = dist / name
        if target.is_dir():
            print(f"\nzipping {name}.zip ...")
            archive = shutil.make_archive(str(dist / name), "zip", root_dir=dist, base_dir=name)
            print(f"  created: {archive}")

    print(f"\nartifacts in {dist}/")
    for item in sorted(dist.iterdir()):
        if item.name == "build":
            continue
        if item.is_file():
            print(f"  {item.name}  {item.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            total = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
            print(f"  {item.name}/  {total / 1024 / 1024:.1f} MB (dir)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
