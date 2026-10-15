"""On-device OCR with Apple's Vision framework. Returns nothing on other platforms."""

from __future__ import annotations

import sys
from pathlib import Path


def ocr_image(path: Path) -> list[dict]:
    """Text found in an image: [{"text", "conf", "box": [x, y, w, h]}], box normalized, top-left origin."""
    if sys.platform != "darwin" or not Path(path).exists():
        return []
    try:
        import Vision
        from Foundation import NSURL
    except ImportError:
        return []
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    ok, _err = handler.performRequests_error_([req], None)
    if not ok:
        return []
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand, bb = cands[0], obs.boundingBox()  # Vision boxes have a bottom-left origin
        out.append({
            "text": str(cand.string()),
            "conf": float(cand.confidence()),
            "box": [bb.origin.x, 1 - bb.origin.y - bb.size.height, bb.size.width, bb.size.height],
        })
    return out


def label_near_center(path: Path, min_conf: float = 0.3) -> str | None:
    """The text closest to the middle of a click crop, which is where the click landed."""
    best, best_d = None, 9.0
    for o in ocr_image(path):
        if o["conf"] < min_conf or not o["text"].strip():
            continue
        x, y, w, h = o["box"]
        d = (x + w / 2 - 0.5) ** 2 + (y + h / 2 - 0.5) ** 2
        if d < best_d:
            best, best_d = o["text"].strip(), d
    return best[:80] if best else None


def read_lines(path: Path, limit: int = 40) -> list[str]:
    items = sorted(ocr_image(path), key=lambda o: (round(o["box"][1], 2), o["box"][0]))
    return [o["text"].strip() for o in items if o["text"].strip()][:limit]
