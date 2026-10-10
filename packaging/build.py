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

产物落在 ``dist/``。命名规则：

===========================================  ==========================================
文件名                                        说明
===========================================  ==========================================
``endfield-scanner-v0.1.0-macos-arm64/``      外层目录，带**版本 + 平台**
``endfield-scanner-v0.1.0-macos-arm64.zip``   分发给用户的压缩包
``.../endfield-scanner``                      里面的**可执行文件，朴素名**
===========================================  ==========================================

目录与 zip 带版本是为了让用户能区分不同版本的下发；**里面的可执行文件保持朴素名**，
这样用户升级后自己的脚本、快捷方式、文档里的路径都不用改。

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


def _platform_tag() -> str:
    """当前平台与架构的后缀，例如 ``macos-arm64`` / ``windows-x64``。"""
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
    return f"{os_name}-{arch}"


def _release_name(version: str | None = None) -> str:
    """分发物的名字：``endfield-scanner-v0.1.0-macos-arm64``。

    只用于**外层的目录与 zip**，让用户下载下来能区分版本与平台。
    **里面的可执行文件始终是朴素的 ``endfield-scanner``** —— 用户解压后拿到的
    是一个名字稳定的程序，而不是每次升级都要改脚本里的路径。
    """
    return f"{NAME}-{_normalize_version(version)}-{_platform_tag()}"


def _resign_tree_macos(target: Path) -> int:
    """在 macOS 上把产物里的每个 Mach-O 二进制重新做一遍 ad-hoc 签名。

    为什么必须做（实测踩到的坑）
    ---------------------------
    PyInstaller 只对**主可执行文件**做 ad-hoc 签名，从 wheel 里拷进来的
    ``.so`` / ``.dylib`` / ``Python.framework`` 用的是 wheel 自带的签名。
    其中 ``Python.framework/Versions/<ver>/Python`` 常常是**无效**的 ——
    实测报错::

        code has no resources but signature indicates they must be present

    后果不是"跑不起来"，而是 Gatekeeper 逐个弹窗拦截：先弹 Python，再一个个弹
    ``*.cpython-312-darwin.so``，用户得手动 Unblock 几十次。全树重签后
    194 个二进制全部 ``codesign -v`` 通过。

    这是个纯本地的 ad-hoc 签名（``--sign -``），**不是**开发者证书签名，
    因此也不能替代公证（notarize）。它的作用是让每个二进制自身有效，
    这样 Gatekeeper 的逐文件校验不会失败。

    返回重新签名的文件数。
    """
    if sys.platform != "darwin":
        return 0

    # 先用一次 `file` 把 Mach-O 认出来。逐个调用 `file` 也能行，但 240 个文件
    # 每次起一个进程太浪费；这里一次批量处理。
    out = subprocess.run(
        ["bash", "-c",
         'find "$1" -type f -size +100k -print0 | '
         'xargs -0 file | grep "Mach-O" | sed "s/:.*//"',
         "_", str(target)],
        capture_output=True, text=True,
    )
    binaries = [Path(line) for line in out.stdout.splitlines() if line.strip()]

    signed = 0
    for path in binaries:
        r = subprocess.run(
            ["codesign", "--force", "--sign", "-", str(path)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            signed += 1

    # 最后给整个目录封一次，让嵌套的 .framework 也有完整的封印
    subprocess.run(["codesign", "--force", "--sign", "-", str(target)],
                   capture_output=True, text=True)
    return signed


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a standalone executable for the current platform")
    parser.add_argument("--onefile", action="store_true",
                        help="single-file output (about 5s startup; use only when a single file is required)")
    parser.add_argument("--no-sign", action="store_true",
                        help="macOS only: skip re-signing the bundled Mach-O binaries")
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

    # 两个名字要分清（早期版本混用成一个，导致可执行文件名里也带上了版本与平台）：
    #   exe_name     —— 可执行文件本体，保持朴素稳定，用户解压后直接就能跑
    #   release_name —— 外层目录与 zip，带版本+平台，便于区分不同版本的下发
    version_label = _normalize_version(args.version)
    release_name = args.name or _release_name(args.version)
    exe_name = args.name or NAME

    # PyInstaller 默认把缓存写到 ~/Library/Application Support 或 %APPDATA%。
    # 在受限环境（沙箱、CI 的干净账户）里那可能不可写，所以固定到项目内。
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(ROOT / ".pyinstaller"))

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if args.onefile else "--onedir",
        "--name", exe_name,
        *[f"--exclude-module={m}" for m in EXCLUDES],
        str(ENTRY),
    ]

    print(f"platform   : {sys.platform} / {platform.machine()}")
    print(f"python     : {sys.executable}")
    src_label = "--version" if args.version else VERSION_FILE.name
    print(f"version    : {version_label}  (from {src_label})")
    print(f"executable : {exe_name}")
    print(f"release    : {release_name}")
    print("command    :", " ".join(cmd))
    print()

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\nbuild failed.", file=sys.stderr)
        return result.returncode

    dist = ROOT / "dist"

    # PyInstaller 按 exe_name 产出 build/<exe_name>/ 与 dist/<exe_name>/。
    # 把 dist 里的那个目录整名改成 release_name —— 于是解压后是
    #   endfield-scanner-v0.1.0-macos-arm64/endfield-scanner
    # 目录带版本（便于区分下发），里面的可执行文件是朴素名（用户路径稳定）。
    built = dist / exe_name
    if not args.name and built.exists() and built.name != release_name:
        # 目录（onedir）与文件（onefile）都走这一条：把名字换成带版本+平台的
        built.rename(dist / release_name)

    # macOS：把每个 Mach-O 重新 ad-hoc 签名，否则 Gatekeeper 会逐个弹窗，
    # 用户要 Unblock 几十次。重签必须在**改名之后**做 —— 签的是最终路径。
    if sys.platform == "darwin" and not args.no_sign:
        final = dist / release_name
        if final.exists():
            print("\nsigning Mach-O binaries (ad-hoc) ...")
            n = _resign_tree_macos(final)
            print(f"  re-signed {n} binaries")

    # 目录形式额外打一个 zip：分发时给用户那个 zip，解压即用。
    # 用 shutil.make_archive 而不是 zip/Compress-Archive：三大平台行为一致，
    # 且不会像 PowerShell 的 Compress-Archive 那样遇到长路径就出问题。
    # 单文件模式不打 zip —— 产物本身就是一个文件，再套一层只会让用户多解压一次。
    if not args.onefile and not args.no_zip:
        target = dist / release_name
        if target.is_dir():
            print(f"\nzipping {release_name}.zip ...")
            archive = shutil.make_archive(
                str(dist / release_name), "zip", root_dir=dist, base_dir=release_name
            )
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
