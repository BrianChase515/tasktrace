"""macOS driver: finds targets through Accessibility, Vision OCR, or window geometry,
and acts by pressing elements directly (AXPress) or posting mouse/keyboard events.

Needs Accessibility permission for the app running Python (to post events), and
Screen Recording for OCR fallback.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path

import Quartz
from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
)

from .. import axcontext, ocr
from ..compile import SPECIAL_KEYS
from ..recorder_macos import (
    _ax,
    _ax_frame,
    _ax_str,
    _dialogish,
    _snapshot,
    _window_list,
    enable_app_accessibility,
    restore_app_accessibility,
)
from . import Driver, Match

ANSI = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "`": 50,
}
KEYCODES = {**ANSI, **{name: code for code, name in SPECIAL_KEYS.items()}}
FLAGS = {"cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift,
         "opt": Quartz.kCGEventFlagMaskAlternate, "ctrl": Quartz.kCGEventFlagMaskControl}
MAX_NODES = 4000


def _post(ev) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _is_secure_el(el) -> bool:
    return "AXSecureTextField" in (_ax_str(el, "AXSubrole"), _ax_str(el, "AXRole"))


def _own_label(el) -> str:
    for attr in ("AXTitle", "AXDescription", "AXPlaceholderValue", "AXHelp"):
        v = _ax_str(el, attr, 200)
        if v:
            return v
    tui = _ax(el, "AXTitleUIElement")  # a field labelled by the text beside it
    if tui is not None:
        return _ax_str(tui, "AXValue", 200) or _ax_str(tui, "AXTitle", 200) or ""
    return ""


def _fieldset_label(el, depth: int = 5) -> str:
    """The question a group of radio buttons belongs to."""
    cur = _ax(el, "AXParent")
    for _ in range(depth):
        if cur is None:
            break
        if _ax_str(cur, "AXSubrole") == "AXFieldset" or _ax_str(cur, "AXRole") in ("AXRadioGroup", "AXGroup"):
            label = _ax_str(cur, "AXTitle", 200) or _ax_str(cur, "AXDescription", 200)
            if label and len(label) > 12:
                return label
        cur = _ax(cur, "AXParent")
    return ""


def _label_of(el) -> str:
    """What this control is called, from whichever attribute the app bothered to set.

    A radio button is usually called "Yes". On its own that is not a question, and an answer
    stored against it would tick "Yes" in an unrelated form, so it is qualified with the
    question it sits under: "Do you require sponsorship... — Yes".
    """
    own = _own_label(el)
    if _ax_str(el, "AXRole") in ("AXRadioButton", "AXCheckBox", "AXSwitch"):
        group = _fieldset_label(el)
        if group and group.lower() not in own.lower():
            return f"{group} — {own}" if own else group
    return own


def _menu_items(el) -> list:
    menu = next((c for c in (_ax(el, "AXChildren") or []) if _ax_str(c, "AXRole") == "AXMenu"), None)
    return list(_ax(menu, "AXChildren") or []) if menu is not None else []


def _menu_options(el) -> list[str]:
    """The choices in a dropdown, without opening it where the app will tell us."""
    out = []
    for item in _menu_items(el):
        t = _ax_str(item, "AXTitle", 120)
        if t:
            out.append(t)
    return out[:40]


def _descend(el, limit: int = 400):
    """Every element inside this one, breadth-first and bounded."""
    queue, seen = deque([el]), 0
    while queue and seen < limit:
        cur = queue.popleft()
        seen += 1
        yield cur
        for child in _ax(cur, "AXChildren") or []:
            queue.append(child)


class MacDriver(Driver):
    name = "macos"

    def __init__(self):
        self._ax_enabled: dict = {}  # pid -> attributes switched on, restored in close()

    def _expose(self, app) -> None:
        """Ask the app to expose its full accessibility tree (needed for Chrome pages and Electron apps)."""
        pid = int(app.processIdentifier())
        if pid not in self._ax_enabled:
            self._ax_enabled[pid] = enable_app_accessibility(pid, str(app.bundleIdentifier() or ""))
            if self._ax_enabled[pid]:
                time.sleep(0.6)  # the app builds its tree asynchronously

    def close(self):
        for pid, attrs in self._ax_enabled.items():
            restore_app_accessibility(pid, attrs)

    # ---- apps and windows
    def _app(self, step: dict):
        name = step.get("app")
        apps = NSWorkspace.sharedWorkspace().runningApplications()
        for a in apps:
            if name and str(a.localizedName() or "") == name:
                return a
        return NSWorkspace.sharedWorkspace().frontmostApplication()

    def prepare(self, step):
        if step.get("action") == "open_app":
            return
        app = self._app(step)
        if app is None:
            return
        self._expose(app)
        if not app.isActive():
            app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
            time.sleep(0.4)

    def _window_el(self, step):
        app = self._app(step)
        if app is None:
            return None
        app_el = AXUIElementCreateApplication(app.processIdentifier())
        wins = _ax(app_el, "AXWindows") or []
        want = step.get("window")
        for w in wins:
            if want and _ax_str(w, "AXTitle") == want:
                return w
        return _ax(app_el, "AXFocusedWindow") or (wins[0] if wins else app_el)

    def _cg_window(self, step) -> dict | None:
        app = self._app(step)
        pid = int(app.processIdentifier()) if app is not None else None
        wins = [w for w in _window_list() if w["pid"] == pid]
        for w in wins:
            if w["title"] == step.get("window"):
                return w
        return wins[0] if wins else None

    # ---- locating
    def find_element(self, step):
        want = (step.get("target") or {}).get("element") or {}
        if not want.get("role"):
            return None
        root = self._window_el(step)
        if root is None:
            return None
        labels = {k: want[k] for k in ("identifier", "title", "description", "placeholder") if want.get(k)}
        if not labels:
            return None
        best, best_score, seen = None, 0, 0
        queue = deque([root])
        while queue and seen < MAX_NODES:
            el = queue.popleft()
            seen += 1
            if _ax_str(el, "AXRole") == want["role"]:
                score = 0
                for key, attr, weight in (("identifier", "AXIdentifier", 4), ("title", "AXTitle", 3),
                                          ("description", "AXDescription", 3),
                                          ("placeholder", "AXPlaceholderValue", 2)):
                    if key in labels and _ax_str(el, attr) == labels[key]:
                        score += weight
                if score > best_score:
                    best, best_score = el, score
            for child in _ax(el, "AXChildren") or []:
                queue.append(child)
        if best is None:
            return None
        frame = _ax_frame(best)
        x, y = (frame[0] + frame[2] // 2, frame[1] + frame[3] // 2) if frame else (None, None)
        return Match("element", x, y, handle=best, detail={"score": best_score, "searched": seen})

    def find_text(self, label, step):
        w = self._cg_window(step)
        if not w:
            return None
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.png"
            subprocess.run(["screencapture", "-x", "-o", "-l", str(w["id"]), str(p)],
                           capture_output=True, timeout=10, check=False)
            items = ocr.ocr_image(p)
        want = label.strip().lower()
        exact = [o for o in items if o["text"].strip().lower() == want]
        partial = [o for o in items if want in o["text"].strip().lower()]
        hit = (exact or partial or [None])[0]
        if hit is None:
            return None
        bx, by, bw, bh = w["bounds"]
        x0, y0, ww, hh = hit["box"]
        return Match("ocr", int(bx + (x0 + ww / 2) * bw), int(by + (y0 + hh / 2) * bh),
                     detail={"text": hit["text"], "exact": bool(exact)})

    def window_bounds(self, step):
        w = self._cg_window(step)
        return w["bounds"] if w else None

    # ---- acting
    def _click(self, x, y, button="left", clicks=1):
        down_t, up_t, btn = {
            "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
            "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight),
        }[button]
        _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), btn))
        time.sleep(0.05)
        for n in range(1, clicks + 1):
            for t in (down_t, up_t):
                ev = Quartz.CGEventCreateMouseEvent(None, t, (x, y), btn)
                Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, n)
                _post(ev)
                time.sleep(0.03)

    def _drag(self, x, y, x2, y2):
        _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x, y), 0))
        for i in range(1, 11):
            px, py = x + (x2 - x) * i / 10, y + (y2 - y) * i / 10
            _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, (px, py), 0))
            time.sleep(0.02)
        _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x2, y2), 0))

    def _type(self, text: str):
        for ch in text:
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                _post(ev)
            time.sleep(0.012)

    def _key(self, combo: str, times: int = 1):
        *mods, key = combo.split("+")
        code = KEYCODES.get(key)
        if code is None:
            raise ValueError(f"no key code for {key!r}")
        flags = 0
        for m in mods:
            flags |= FLAGS[m]
        for _ in range(times):
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
                Quartz.CGEventSetFlags(ev, flags)
                _post(ev)
            time.sleep(0.05)

    def act(self, step, match, text=None):
        a = step["action"]
        if a == "open_app":
            subprocess.run(["open", "-a", step["app"]], capture_output=True, timeout=30, check=True)
            time.sleep(1.5)
            app = self._app(step)
            if app is not None:
                self._expose(app)
            return
        if a == "click" and match and match.strategy == "element" and match.handle is not None:
            if AXUIElementPerformAction(match.handle, "AXPress") == 0:
                return
        if a in ("click", "double_click", "triple_click", "right_click"):
            clicks = {"double_click": 2, "triple_click": 3}.get(a, 1)
            self._click(match.x, match.y, "right" if a == "right_click" else "left", clicks)
        elif a == "drag":
            tgt = step.get("target") or {}
            to = tgt.get("to_xy") or [match.x, match.y]
            src = tgt.get("abs_xy") or [match.x, match.y]
            self._drag(match.x, match.y, match.x + to[0] - src[0], match.y + to[1] - src[1])
        elif a == "type":
            if match and match.handle is not None:
                AXUIElementSetAttributeValue(match.handle, "AXFocused", True)
                time.sleep(0.1)
            self._type(text or "")
        elif a in ("key", "hotkey"):
            self._key(step["keys"], step.get("times", 1))
        elif a == "scroll":
            sc = step.get("scroll") or {}
            if match and match.x is not None:
                _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (match.x, match.y), 0))
            _post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2,
                                                        int(sc.get("dy", 0)), int(sc.get("dx", 0))))

    # ---- reading the page as it is now
    def _form_root(self, step):
        """The panel being filled in, if there is one, else the window.

        Reading controls from a whole browser window drags in the site's navigation; a form is
        a panel inside it, and on a wizard that panel is the one carrying the progress indicator.
        """
        win = self._window_el(step)
        if win is None:
            return None
        bounds = self.window_bounds(step)
        best, queue, seen = None, deque([win]), 0
        while queue and seen < MAX_NODES:
            el = queue.popleft()
            seen += 1
            role = _ax_str(el, "AXRole")
            if role in ("AXGroup", "AXScrollArea", "AXSheet") and _dialogish(el, role, bounds):
                best = el  # keep descending: the innermost panel wins
            for child in _ax(el, "AXChildren") or []:
                queue.append(child)
        return best or win

    def read_form(self, step):
        root = self._form_root(step)
        if root is None:
            return {"controls": [], "buttons": [], "progress": None}
        controls, buttons, handles = [], [], {}
        queue, seen = deque([(root, 0)]), 0
        while queue and seen < MAX_NODES:
            el, depth = queue.popleft()
            seen += 1
            role = _ax_str(el, "AXRole")
            if role in axcontext.INTERACTIVE_ROLES or _is_secure_el(el):
                item = {"role": "AXSecureTextField" if _is_secure_el(el) else role,
                        "title": _label_of(el), "handle": el}
                if not _is_secure_el(el):
                    item["value"] = _ax_str(el, "AXValue", 200) or ""
                item["placeholder"] = _ax_str(el, "AXPlaceholderValue") or ""
                if role in ("AXPopUpButton", "AXComboBox", "AXMenuButton"):
                    item["options"] = _menu_options(el)
                if item["title"] or item["placeholder"]:
                    (buttons if role in ("AXButton", "AXLink") else controls).append(item)
                    handles[id(el)] = el
            for child in _ax(el, "AXChildren") or []:
                queue.append((child, depth + 1))
        progress = (axcontext.progress_strings(_snapshot(root, max_nodes=400, depth=12)) or [None])[0]
        return {"controls": controls, "buttons": buttons, "progress": progress}

    def fill(self, control, value):
        el, role = control.get("handle"), control.get("role")
        if el is None:
            return False
        if role in ("AXCheckBox", "AXRadioButton", "AXSwitch", "AXToggle"):
            want = str(value).strip().lower() in ("1", "true", "yes", "on", "checked")
            if (str(_ax_str(el, "AXValue") or "0") in ("1", "true")) != want:
                AXUIElementPerformAction(el, "AXPress")
            return True
        if role in ("AXPopUpButton", "AXMenuButton"):
            if AXUIElementPerformAction(el, "AXPress") != 0:
                return False
            time.sleep(0.35)
            for item in _menu_items(el):
                if _ax_str(item, "AXTitle", 200) == str(value):
                    return AXUIElementPerformAction(item, "AXPress") == 0
            self._key("escape")
            return False
        # Text and combo boxes: type it, because setting the value directly often leaves a web
        # app's own state untouched, and the page then behaves as if the field were empty.
        AXUIElementSetAttributeValue(el, "AXFocused", True)
        time.sleep(0.15)
        current = _ax_str(el, "AXValue", 200) or ""
        if current:
            self._key("cmd+a")
        self._type(str(value))
        time.sleep(0.2)
        return True

    def press(self, control):
        el = control.get("handle")
        if el is None:
            return False
        if AXUIElementPerformAction(el, "AXPress") == 0:
            return True
        frame = _ax_frame(el)
        if not frame:
            return False
        self._click(frame[0] + frame[2] // 2, frame[1] + frame[3] // 2)
        return True

    def list_items(self, step):
        win = self._window_el(step)
        if win is None:
            return []
        lists, queue, seen = [], deque([win]), 0
        while queue and seen < MAX_NODES:
            el = queue.popleft()
            seen += 1
            if _ax_str(el, "AXRole") in axcontext.LIST_ROLES:
                lists.append(el)
                continue  # rows come from this list, not from lists nested inside it
            for child in _ax(el, "AXChildren") or []:
                queue.append(child)
        if not lists:
            return []
        biggest = max(lists, key=lambda el: len(_ax(el, "AXChildren") or []))
        out = []
        for i, row in enumerate(list(_ax(biggest, "AXChildren") or [])[:60]):
            node = _snapshot(row, max_nodes=120, depth=7)
            if not node:
                continue
            controls = []
            for el in _descend(row, 400):
                role = _ax_str(el, "AXRole")
                if role in ("AXButton", "AXLink"):
                    controls.append({"role": role, "title": _label_of(el), "handle": el})
            out.append({"index": i, "text": axcontext.text_summary(node), "controls": controls,
                        "handle": row})
        return out

    # ---- observing
    def snapshot(self):
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        return {"front_app": str(front.localizedName()) if front else None,
                "windows": [{"id": w["id"], "title": w["title"], "app": w["app"]} for w in _window_list()]}

    def screenshot(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["screencapture", "-x", str(path)], capture_output=True, timeout=10, check=False)
        return Path(path).exists()
