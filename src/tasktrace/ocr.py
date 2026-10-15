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


def pick_label(items: list[dict], min_conf: float = 0.3,
               point: tuple[float, float] = (0.5, 0.5)) -> str | None:
    """Choose the text a click was aimed at from OCR results of the crop around it.

    `point` is where the pointer sits inside the crop, as fractions. Crops are cut to the
    target element's bounds rather than centred on the pointer, so the pointer is usually
    off-centre. Prefers text under the pointer, then text nearest to it. Text running off the
    edge of the crop is probably cut off ("Submit applicatior"), so it only wins when nothing
    else is close.
    """
    px, py = point
    best, best_box, best_score = None, None, 99.0
    for o in items:
        text = o["text"].strip()
        if o["conf"] < min_conf or not text:
            continue
        x, y, w, h = o["box"]
        cx, cy = x + w / 2, y + h / 2
        score = (cx - px) ** 2 + 4 * (cy - py) ** 2  # vertical distance matters more: lines are wide
        if x <= px <= x + w and y <= py <= y + h:
            score -= 1.0  # the click landed on this text
        if any(_clipped(o["box"])):
            score += 1.2  # cut off by the crop edge: only wins if nothing whole is near
        if score < best_score:
            best, best_box, best_score = text, o["box"], score
    best = trim_clipped(best, best_box)
    return best[:80] if best else None


def _clipped(box) -> tuple[bool, bool]:
    x, _y, w, _h = box
    return x < 0.01, x + w > 0.99


def trim_clipped(text: str | None, box) -> str | None:
    """Drop words the crop sliced in half, rather than reporting them as real text.

    A crop can only be cut to the element's bounds when the element has sensible bounds; clicking
    a large region still slices whatever sits at the edge, and half a word ("edin profile.",
    "Winston-Sale") is worse than no label at all, because it reads like a real one.
    """
    if not text or box is None:
        return text
    left, right = _clipped(box)
    if not (left or right):
        return text
    words = text.split()
    if left:
        words = words[1:]
    if right and words:
        words = words[:-1]
    return " ".join(words) or None


def label_near_point(path: Path, point: tuple[float, float] = (0.5, 0.5),
                     min_conf: float = 0.3) -> str | None:
    """The text at a given spot in a click crop."""
    return pick_label(ocr_image(path), min_conf, point)


def label_near_center(path: Path, min_conf: float = 0.3) -> str | None:
    """The text a click crop was centered on."""
    return pick_label(ocr_image(path), min_conf)


def read_lines(path: Path, limit: int = 40) -> list[str]:
    items = sorted(ocr_image(path), key=lambda o: (round(o["box"][1], 2), o["box"][0]))
    return [o["text"].strip() for o in items if o["text"].strip()][:limit]
