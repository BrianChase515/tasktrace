"""Where runs live: $TASKTRACE_HOME, default ~/TaskTraces."""

from __future__ import annotations

import json
import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("TASKTRACE_HOME", "~/TaskTraces")).expanduser()


def resolve_trace(ref: str) -> Path:
    """Accept a trace.json path, a run folder, or a run folder name under home()."""
    p = Path(ref).expanduser()
    for cand in (p, p / "trace.json", home() / ref / "trace.json"):
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"No trace found for {ref!r} (looked in {home()})")


def list_runs(limit: int = 50) -> list[dict]:
    root = home()
    if not root.is_dir():
        return []
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:limit]:
        tp = d / "trace.json"
        row = {"run": d.name, "compiled": tp.is_file()}
        if tp.is_file():
            try:
                t = json.loads(tp.read_text(encoding="utf-8"))
                row.update(goal=t.get("task", {}).get("goal"), steps=len(t.get("steps", [])),
                           parameters=[p["name"] for p in t.get("parameters", [])])
            except (OSError, ValueError):
                row["error"] = "unreadable trace.json"
        out.append(row)
    return out
