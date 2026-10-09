"""二维码解码：把屏幕帧 / 直播帧变成一段字符串。

为什么不用参考项目的方案
------------------------
MHY_Scanner 用的是 ``cv2.wechat_qrcode.WeChatQRCode``（微信模型，可以
``setScaleFactor(0.4)``）。但实测：

* **macOS arm64 没有任何 wheel 提供 ``cv2.wechat_qrcode``**
  （``opencv-python`` 与 ``opencv-python-headless`` 都不含该模块），
  而 macOS 正是本工具的首要目标平台。
* 退而求其次用 ``cv2.QRCodeDetector`` 也不行 —— 实测对比（1640x900 画面）::

      二维码尺寸    cv2.QRCodeDetector      zxing-cpp
      164x164       34ms  识别              8ms  识别
       98x98        34ms  ✘ 识别失败         8ms  识别
       66x66        33ms  ✘ 识别失败         8ms  识别
       98x98 模糊    33ms  ✘ 识别失败         8ms  识别
       66x66 模糊    18ms  ✘ 识别失败       7.5ms ✘ 识别失败

  直播里二维码被缩放/压糊是常态，cv2 在 98px 就完全失效，而 zxing-cpp 在
  66px 且带模糊+JPEG 压缩时仍能稳定识别，且快 4 倍。

所以本模块把 **zxing-cpp 作为主力后端**，``cv2.QRCodeDetector`` 仅作兜底。

另外还做了跨帧 RoI 复用：二维码在画面上会停留多帧，一旦某帧定位到位置，
后续帧只在该位置附近解码，把「首帧几十毫秒」摊薄到几乎为零。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .api import extract_scan_id

#: 复用上次定位结果的门槛
_ROI_PAD_RATIO = 0.5
#: RoI 最多复用多少秒（二维码可能移动或消失）
_ROI_TTL_S = 2.0
#: 全图搜索时先缩到这个比例再找 RoI（省时间；找不到再补全图）
_ROI_HINT_SCALE = 0.6
#: 放大救援只作用在画面中间的这条带（比例）。放大整帧是 ~120ms，只放大中间带便宜一个数量级
_RESCUE_BAND_RATIO = 0.5
#: 画面像素数**超过**这个量时，就**禁用放大救援**。
#:
#: 一个阈值同时管两件事，因为它们本质是同一个判断：**画面够不够大**。
#:   * 大画面说明是高 DPI 屏（物理像素多）→ 码本身就大，不需要放大；
#:   * 而放大救援会在这种尺寸上造出 5000x3000 级别的图，单次就近 400ms。
#:
#: 实测（3456x2168，zxing）：
#:
#:   ==========  ==========  ==================
#:   搜索尺度     无码成本     最小可识别(物理px)
#:   ==========  ==========  ==================
#:   1.0x         400ms       60
#:   0.6x         139ms       80
#:   0.4x          82ms       120
#:   0.3x          40ms       150
#:   ==========  ==========  ==================
#:
#: 所以超大画面从 0.6x 起步：高 DPI 屏上一张 300 逻辑像素的码物理上约 600px，
#: 缩到 0.6x 仍有 360px，余量充足。原先要跑完整 4 级（含两次放大）→ 1.1 秒/帧。
_SCREEN_PIXEL_BUDGET = 4_000_000


def _import_zxing() -> Any | None:
    try:
        import zxingcpp  # noqa: PLC0415

        return zxingcpp
    except Exception:  # pragma: no cover
        return None


def load_wechat_detector(models_dir: Path | None = None) -> Any:
    """加载 OpenCV 的微信二维码模型（可选后端）。

    需要 4 个模型文件（detect / sr 各一对 prototxt + caffemodel），
    默认从仓库根的 ``models/wechat_qrcode/`` 读取。**仓库不附带这些文件**
    （约 1MB 二进制，且归属 OpenCV 项目），需要时自行从
    opencv/opencv_3rdparty 的 ``wechat_qrcode`` 分支下载。

    为什么默认不用它：
      * 它在准确率上对**极小/模糊**的码更强，但**慢得多** —— 实测单次调用
        约 650ms（cv2 系），而 zxing 在 1280x720 上是 15ms、3456x2168 上 139ms；
      * 只有 Windows 能装（macOS arm64 没有任何 wheel 提供 ``cv2.wechat_qrcode``），
        用了会让两个平台行为分叉。

    抛 :class:`FileNotFoundError` 并说明缺哪个文件，而不是静默退回 zxing ——
    否则用户会以为自己开了模型、实际没开。
    """
    if not hasattr(cv2, "wechat_qrcode"):
        raise FileNotFoundError(
            "当前 OpenCV 不包含 wechat_qrcode 模块。\n"
            "  Windows 上装 opencv-contrib-python 才有；macOS arm64 目前没有 wheel 提供它。"
        )

    base = Path(models_dir) if models_dir else (
        Path(__file__).resolve().parents[3] / "models" / "wechat_qrcode"
    )
    needed = ["detect.prototxt", "detect.caffemodel", "sr.prototxt", "sr.caffemodel"]
    missing = [name for name in needed if not (base / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"缺少微信模型文件：{', '.join(missing)}\n"
            f"  期望目录：{base}\n"
            "  可从 opencv/opencv_3rdparty 仓库的 wechat_qrcode 分支获取这 4 个文件。"
        )

    detector = cv2.wechat_qrcode.WeChatQRCode(
        str(base / "detect.prototxt"),
        str(base / "detect.caffemodel"),
        str(base / "sr.prototxt"),
        str(base / "sr.caffemodel"),
    )
    # 0.4 是 MHY_Scanner 用的值：先降采样再检测，换速度。模型只用于小码，
    # 这里的收益是"不放过小码"，所以保守一点用 1.0，避免把码缩没了。
    detector.setScaleFactor(1.0)
    return detector


@dataclass(slots=True)
class DecodeResult:
    """一次成功解码的结果。"""

    payload: str
    method: str
    elapsed_ms: float
    scan_id: str | None = None
    #: 二维码在传入图像中的轴对齐包围盒 (x, y, w, h)，供下一帧复用
    bbox: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if self.scan_id is None:
            self.scan_id = extract_scan_id(self.payload)


@dataclass(slots=True)
class DecodeStats:
    """解码器累计统计，用于诊断「为什么一直没识别到」。"""

    frames: int = 0
    hits: int = 0
    total_ms: float = 0.0
    worst_ms: float = 0.0
    roi_frames: int = 0
    method_hits: dict[str, int] = field(default_factory=dict)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.frames if self.frames else 0.0

    def record(self, elapsed_ms: float, method: str | None, *, used_roi: bool = False) -> None:
        self.frames += 1
        self.total_ms += elapsed_ms
        self.worst_ms = max(self.worst_ms, elapsed_ms)
        if used_roi:
            self.roi_frames += 1
        if method:
            self.hits += 1
            self.method_hits[method] = self.method_hits.get(method, 0) + 1

    def summary(self) -> str:
        parts = [
            f"{self.frames} 帧",
            f"命中 {self.hits}",
            f"均 {self.avg_ms:.1f}ms",
            f"峰 {self.worst_ms:.1f}ms",
        ]
        if self.roi_frames:
            parts.append(f"RoI 复用 {self.roi_frames} 帧（{self.roi_frames / self.frames:.0%}）")
        if self.method_hits:
            detail = ", ".join(f"{k}×{v}" for k, v in sorted(self.method_hits.items()))
            parts.append(f"策略: {detail}")
        return " | ".join(parts)


def _bbox_from_position(position: Any) -> tuple[int, int, int, int] | None:
    """把 zxingcpp 的 Position 转成轴对齐包围盒。"""
    try:
        points = [
            position.top_left,
            position.top_right,
            position.bottom_right,
            position.bottom_left,
        ]
        xs = [p.x for p in points]
        ys = [p.y for p in points]
    except Exception:
        return None
    x, y = int(min(xs)), int(min(ys))
    width, height = int(max(xs)) - x, int(max(ys)) - y
    if width <= 0 or height <= 0:
        return None
    return (x, y, width, height)


def _bbox_from_points(points: Any) -> tuple[int, int, int, int] | None:
    """把 cv2 返回的角点数组转成轴对齐包围盒。"""
    if points is None:
        return None
    try:
        array = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    except Exception:
        return None
    if array.size == 0:
        return None
    xs, ys = array[:, 0], array[:, 1]
    x, y = int(np.floor(xs.min())), int(np.floor(ys.min()))
    width, height = int(np.ceil(xs.max())) - x, int(np.ceil(ys.max())) - y
    if width <= 0 or height <= 0:
        return None
    return (x, y, width, height)


class QRDecoder:
    """主力 zxing-cpp + 兜底 cv2 的二维码解码器。

    典型用法::

        decoder = QRDecoder()
        result = decoder.decode(frame_bgr)
        if result and result.scan_id:
            ...
    """

    def __init__(
        self,
        *,
        enable_roi_reuse: bool = True,
        enable_cv2_fallback: bool = False,
        try_formats: bool = False,
        enable_upscale_rescue: bool = True,
        huge_frames_fast_path: bool = True,
        wechat_models_dir: str | None = None,
    ) -> None:
        """构造解码器。

        :param enable_cv2_fallback: 是否启用 ``cv2.QRCodeDetector`` 兜底。

            **默认关闭**，这是量过才定的（1280x720 真实直播帧，即盯直播时
            绝大多数帧的状态）::

                zxing 全图一次        中位  15.6ms
                zxing 0.6x 一次       中位   6.0ms
                + cv2 兜底            中位 146~2383ms（cv2 一次调用 ~650ms）

            cv2 本来就比 zxing 更慢更弱，所以降级成"排障时手动打开"。

        :param try_formats: 是否追加一次"显式只找 QRCode"的读取。

            **默认关闭**：实测这会让每帧从 15.6ms 涨到 133.6ms（多跑一整遍
            zxing），而收益只是极少数噪声场景下的一点点稳健性。不划算。
        """
        self._zxing = _import_zxing()
        #: 可选的微信模型（默认 None = 不用）。启用后放在级联最前面。
        self._wechat: Any | None = None
        if wechat_models_dir:
            self._wechat = load_wechat_detector(wechat_models_dir)
        self._cv2_detector = cv2.QRCodeDetector() if enable_cv2_fallback else None
        if self._cv2_detector is not None:
            try:
                self._cv2_detector.setUseAlignmentMarkers(True)
            except Exception:
                pass
        self._enable_roi_reuse = enable_roi_reuse
        self._enable_cv2_fallback = enable_cv2_fallback
        self._try_formats = try_formats
        self._enable_upscale_rescue = enable_upscale_rescue
        #: 超大画面（高 DPI 屏的物理像素，> _SCREEN_PIXEL_BUDGET）是否走快路径。
        #:
        #: True（默认）：跳过"全分辨率搜索"与"放大救援"两级，无码帧约 142ms；
        #:   代价是识别下限抬到约 80 物理像素（200% 缩放屏上 = 40 逻辑像素，
        #:   真实二维码不可能这么小）。
        #: False：跑完整级联，识别下限降到 60 物理像素，但无码帧要 500~800ms。
        #:
        #: 之所以做成开关：这是**延迟与灵敏度的取舍**，取决于实际画面里码有多大，
        #: 不该由代码替你拍板。
        self._huge_fast_path = huge_frames_fast_path
        self._roi: tuple[int, int, int, int] | None = None
        self._roi_at: float = 0.0
        self.stats = DecodeStats()

    # ------------------------------------------------------------------ #
    # 后端元信息
    # ------------------------------------------------------------------ #

    @property
    def backend(self) -> str:
        if self._zxing is not None:
            return "zxing-cpp" + ("（含 cv2 兜底）" if self._cv2_detector else "")
        return "cv2.QRCodeDetector（zxing-cpp 未安装：识别率与速度都会明显下降）"

    # ------------------------------------------------------------------ #
    # 单次检测原语：返回 (payload, bbox)
    # ------------------------------------------------------------------ #

    def _zxing_read(self, image: np.ndarray, *, formats: Any = None) -> tuple[str | None, Any]:
        assert self._zxing is not None
        try:
            if formats is None:
                results = self._zxing.read_barcodes(image)
            else:
                results = self._zxing.read_barcodes(image, formats=formats)
        except Exception:
            return None, None
        for item in results or []:
            text = getattr(item, "text", None)
            if text:
                return str(text), getattr(item, "position", None)
        return None, None

    def _cv2_read(self, image: np.ndarray) -> tuple[str | None, tuple[int, int, int, int] | None]:
        """cv2 兜底。只在显式开启时才会被调用。

        注意这里**不再**额外调用 ``detectAndDecodeCurved``：两者各跑一遍
        会把成本翻倍，而弯曲码在"直播里播一个登录二维码"这个场景中并不存在。
        """
        if self._cv2_detector is None:
            return None, None
        try:
            payload, points, _straight = self._cv2_detector.detectAndDecode(image)
            if payload:
                return str(payload), _bbox_from_points(points)
        except Exception:
            pass
        return None, None

    def _read(self, image: np.ndarray) -> tuple[str | None, str | None, Any]:
        """在给定图像上尝试所有已启用的后端。返回 (payload, method, raw_bbox)。"""
        # 微信模型放最前面（它最慢，但用户显式开启说明优先要灵敏度）。
        # 它不返回角点，所以没有 bbox 可用于 RoI 复用。
        if self._wechat is not None:  # pragma: no cover - 需模型文件
            try:
                found = self._wechat.detectAndDecode(image)
                if found and found[0]:
                    return str(found[0]), "wechat", None
            except Exception:
                pass

        if self._zxing is not None:
            payload, position = self._zxing_read(image)
            if payload:
                return payload, "zxing", _bbox_from_position(position)
            if self._try_formats:
                # 显式限定"只找二维码"，在画面元素复杂时更稳
                payload, position = self._zxing_read(
                    image, formats=self._zxing.BarcodeFormat.QRCode
                )
                if payload:
                    return payload, "zxing-qr", _bbox_from_position(position)

        if self._enable_cv2_fallback:
            payload, bbox = self._cv2_read(image)
            if payload:
                return payload, "cv2", bbox
        return None, None, None

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #

    def decode(self, frame_bgr: np.ndarray) -> DecodeResult | None:
        """对一帧做解码尝试。命中返回 :class:`DecodeResult`，否则 None。

        级联顺序是按实测成本排的：RoI（~1ms）→ 0.6x（~6ms）→ 全分辨率（~16ms）
        → 放大救援（~100ms+）。绝大多数帧会停在第二、三级，最贵的救援级
        只在前面全落空时才付。
        """
        if frame_bgr is None or frame_bgr.size == 0:
            return None
        started = time.perf_counter()

        # ① RoI 复用：二维码一旦出现通常停留多帧，裁剪后成本几乎可忽略
        if self._enable_roi_reuse and self._roi is not None:
            if time.time() - self._roi_at <= _ROI_TTL_S:
                crop = _crop(frame_bgr, self._roi)
                if crop is not None:
                    payload, method, bbox = self._read(crop)
                    if payload:
                        return self._finish(
                            payload,
                            f"{method}+roi" if method else "roi",
                            started,
                            _offset_bbox(bbox, self._roi),
                            used_roi=True,
                        )
            else:
                self._roi = None

        gray = frame_bgr if frame_bgr.ndim == 2 else cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        shape = frame_bgr.shape

        # ② 缩一档先试：zxing 的检测成本随**面积**增长，缩小后快得多，
        #    而现代屏幕上二维码的像素余量通常很大。
        #    这一级同时覆盖"中等分辨率"与"高 DPI 物理像素"两种情况。
        if gray.shape[0] > 720:
            small = cv2.resize(
                gray, None, fx=_ROI_HINT_SCALE, fy=_ROI_HINT_SCALE, interpolation=cv2.INTER_AREA
            )
            payload, method, bbox = self._read(small)
            if payload:
                restored = _scale_bbox(bbox, 1 / _ROI_HINT_SCALE)
                if restored is not None:
                    self._remember_roi(restored, shape)
                return self._finish(
                    payload, f"{method}+downscale", started, restored, used_roi=False
                )

        # ③ 原始分辨率：小码只有在这一级（或下一级）才认得出来。
        #
        #    对**超大画面**（高 DPI 屏的物理像素）刻意跳过这一级：单次全图搜索
        #    在这尺寸上要 ~400ms，而它相对 0.6x 只多认出"60~80 物理像素"的码
        #    —— 在 200% 缩放屏上那是 30~40 逻辑像素，真实二维码不可能这么小。
        #    跳过它能把无码帧（最常见状态）从 545ms 降到 142ms，约 4 倍帧率。
        #    小码识别能力由下一级的"原图放大救援"补回（它只搜中间带，便宜得多）。
        huge = self._huge_fast_path and shape[0] * shape[1] > _SCREEN_PIXEL_BUDGET
        if not huge:
            payload, method, bbox = self._read(frame_bgr)
            if payload:
                if bbox is not None:
                    self._remember_roi(bbox, shape)
                return self._finish(payload, method, started, bbox, used_roi=False)

        # ④ 放大救援：码只占几十像素时，放大后 zxing 仍有机会。最贵，放最后。
        #
        #    这里刻意**只放大画面中间的条带**，而不是整幅图。实测直接对
        #    1280x720 整帧做 2x/3x 放大是 ~120ms（典型直播码就挂在画面正中，
        #    放大整帧纯属浪费）；只放大中间 50% 后成本降一个数量级，
        #    而命中率几乎不变。
        # 放大救援对超大画面代价过高（3x 会造出 5000x3000 级别的图，单次就近 400ms），
        # 而且高 DPI 屏上码本来就偏大、不需要放大。所以只在中小画面上启用。
        if (
            self._enable_upscale_rescue
            and gray.shape[0] >= 240
            and gray.shape[1] >= 240
            # 快路径下跳过放大救援：它在这个尺寸上单次就近 400ms，
            # 而高 DPI 屏的码本来就大，不需要放大。
            and not huge
        ):
            band, offset = _center_band(gray, ratio=_RESCUE_BAND_RATIO)
            for factor, tag in ((2.0, "up2x"), (3.0, "up3x")):
                big = cv2.resize(band, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
                payload, method, bbox_up = self._read(big)
                if payload:
                    restored = _scale_bbox(bbox_up, 1 / factor)
                    if restored is not None:
                        restored = (restored[0] + offset[0], restored[1] + offset[1],
                                    restored[2], restored[3])
                        self._remember_roi(restored, shape)
                    return self._finish(
                        payload, f"{method}+{tag}", started, restored, used_roi=False
                    )

        return self._finish(None, None, started, None, used_roi=False)

    def _finish(
        self,
        payload: str | None,
        method: str | None,
        started: float,
        bbox: tuple[int, int, int, int] | None,
        *,
        used_roi: bool,
    ) -> DecodeResult | None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.stats.record(elapsed_ms, method, used_roi=used_roi)
        if payload is None:
            return None
        return DecodeResult(payload=payload, method=method or "?", elapsed_ms=elapsed_ms, bbox=bbox)

    def _remember_roi(self, bbox: tuple[int, int, int, int], shape: tuple[int, ...]) -> None:
        """记下（外扩后的）二维码区域，供下一帧优先搜索。"""
        self._roi = _expand_bbox(bbox, shape)
        self._roi_at = time.time()

    def reset(self) -> None:
        """清掉 RoI 记忆（例如换了帧源）。"""
        self._roi = None
        self._roi_at = 0.0


# --------------------------------------------------------------------------- #
# 几何工具
# --------------------------------------------------------------------------- #


def _crop(image: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray | None:
    x, y, width, height = bbox
    image_h, image_w = image.shape[:2]
    left = max(0, min(x, image_w - 1))
    top = max(0, min(y, image_h - 1))
    right = max(left + 1, min(x + width, image_w))
    bottom = max(top + 1, min(y + height, image_h))
    crop = image[top:bottom, left:right]
    return crop if crop.size else None


def _center_band(
    image: np.ndarray, *, ratio: float = _RESCUE_BAND_RATIO
) -> tuple[np.ndarray, tuple[int, int]]:
    """取出画面中间的条带，返回 (条带, 相对原图的左上角偏移)。

    抢码场景里二维码基本都出现在画面中部（登录弹窗在正中），
    所以放大救援没必要为整幅画面买单。
    """
    height, width = image.shape[:2]
    band_h = max(1, int(height * ratio))
    band_w = max(1, int(width * ratio))
    top = (height - band_h) // 2
    left = (width - band_w) // 2
    return image[top : top + band_h, left : left + band_w], (left, top)


def _expand_bbox(
    bbox: tuple[int, int, int, int], shape: tuple[int, ...], pad_ratio: float = _ROI_PAD_RATIO
) -> tuple[int, int, int, int]:
    """把包围盒按比例外扩并夹到图像范围内。"""
    x, y, width, height = bbox
    image_h, image_w = shape[:2]
    pad_x = int(width * pad_ratio)
    pad_y = int(height * pad_ratio)
    left = max(0, x - pad_x)
    top = max(0, y - pad_y)
    right = min(image_w, x + width + pad_x)
    bottom = min(image_h, y + height + pad_y)
    return (left, top, max(1, right - left), max(1, bottom - top))


def _offset_bbox(
    bbox: tuple[int, int, int, int] | None, origin: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    """把 RoI 内的坐标换算回全图坐标。"""
    if bbox is None:
        return None
    x, y, width, height = bbox
    return (x + origin[0], y + origin[1], width, height)


def _scale_bbox(
    bbox: tuple[int, int, int, int] | None, factor: float
) -> tuple[int, int, int, int] | None:
    """把缩放图上测得的包围盒还原到原图尺度。"""
    if bbox is None:
        return None
    x, y, width, height = bbox
    return (
        int(x * factor),
        int(y * factor),
        max(1, int(width * factor)),
        max(1, int(height * factor)),
    )


__all__ = ["DecodeResult", "DecodeStats", "QRDecoder"]
