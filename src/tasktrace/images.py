"""Click crop geometry, and optional small thumbnails embedded in the trace (needs Pillow)."""

from __future__ import annotations

import base64
import io
from pathlib import Path

# A crop is read by OCR, so it has to contain whole words. Centring a fixed box on the
# pointer slices the label it landed on ("asy Apply"); the element's own bounds don't.
CROP_PAD_X, CROP_PAD_Y = 15, 8
CROP_MIN_W, CROP_MIN_H = 240, 72
CROP_MAX_W, CROP_MAX_H = 900, 260


def _axis(click: float, start: float, size: float, pad: float, lo_min: float, lo_max: float) -> tuple[float, float]:
    lo, hi = start - pad, start + size + pad
    if hi - lo < lo_min:  # a small control: widen around it
        mid = (lo + hi) / 2
        lo, hi = mid - lo_min / 2, mid + lo_min / 2
    if hi - lo > lo_max:  # a huge container: keep the part the pointer is in
        length = lo_max
        mid = min(max(click, lo + length / 2), hi - length / 2)
        lo, hi = mid - length / 2, mid + length / 2
    if not lo + 8 <= click <= hi - 8:  # the pointer must always be inside its own crop
        shift = (click - 8) - lo if click < lo + 8 else (click + 8) - hi
        lo, hi = lo + shift, hi + shift
    return lo, hi - lo


def crop_rect(x: int, y: int, frame: list | None = None) -> list[int]:
    """The screen region to capture for a click, as [x, y, w, h].

    Uses the target element's bounding rect (AXPosition + AXSize) padded out, so OCR reads
    complete tokens. Falls back to a box centred on the pointer when the element has no size.
    """
    if frame and len(frame) >= 4 and frame[2] > 0 and frame[3] > 0:
        fx, fy, fw, fh = frame[:4]
        left, w = _axis(x, fx, fw, CROP_PAD_X, CROP_MIN_W, CROP_MAX_W)
        top, h = _axis(y, fy, fh, CROP_PAD_Y, CROP_MIN_H, CROP_MAX_H)
    else:
        w, h = CROP_MIN_W * 1.5, CROP_MIN_H * 1.5
        left, top = x - w / 2, y - h / 2
    return [max(0, int(round(left))), max(0, int(round(top))), int(round(w)), int(round(h))]


def point_in_crop(x: int, y: int, rect: list | None) -> tuple[float, float]:
    """Where the pointer sits inside its crop, as fractions. (0.5, 0.5) when unknown."""
    if not rect or len(rect) < 4 or rect[2] <= 0 or rect[3] <= 0:
        return 0.5, 0.5
    return (min(1.0, max(0.0, (x - rect[0]) / rect[2])),
            min(1.0, max(0.0, (y - rect[1]) / rect[3])))


def thumbnail_b64(path: Path, max_w: int = 240) -> str | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    if not Path(path).exists():
        return None
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > max_w:
            im.thumbnail((max_w, max_w * 4))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
