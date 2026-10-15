"""Plans: what an executor should do, written by a tool, an AI, or a person.

A trace records what *happened*. A plan says what *should happen next time*,
with parameter values filled in. Every plan carries its lineage, so an audit
can always answer "who generated this, from what, and why did it change":

    author      who wrote this version (tool, AI model, or human)
    version     1, 2, 3...
    parent      the plan_id@version it was revised from
    changelog   one entry per change, each pointing at the review finding it addresses
"""

from __future__ import annotations

import datetime as dt
import re

from .compile import element_label

PLAN_SCHEMA = "tasktrace-plan/0.1"
CLICKS = {"click", "double_click", "triple_click", "right_click", "drag"}
TARGETED = CLICKS | {"type", "scroll"}
# form_loop and pick_list_item are not recorded actions: they are what an author writes when the
# page cannot be known in advance. form_loop reads each page of a form and fills it from the
# answer bank, asking about anything new; pick_list_item chooses a row by what it says.
ACTIONS = TARGETED | {"key", "hotkey", "open_app", "ask", "form_loop", "pick_list_item"}
LIVE = {"form_loop", "pick_list_item"}

# Buttons whose label suggests the step changes something that's hard to undo.
CONSEQUENTIAL = re.compile(
    r"\b(delete|remove|submit|send|pay|purchase|buy|order|confirm|approve|publish|transfer|"
    r"sign|create|save|post|apply|deploy|merge|close account)\b", re.I)
CONSEQUENTIAL_KEYS = {"cmd+s", "cmd+return", "cmd+enter", "cmd+backspace", "cmd+delete"}
PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "task").lower()).strip("-")[:40] or "task"


def step_label(step: dict) -> str | None:
    tgt = step.get("target") or {}
    return element_label(tgt.get("element") or {}, tgt.get("ocr_label"))


def is_consequential(step: dict) -> bool:
    if step.get("action") in CLICKS:
        label = step_label(step)
        return bool(label and CONSEQUENTIAL.search(label))
    if step.get("action") == "hotkey":
        return step.get("keys") in CONSEQUENTIAL_KEYS
    return False


def make_plan(trace: dict, values: dict | None = None, author: dict | None = None,
              source: str | None = None) -> dict:
    """Deterministically turn a trace into a version-1 plan (no AI involved)."""
    values = dict(values or {})
    params = {p["name"]: p for p in trace.get("parameters", [])}
    steps = []
    for s in trace.get("steps", []):
        st: dict = {
            "id": len(steps) + 1,
            "from_step": s["id"],
            "action": s["action"],
            "app": s.get("app"),
            "window": s.get("window"),
            "target": {k: v for k, v in (s.get("target") or {}).items() if k != "thumbnail"},
        }
        if s["action"] == "type":
            if s.get("secret"):
                st["secret"] = True
                st["ask"] = step_label(s) or "secret value"
            elif s.get("param"):
                st["text"] = "{" + s["param"] + "}"
            else:
                st["text"] = s.get("text", "")
        for k in ("keys", "times", "scroll", "optional"):
            if k in s:
                st[k] = s[k]
        eff = s.get("effect") or {}
        expect = {k: eff[k] for k in ("opened", "closed", "retitled") if eff.get(k)}
        if expect:
            st["expect"] = expect
        if s.get("intent"):
            st["note"] = s["intent"]
        if is_consequential(s):
            st["consequential"] = True
        st["describe"] = s.get("describe", "")
        steps.append({k: v for k, v in st.items() if v not in (None, "", {}, [])})

    name = trace.get("task", {}).get("name") or "task"
    plan_values = {n: values.get(n) for n, p in params.items() if p["kind"] != "secret"}
    plan = {
        "schema": PLAN_SCHEMA,
        "plan_id": _slug(name),
        "version": 1,
        "parent": None,
        "source_trace": source or name,
        "goal": trace.get("task", {}).get("goal"),
        "author": author or {"kind": "tool", "name": "tasktrace plan"},
        "created_at": _now(),
        "values": plan_values,
        "steps": steps,
        "done_when": {k: v for k, v in (trace.get("success_criteria") or {}).items()
                      if k in ("final_app", "final_window", "visible_text")},
        "changelog": [],
    }
    return plan


def missing_values(plan: dict) -> list[str]:
    needed = set()
    for st in plan.get("steps", []):
        needed.update(PLACEHOLDER.findall(st.get("text", "")))
    return sorted(n for n in needed if not (plan.get("values") or {}).get(n))


def fill(text: str, values: dict) -> str:
    return PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def validate_plan(plan: dict) -> list[str]:
    """Rule checks on top of the JSON schema. Returns a list of problems (empty = OK)."""
    errs = []
    if plan.get("schema") != PLAN_SCHEMA:
        errs.append(f"schema must be {PLAN_SCHEMA!r}")
    author = plan.get("author") or {}
    if not author.get("name") or author.get("kind") not in ("ai", "human", "tool"):
        errs.append("author needs kind (ai|human|tool) and name, so audits know who generated it")
    v = plan.get("version")
    if not isinstance(v, int) or v < 1:
        errs.append("version must be an integer >= 1")
    elif v > 1:
        if not plan.get("parent"):
            errs.append("a revised plan (version > 1) must name its parent")
        if not plan.get("changelog"):
            errs.append("a revised plan must say what changed in changelog")
    ids = [s.get("id") for s in plan.get("steps", [])]
    if ids != list(range(1, len(ids) + 1)):
        errs.append("step ids must be 1..n in order")
    for s in plan.get("steps", []):
        a = s.get("action")
        if a not in ACTIONS:
            errs.append(f"step {s.get('id')}: unknown action {a!r}")
        if a == "open_app" and not s.get("app"):
            errs.append(f"step {s.get('id')}: open_app needs app")
        if a == "ask" and not (s.get("question") and s.get("param")):
            errs.append(f"step {s.get('id')}: ask needs a question and a param to store the answer in")
        if a in CLICKS and not (s.get("target") or {}):
            errs.append(f"step {s.get('id')}: {a} needs a target")
        if a == "pick_list_item" and not (s.get("require_any") or s.get("press")):
            errs.append(f"step {s.get('id')}: pick_list_item needs require_any (what the row must say) "
                        "and/or press (the control to press in it)")
        if a == "form_loop" and not (s.get("finish") or s.get("advance")):
            errs.append(f"step {s.get('id')}: form_loop needs finish (the button that ends the form) "
                        "or advance (the button that moves to the next page)")
        if a in ("key", "hotkey") and not s.get("keys"):
            errs.append(f"step {s.get('id')}: {a} needs keys")
        if a == "type" and not s.get("secret") and "text" not in s:
            errs.append(f"step {s.get('id')}: type needs text (or secret: true)")
        if s.get("secret") and s.get("text"):
            errs.append(f"step {s.get('id')}: secret steps must not contain text")
    return errs
