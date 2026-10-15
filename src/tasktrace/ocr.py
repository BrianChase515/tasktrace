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


def pick_label(items: list[dict], min_conf: float = 0.3) -> str | None:
    """Choose the text a click was aimed at from OCR results of the crop around it.

    Prefers text under the click point, then text nearest to it. Text running off the edge of
    the crop is probably cut off ("Submit applicatior"), so it only wins when nothing else is close.
    """
    best, best_score = None, 99.0
    for o in items:
        text = o["text"].strip()
        if o["conf"] < min_conf or not text:
            continue
        x, y, w, h = o["box"]
        cx, cy = x + w / 2, y + h / 2
        score = (cx - 0.5) ** 2 + 4 * (cy - 0.5) ** 2  # vertical distance matters more: lines are wide
        if x <= 0.5 <= x + w and y <= 0.5 <= y + h:
            score -= 1.0  # the click landed on this text
        if x < 0.01 or x + w > 0.99:
            score += 0.5  # clipped by the crop edge
        if score < best_score:
            best, best_score = text, score
    return best[:80] if best else None


def label_near_center(path: Path, min_conf: float = 0.3) -> str | None:
    """The text a click crop was centered on."""
    return pick_label(ocr_image(path), min_conf)


def read_lines(path: Path, limit: int = 40) -> list[str]:
    items = sorted(ocr_image(path), key=lambda o: (round(o["box"][1], 2), o["box"][0]))
    return [o["text"].strip() for o in items if o["text"].strip()][:limit]
