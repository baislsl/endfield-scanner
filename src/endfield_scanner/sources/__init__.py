"""直播流取流的**接口占位**。

这一块暂时搁置了
----------------
直播取流（B 站 / 抖音）曾实现并跑通过，但依赖平台侧的接口与反爬策略：

* B 站：``getRoomPlayInfo`` 取 FLV 直链，需要正确的 Referer；
* 抖音：``webcast/room/web/enter`` 对缺少 ``ttwid`` cookie 的请求会
  **静默返回 HTTP 200 + 空 body**（而不是 403），且改版频繁。

这些都会随平台变动而失效，维护成本高、不确定性大，所以先不做。
**帧源抽象与解码链路保留着**（见 :mod:`endfield_scanner.core.frames`
的 ``StreamTarget`` / ``StreamSource``），将来要重新做时只需在本包内
补上各平台的 resolver。

想恢复的话需要提供的东西
------------------------
每个平台的 resolver 就是一个可调用对象，返回
``(url, ffmpeg_options)`` 或 ``None``（表示未开播）::

    def resolve() -> StreamTarget | None: ...

B 站要点：``Referer`` 必须带，否则 CDN 403。
抖音要点：先访问一次直播间页面拿 ``ttwid`` cookie，再请求 webcast 接口；
          空 body 就是反爬，不是网络故障。
"""

from __future__ import annotations

from typing import Any

#: 已搁置的平台 -> 当初的实现要点（仅作文档保留，避免以后重新踩坑）
DEFERRED_PLATFORMS: dict[str, str] = {
    "bilibili": "getRoomPlayInfo 取 FLV 直链；必须带 Referer: https://live.bilibili.com/<room>",
    "douyin": "webcast/room/web/enter；必须先取 ttwid cookie，空 body 即反爬",
}


def resolve_platform(platform: str) -> Any:
    """取得某平台的流地址解析器。

    目前**未启用**，一律抛 :class:`NotImplementedError` 并说明原因。
    """
    if platform in ("bilibili", "bili"):
        readable = "bilibili"
    elif platform in ("douyin", "dy"):
        readable = "douyin"
    else:
        raise ValueError(f"未知直播平台 {platform!r}")

    raise NotImplementedError(
        f"直播取流（{readable}）暂时搁置未启用。\n"
        f"  当初的实现要点：{DEFERRED_PLATFORMS[readable]}\n"
        f"  恢复要点见本模块（sources）的说明。\n"
        f"  目前请改用 `--input screen` 盯屏幕上的画面。"
    )


__all__ = ["DEFERRED_PLATFORMS", "resolve_platform"]
