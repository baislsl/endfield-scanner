"""帧源：把「屏幕」或「直播流」统一成 BGR ndarray 的迭代器。

三种来源
--------
* :class:`ScreenSource`  —— 截取屏幕（含区域裁剪），用 mss，跨平台。
* :class:`StreamSource`  —— 拉直播流并用 PyAV 解码，内置断流重连。
* :class:`FileSource`    —— 读图片/视频文件，用于自测与回归。

设计约定：所有帧源都实现 ``frames()`` 生成器，产出 :class:`Frame`。
"""

from __future__ import annotations

import io
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

import numpy as np

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Frame:
    """一帧 BGR 图像及其采集时刻。

    ``timestamp`` 用 **``time.monotonic()``**（开机以来的秒数），
    因为它唯一的用途是和"当前时刻"做差、算采集耗时。
    早期版本用的是 ``time.time()``（Unix 墙钟，约 1.79e9），
    一旦拿去减 ``time.monotonic()`` 就会得到 -1.79e12 这种天文数字 ——
    这个坑在本项目里犯过两次（一次是 TTL，一次是耗时埋点）。
    """

    image: np.ndarray
    timestamp: float
    #: 该帧在原始画面中的区域，用于把检测结果映射回全屏坐标
    region: tuple[int, int, int, int] | None = None

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self.image.shape)


def new_mss_instance() -> Any:
    """取一个 mss 实例，兼容新旧入口。

    * mss >= 10：``mss.mss`` 已废弃，改为模块属性 ``mss.MSS``，而且它**本身就是实例**
      （所以不能写 ``mss.MSS()``）。
    * mss < 10：``mss.mss`` 是类，需要实例化。

    注意：mss 实例带线程局部状态，**每个线程都要自己建一个**。
    """
    import mss  # noqa: PLC0415

    candidate = getattr(mss, "MSS", None)
    if candidate is None:
        return mss.mss()
    return candidate() if callable(candidate) else candidate


def _screen_capture_error(exc: Exception) -> FrameSourceError:
    """把"截屏失败"翻译成用户能照做的提示。

    平台差异很大，原始报错往往没有可操作性：

    * **枚举不到显示器**（``IndexError``）：当前上下文根本没有可用桌面 ——
      无 GUI 的会话、后台服务、CI 等。这与"权限不足"是两回事，
      提示必须分清楚，否则会把人引向错误的排查方向。
    * **Windows**：``mss`` 抛 ``ScreenShotError: BitBlt: Access is denied``。
      常见于进程跑在 **Session 0**（服务会话）里 —— 例如通过 Windows 自带的
      SSH 服务远程执行，或计划任务设成"不管用户是否登录都运行"。
      Session 0 是会话隔离的，没有可捕获的桌面。
    * **macOS**：没有屏幕录制权限，或权限变更后没有重启进程。
    """
    import sys as _sys  # noqa: PLC0415

    detail = f"{type(exc).__name__}: {exc}"

    # 枚举不到显示器：无桌面上下文，跟权限无关，必须先判掉
    if isinstance(exc, IndexError):
        return FrameSourceError(
            "找不到可截屏的显示器（当前上下文没有可用桌面）。\n"
            "  常见于：无图形界面的会话、后台服务、CI 环境。\n"
            "  请在**已登录的桌面会话**里直接运行本工具。\n"
            f"  原始错误：{detail}"
        )

    if _sys.platform == "win32":
        hint = (
            "Windows 上截屏失败。常见原因：\n"
            "  · 进程跑在 **Session 0（服务会话）**里 —— 例如通过 Windows 自带的\n"
            "    OpenSSH 服务远程执行，或计划任务勾了「不管用户是否登录都运行」。\n"
            "    Session 0 是会话隔离的，没有可捕获的桌面。\n"
            "  · 请在本机**已登录的桌面会话**里直接运行（双击或在自己的终端里跑），\n"
            "    不要通过 SSH 服务 / 服务式计划任务运行。\n"
            "  · 安全软件有时也会拦截屏幕捕获。"
        )
    elif _sys.platform == "darwin":
        hint = (
            "macOS 上截屏失败。请在「系统设置 → 隐私与安全性 → 屏幕录制」里\n"
            "  勾选你的终端，然后**重启终端**（权限变更后必须重启进程才生效）。"
        )
    else:
        hint = "截屏失败：请确认当前会话能访问桌面（X11/Wayland 显示环境是否可用）。"
    return FrameSourceError(f"{hint}\n  原始错误：{detail}")


@dataclass(slots=True)
class StreamTarget:
    """一条可拉取的直播流地址，以及拉取它必须附带的 ffmpeg 选项。

    ``options`` 是必要的：B 站 CDN 会校验 ``Referer``，不带就 403。
    """

    url: str
    options: dict[str, str] = field(default_factory=dict)


class FrameSourceError(RuntimeError):
    """帧源无法提供数据。"""


# --------------------------------------------------------------------------- #
# 屏幕
# --------------------------------------------------------------------------- #


class ScreenSource:
    """截屏帧源。

    :param monitor: mss 的显示器编号，1 为主屏。
    :param region: ``(left, top, width, height)``；为 None 时自动取竖向中间 60% 条带。
        抢码场景下二维码通常出现在画面中部，裁掉上下无关区域能显著降延迟。
    :param fps: 目标采集帧率上限。
    """

    def __init__(
        self,
        *,
        monitor: int = 1,
        region: tuple[int, int, int, int] | None = None,
        fps: float = 20.0,
        band: float = 0.6,
    ) -> None:
        self.monitor = monitor
        self._region = region
        self.fps = max(1.0, fps)
        self.band = band

    def _resolve_region(self, mss_instance: Any) -> tuple[int, int, int, int]:
        if len(mss_instance.monitors) <= self.monitor:
            # monitors[0] 是虚拟全屏，真正的显示器从 [1] 开始。
            # 只有 1 个条目就意味着当前上下文枚举不到任何显示器。
            raise _screen_capture_error(
                IndexError(
                    f"只枚举到 {len(mss_instance.monitors) - 1} 个显示器"
                    f"（请求 monitor={self.monitor}）"
                )
            )
        mon = mss_instance.monitors[self.monitor]
        if self._region is not None:
            left, top, width, height = self._region
            return (
                max(0, left),
                max(0, top),
                min(width, mon["width"]),
                min(height, mon["height"]),
            )
        height = int(mon["height"] * self.band)
        top = int((mon["height"] - height) / 2)
        return (0, top, mon["width"], height)

    def frames(self) -> Iterator[Frame]:
        interval = 1.0 / self.fps
        with new_mss_instance() as sct:
            region = self._resolve_region(sct)
            grab_region = {
                "left": region[0],
                "top": region[1],
                "width": region[2],
                "height": region[3],
            }
            next_at = time.perf_counter()
            while True:
                try:
                    shot = sct.grab(grab_region)
                except Exception as exc:  # 见 _screen_capture_error 的平台说明
                    raise _screen_capture_error(exc) from exc
                # mss 给的是 BGRA，去掉 alpha 通道即得 BGR
                image = np.asarray(shot, dtype=np.uint8)[:, :, :3]
                yield Frame(image=image, timestamp=time.monotonic(), region=region)

                next_at += interval
                sleep_for = next_at - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_at = time.perf_counter()


# --------------------------------------------------------------------------- #
# 文件（自测 / 回归）
# --------------------------------------------------------------------------- #


class FileSource:
    """反复播放同一个图片或视频文件，用于不消耗真实二维码的端到端测试。"""

    def __init__(
        self,
        path: str,
        *,
        fps: float = 10.0,
        loop: bool = True,
    ) -> None:
        self.path = path
        self.fps = max(0.5, fps)
        self.loop = loop

    def frames(self) -> Iterator[Frame]:
        import cv2  # noqa: PLC0415

        image = cv2.imread(self.path)
        if image is not None:
            while True:
                yield Frame(image=image, timestamp=time.monotonic())
                if not self.loop:
                    return
                time.sleep(1.0 / self.fps)

        # 不是图片就当作视频
        capture = cv2.VideoCapture(self.path)
        if not capture.isOpened():
            raise FrameSourceError(f"无法读取帧源文件: {self.path}")
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    if not self.loop:
                        return
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                yield Frame(image=frame, timestamp=time.monotonic())
                time.sleep(1.0 / self.fps)
        finally:
            capture.release()


# --------------------------------------------------------------------------- #
# 直播流
# --------------------------------------------------------------------------- #


class StreamSource:
    """直播流帧源（HTTP-FLV / HLS / 任意 ffmpeg 可读协议）。

    PyAV 自带 ffmpeg，因此不需要用户额外安装。为压低直播延迟，打开时
    会设置一组 ``fflags/nobuffer``、``low_delay`` 之类的参数 —— 对抢码来说，
    画面上出现二维码到我们处理它之间的每一毫秒都是要省的。

    :param url_resolver: 返回拉流信息 ``(url, ffmpeg_options)`` 的可调用对象；
        返回 None 表示当前未开播。这样设计是为了支持「等待开播」——
        主播没开播时自动重试，而不是退出。``ffmpeg_options`` 用于传
        B 站 CDN 强制的 ``Referer`` 之类的头。
    """

    def __init__(
        self,
        url_resolver: Callable[[], StreamTarget | None],
        *,
        probe_interval_s: float = 5.0,
        max_reconnect: int = 0,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self._resolve = url_resolver
        self.probe_interval_s = probe_interval_s
        self.max_reconnect = max_reconnect  # 0 = 无限
        self._on_status = on_status or (lambda _msg: None)

    def _open(self, target: "StreamTarget") -> Any:
        import av  # noqa: PLC0415

        # probesize/analyzeduration 压到最小：少等一点是一次性的便宜，抢码每一轮都要付
        options: dict[str, str] = {
            "fflags": "nobuffer",
            "flags": "low_delay",
            "probesize": "1024",
            "analyzeduration": "0",
            "reorder_queue_size": "0",
            "rtbufsize": "0",
            "max_delay": "0",
        }
        options.update(target.options)
        return av.open(target.url, options=options, timeout=(5.0, 5.0))

    def frames(self) -> Iterator[Frame]:
        reconnect = 0
        while True:
            target = self._resolve()
            if target is None:
                self._on_status("未开播，稍后重试…")
                time.sleep(self.probe_interval_s)
                continue

            self._on_status("已取得拉流地址，开始解码")
            try:
                container = self._open(target)
            except Exception as exc:
                self._on_status(f"拉流失败: {exc}")
                reconnect += 1
                if self.max_reconnect and reconnect > self.max_reconnect:
                    raise FrameSourceError(f"连续拉流失败 {reconnect} 次，放弃") from exc
                time.sleep(self.probe_interval_s)
                continue

            reconnect = 0
            try:
                for frame in container.decode(video=0):
                    try:
                        image = frame.to_ndarray(format="bgr24")
                    except Exception:
                        continue
                    yield Frame(image=image, timestamp=time.monotonic(), region=None)
                self._on_status("直播流结束（主播可能下播了），重新连接…")
            except FrameSourceError:
                raise
            except Exception as exc:
                # av.error.* 在断流/切流时很常见，重连即可
                self._on_status(f"解码中断: {type(exc).__name__}: {exc}，重连中…")
            finally:
                try:
                    container.close()
                except Exception:
                    pass

            reconnect += 1
            if self.max_reconnect and reconnect > self.max_reconnect:
                raise FrameSourceError(f"连续断流 {reconnect} 次，放弃")
            time.sleep(min(self.probe_interval_s, 1.0))


# --------------------------------------------------------------------------- #
# 工具：区域裁剪
# --------------------------------------------------------------------------- #


def crop_region(frame: Frame, region: Sequence[int]) -> Frame:
    """按屏幕坐标裁剪一个帧（用于在整屏帧上缩小搜索范围）。"""
    left, top, width, height = (int(v) for v in region)
    height_avail, width_avail = frame.image.shape[:2]
    left = max(0, min(left, width_avail - 1))
    top = max(0, min(top, height_avail - 1))
    width = max(1, min(width, width_avail - left))
    height = max(1, min(height, height_avail - top))
    return Frame(
        image=frame.image[top : top + height, left : left + width],
        timestamp=frame.timestamp,
        region=(left, top, width, height),
    )


REGION_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")


def parse_region(text: str | None) -> tuple[int, int, int, int] | None:
    """解析 ``--region 100,200,800,400`` 形式的区域参数。"""
    if not text:
        return None
    match = REGION_RE.match(text)
    if not match:
        raise ValueError(f"区域格式应为 left,top,width,height，收到: {text!r}")
    return tuple(int(v) for v in match.groups())  # type: ignore[return-value]


__all__ = [
    "FileSource",
    "_screen_capture_error",
    "new_mss_instance",
    "Frame",
    "FrameSourceError",
    "ScreenSource",
    "StreamSource",
    "StreamTarget",
    "crop_region",
    "parse_region",
]
