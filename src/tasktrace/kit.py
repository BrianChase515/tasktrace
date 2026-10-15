"""The hands and eyes a generated script runs on.

A trace says how a task went once. A plan — a flat list of steps — can only repeat that, which
is why it falls over the moment the next form asks different things in a different order. This
module is the other half: an API small enough that an AI can write an ordinary Python script
against it, with real loops and conditions, and that script runs on its own afterwards.

No model runs here. Nothing leaves the machine. The script decides; this does the looking and
the pressing, keeps the person in charge of anything consequential, remembers answers, and
writes the same audit the rest of the loop reads.

    from tasktrace.kit import Session

    with Session("Google Chrome", window="LinkedIn") as s:
        for job in s.rows():                                   # whatever is in the list today
            s.open(job)
            if not s.page_says("job match is high", "top applicant"):
                continue
            apply_button = s.find("AXButton", starts_with="Easy Apply")
            if apply_button and s.click(apply_button):
                break

        while s.running:                                       # however many pages it has
            page = s.form()
            for control in page.empty():
                s.answer(control)                              # from the bank, or ask once
            if page.button("Review your application", "Submit application"):
                break
            s.click(page.button("Continue to next step", "Next") or s.give_up("no continue button"))
            s.settle()

        s.click(s.form().button("Submit application"), consequential=True)
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

from . import __version__, alert
from .answers import AnswerBank, default_path
from .paths import home

AUDIT_SCHEMA = "tasktrace-audit/0.1"
SETTLE_S = 0.6

_defaults: dict = {"mode": "watch", "driver": "auto", "audit_dir": None, "ui": None, "answers": None}


def configure(**kw) -> None:
    """Set what `tasktrace run` passes to every Session the script opens."""
    _defaults.update({k: v for k, v in kw.items() if v is not None})


class Stopped(Exception):
    """Raised inside a script when the person stops the run. Let it propagate."""


class HandedOver(Exception):
    """Raised when the script asks a person to take over and they end the run."""


class Page:
    """One screenful of a form: what it asks, what it offers to press."""

    def __init__(self, raw: dict):
        self.controls = raw.get("controls") or []
        self.buttons = raw.get("buttons") or []
        self.progress = raw.get("progress")

    def empty(self) -> list[dict]:
        """Controls still waiting for an answer, skipping anything already filled in."""
        from .replay import _needs_answer
        return [c for c in self.controls if _needs_answer(c)]

    def button(self, *names: str) -> dict | None:
        """The first button matching any of these names, in the order given."""
        from .replay import _button
        return _button(self.buttons, [n for n in names if n])

    def control(self, label: str) -> dict | None:
        want = label.strip().lower()
        for c in self.controls:
            title = (c.get("title") or "").strip().lower()
            if title == want or want in title:
                return c
        return None

    def says(self, *phrases: str) -> bool:
        from .replay import _text_matches
        text = " · ".join(filter(None, [c.get("title") for c in self.controls + self.buttons]))
        return _text_matches(text, list(phrases))

    def __repr__(self) -> str:
        return f"<Page {len(self.controls)} fields, {len(self.buttons)} buttons, progress={self.progress!r}>"


class Session:
    """A run of a script against one app, with an audit and a person watching."""

    def __init__(self, app: str, window: str = "", mode: str | None = None, driver=None,
                 audit_dir: Path | None = None, ui=None, answers: AnswerBank | None = None,
                 name: str = "script"):
        from .drivers import get_driver
        from .replay import TerminalUI

        self.app, self.window = app, window
        self.mode = mode or _defaults["mode"]
        self.driver = driver or _defaults.get("_driver") or get_driver(_defaults["driver"])
        self.ui = ui or _defaults.get("ui") or TerminalUI()
        self.answers = answers or _defaults.get("answers") or AnswerBank(default_path(home()))
        self.audit_dir = Path(audit_dir or _defaults.get("audit_dir") or (home() / "runs"))
        self.name = name
        self.entries: list[dict] = []
        self.running = True
        self.result = "ok"
        self.stopped_by: str | None = None
        self.started_at = _now()
        self.audit_id = f"{name}-{dt.datetime.now():%Y%m%d-%H%M%S}"
        self._step = 0
        self.driver.prepare(self._step_dict())  # brings the app forward, exposes its page

    # ---- plumbing
    def _step_dict(self) -> dict:
        """What the person's interface sees: the step about to happen, numbered from 1."""
        return {"id": self._step + 1, "action": "script", "app": self.app, "window": self.window}

    def _log(self, action: str, describe: str, **extra) -> dict:
        self._step += 1
        entry = {"step": self._step, "action": action, "describe": describe, **extra}
        self.entries.append(entry)
        return entry

    def stop(self, by: str = "person") -> None:
        self.running, self.result, self.stopped_by = False, "stopped", by
        raise Stopped(by)

    def give_up(self, reason: str):
        """Hand over to the person. They finish it by hand, skip it, or end the run."""
        alert.attention("problem")
        self._log("handoff", reason, outcome="handed_to_human")
        if self.mode == "dry-run":
            return None
        answer = self.ui.handoff(self._step_dict(), reason)
        if answer == "stop":
            self.stop()
        return None

    # ---- looking
    def form(self) -> Page:
        """What the form on screen is asking right now."""
        return Page(self.driver.read_form(self._step_dict()))

    def rows(self) -> list[dict]:
        """The rows of the list on screen, as they are today."""
        return self.driver.list_items(self._step_dict())

    def text(self) -> str:
        """Everything the window currently says."""
        return self.driver.window_text(self._step_dict())

    def page_says(self, *phrases: str) -> bool:
        """Does the window say any of these? Matches the site's wording, not only yours."""
        from .replay import _text_matches
        return _text_matches(self.text(), list(phrases))

    def find(self, role: str = "", label: str = "", starts_with: str = "",
             contains: str = "") -> dict | None:
        """A control on screen, by what it is and what it is called."""
        page = self.form()
        for c in page.controls + page.buttons:
            title = (c.get("title") or "").strip()
            low = title.lower()
            if role and c.get("role") != role:
                continue
            if label and low != label.strip().lower():
                continue
            if starts_with and not low.startswith(starts_with.lower()):
                continue
            if contains and contains.lower() not in low:
                continue
            return c
        return None

    def wait_for(self, *phrases: str, timeout: float = 10.0) -> bool:
        """Wait until the window says one of these. False if it never does."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.page_says(*phrases):
                return True
            time.sleep(0.3)
        return False

    def settle(self, seconds: float = SETTLE_S) -> None:
        time.sleep(seconds)

    # ---- acting
    def click(self, control: dict | None, consequential: bool = False) -> bool:
        """Press something. Anything consequential always asks the person first."""
        if control is None:
            return False
        what = control.get("title") or control.get("role") or "control"
        if self.mode == "dry-run":
            self._log("click", f"would click {what!r}", outcome="would_run")
            return True
        if consequential or self.mode == "step":
            alert.attention("ask")
            answer = self.ui.confirm({**self._step_dict(), "consequential": consequential},
                                     f"Click {what!r}", None)
            if answer == "stop":
                self.stop()
            if answer in ("skip", "manual"):
                self._log("click", f"skipped {what!r}", outcome="skipped")
                return False
        ok = self.driver.press(control)
        self._log("click", f"click {what!r}", outcome="ok" if ok else "error",
                  **({"consequential": True} if consequential else {}))
        return ok

    def open(self, row: dict) -> bool:
        """Open a row of a list — the panel it shows is often where the detail lives."""
        if self.mode == "dry-run":
            self._log("open", f"would open {(row.get('text') or '')[:80]!r}", outcome="would_run")
            return True
        ok = self.driver.press({"handle": row.get("handle"), "title": (row.get("text") or "")[:60]})
        self._log("open", f"open {(row.get('text') or '')[:80]!r}", outcome="ok" if ok else "error")
        self.settle()
        return ok

    def fill(self, control: dict | None, value: str) -> bool:
        if control is None:
            return False
        if self.mode == "dry-run":
            self._log("fill", f"would fill {control.get('title')!r}", outcome="would_run")
            return True
        ok = self.driver.fill(control, value)
        self._log("fill", f"fill {control.get('title')!r}", outcome="ok" if ok else "error",
                  text_used=value)
        return ok

    def answer(self, control: dict) -> str | None:
        """Fill a control from the answer bank, or ask the person — once, ever.

        This is the whole point of a run that nobody is watching closely: a question answered
        before is filled in silently, and anything new rings a bell and waits for a person.
        """
        question = control.get("title") or control.get("placeholder") or ""
        known = self.answers.find(question, control.get("role"))
        if known:
            self.answers.used(known)
            self.fill(control, known["answer"])
            return known["answer"]
        if self.mode == "dry-run":
            self._log("ask", f"would ask {question!r}", outcome="would_ask")
            return None
        value = self.ask(question, options=control.get("options"), role=control.get("role"))
        if value:
            self.fill(control, value)
        return value

    def ask(self, question: str, options: list | None = None, role: str | None = None,
            remember: bool = True) -> str:
        """Ask the person something, with a sound, and remember the answer for next time."""
        from .replay import _param_name
        alert.attention("ask")
        alert.say("Question for you")
        value = self.ui.ask_value(_param_name(question), question, options or None)
        entry = self._log("ask", question, outcome="answered" if value else "unanswered")
        if value:
            entry["answer"] = value
            if remember:
                saved = self.answers.learn(question, value, role, options)
                entry["remembered"] = not saved.get("ask_always")
        return value

    def note(self, text: str) -> None:
        """Put a line in the audit, so the review can see what the script was thinking."""
        self._log("note", text, outcome="ok")

    # ---- finishing
    def finish(self) -> dict:
        outcomes: dict = {}
        for e in self.entries:
            outcomes[e.get("outcome", "ok")] = outcomes.get(e.get("outcome", "ok"), 0) + 1
        audit = {
            "schema": AUDIT_SCHEMA,
            "audit_id": self.audit_id,
            "plan": {"plan_id": self.name, "version": 1, "parent": None,
                     "author": {"kind": "script", "name": self.name}, "source_trace": None},
            "executor": {"tool": f"tasktrace {__version__}", "driver": self.driver.name,
                         "mode": self.mode, "kind": "script"},
            "started_at": self.started_at, "ended_at": _now(),
            "entries": self.entries,
            "result": self.result,
            "summary": {"outcomes": outcomes},
            "stopped_by": self.stopped_by,
        }
        if self.audit_dir:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            (self.audit_dir / f"{self.audit_id}.json").write_text(
                json.dumps(audit, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        self.audit = audit
        _defaults["_last_audit"] = audit  # so `tasktrace run` can report on it
        return audit

    def __enter__(self) -> Session:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is Stopped:
            self.result = "stopped"
        elif exc_type is not None:
            self.result = "error"
            self._log("error", repr(exc)[:300], outcome="error")
        self.running = False
        self.finish()
        try:
            self.driver.close()
        except Exception:
            pass
        alert.attention("problem" if self.result != "ok" else "done")
        return exc_type in (Stopped, HandedOver)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_script(path: Path, **kw) -> dict | None:
    """Run a script file with these settings. Returns its audit, if it opened a session."""
    import runpy
    configure(**kw)
    runpy.run_path(str(path), run_name="__main__")
    return _defaults.get("_last_audit")
