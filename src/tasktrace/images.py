"""Optional small thumbnails embedded in the trace (needs Pillow)."""

from __future__ import annotations

import base64
import io
from pathlib import Path


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
