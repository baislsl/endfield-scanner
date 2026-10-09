"""endfield-scanner —— 明日方舟：终末地 扫码登录抢码工具。

核心链路（均已在 2026-10 实测）::

    gen_scan/login   -> scanUrl = hypergryph://scan_login?scanId=<32hex>
    scan_status      -> 「未扫码」/ scanCode
    token_by_scan_code (无鉴权) -> 抢占提交

其中 scan_login / update_scan_status 是逆向 APK 得到的（公开文档里没有），
它们让本工具可以代做「扫码 + 确认」两步，而不必等人先扫码。
各端点的字段与语义见 core/api.py 的模块说明。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
