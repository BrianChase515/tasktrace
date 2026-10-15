"""The answer bank: what to type when a form asks something it has been asked before.

Every employer's application form is different, so a plan cannot list the fields in advance.
What repeats is the *questions*. "Years of experience with CX", "Are you authorized to work in
the US", "Notice period" come up again and again, and the person should answer each of those
once, ever. A question that is specific to one employer — "why do you want to work at Acme" —
must always come back to the person, because reusing last week's answer there is worse than
not answering at all.

The bank is a plain JSON file in the TaskTrace home folder. It is the person's own writing,
so nothing is ever sent anywhere; it is only read when a form asks for it.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from pathlib import Path

BANK_FILE = "answers.json"
SCHEMA = "tasktrace-answers/0.1"
MATCH_RATIO = 0.88  # how close two questions must read before one answers the other

# Questions that want prose about this particular employer. The answer is never reusable,
# however closely the wording matches something answered before.
ESSAY_RE = re.compile(
    r"\b(why do you|why are you|why would you|describe|tell us|tell me|in your own words|"
    r"cover letter|what interests you|what excites you|explain how|elaborate)\b", re.I)
NOISE = re.compile(r"[*?:•()\[\]]|\s+")
STOP_CAPS = {"I", "US", "USA", "UK", "EU", "AI", "CX", "HR", "IT", "PTO", "OK", "English"}

# Controls that hold a written answer. A tick box's "1" is a state, not an answer (see seed_from_trace).
SEEDABLE_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXPopUpButton"}
# The browser and the operating system are not asking the person anything.
FURNITURE = re.compile(
    r"memory usage|address and search bar|^where\b|^search\b|^deselect |^select resume|^mark job|"
    r"^set job alert|to stay up to date|^new tab$|^back$|^forward$|^reload|^bookmark|^profile\b|"
    r"\bgoogle chrome\b|\bsafari\b", re.I)


def is_furniture(question: str) -> bool:
    """Is this a control of the browser or the page, rather than a question being asked?"""
    return bool(FURNITURE.search(str(question or "")))


def normalize(question: str) -> str:
    """Two questions that mean the same thing should normalize to the same string."""
    q = NOISE.sub(" ", str(question or "")).strip().lower()
    q = re.sub(r"^(please|kindly)\s+", "", q)
    q = re.sub(r"\s*\((optional|required)\)\s*$", "", q)
    return re.sub(r"\s+", " ", q).strip(" .")


def entities(question: str) -> set:
    """Capitalised words that aren't sentence-initial: usually a company or product name.

    "Why do you want to work at Bitdefender" and "...at SquareTrade" read almost identically
    to a string comparison, and must never answer for one another.
    """
    words = re.findall(r"\b[A-Z][A-Za-z0-9&.\-]{1,}\b", str(question or ""))
    return {w for i, w in enumerate(words) if not (i == 0 or w in STOP_CAPS)}


def is_essay(question: str, answer: str | None = None) -> bool:
    return bool(ESSAY_RE.search(str(question or "")) or (answer and len(str(answer)) > 300))


class AnswerBank:
    """Questions this person has answered before, and what they said."""

    def __init__(self, path: Path | None = None, entries: list | None = None):
        self.path = Path(path) if path else None
        self.entries: list[dict] = list(entries or [])
        if self.path and self.path.exists() and entries is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.entries = data.get("answers", []) if isinstance(data, dict) else list(data)
            except ValueError:
                self.entries = []

    # ---- lookup
    def find(self, question: str, role: str | None = None) -> dict | None:
        """The stored answer for this question, or None if it has to be asked."""
        key, ents = normalize(question), entities(question)
        if not key:
            return None
        best, best_ratio = None, 0.0
        for e in self.entries:
            if e.get("ask_always"):
                continue
            if entities(e.get("question", "")) != ents:
                continue  # a different company's question, however similar the wording
            if role and e.get("role") and _kind(role) != _kind(e["role"]):
                continue
            ratio = 1.0 if e.get("key") == key else difflib.SequenceMatcher(None, e.get("key", ""), key).ratio()
            if ratio > best_ratio:
                best, best_ratio = e, ratio
        return best if best_ratio >= MATCH_RATIO else None

    # ---- writing
    def learn(self, question: str, answer: str, role: str | None = None,
              options: list | None = None, ask_always: bool | None = None) -> dict:
        """Remember an answer the person just gave, so the same question never interrupts twice."""
        key = normalize(question)
        entry = next((e for e in self.entries if e.get("key") == key
                      and entities(e.get("question", "")) == entities(question)), None)
        if entry is None:
            entry = {"key": key, "question": str(question)}
            self.entries.append(entry)
        entry.update(answer=str(answer), role=role or entry.get("role"),
                     updated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     times_used=entry.get("times_used", 0))
        if options:
            entry["options"] = list(options)[:20]
        flag = is_essay(question, answer) if ask_always is None else ask_always
        if flag:
            entry["ask_always"] = True  # kept as a starting point, never filled in automatically
        else:
            entry.pop("ask_always", None)
        self.save()
        return entry

    def used(self, entry: dict) -> None:
        entry["times_used"] = entry.get("times_used", 0) + 1

    def save(self) -> Path | None:
        if not self.path:
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"schema": SCHEMA, "answers": self.entries}, indent=1,
                                        ensure_ascii=False) + "\n", encoding="utf-8")
        return self.path

    # ---- bootstrapping
    def seed_from_trace(self, trace: dict) -> int:
        """Harvest question/answer pairs from a recording.

        Every form the person filled in is already in the trace: `container_context` lists each
        control with the value it held. That is a ready-made answer bank for the next form.

        Only fields with a written answer are taken. A tick box records its state ("1") and not
        what it was asking, and on its own that is not an answer to anything: a box labelled
        "Yes" in one form would otherwise tick "Yes" in an unrelated one. Those are learned
        properly on the first run, where the question around them can be read.
        """
        learned = 0
        for step in trace.get("steps", []):
            if step.get("secret"):
                continue
            cc = step.get("container_context") or {}
            for blk in (cc, cc.get("modal") or {}):
                for c in blk.get("interactive_siblings") or []:
                    q, v = (c.get("title") or "").strip(), (c.get("value") or "").strip()
                    if c.get("role") not in SEEDABLE_ROLES or not (q and v):
                        continue
                    if is_furniture(q) or v.lower() in ("select an option", "-", "0", "1", "false", "true"):
                        continue
                    prior = self.find(q, c.get("role"))
                    if prior and len(str(prior.get("answer", ""))) >= len(v):
                        continue  # keep the fullest version: fields get typed into progressively
                    self.learn(q, v, c.get("role"))
                    learned += 0 if prior else 1
        return learned


def _kind(role: str) -> str:
    if role in ("AXCheckBox", "AXRadioButton", "AXSwitch", "AXToggle"):
        return "toggle"
    if role in ("AXPopUpButton", "AXComboBox", "AXMenuButton"):
        return "choice"
    return "text"


def default_path(home: Path) -> Path:
    return Path(home) / BANK_FILE
