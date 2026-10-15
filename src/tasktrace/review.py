"""Triage an audit: classify every problem and route it to whoever can fix it.

Routes:
    author    the plan is wrong (target renamed, wrong value, missing step); an AI revises the plan
    executor  tasktrace itself misbehaved; a bug report, not a plan change
    human     only the person can resolve it (a secret, a value nobody gave, a judgment call)
    trace     the recording no longer matches the app; record the task again

The deterministic triage runs first so the reviewing AI starts from facts, not vibes.
"""

from __future__ import annotations

import datetime as dt

REVIEW_SCHEMA = "tasktrace-review/0.1"

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def triage(audit: dict, plan: dict | None = None) -> dict:
    findings: list[dict] = []
    plan_steps = {s["id"]: s for s in (plan or {}).get("steps", [])}

    def add(entry: dict, category: str, route: str, severity: str, summary: str, **extra):
        findings.append({
            "id": f"F{len(findings) + 1}",
            "step": entry["step"],
            "category": category,
            "route_to": route,
            "severity": severity,
            "summary": summary,
            **{k: v for k, v in extra.items() if v not in (None, "", [], {})},
        })

    for e in audit.get("entries", []):
        out = e.get("outcome")
        human = e.get("human") or {}
        attempt = e.get("attempt") or {}
        strategy = (attempt.get("match") or {}).get("strategy")
        step = plan_steps.get(e["step"], {})

        if human.get("verdict") == "rejected":
            add(e, "human_correction", "author", "high",
                "The person said this step was wrong.", human_comment=human.get("comment"),
                text_used=e.get("text_used"))
        if out == "locator_failed":
            repeated = attempt.get("n", 1) > 1
            add(e, "locator", "trace" if repeated else "author", "high",
                "Couldn't find the target with any locator (" + ", ".join(attempt.get("tried") or ["none"]) + "). "
                "The element was probably renamed, moved, or is on a different screen."
                + (" Retries failed too; re-recording this part is likely faster." if repeated else ""),
                target=step.get("target", {}).get("element"))
        elif out == "effect_mismatch":
            obs = e.get("observed") or {}
            why = ("Nothing changed on screen: the action may not have registered, or the app was slow."
                   if not obs else "Something different happened than in the recording: the app took another path.")
            add(e, "effect", "author", "high", why, expected=e.get("expected"), observed=obs)
        elif out == "error":
            add(e, "executor_error", "executor", "high", "tasktrace raised an error running this step.",
                error=e.get("error"))
        elif out == "handed_to_human" and e.get("handoff", "").startswith("secret"):
            pass  # by design: secrets are always typed by a person
        elif out == "handed_to_human":
            add(e, "manual_takeover", "author", "medium",
                f"The person did this step by hand ({e.get('handoff')}). Find out what the executor got wrong.")
        elif out == "skipped":
            add(e, "skipped", "human", "low", "The person skipped this step. Is it still needed?")
        elif out == "stopped":
            add(e, "stopped", "human", "medium", "The person stopped the run here.")

        if out == "ok" and strategy in ("rel_xy", "abs_xy"):
            add(e, "fragile_locator", "author", "low",
                f"Worked only by {strategy}. If the window moves or resizes this will break; "
                "add a better locator (element title or visible text).",
                matched=attempt.get("match"))
        if out == "ok" and strategy == "ocr" and (attempt.get("match") or {}).get("exact") is False:
            add(e, "fuzzy_text_match", "author", "low",
                "Matched by partial on-screen text. Check the label is still right.", matched=attempt.get("match"))

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["step"]))
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i}"

    blocking = [f for f in findings if f["severity"] in ("high", "medium")]
    routes = sorted({f["route_to"] for f in blocking})
    if audit.get("result") == "dry_run":
        verdict = "dry_run"
    elif not blocking and audit.get("result") == "completed":
        verdict = "pass"
    elif routes == ["executor"]:
        verdict = "tool_bug"
    elif "trace" in routes:
        verdict = "re_record"
    else:
        verdict = "revise"

    next_steps = {
        "pass": "No action needed. Low-severity findings are optional hardening.",
        "revise": "Give the plan, the audit and this review to the author AI (tasktrace prompt revise) for version "
                  f"{(audit.get('plan') or {}).get('version', 1) + 1}.",
        "re_record": "Record the task again (tasktrace record) and regenerate the plan from the new trace.",
        "tool_bug": "File the executor error against tasktrace; the plan itself looks fine.",
        "dry_run": "Fix any locator failures, then run for real.",
    }[verdict]

    return {
        "schema": REVIEW_SCHEMA,
        "audit_id": audit.get("audit_id"),
        "plan": audit.get("plan"),
        "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "reviewer": {"kind": "tool", "name": "tasktrace review"},
        "verdict": verdict,
        "routes": {r: [f["id"] for f in findings if f["route_to"] == r] for r in sorted({f["route_to"] for f in findings})},
        "findings": findings,
        "next": next_steps,
    }


def lineage(plans: list[dict]) -> list[str]:
    """Readable history: v1 by tasktrace plan -> v2 by Copilot (2 changes) -> ..."""
    out = []
    for p in sorted(plans, key=lambda p: p.get("version", 1)):
        a = p.get("author") or {}
        changes = [c for c in p.get("changelog", []) if c.get("version") == p.get("version")]
        line = f"v{p.get('version')} by {a.get('name')} ({a.get('kind')})"
        if p.get("parent"):
            line += f", from {p['parent']}"
        if changes:
            line += f": {len(changes)} change(s) addressing " + ", ".join(sorted({c.get('finding', '?') for c in changes}))
        out.append(line)
    return out
