"""Compare two recordings of the same task to tell variables from constants.

One run can only guess which typed values change each time. Two runs settle it:
values that differ are confirmed parameters; values that match are constants;
steps that appear in only one run are optional or conditional.
"""

from __future__ import annotations

import copy
import difflib

from .compile import element_label, refresh_text


def signature(step: dict) -> str:
    tgt = step.get("target") or {}
    el = tgt.get("element") or {}
    label = element_label(el, tgt.get("ocr_label")) or ""
    return f"{step['action']}|{el.get('role', '')}|{label}|{step.get('keys', '')}"


def diff_traces(a: dict, b: dict) -> dict:
    sa, sb = [signature(s) for s in a["steps"]], [signature(s) for s in b["steps"]]
    sm = difflib.SequenceMatcher(a=sa, b=sb, autojunk=False)
    pairs = [(blk.a + k, blk.b + k) for blk in sm.get_matching_blocks() for k in range(blk.size)]
    variables, constants = [], []
    for i, j in pairs:
        s, t = a["steps"][i], b["steps"][j]
        if s["action"] != "type" or s.get("secret"):
            continue
        entry = {"step": s["id"], "param": s.get("param")}
        if s.get("text") != t.get("text"):
            variables.append({**entry, "values": [s.get("text", ""), t.get("text", "")]})
        else:
            constants.append({**entry, "value": s.get("text", "")})
    ma, mb = {i for i, _ in pairs}, {j for _, j in pairs}
    return {
        "aligned_steps": len(pairs),
        "steps_a": len(sa),
        "steps_b": len(sb),
        "similarity": round(sm.ratio(), 3),
        "variables": variables,
        "constants": constants,
        "only_in_a": [a["steps"][i]["id"] for i in range(len(sa)) if i not in ma],
        "only_in_b": [b["steps"][j]["id"] for j in range(len(sb)) if j not in mb],
    }


def apply_diff(a: dict, report: dict) -> dict:
    """Return a copy of trace `a` updated with what the comparison learned."""
    t = copy.deepcopy(a)
    var = {v["step"]: v for v in report["variables"]}
    const = {c["step"] for c in report["constants"]}
    steps = {s["id"]: s for s in t["steps"]}
    params = []
    for p in t.get("parameters", []):
        if p["step"] in var:
            p["status"] = "confirmed"
            p["seen"] = var[p["step"]]["values"]
            params.append(p)
        elif p["step"] in const:
            st = steps[p["step"]]
            st.pop("param", None)
            st["constant"] = True
        else:
            params.append(p)
    t["parameters"] = params
    for sid in report["only_in_a"]:
        steps[sid]["optional"] = True
    task = t.setdefault("task", {})
    task["runs_compared"] = task.get("runs_compared", 1) + 1
    refresh_text(t)
    return t
