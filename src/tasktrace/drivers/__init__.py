"""Drivers: the only part of replay that touches the operating system.

The executor decides *what* to do and *whether* it worked; a driver knows *how*
to find things on screen and press them. That split keeps the executor testable
(see drivers/fake.py) and leaves room for a Windows driver later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Match:
    strategy: str  # element | ocr | rel_xy | abs_xy
    x: int | None = None
    y: int | None = None
    handle: Any = None
    detail: dict = field(default_factory=dict)

    def public(self) -> dict:
        out = {"strategy": self.strategy}
        if self.x is not None:
            out["xy"] = [self.x, self.y]
        out.update(self.detail)
        return out


class Driver:
    name = "driver"

    def prepare(self, step: dict) -> None:
        """Bring the step's app to the front."""

    def find_element(self, step: dict) -> Match | None:
        return None

    def find_text(self, label: str, step: dict) -> Match | None:
        return None

    def window_bounds(self, step: dict) -> list[int] | None:
        return None

    def act(self, step: dict, match: Match | None, text: str | None = None) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        """{"front_app": str, "windows": [{"id", "title", "app"}]}"""
        return {"front_app": None, "windows": []}

    def screenshot(self, path: Path) -> bool:
        return False


def get_driver(name: str = "auto") -> Driver:
    import sys

    if name == "fake":
        from .fake import FakeDriver
        return FakeDriver()
    if name in ("auto", "macos"):
        if sys.platform != "darwin":
            raise RuntimeError("Replay needs macOS (use --dry-run with --driver fake to try the loop elsewhere).")
        from .macos import MacDriver
        return MacDriver()
    raise ValueError(f"unknown driver {name!r}")
