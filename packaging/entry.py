"""PyInstaller 等打包工具的入口脚本。

为什么不直接用 ``src/endfield_scanner/__main__.py``：
那个文件用的是相对导入（``from .cli import main``），作为 ``python -m`` 运行没问题，
但被当成**独立脚本**执行时没有父包，会直接报
``ImportError: attempted relative import with no known parent package``。
这里用绝对导入，两种运行方式都成立。
"""

from endfield_scanner.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
