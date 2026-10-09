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
"""

from __future__ import annotations

import argparse
import os
import platform
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


def _arch_tag() -> str:
    """给产物起个带架构的名字，方便同时分发多架构。"""
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
    return f"{NAME}-{os_name}-{arch}"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包成独立可执行文件")
    parser.add_argument("--onefile", action="store_true",
                        help="产出单文件（启动约 5 秒，仅在必须单文件分发时用）")
    parser.add_argument("--no-zip", action="store_true",
                        help="不额外打 zip（默认会为目录形式打一个同名 zip）")
    parser.add_argument("--clean", action="store_true", help="先清理 build/dist")
    parser.add_argument("--name", default=None, help="产物文件名（默认按平台架构自动命名）")
    args = parser.parse_args()

    if not ENTRY.exists():
        print(f"找不到入口脚本：{ENTRY}", file=sys.stderr)
        return 1

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)

    # PyInstaller 默认把缓存写到 ~/Library/Application Support 或 %APPDATA%。
    # 在受限环境（沙箱、CI 的干净账户）里那可能不可写，所以固定到项目内。
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(ROOT / ".pyinstaller"))

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if args.onefile else "--onedir",
        "--name", args.name or _arch_tag(),
        *[f"--exclude-module={m}" for m in EXCLUDES],
        str(ENTRY),
    ]

    print(f"平台：{sys.platform} / {platform.machine()}")
    print(f"解释器：{sys.executable}")
    print(f"产物名：{args.name or _arch_tag()}")
    print("执行：", " ".join(cmd))
    print()

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\n打包失败。", file=sys.stderr)
        return result.returncode

    dist = ROOT / "dist"
    name = args.name or _arch_tag()

    # 目录形式额外打一个 zip：分发时给用户那个 zip，解压即用。
    # 用 shutil.make_archive 而不是 zip/Compress-Archive：三大平台行为一致，
    # 且不会像 PowerShell 的 Compress-Archive 那样遇到长路径就出问题。
    if not args.onefile and not args.no_zip:
        target = dist / name
        if target.is_dir():
            print(f"\n正在打 zip（{name}.zip）…")
            archive = shutil.make_archive(str(dist / name), "zip", root_dir=dist, base_dir=name)
            print(f"  完成：{archive}")

    print(f"\n产物在 {dist}/：")
    for item in sorted(dist.iterdir()):
        if item.name == "build":
            continue
        if item.is_file():
            print(f"  {item.name}  {item.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            total = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
            print(f"  {item.name}/  {total / 1024 / 1024:.1f} MB（目录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
