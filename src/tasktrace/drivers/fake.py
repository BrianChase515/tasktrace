"""A simulated app for exercising the replay loop without a Mac.

It models the sample task (tests/fixtures/run_q3): a portal window with a
"New Report" button that opens a wizard; "Create" closes the wizard and retitles
the portal after the report title. Knobs let tests break it the way real apps break.
"""

from __future__ import annotations

from pathlib import Path

from . import Driver, Match

MAIN, WIZ = "Acme Portal - Reports", "Report Wizard"


class FakeDriver(Driver):
    name = "fake"

    def __init__(self, rename: dict | None = None, broken: set | None = None, ocr_text: bool = True):
        self.rename = rename or {}  # label -> new label (simulates a UI change)
        self.broken = broken or set()  # labels whose click does nothing
        self.ocr_text = ocr_text
        self.windows = [{"id": 101, "title": MAIN, "app": "Safari", "bounds": [0, 25, 1440, 875]}]
        self.focused: str | None = None
        self.fields: dict[str, str] = {}
        self.saved = False
        self.log: list[str] = []
        self._next_id = 102

    # ---- the simulated UI
    def _elements(self) -> list[dict]:
        els = []
        titles = {w["title"] for w in self.windows}
        if any(t.startswith("Acme Portal") for t in titles):
            els.append({"role": "AXButton", "title": "New Report", "frame": [1240, 128, 110, 30], "win": "main"})
        if WIZ in titles:
            els += [
                {"role": "AXTextField", "title": "Report title", "frame": [560, 288, 320, 24], "win": WIZ},
                {"role": "AXTextField", "title": "Period end", "frame": [560, 330, 160, 24], "win": WIZ},
                {"role": "AXTextField", "subrole": "AXSecureTextField", "title": "Approval PIN",
                 "frame": [560, 410, 160, 24], "win": WIZ},
                {"role": "AXButton", "title": "Create", "frame": [900, 566, 90, 28], "win": WIZ},
            ]
        for e in els:
            e["shown"] = self.rename.get(e["title"], e["title"])
        return els

    def _at(self, x: int, y: int) -> dict | None:
        for e in self._elements():
            fx, fy, fw, fh = e["frame"]
            if fx <= x < fx + fw and fy <= y < fy + fh:
                return e
        return None

    @staticmethod
    def _center(e: dict) -> tuple[int, int]:
        fx, fy, fw, fh = e["frame"]
        return fx + fw // 2, fy + fh // 2

    # ---- Driver API
    def find_element(self, step):
        el = (step.get("target") or {}).get("element") or {}
        for e in self._elements():
            if e["role"] == el.get("role") and e["shown"] == el.get("title"):
                x, y = self._center(e)
                return Match("element", x, y, handle=e, detail={"matched": {"role": e["role"], "title": e["shown"]}})
        return None

    def find_text(self, label, step):
        if not self.ocr_text:
            return None
        for e in self._elements():
            if e["shown"].lower() == label.lower():
                x, y = self._center(e)
                return Match("ocr", x, y, detail={"text": e["shown"]})
        return None

    def window_bounds(self, step):
        for w in self.windows:
            if w["title"] == step.get("window"):
                return w["bounds"]
        return self.windows[0]["bounds"] if self.windows else None

    def act(self, step, match, text=None):
        a = step["action"]
        if a == "open_app":
            self.log.append(f"open {step.get('app')}")
            return
        if a in ("click", "double_click", "triple_click", "right_click"):
            e = match.handle if (match and match.handle) else self._at(match.x, match.y)
            self.log.append(f"{a} {e['title'] if e else (match.x, match.y)}")
            if not e or e["title"] in self.broken:
                return
            if e["role"] == "AXTextField":
                self.focused = e["title"]
            elif e["title"] == "New Report":
                self.windows.append({"id": self._next_id, "title": WIZ, "app": "Safari",
                                     "bounds": [420, 200, 600, 420]})
                self._next_id += 1
            elif e["title"] == "Create":
                self.windows = [w for w in self.windows if w["title"] != WIZ]
                self.windows[0]["title"] = f"Acme Portal - {self.fields.get('Report title', '')}"
        elif a == "type":
            if match and match.handle and match.handle.get("role") == "AXTextField":
                self.focused = match.handle["title"]
            if self.focused:
                self.fields[self.focused] = self.fields.get(self.focused, "") + (text or "")
            self.log.append(f"type {text!r} into {self.focused}")
        elif a in ("key", "hotkey"):
            keys = step.get("keys")
            self.log.append(f"press {keys}")
            if keys == "tab" and self.focused == "Report title":
                self.focused = "Period end"
            if keys == "cmd+s":
                self.saved = True

    # ---- a simulated multi-page application form, for exercising form_loop
    def load_form(self, pages: list[list[dict]], advance: str = "Continue to next step",
                  finish: str = "Submit application") -> None:
        """pages[i] = [{"role", "title", "value", "options"}]: what page i asks for."""
        self.form_pages = [[dict(c) for c in page] for page in pages]
        self.page = 0
        self.advance_label, self.finish_label = advance, finish
        self.pressed: list[str] = []

    def load_list(self, items: list[dict]) -> None:
        """items = [{"text", "button", "detail", "detail_only"}].

        `detail` is what the window says once the row is opened, and `detail_only` means the
        button is only there — which is how a job board works: the badge and the apply button
        live in the panel, not on the card.
        """
        self.list_rows = [dict(i) for i in items]
        self.clicked_row: dict | None = None
        self.opened_row: dict | None = None

    def _form_pages(self, step):
        pages = getattr(self, "form_pages", None)
        if not pages or self.page >= len(pages):
            return {"controls": [], "buttons": [], "progress": None}
        controls = [{**c, "handle": (self.page, i)} for i, c in enumerate(pages[self.page])]
        last = self.page == len(pages) - 1
        label = self.finish_label if last else self.advance_label
        return {"controls": controls, "buttons": [{"title": label, "handle": ("btn", self.page)}],
                "progress": f"{int(100 * self.page / max(1, len(pages) - 1))} percent"}

    def fill(self, control, value):
        page, i = control["handle"]
        self.form_pages[page][i]["value"] = value
        self.log.append(f"fill {control.get('title')!r} = {value!r}")
        return True

    def read_form(self, step):
        form = self._form_pages(step)
        row = getattr(self, "opened_row", None)
        if row and row.get("button"):  # an opened row's panel brings its own buttons
            form["buttons"] = form["buttons"] + [{"title": row["button"], "handle": ("apply", row["index"])}]
        return form

    def press(self, control):
        kind, ref = control.get("handle", (None, None))
        if kind == "btn":
            self.pressed.append(control["title"])
            if control["title"] != self.finish_label:
                self.page += 1
        elif kind == "row":  # opening a job shows its detail panel
            self.opened_row = self.list_rows[ref]
            self.log.append(f"open {self.list_rows[ref]['text'][:40]!r}")
        elif kind == "apply":
            self.clicked_row = self.list_rows[ref]
            self.log.append(f"press {control.get('title')!r}")
        return True

    def list_items(self, step):
        """Rows carry an apply button only when the list itself shows one."""
        out = []
        for i, r in enumerate(getattr(self, "list_rows", [])):
            r["index"] = i
            controls = [] if r.get("detail_only") else [
                {"role": "AXButton", "title": r["button"], "handle": ("apply", i)}]
            out.append({"index": i, "text": r["text"], "handle": ("row", i), "controls": controls})
        return out

    def window_text(self, step):
        """What the window says, including the detail panel of whichever row is open."""
        row = getattr(self, "opened_row", None)
        return f"{row.get('text', '')} · {row.get('detail', '')}" if row else ""

    def human_types(self, field: str, value: str) -> None:
        """What a person does when the executor hands a secret step to them."""
        self.fields[field] = value

    def snapshot(self):
        return {"front_app": "Safari",
                "windows": [{"id": w["id"], "title": w["title"], "app": w["app"]} for w in self.windows]}

    def screenshot(self, path: Path) -> bool:
        return False
