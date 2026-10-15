"""Prompts that let any AI take a role in the loop, including locked-down ones.

An assistant that can't touch your machine (e.g. a corporate Copilot) only has
to read and write text: paste the prompt, get a plan back, and the local
executor does the clicking while you watch.
"""

from __future__ import annotations

import json

from .plan import PLAN_SCHEMA

_PLAN_RULES = f"""\
Plan rules:
- Output ONLY one JSON object, schema "{PLAN_SCHEMA}". No prose, no code fences.
- "author": {{"kind": "ai", "name": "<your product name>", "model": "<model if known>"}}.
- Steps use the same actions as the trace: click, double_click, triple_click, right_click, drag,
  type, key, hotkey, scroll, open_app ({{"action": "open_app", "app": "<name>"}}). Number steps 1..n.
- When you are not sure what the person wants, DON'T guess: add an ask step before it,
  {{"action": "ask", "question": "<plain question>", "param": "<name>", "options": ["optional", "choices"]}},
  and use "{{name}}" in later steps. The run pauses and the person answers.
- Keep each step's "target" locators from the trace (element, path, ocr_label, rel_xy, abs_xy).
  The executor tries them in that order. Never invent coordinates.
- Typed values: write "{{param_name}}" in "text" and put the value in "values".
- Password fields: "secret": true with an "ask" label and NO text. A human types those.
- Keep "expect" (windows that should open/close/retitle) so the executor can verify each step.
- Set "consequential": true on any step that saves, submits, sends, deletes, pays or creates.
  A human must confirm those before they run.
- If something is ambiguous, say so in that step's "note" rather than guessing."""


def author_prompt(trace: dict, values: dict | None = None, author_name: str = "<your name>") -> str:
    task = trace.get("task", {})
    return f"""You are the AUTHOR in a human-supervised automation loop.

A person recorded themselves doing a task (the trace below). Write a plan that a local executor
will run on their Mac while they watch. You do not control the mouse; the executor does.

Task: {task.get('name')}{' - ' + task['goal'] if task.get('goal') else ''}
Values for this run: {json.dumps(values or {})}
Ask for any parameter value you were not given instead of reusing the example.

Plan shape:
{{"schema": "{PLAN_SCHEMA}", "plan_id": "<slug>", "version": 1, "parent": null,
 "source_trace": "<trace name>", "goal": "...",
 "author": {{"kind": "ai", "name": "{author_name}"}}, "created_at": "<ISO time>",
 "values": {{...}}, "steps": [...], "done_when": {{...}}, "changelog": []}}

{_PLAN_RULES}

Trace:
{json.dumps(trace, separators=(',', ':'), ensure_ascii=False)}
"""


def revise_prompt(plan: dict, audit: dict, review: dict, reviser_name: str = "<your name>") -> str:
    nxt = plan.get("version", 1) + 1
    return f"""You are the REVISER in a human-supervised automation loop.

A plan was run by the local executor while a human watched. The audit records exactly what
happened at each step, and the review triages every problem and routes it. Write version {nxt}.

How to revise:
- Only fix findings routed to "author". Findings routed to "executor" are tool bugs: don't
  work around them in the plan. Findings routed to "human" need information only the person has:
  ask for it. Findings routed to "trace" mean the task should be re-recorded: say so.
- Human comments are ground truth. They outrank the trace, the audit, and your own judgment.
- Change as little as possible. Keep step ids 1..n.
- Set "version": {nxt}, "parent": "{plan.get('plan_id')}@{plan.get('version')}",
  "author": {{"kind": "ai", "name": "{reviser_name}"}}, a new "created_at",
  and APPEND to "changelog" one entry per change:
  {{"version": {nxt}, "finding": "<finding id>", "step": <step id>, "change": "<what you changed and why>"}}

{_PLAN_RULES}

Current plan:
{json.dumps(plan, separators=(',', ':'), ensure_ascii=False)}

Review (triage):
{json.dumps(review, separators=(',', ':'), ensure_ascii=False)}

Audit:
{json.dumps(audit, separators=(',', ':'), ensure_ascii=False)}
"""
