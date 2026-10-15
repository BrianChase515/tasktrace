"""Run a plan on this machine while a human watches, and write an audit of everything.

The executor contains no AI. It finds each target with the locator ladder
(element -> on-screen text -> window-relative position -> absolute position),
acts, checks the expected effect, and records what happened. A person gates
the steps (all of them in `step` mode, only consequential ones in `watch` mode)
and can say "that was wrong, here's why" at any point; that comment goes in the
audit for the reviewing AI.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import threading
import time
from pathlib import Path

from . import __version__, alert
from .answers import AnswerBank, is_essay
from .drivers import Driver, Match
from .plan import TARGETED, fill, missing_values, step_label

AUDIT_SCHEMA = "tasktrace-audit/0.1"
MAX_ATTEMPTS = 3
POSITIONAL = {"rel_xy", "abs_xy"}
DEFAULT_ADVANCE = ["Continue to next step", "Next", "Continue"]
DEFAULT_FINISH = ["Review your application", "Submit application", "Submit", "Review"]
MAX_PAGES = 12
FILLABLE = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXPopUpButton",
            "AXRadioButton", "AXCheckBox", "AXMenuButton"}
EMPTY_VALUES = {"", "-", "select an option", "please select", "choose an option"}


def _param_name(question: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(question).lower()).strip("_")[:40] or "answer"


def _button(buttons: list, names: list) -> dict | None:
    """The first button whose name matches one the plan is looking for."""
    wanted = [n.strip().lower() for n in names]
    for want in wanted:  # in the order the plan lists them: finishing beats advancing
        for b in buttons:
            title = (b.get("title") or "").strip().lower()
            if title == want or title.startswith(want) or want in title:
                return b
    return None


def _matches_control(control: dict, spec: dict) -> bool:
    if spec.get("role") and control.get("role") != spec["role"]:
        return False
    prefix = (spec.get("title_prefix") or "").lower()
    title = (control.get("title") or "").lower()
    return title.startswith(prefix) if prefix else True


def _page_summary(page: dict) -> str:
    bits = []
    if page.get("progress"):
        bits.append(page["progress"])
    if page["filled"]:
        bits.append(f"{len(page['filled'])} filled from your saved answers")
    if page["asked"]:
        bits.append(f"{len(page['asked'])} you just answered")
    return "; ".join(bits) or "nothing to fill in"


def _needs_answer(control: dict) -> bool:
    """Is this control still waiting for something?

    A field the site has already filled in (your name, your email) is left alone: the person
    answered it once, on their profile, and retyping it is how a run breaks a form.
    """
    if control.get("role") not in FILLABLE:
        return False
    if control.get("role") == "AXSecureTextField":
        return False
    value = str(control.get("value") or "").strip().lower()
    if control.get("role") in ("AXRadioButton", "AXCheckBox"):
        return value in ("", "0", "false")
    return value in EMPTY_VALUES


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def observe(before: dict, after: dict) -> dict:
    bt = {w["id"]: w["title"] for w in before.get("windows", [])}
    at = {w["id"]: w["title"] for w in after.get("windows", [])}
    obs = {
        "opened": [at[i] for i in at if i not in bt],
        "closed": [bt[i] for i in bt if i not in at],
        "retitled": [{"from": bt[i], "to": at[i]} for i in at if i in bt and bt[i] != at[i]],
    }
    if after.get("front_app") != before.get("front_app"):
        obs["app"] = after.get("front_app")
    return {k: v for k, v in obs.items() if v}


def effect_ok(expect: dict | None, obs: dict) -> bool:
    if not expect:
        return True
    norm = lambda s: (s or "").strip().lower()  # noqa: E731
    for key in ("opened", "closed"):
        seen = {norm(t) for t in obs.get(key, [])}
        if any(norm(t) not in seen for t in expect.get(key, [])):
            return False
    # Retitled windows usually contain this run's values, so only require that a retitle happened.
    if expect.get("retitled") and not obs.get("retitled"):
        return False
    return True


# --------------------------------------------------------------------------- human interface

class TerminalUI:
    """Questions in the terminal. Keep it beside the app you're automating."""

    def __init__(self, inp=input, out=print):
        self.inp, self.out = inp, out

    def _choose(self, prompt: str, choices: dict) -> str:
        """Unrecognized answers re-ask. A typo must never mean 'go ahead'."""
        while True:
            a = self.inp(prompt).strip().lower()
            key = a[:1] if a else ""
            if key in choices and len(a) <= 1:
                return choices[key]
            self.out("     (answer with one of the letters shown)")

    def ask_value(self, name: str, question: str | None = None, options: list | None = None) -> str:
        if question:
            self.out(f"\n? {question}" + (f"  ({' / '.join(options)})" if options else ""))
        return self.inp(f"Value for {{{name}}}: ").strip()

    def confirm(self, step: dict, desc: str, match: Match | None) -> str:
        flag = "  [consequential]" if step.get("consequential") else ""
        self.out(f"\n[{step['id']}] {desc}{flag}")
        if match:
            warn = "  <- position only, check the pointer target" if match.strategy in POSITIONAL else ""
            self.out(f"     found by {match.strategy}{warn}")
        if step.get("note"):
            self.out(f"     note: {step['note']}")
        return self._choose("     [Enter] run   s skip   m I'll do it   q stop > ",
                            {"": "go", "s": "skip", "m": "manual", "q": "stop"})

    def handoff(self, step: dict, reason: str) -> str:
        self.out(f"\n[{step['id']}] Your turn ({reason}): {step.get('describe') or step.get('ask', '')}")
        return self._choose("     do it, then [Enter]   s skip   q stop > ", {"": "done", "s": "skip", "q": "stop"})

    def on_failure(self, step: dict, entry: dict) -> str:
        self.out(f"     FAILED: {entry['outcome']}" + (f" (tried {', '.join(entry['attempt']['tried'])})"
                                                        if entry.get("attempt", {}).get("tried") else ""))
        return self._choose("     r retry   m I'll do it   s skip   q stop > ",
                            {"r": "retry", "m": "manual", "s": "skip", "q": "stop"})

    def verdict(self, step: dict, entry: dict) -> dict | None:
        a = self.inp("     look right? [Enter] yes, or type what went wrong (start with ! to also stop) > ")
        if not a.strip():
            return {"verdict": "approved"}
        return {"verdict": "rejected", "comment": a.strip().lstrip("!").strip(), "stop": a.strip().startswith("!")}


class ScriptedUI:
    """Canned answers, for tests and demos. answers[step_id] = {"confirm": ..., "verdict": {...}, ...}."""

    def __init__(self, answers: dict | None = None, values: dict | None = None, on_handoff=None):
        self.answers, self.values, self.on_handoff = answers or {}, values or {}, on_handoff
        self.asked: list[str] = []

    def _a(self, step, key, default):
        return (self.answers.get(step["id"]) or {}).get(key, default)

    def ask_value(self, name, question=None, options=None):
        self.asked.append(name)
        return self.values.get(name, "")

    def confirm(self, step, desc, match):
        return self._a(step, "confirm", "go")

    def handoff(self, step, reason):
        if self.on_handoff:
            self.on_handoff(step)
        return self._a(step, "handoff", "done")

    def on_failure(self, step, entry):
        return self._a(step, "on_failure", "stop")

    def verdict(self, step, entry):
        return self._a(step, "verdict", {"verdict": "approved"})


# --------------------------------------------------------------------------- executor

class Executor:
    def __init__(self, plan: dict, driver: Driver, ui, mode: str = "step", audit_dir: Path | None = None,
                 effect_timeout: float = 4.0, poll: float = 0.25, allow_abs: bool = False,
                 answers: AnswerBank | None = None):
        if mode not in ("step", "watch", "dry-run"):
            raise ValueError("mode must be step, watch or dry-run")
        self.plan, self.driver, self.ui, self.mode = plan, driver, ui, mode
        self.answers = answers if answers is not None else AnswerBank()
        self.audit_dir = Path(audit_dir) if audit_dir else None
        self.effect_timeout, self.poll, self.allow_abs = effect_timeout, poll, allow_abs
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.audit_id = f"{plan.get('plan_id', 'plan')}-v{plan.get('version', 1)}-{stamp}"
        self._stop = threading.Event()
        self.stopped_by: str | None = None

    def request_stop(self, by: str = "person") -> None:
        """Stop the run as soon as possible. Safe to call from any thread, at any time.

        The step in progress finishes its current click or keystroke; nothing after it runs."""
        if not self._stop.is_set():
            self.stopped_by = by
            self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ---- locating
    def locate(self, step: dict) -> tuple[Match | None, list[str]]:
        tgt, tried = step.get("target") or {}, []
        if (tgt.get("element") or {}).get("role"):
            tried.append("element")
            m = self.driver.find_element(step)
            if m:
                return m, tried
        label = tgt.get("ocr_label") or step_label(step)
        if label:
            tried.append("ocr")
            m = self.driver.find_text(label, step)
            if m:
                return m, tried
        if tgt.get("rel_xy"):
            tried.append("rel_xy")
            b = self.driver.window_bounds(step)
            if b:
                rx, ry = tgt["rel_xy"]
                return Match("rel_xy", int(b[0] + rx * b[2]), int(b[1] + ry * b[3])), tried
        if tgt.get("abs_xy") and self.allow_abs:
            tried.append("abs_xy")
            return Match("abs_xy", *tgt["abs_xy"]), tried
        return None, tried

    def _shot(self, step_id: int, tag: str) -> str | None:
        if not self.audit_dir:
            return None
        rel = f"{self.audit_id}/step-{step_id:02d}-{tag}.png"
        return rel if self.driver.screenshot(self.audit_dir / rel) else None

    def _wait_effect(self, before: dict, expect: dict | None) -> tuple[dict, bool]:
        deadline = time.monotonic() + (self.effect_timeout if expect else 0.4)
        while True:
            obs = observe(before, self.driver.snapshot())
            ok = effect_ok(expect, obs)
            if (ok and expect) or time.monotonic() >= deadline or self._stop.is_set():
                return obs, ok or self._stop.is_set()
            time.sleep(self.poll)

    # ---- working a form that nobody has seen before
    def _answer_control(self, control: dict, page: dict) -> str:
        """Fill one control from the bank, or stop and ask the person — once, ever."""
        question = control.get("title") or control.get("placeholder") or ""
        hit = self.answers.find(question, control.get("role"))
        if hit:
            self.answers.used(hit)
            if self.driver.fill(control, hit["answer"]):
                page["filled"].append({"question": question, "answer": hit["answer"], "from": "bank"})
                return "filled"
            page["problems"].append({"question": question, "problem": "could not fill"})
            return "failed"
        if self.mode == "dry-run":
            page["would_ask"].append(question)
            return "would_ask"
        # Something this person has not answered before: their call, and worth interrupting for.
        alert.attention("ask")
        alert.say("Question for you")
        answer = self.ui.ask_value(_param_name(question), question, control.get("options") or None)
        if not answer:
            page["problems"].append({"question": question, "problem": "unanswered"})
            return "unanswered"
        self.answers.learn(question, answer, control.get("role"), control.get("options"))
        self.driver.fill(control, answer)
        page["asked"].append({"question": question, "answer": answer,
                              "reusable": not is_essay(question, answer)})
        return "asked"

    def _form_loop(self, step: dict, entry: dict, finish, hand_to_human):
        """Work through however many pages this form turns out to have.

        A recorded step list cannot do this: the next employer's form has different fields in a
        different order over a different number of pages. So the executor reads the page in front
        of it, fills what it has an answer for, asks about what it doesn't, and advances.
        """
        advance = step.get("advance") or DEFAULT_ADVANCE
        done_on = step.get("finish") or DEFAULT_FINISH
        pages, seen_progress = [], []
        for n in range(1, int(step.get("max_pages") or MAX_PAGES) + 1):
            if self._stop.is_set():
                entry["pages"] = pages
                return finish("stopped", False)
            form = self.driver.read_form(step)
            page = {"page": n, "progress": form.get("progress"), "filled": [], "asked": [],
                    "problems": [], "would_ask": []}
            for control in form.get("controls", []):
                if not _needs_answer(control):
                    continue
                outcome = self._answer_control(control, page)
                if outcome == "unanswered":
                    pages.append(page)
                    entry["pages"] = pages
                    return hand_to_human("a question needs your answer")
            pages.append(page)
            buttons = form.get("buttons", [])
            if _button(buttons, done_on):
                entry["pages"] = pages
                entry["ended_on"] = _button(buttons, done_on)["title"]
                return finish("form_complete", True)
            nxt = _button(buttons, advance)
            if nxt is None:
                entry["pages"] = pages
                return hand_to_human("no button to continue with, and nothing that finishes the form")
            if self.mode == "dry-run":
                entry["pages"] = pages
                return finish("would_run", True)
            if self.mode == "step":
                ans = self.ui.confirm(step, f'Page {n}: {_page_summary(page)} -> press "{nxt["title"]}"', None)
                if ans == "stop":
                    entry["pages"] = pages
                    return finish("stopped", False)
                if ans == "manual":
                    return hand_to_human("you chose to do this page")
            self.driver.press(nxt)
            time.sleep(max(self.poll, 0.4))
            after = self.driver.read_form(step).get("progress")
            page["progress_after"] = after
            if after and after == page.get("progress") and after in seen_progress:
                entry["pages"] = pages  # pressing is not getting us anywhere
                return hand_to_human(f"the form stopped moving at {after}")
            seen_progress.append(page.get("progress"))
        entry["pages"] = pages
        return hand_to_human(f"still going after {len(pages)} pages")

    def _pick_list_item(self, step: dict, values: dict, entry: dict, finish, hand_to_human):
        """Choose a row by what it says, not by where it sat the day it was recorded."""
        want = [w.lower() for w in (step.get("require_any") or [])]
        avoid = [w.lower() for w in (step.get("exclude_any") or [])]
        press = step.get("press") or {}
        items = self.driver.list_items(step)
        entry["scanned"] = len(items)
        matches = []
        for item in items:
            text = (item.get("text") or "").lower()
            if avoid and any(w in text for w in avoid):
                continue
            if want and not any(w in text for w in want):
                continue
            control = next((c for c in item.get("controls", [])
                            if _matches_control(c, press)), None)
            if control is not None:
                matches.append((item, control))
        entry["matched"] = [{"index": i["index"], "text": (i.get("text") or "")[:200]} for i, _ in matches[:5]]
        if not matches:
            return hand_to_human(f"nothing in the list matching {', '.join(step.get('require_any') or []) or 'the plan'}")
        if self.mode == "dry-run":
            return finish("would_run", True)
        item, control = matches[0]
        values[step["param"]] = item.get("text", "")[:200] if step.get("param") else None
        if self.mode == "step" or step.get("consequential"):
            ans = self.ui.confirm(step, f'Open: {(item.get("text") or "")[:160]}', None)
            if ans == "stop":
                return finish("stopped", False)
            if ans == "skip":
                return finish("skipped", True)
        before = self.driver.snapshot()
        self.driver.press(control)
        self._wait_effect(before, step.get("expect"))
        entry["chose"] = {"index": item["index"], "text": (item.get("text") or "")[:200]}
        return finish("ok", True)

    # ---- one step
    def _run_step(self, step: dict, values: dict) -> tuple[dict, bool]:
        """Returns (audit entry, keep_going)."""
        t0 = time.monotonic()
        text = fill(step["text"], values) if "text" in step else None
        desc = step.get("describe") or step["action"]
        if text is not None and "{" in (step.get("text") or ""):
            desc = f"{desc}  [value: {text!r}]"
        entry: dict = {"step": step["id"], "action": step["action"], "describe": desc}
        if step.get("consequential"):
            entry["consequential"] = True
        if text is not None:
            entry["text_used"] = text

        def finish(outcome: str, keep: bool) -> tuple[dict, bool]:
            entry["outcome"] = outcome
            entry["duration_ms"] = int((time.monotonic() - t0) * 1000)
            return entry, keep

        def hand_to_human(reason: str) -> tuple[dict, bool]:
            if self.mode == "dry-run":
                return finish("would_hand_to_human", True)
            alert.attention("problem")
            ans = self.ui.handoff(step, reason)
            entry["handoff"] = reason
            if ans == "stop":
                return finish("stopped", False)
            return finish("skipped" if ans == "skip" else "handed_to_human", True)

        if step["action"] == "form_loop":
            return self._form_loop(step, entry, finish, hand_to_human)

        if step["action"] == "pick_list_item":
            return self._pick_list_item(step, values, entry, finish, hand_to_human)

        if step["action"] == "ask":
            if self.mode == "dry-run":
                return finish("would_ask", True)
            alert.attention("ask")
            answer = self.ui.ask_value(step["param"], step.get("question"), step.get("options"))
            values[step["param"]] = answer
            entry["question"] = step.get("question")
            if not step.get("secret"):
                entry["answer"] = answer
            return finish("answered" if answer else "unanswered", True)

        if step.get("secret"):
            return hand_to_human("secret: you type it, it is never recorded")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            match, tried = (None, [])
            if step["action"] in TARGETED:
                match, tried = self.locate(step)
            entry["attempt"] = {"n": attempt, "tried": tried, "match": match.public() if match else None}
            needs_target = step["action"] in TARGETED - {"type", "scroll"}

            if needs_target and match is None:
                if step.get("optional"):
                    return finish("skipped_optional", True)
                entry["outcome"] = "locator_failed"
                if self.mode == "dry-run":
                    return finish("locator_failed", True)
                ans = self.ui.on_failure(step, entry)
                if ans == "retry":
                    continue
                if ans == "manual":
                    return hand_to_human("locator failed")
                return finish("locator_failed" if ans == "skip" else "stopped", ans == "skip")

            if self.mode == "dry-run":
                return finish("would_run", True)

            gate = self.mode == "step" or step.get("consequential") or (match and match.strategy in POSITIONAL
                                                                         and self.mode == "watch")
            if gate:
                ans = self.ui.confirm(step, desc, match)
                if ans == "stop":
                    return finish("stopped", False)
                if ans == "skip":
                    return finish("skipped", True)
                if ans == "manual":
                    return hand_to_human("you chose to do this step")

            self.driver.prepare(step)
            before = self.driver.snapshot()
            shot_before = self._shot(step["id"], "before") if self.mode == "step" else None
            try:
                self.driver.act(step, match, text)
            except Exception as exc:
                entry["error"] = repr(exc)[:300]
                ans = self.ui.on_failure(step, {**entry, "outcome": "error"})
                if ans == "retry":
                    continue
                if ans == "manual":
                    return hand_to_human("executor error")
                return finish("error", ans == "skip")

            obs, ok = self._wait_effect(before, step.get("expect"))
            if step.get("expect"):
                entry["expected"] = step["expect"]
            if obs:
                entry["observed"] = obs
            shots = {k: v for k, v in (("before", shot_before), ("after", self._shot(step["id"], "after"))) if v}
            if shots:
                entry["screenshots"] = shots

            if not ok:
                entry["outcome"] = "effect_mismatch"
                human = self.ui.verdict(step, entry)
                if human:
                    entry["human"] = {k: v for k, v in human.items() if k != "stop"}
                ans = self.ui.on_failure(step, entry)
                if ans == "retry":
                    continue
                if ans == "manual":
                    return hand_to_human("expected result didn't happen")
                return finish("effect_mismatch", ans == "skip")

            human = self.ui.verdict(step, entry) if self.mode == "step" or step.get("consequential") else None
            if human:
                entry["human"] = {k: v for k, v in human.items() if k != "stop"}
                if human.get("stop"):
                    return finish("ok", False)
            return finish("ok", True)
        return finish("locator_failed", False)

    # ---- whole plan
    def run(self) -> dict:
        started = _now()
        values = dict(self.plan.get("values") or {})
        steps = self.plan.get("steps", [])
        start_state = self.driver.snapshot()
        entries, keep = [], True
        try:
            asked_later = {s.get("param") for s in steps if s.get("action") == "ask"}
            for name in missing_values(self.plan):
                if name not in asked_later and not self._stop.is_set():
                    values[name] = self.ui.ask_value(name, f"What should “{name}” be for this run?")
            for step in steps:
                if keep and self._stop.is_set():
                    keep = False
                    entries.append({"step": step["id"], "action": step["action"],
                                    "describe": step.get("describe", ""), "outcome": "stopped",
                                    "stopped_by": self.stopped_by})
                    continue
                if not keep:
                    entries.append({"step": step["id"], "action": step["action"],
                                    "describe": step.get("describe", ""), "outcome": "not_run"})
                    continue
                entry, keep = self._run_step(step, values)
                entries.append(entry)
        except KeyboardInterrupt:  # ctrl+C in the terminal: stop cleanly and still write the audit
            self.request_stop("person (ctrl+C)")
            done_ids = {e["step"] for e in entries}
            first = True
            for step in steps:
                if step["id"] in done_ids:
                    continue
                entries.append({"step": step["id"], "action": step["action"], "describe": step.get("describe", ""),
                                "outcome": "stopped" if first else "not_run",
                                **({"stopped_by": self.stopped_by} if first else {})})
                first = False
            keep = False

        try:
            self.driver.close()
        except Exception:
            pass
        end_state = self.driver.snapshot()
        counts: dict[str, int] = {}
        for e in entries:
            counts[e["outcome"]] = counts.get(e["outcome"], 0) + 1
        rejected = sum(1 for e in entries if (e.get("human") or {}).get("verdict") == "rejected")
        if self.mode == "dry-run":
            result = "dry_run"
        elif any(e["outcome"] in ("stopped", "not_run") for e in entries) or not keep:
            result = "stopped"
        elif any(e["outcome"] in ("locator_failed", "effect_mismatch", "error") for e in entries) or rejected:
            result = "completed_with_issues"
        else:
            result = "completed"

        done = self.plan.get("done_when") or {}
        audit = {
            "schema": AUDIT_SCHEMA,
            "audit_id": self.audit_id,
            "plan": {k: self.plan.get(k) for k in ("plan_id", "version", "parent", "author", "source_trace")},
            "executor": {"tool": f"tasktrace {__version__}", "driver": self.driver.name, "mode": self.mode},
            "operator": getpass.getuser(),
            "started_at": started,
            "ended_at": _now(),
            "values_used": values,
            "entries": entries,
            "result": result,
            "summary": {"outcomes": counts, "human_rejections": rejected},
            "stopped_by": self.stopped_by,
            "final_state": {
                "front_app": end_state.get("front_app"),
                "windows": [w["title"] for w in end_state.get("windows", [])][:10],
                "done_when_app_ok": (end_state.get("front_app") == done["final_app"]) if done.get("final_app") else None,
            },
            "start_state": {"front_app": start_state.get("front_app"),
                            "windows": [w["title"] for w in start_state.get("windows", [])][:10]},
        }
        if self.audit_dir:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            (self.audit_dir / f"{self.audit_id}.json").write_text(
                json.dumps(audit, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return audit
