"""Scrub secrets from every string in a trace before it is written."""

from __future__ import annotations

import re

_PATTERNS = [
    ("api_key", re.compile(
        r"(?:sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_\-]{35})")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# File references and image data are never rewritten.
SKIP_KEYS = {"crop", "screenshot", "thumbnail"}


def luhn_ok(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class Redactor:
    def __init__(self, emails: bool = False):
        self.emails = emails
        self.counts: dict[str, int] = {}

    def _hit(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f"[REDACTED:{kind}]"

    def scrub(self, s: str) -> str:
        for kind, pat in _PATTERNS:
            s = pat.sub(lambda m, k=kind: self._hit(k), s)

        def card(m: re.Match) -> str:
            return self._hit("card") if luhn_ok(re.sub(r"\D", "", m.group())) else m.group()

        s = _CARD.sub(card, s)
        if self.emails:
            s = _EMAIL.sub(lambda m: self._hit("email"), s)
        return s

    def walk(self, obj, key: str | None = None):
        if isinstance(obj, str):
            return obj if key in SKIP_KEYS else self.scrub(obj)
        if isinstance(obj, dict):
            return {k: self.walk(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        return obj

    @property
    def total(self) -> int:
        return sum(self.counts.values())
