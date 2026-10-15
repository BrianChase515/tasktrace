"""Plain logic behind the menu-bar app: no macOS imports, so it can be tested anywhere."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_URL = "https://github.com/BrianChase515/tasktrace"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_reply(text: str) -> dict:
    """Pull one JSON object out of whatever an AI pasted back (code fences, chatter around it)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("The clipboard is empty. Copy the AI's reply first.")
    for candidate in [m.group(1) for m in _FENCE.finditer(text)] + [text]:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            obj = json.loads(candidate[start:end + 1])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("Couldn't find a plan in the clipboard. Copy the AI's whole reply, including the { ... } part.")


def trace_for_ai(trace: dict) -> str:
    slim = json.loads(json.dumps(trace))
    for s in slim.get("steps", []):
        (s.get("target") or {}).pop("thumbnail", None)
    return (
        "Here is a recording of a task I did on my Mac, captured with TaskTrace "
        f"({REPO_URL}). Read `outline` first, then `steps`. Each step lists what I clicked "
        "(accessibility element, on-screen text, position), what it caused (`effect`), and my own notes "
        "(`intent`). Values I typed that may change between runs are listed in `parameters`.\n\n"
        + json.dumps(slim, ensure_ascii=False)
    )


def run_label(row: dict, width: int = 42) -> str:
    name = row.get("goal") or row.get("run", "task")
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", row.get("run", ""))
    when = f"{m.group(2)}/{m.group(3)} {m.group(4)}:{m.group(5)}" if m else ""
    name = name if len(name) <= width else name[: width - 1] + "…"
    return f"{name}  ({when})" if when else name


def plan_dir_for(plan: dict, home: Path) -> Path:
    """Keep a plan next to the recording it came from, so audits and reviews sit together."""
    src = str(plan.get("source_trace") or "")
    if src and "/" not in src and "\\" not in src and src not in (".", "..") and (home / src).is_dir():
        return home / src / "plans"
    return home / "plans"


def plan_filename(plan: dict) -> str:
    pid = re.sub(r"[^A-Za-z0-9_-]+", "-", str(plan.get("plan_id") or "plan"))[:60]
    return f"{pid}-v{plan.get('version', 1)}.json"


VERDICT_TITLES = {
    "pass": "Done. Everything worked.",
    "revise": "Finished, with problems to fix",
    "re_record": "The app has changed since you recorded this",
    "tool_bug": "TaskTrace hit a bug",
    "dry_run": "Check complete",
}


def result_summary(audit: dict, review: dict, max_items: int = 5) -> tuple[str, str, bool]:
    """(title, body, offer_fix) for the end-of-run dialog, in plain words."""
    verdict = review.get("verdict", "revise")
    title = VERDICT_TITLES.get(verdict, "Finished")
    outcomes = (audit.get("summary") or {}).get("outcomes", {})
    if verdict == "dry_run":
        missing = outcomes.get("locator_failed", 0)
        body = ("Every step's target was found on screen." if not missing
                else f"{missing} step(s) couldn't find their target on screen right now.")
    else:
        done = outcomes.get("ok", 0) + outcomes.get("handed_to_human", 0)
        body = f"{done} of {len(audit.get('entries', []))} steps completed."
    serious = [f for f in review.get("findings", []) if f["severity"] in ("high", "medium")]
    lines = []
    for f in serious[:max_items]:
        line = f"• Step {f['step']}: {f['summary']}"
        if f.get("human_comment"):
            line += f'  You said: "{f["human_comment"]}"'
        lines.append(line)
    if lines:
        body += "\n\n" + "\n".join(lines)
    offer_fix = any(f["route_to"] == "author" for f in serious)
    if offer_fix:
        body += ("\n\nCopy the fix request and paste it into your AI. It will send back a corrected plan; "
                 "copy that and choose \"Run a plan from clipboard\".")
    elif verdict == "re_record":
        body += "\n\nRecording the task again is the quickest fix."
    elif verdict == "tool_bug":
        body += f"\n\nPlease report it at {REPO_URL}/issues"
    return title, body, offer_fix
