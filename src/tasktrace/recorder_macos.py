"""macOS recorder.

Captures one task demonstration into a run folder:

    meta.json     host details and the window state when recording started
    events.jsonl  raw, timestamped input and window events
    crops/        small screenshots around each click
    keyframes/    a screenshot of each window that opens, plus the "done" snapshot

Permissions (granted to the terminal app that runs Python, in
System Settings > Privacy & Security): Accessibility, Input Monitoring,
Screen Recording. Run `tasktrace doctor --request` to check and prompt.

Hotkeys while recording (ctrl+option+...):
    N  add a note: why you're about to do the next step
    D  mark done: snapshot what success looks like
    P  pause / resume
    Q  stop recording
"""

from __future__ import annotations

import datetime as dt
import json
import platform
import queue
import re
import signal
import subprocess
import os
import threading
import time
from pathlib import Path

import Quartz
from AppKit import NSEvent, NSRunningApplication, NSScreen, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyElementAtPosition,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
    AXValueGetValue,
)
from CoreFoundation import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRunInMode,
    kCFRunLoopCommonModes,
    kCFRunLoopDefaultMode,
)

from . import __version__, axcontext
from .images import crop_rect

AX_POINT = 1  # kAXValueCGPointType
AX_SIZE = 2  # kAXValueCGSizeType

# Password managers and similar: nothing is recorded while one is in front.
DEFAULT_EXCLUDED = {
    "com.1password.1password",
    "com.agilebits.onepassword7",
    "com.apple.keychainaccess",
    "com.apple.Passwords",
    "com.bitwarden.desktop",
    "com.lastpass.LastPass",
    "com.dashlane.dashlanephonefinal",
}

# Window owners that are system chrome, not part of the task.
IGNORED_WINDOW_OWNERS = {
    "Window Server", "Dock", "Control Center", "SystemUIServer", "Notification Center",
    "NotificationCenter", "osascript", "Spotlight", "TextInputMenuAgent", "Wallpaper",
    "screencaptureui", "Screenshot",
}

HOTKEY_MODS = Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate
KC_N, KC_D, KC_P, KC_Q = 45, 2, 35, 12  # ANSI key codes
FOCUS_RESET_KEYS = {36, 48, 53, 76}  # return, tab, escape, enter
# Click crops are cut to the clicked element's own bounds: see images.crop_rect.

# System prompts that pop up while you work but aren't part of the task.
NOISE_BUNDLES = {"com.apple.UserNotificationCenter", "com.apple.notificationcenterui"}
DOCK_BUNDLE = "com.apple.dock"

# Chromium browsers and Electron apps keep their page/app contents hidden from accessibility
# tools until asked. Without this, every click in a web page reads as an unnamed "scroll area".
CHROMIUM_BUNDLES = {
    "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.dev", "com.google.Chrome.canary",
    "org.chromium.Chromium", "com.microsoft.edgemac", "com.brave.Browser", "company.thebrowser.Browser",
    "com.vivaldi.Vivaldi", "com.operasoftware.Opera",
}

# Roles a person actually means to click. A click on a button's text label resolves to the button.
ACTIONABLE_ROLES = {
    "AXButton", "AXLink", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXTextField",
    "AXTextArea", "AXSearchField", "AXComboBox", "AXMenuItem", "AXMenuBarItem", "AXTab", "AXCell", "AXRow",
    "AXSlider", "AXDisclosureTriangle", "AXDockItem", "AXIncrementor", "AXColorWell", "AXSwitch", "AXToggle",
}
PASS_THROUGH_ROLES = {"AXStaticText", "AXImage", "AXGroup", "AXUnknown", "AXHeading", "AXGenericElement"}
POLL_S = 0.2

# Context around a click: what it was clicked *inside*, not just the leaf under the pointer.
CONTAINER_DEPTH = 8  # ancestors to walk looking for a list row or a modal
SNAPSHOT_NODES, SNAPSHOT_DEPTH = 300, 8
SNAPSHOT_BUDGET_S = 1.2  # never let a slow app's accessibility tree hold up the recording
LIST_SCAN = 200  # children of a list to scan for the row that was clicked

# Inside a single-page app nothing opens a window, so the window poller sees nothing. For a few
# seconds after each input, the focused window's contents are sampled and diffed instead.
UI_NODES, UI_DEPTH = 400, 12
UI_WATCH_S = 3.5
UI_POLL_EVERY = 1  # every poll tick (~0.2 s): slower than this and a change lands on the next step

MOUSE_DOWN = {Quartz.kCGEventLeftMouseDown: "left", Quartz.kCGEventRightMouseDown: "right",
              Quartz.kCGEventOtherMouseDown: "other"}
MOUSE_UP = {Quartz.kCGEventLeftMouseUp: "left", Quartz.kCGEventRightMouseUp: "right",
            Quartz.kCGEventOtherMouseUp: "other"}


# --------------------------------------------------------------------------- helpers

def _mods(flags: int) -> list[str]:
    out = []
    if flags & Quartz.kCGEventFlagMaskControl:
        out.append("ctrl")
    if flags & Quartz.kCGEventFlagMaskAlternate:
        out.append("opt")
    if flags & Quartz.kCGEventFlagMaskShift:
        out.append("shift")
    if flags & Quartz.kCGEventFlagMaskCommand:
        out.append("cmd")
    return out


def _ax(el, attr):
    try:
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
    except Exception:
        return None
    return val if err == 0 else None


def _ax_str(el, attr, limit: int = 120) -> str | None:
    v = _ax(el, attr)
    if isinstance(v, (str, int, float)):
        s = str(v).strip()
        return s[:limit] or None
    return None


def _xy(v):
    return (v.x, v.y) if hasattr(v, "x") else (v[0], v[1])


def _wh(v):
    return (v.width, v.height) if hasattr(v, "width") else (v[0], v[1])


def _ax_frame(el) -> list[int] | None:
    pos, size = _ax(el, "AXPosition"), _ax(el, "AXSize")
    if pos is None or size is None:
        return None
    try:
        ok1, p = AXValueGetValue(pos, AX_POINT, None)
        ok2, s = AXValueGetValue(size, AX_SIZE, None)
        if not (ok1 and ok2):
            return None
        (x, y), (w, h) = _xy(p), _wh(s)
        return [round(x), round(y), round(w), round(h)]
    except Exception:
        return None


def _ax_path(el, depth: int = 6) -> str:
    """Ancestry like: AXWindow 'Report Wizard' > AXGroup > AXButton 'Create'."""
    parts, cur = [], el
    for _ in range(depth):
        if cur is None:
            break
        role = _ax_str(cur, "AXRole") or "?"
        label = (_ax_str(cur, "AXTitle", 60) or _ax_str(cur, "AXDescription", 60)
                 or _ax_str(cur, "AXIdentifier", 60))
        parts.append(f"{role} '{label}'" if label else role)
        if role in ("AXWindow", "AXApplication", "AXMenuBar"):
            break
        cur = _ax(cur, "AXParent")
    return " > ".join(reversed(parts))


def enable_app_accessibility(pid: int, bundle_id: str) -> list[str]:
    """Ask an app to expose its full accessibility tree (web pages in Chromium, Electron app contents).

    Returns the attributes that were switched on, so they can be switched back off later.
    """
    app_el = AXUIElementCreateApplication(pid)
    turned_on = []
    attrs = ["AXManualAccessibility"]  # Electron and Chromium; no side effects
    if bundle_id in CHROMIUM_BUNDLES:
        attrs.append("AXEnhancedUserInterface")  # what VoiceOver sets; Chrome keys off it
    for attr in attrs:
        try:
            if AXUIElementSetAttributeValue(app_el, attr, True) == 0:
                turned_on.append(attr)
        except Exception:
            pass
    return turned_on


def restore_app_accessibility(pid: int, attrs: list[str]) -> None:
    # Leaving AXEnhancedUserInterface on can slow window animations, so put it back.
    if "AXEnhancedUserInterface" in attrs:
        try:
            AXUIElementSetAttributeValue(AXUIElementCreateApplication(pid), "AXEnhancedUserInterface", False)
        except Exception:
            pass


def _actionable(el, depth: int = 5):
    """Walk up from what's under the pointer to the thing the person meant to click.

    Returns (target, text) where text is the label read from a child (e.g. a link's inner text)."""
    cur, text = el, None
    for _ in range(depth):
        role = _ax_str(cur, "AXRole")
        if role in ACTIONABLE_ROLES:
            return cur, text
        if role not in PASS_THROUGH_ROLES:
            break
        if text is None:
            text = _ax_str(cur, "AXValue", 80) or _ax_str(cur, "AXTitle", 80) or _ax_str(cur, "AXDescription", 80)
        parent = _ax(cur, "AXParent")
        if parent is None:
            break
        cur = parent
    return el, None


def _element_info(el) -> tuple[dict, bool]:
    info: dict = {}
    for key, attr in (("role", "AXRole"), ("subrole", "AXSubrole"), ("title", "AXTitle"),
                      ("description", "AXDescription"), ("placeholder", "AXPlaceholderValue"),
                      ("identifier", "AXIdentifier"), ("help", "AXHelp")):
        v = _ax_str(el, attr)
        if v:
            info[key] = v
    if "title" not in info:  # text fields are often labelled by a sibling
        tui = _ax(el, "AXTitleUIElement")
        if tui is not None:
            v = _ax_str(tui, "AXValue") or _ax_str(tui, "AXTitle")
            if v:
                info["title"] = v
    secure = "AXSecureTextField" in (info.get("subrole"), info.get("role"))
    if not secure:
        v = _ax_str(el, "AXValue", 80)
        if v:
            info["value"] = v
    if "title" not in info and "description" not in info and info.get("role") in ("AXButton", "AXLink",
                                                                               "AXMenuItem", "AXTab", "AXCell"):
        # Web buttons and links often carry their label in a child text element.
        for child in (_ax(el, "AXChildren") or [])[:6]:
            v = _ax_str(child, "AXValue", 80) or _ax_str(child, "AXTitle", 80)
            if v:
                info["title"] = v
                break
    frame = _ax_frame(el)
    if frame:
        info["frame"] = frame
    info["path"] = _ax_path(el)
    return info, secure


def _snapshot(el, max_nodes: int = SNAPSHOT_NODES, depth: int = SNAPSHOT_DEPTH,
              budget_s: float = SNAPSHOT_BUDGET_S) -> dict | None:
    """A bounded plain-dict copy of an accessibility subtree, in the shape `axcontext` reads.

    Bounded three ways (nodes, depth, wall clock) because a web page's tree can be enormous
    and an unresponsive app can make every attribute read wait for its timeout.
    """
    deadline = time.monotonic() + budget_s

    def describe(cur) -> dict:
        node: dict = {}
        for key, attr in (("role", "AXRole"), ("subrole", "AXSubrole"),
                          ("role_description", "AXRoleDescription"), ("title", "AXTitle"),
                          ("description", "AXDescription"), ("placeholder", "AXPlaceholderValue"),
                          ("identifier", "AXIdentifier")):
            v = _ax_str(cur, attr, 120)
            if v:
                node[key] = v
        if not axcontext.is_secure(node):
            v = _ax_str(cur, "AXValue", 120)
            if v:
                node["value"] = v
        if "title" not in node and node.get("role") in axcontext.INTERACTIVE_ROLES:
            tui = _ax(cur, "AXTitleUIElement")  # a field labelled by a sibling
            if tui is not None:
                v = _ax_str(tui, "AXValue", 80) or _ax_str(tui, "AXTitle", 80)
                if v:
                    node["title"] = v
        return node

    try:
        root = describe(el)
        # Breadth-first: a depth-first walk spends the whole budget inside the first branch,
        # which on a web page is the navigation, and never reaches the panel being worked in.
        queue, left = [(el, root, 0)], max_nodes - 1
        while queue and left > 0 and time.monotonic() < deadline:
            cur, node, d = queue.pop(0)
            if d >= depth:
                continue
            kids = []
            for child in (_ax(cur, "AXChildren") or [])[:60]:
                if left <= 0:
                    break
                left -= 1
                kid = describe(child)
                kids.append(kid)
                queue.append((child, kid, d + 1))
            if kids:
                node["children"] = kids
        return root or None
    except Exception:
        return None


def _eq(a, b) -> bool:
    try:
        return bool(a == b)
    except Exception:
        return False


MODAL_MAX_AREA = 0.6  # of the window: a "modal" that fills the page is just the page
MODAL_MAX_TITLE = 80  # a run-on title is page text, not a dialog's name


def _dialogish(el, role: str | None, win_bounds: list | None = None) -> bool:
    """Does this ancestor look like a modal, sheet, or a titled form group worth describing?"""
    if role in axcontext.DIALOG_ROLES or _ax_str(el, "AXSubrole") in axcontext.DIALOG_SUBROLES:
        return True
    rd = (_ax_str(el, "AXRoleDescription") or "").lower()
    if "dialog" in rd or "modal" in rd or "alert" in rd:
        return True
    if role not in ("AXGroup", "AXScrollArea"):
        return False
    title = _ax_str(el, "AXTitle") or _ax_str(el, "AXDescription")
    if not title or len(title) > MODAL_MAX_TITLE:
        return False
    # Web pages wrap everything in titled groups and landmark regions. The one a person is
    # working inside is a panel on the page, not the page itself.
    frame = _ax_frame(el)
    if frame and win_bounds and win_bounds[2] > 0 and win_bounds[3] > 0:
        if (frame[2] * frame[3]) > MODAL_MAX_AREA * (win_bounds[2] * win_bounds[3]):
            return False
    return True


def _list_context(list_el, item_el) -> dict | None:
    """Which row of a list was clicked, out of how many, and everything that row says."""
    children = list(_ax(list_el, "AXChildren") or [])[:LIST_SCAN]
    idx = next((i for i, c in enumerate(children) if _eq(c, item_el)), None)
    if idx is None:
        fi = _ax_frame(item_el)
        if fi:
            idx = next((i for i, c in enumerate(children[:60]) if _ax_frame(c) == fi), None)
    shell = {"role": _ax_str(list_el, "AXRole"), "title": _ax_str(list_el, "AXTitle"),
             "description": _ax_str(list_el, "AXDescription"), "children": [{} for _ in children]}
    return axcontext.list_context({k: v for k, v in shell.items() if v is not None}, idx,
                                  _snapshot(item_el, max_nodes=120, depth=7))


def container_context(el, win_bounds: list | None = None) -> tuple[dict | None, object]:
    """Walk up from the clicked element to the list row and/or modal it sits in.

    Also returns the container element itself, which becomes the thing the UI poller watches:
    pointed at a whole browser window it never gets as far as the panel being worked in.
    """
    list_ctx = modal_ctx = watch = None
    cur, child = el, None
    for _ in range(CONTAINER_DEPTH):
        role = _ax_str(cur, "AXRole")
        if role is None:
            break
        if list_ctx is None and role in axcontext.LIST_ROLES and child is not None:
            list_ctx = _list_context(cur, child)
        elif modal_ctx is None and _dialogish(cur, role, win_bounds):
            # A group with one stray control isn't a form: keep walking until something is.
            modal_ctx = axcontext.modal_context(_snapshot(cur), min_controls=2)
            if modal_ctx is not None:
                watch = cur
        if modal_ctx is not None or role in ("AXWindow", "AXApplication", "AXMenuBar"):
            break
        parent = _ax(cur, "AXParent")
        if parent is None:
            break
        child, cur = cur, parent
    return axcontext.merge_context(list_ctx, modal_ctx), watch


def _window_list() -> list[dict]:
    """On-screen app windows, front to back."""
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    infos = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    wins = []
    for w in infos:
        if w.get(Quartz.kCGWindowLayer, 0) != 0:
            continue
        owner = str(w.get(Quartz.kCGWindowOwnerName) or "")
        if owner in IGNORED_WINDOW_OWNERS:
            continue
        b = w.get(Quartz.kCGWindowBounds) or {}
        bounds = [int(b.get("X", 0)), int(b.get("Y", 0)), int(b.get("Width", 0)), int(b.get("Height", 0))]
        if bounds[2] < 40 or bounds[3] < 40:
            continue
        wins.append({
            "id": int(w.get(Quartz.kCGWindowNumber)),
            "app": owner,
            "pid": int(w.get(Quartz.kCGWindowOwnerPID, 0)),
            "title": str(w.get(Quartz.kCGWindowName) or ""),
            "bounds": bounds,
        })
    return wins


def _screens() -> list[dict]:
    screens = list(NSScreen.screens() or [])
    if not screens:
        return []
    main_h = screens[0].frame().size.height
    out = []
    for s in screens:
        f = s.frame()  # Cocoa coordinates: bottom-left origin. Convert to top-left like CG events.
        out.append({
            "frame": [int(f.origin.x), int(main_h - (f.origin.y + f.size.height)),
                      int(f.size.width), int(f.size.height)],
            "scale": float(s.backingScaleFactor()),
        })
    return out


def _notify(text: str) -> None:
    safe = text.replace('"', "'")
    subprocess.Popen(["osascript", "-e", f'display notification "{safe}" with title "tasktrace"'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check_permissions(request: bool = False) -> dict:
    from . import permissions
    return permissions.request_all() if request else permissions.status()


# --------------------------------------------------------------------------- recorder

class Recorder:
    def __init__(self, run_dir: Path, name: str, goal: str, excluded: set[str],
                 crops: bool = True, keyframes: bool = True):
        self.run_dir, self.name, self.goal = run_dir, name, goal
        self.excluded = excluded
        self.crops, self.keyframes = crops, keyframes
        (run_dir / "crops").mkdir(parents=True, exist_ok=True)
        (run_dir / "keyframes").mkdir(parents=True, exist_ok=True)
        self._out = open(run_dir / "events.jsonl", "w", encoding="utf-8")
        self._write_lock = threading.Lock()
        self._win_lock = threading.Lock()
        self.t0 = time.monotonic()
        self.stop = threading.Event()
        self.paused = False
        self.suppress = False  # true while our own note dialog is up
        self.in_excluded = False
        self.jobs: queue.Queue = queue.Queue()
        self.system = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(self.system, 0.15)
        self.windows: list[dict] = []
        self.front: dict = {"name": None, "bundle_id": None, "pid": None}
        self._bundles: dict[int, str] = {}
        self._focus_cache: tuple = (0.0, None, False)
        self.crop_n = self.key_n = self.done_n = self.click_n = 0
        self._ui_prev: dict | None = None
        self._ui_key: tuple | None = None
        self._ui_tick = 0
        self._ui_watch_until = 0.0
        self._ui_last_progress: str | None = None
        self._ui_root = None  # the container last clicked in, set by the worker thread
        self._ui_root_gen = 0
        self._ui_prev_gen = -1
        self.tap = None
        self._own_pid = os.getpid()
        self._ax_enabled: dict = {}  # pid -> accessibility attributes we switched on
        self._ignored_buttons: set = set()

    # ---- plumbing
    def now(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def emit(self, ev: dict) -> None:
        t = ev.pop("t", None)
        ev = {"t": self.now() if t is None else t, **ev}
        line = json.dumps(ev, ensure_ascii=False)
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _bundle(self, pid: int) -> str:
        if pid not in self._bundles:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            self._bundles[pid] = str(app.bundleIdentifier() or "") if app else ""
        return self._bundles[pid]

    def _front_app(self) -> dict:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return {"name": None, "bundle_id": None, "pid": None}
        return {"name": str(app.localizedName() or ""), "bundle_id": str(app.bundleIdentifier() or ""),
                "pid": int(app.processIdentifier())}

    def _visible_windows(self) -> list[dict]:
        return [w for w in _window_list()
                if w["pid"] != self._own_pid and self._bundle(w["pid"]) not in self.excluded]

    @staticmethod
    def _wpub(w: dict) -> dict:
        return {"app": w["app"], "title": w["title"], "window_id": w["id"], "bounds": w["bounds"]}

    def _window_at(self, x: int, y: int) -> dict | None:
        with self._win_lock:
            wins = list(self.windows)
        for w in wins:
            bx, by, bw, bh = w["bounds"]
            if bx <= x < bx + bw and by <= y < by + bh:
                return w
        return None

    def _blocked(self) -> bool:
        # Never record TaskTrace itself (its menu, dialogs, or the terminal it runs in is fine).
        return self.paused or self.suppress or self.in_excluded or self.front.get("pid") == self._own_pid

    # ---- context
    def _ctx_for_window(self, w: dict | None) -> dict:
        if not w:
            return {"app": dict(self.front)}
        return {
            "app": {"name": w["app"], "bundle_id": self._bundle(w["pid"]), "pid": w["pid"]},
            "window": {"title": w["title"], "id": w["id"], "bounds": w["bounds"]},
        }

    def _app_info(self, pid: int) -> dict:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return {"name": str(app.localizedName() or "") if app else "", "bundle_id": self._bundle(pid), "pid": pid}

    def _mouse_ctx(self, x: int, y: int) -> tuple[dict | None, bool, object]:
        """Context for a click, or (None, False, None) for clicks that aren't part of the task
        (TaskTrace's own menu and dialogs, system notification prompts).

        The third value is the accessibility element that was clicked; its surrounding container
        is described on the worker thread, off the event tap.
        """
        win = self._window_at(x, y)
        ctx = self._ctx_for_window(win)
        secure, target = False, None
        try:
            err, el = AXUIElementCopyElementAtPosition(self.system, float(x), float(y), None)
            if err == 0 and el is not None:
                pid = None
                try:
                    perr, pid = AXUIElementGetPid(el, None)
                    pid = pid if perr == 0 else None
                except Exception:
                    pass
                if pid == self._own_pid:
                    return None, False, None
                if pid is not None:
                    owner = self._bundle(pid)
                    if owner in NOISE_BUNDLES:
                        return None, False, None
                    if not win or win["pid"] != pid:
                        # The Dock, the menu bar, a popover: the window underneath isn't what was clicked.
                        ctx = {"app": self._app_info(pid)}
                    self._ensure_ax(pid, owner)
                target, child_text = _actionable(el)
                ctx["element"], secure = _element_info(target)
                if child_text and not (ctx["element"].get("title") or ctx["element"].get("description")):
                    ctx["element"]["title"] = child_text
        except Exception:
            pass
        return ctx, secure, target

    def _ensure_ax(self, pid: int, bundle_id: str) -> None:
        if pid in self._ax_enabled or pid == self._own_pid or bundle_id in (DOCK_BUNDLE, ""):
            return
        self._ax_enabled[pid] = enable_app_accessibility(pid, bundle_id)

    def _focus_ctx(self) -> tuple[dict, bool]:
        now = time.monotonic()
        cached_at, ctx, secure = self._focus_cache
        if ctx is not None and now - cached_at < 0.3:
            return ctx, secure
        with self._win_lock:
            fw = next((w for w in self.windows if w["pid"] == self.front.get("pid")), None)
        ctx, secure = self._ctx_for_window(fw), False
        el = _ax(self.system, "AXFocusedUIElement")
        if el is not None:
            ctx["element"], secure = _element_info(el)
        self._focus_cache = (now, ctx, secure)
        return ctx, secure

    # ---- event tap callback (runs on the main run loop)
    def _on_event(self, proxy, etype, event, refcon):
        try:
            if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                Quartz.CGEventTapEnable(self.tap, True)
            elif etype == Quartz.kCGEventKeyDown:
                self._on_key(event)
            elif etype in MOUSE_DOWN:
                self._on_mouse(event, MOUSE_DOWN[etype], down=True)
            elif etype in MOUSE_UP:
                self._on_mouse(event, MOUSE_UP[etype], down=False)
            elif etype == Quartz.kCGEventScrollWheel:
                self._on_scroll(event)
        except Exception as exc:  # never let the tap die
            self.emit({"type": "error", "where": "tap", "error": repr(exc)[:300]})
        return event

    def _on_key(self, event) -> None:
        flags = Quartz.CGEventGetFlags(event)
        kc = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
        if (flags & HOTKEY_MODS) == HOTKEY_MODS and not (flags & Quartz.kCGEventFlagMaskCommand):
            if kc == KC_Q:
                self.stop.set()
                return
            if kc == KC_P:
                self.paused = not self.paused
                self.emit({"type": "paused" if self.paused else "resumed", "reason": "user"})
                _notify("Paused" if self.paused else "Recording")
                return
            if kc == KC_N:
                threading.Thread(target=self._ask_note, daemon=True).start()
                return
            if kc == KC_D:
                self._mark_done()
                return
        if self._blocked():
            return
        self._ui_watch_until = time.monotonic() + UI_WATCH_S
        ns = NSEvent.eventWithCGEvent_(event)
        chars = str(ns.characters() or "") if ns else ""
        base = str(ns.charactersIgnoringModifiers() or "") if ns else ""
        ctx, secure = self._focus_ctx()
        ev = {"type": "key", "keycode": kc, "mods": _mods(flags), "ctx": ctx}
        if secure:
            ev["secure"] = True
            ev["chars"] = "•" if chars else ""  # never store what was typed
        else:
            ev["chars"], ev["base"] = chars, base
        if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
            ev["repeat"] = True
        if kc in FOCUS_RESET_KEYS:
            self._focus_cache = (0.0, None, False)
        self.emit(ev)

    def _on_mouse(self, event, button: str, down: bool) -> None:
        if self._blocked():
            return
        p = Quartz.CGEventGetLocation(event)
        x, y = int(round(p.x)), int(round(p.y))
        if not down:
            if button in self._ignored_buttons:
                self._ignored_buttons.discard(button)
                return
            self.emit({"type": "mouse_up", "button": button, "x": x, "y": y})
            return
        t = self.now()
        self._focus_cache = (0.0, None, False)
        self._ui_watch_until = time.monotonic() + UI_WATCH_S
        ctx, secure, target = self._mouse_ctx(x, y)
        if ctx is None:  # a click on TaskTrace's own menu or dialog
            self._ignored_buttons.add(button)
            return
        self.click_n += 1
        ev = {
            "t": t, "type": "mouse_down", "button": button, "x": x, "y": y, "ref": self.click_n,
            "clicks": int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventClickState)),
            "mods": _mods(Quartz.CGEventGetFlags(event)), "ctx": ctx,
        }
        # Describing the container means walking the tree, which is far too slow for the event
        # tap (macOS disables a tap that takes too long), so the worker thread does it.
        if target is not None:
            self.jobs.put(("container", target, self.click_n, (ctx.get("window") or {}).get("bounds")))
        if self.crops and not secure:
            self.crop_n += 1
            rel = f"crops/{self.crop_n:04d}.png"
            rect = crop_rect(x, y, (ctx.get("element") or {}).get("frame"))
            ev["crop"], ev["crop_rect"] = rel, rect
            self.jobs.put(("crop", rect, rel))
        self.emit(ev)

    def _on_scroll(self, event) -> None:
        if self._blocked():
            return
        p = Quartz.CGEventGetLocation(event)
        x, y = int(round(p.x)), int(round(p.y))
        dy = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis1))
        dx = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis2))
        if dx == 0 and dy == 0:
            return
        self.emit({"type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy,
                   "ctx": self._ctx_for_window(self._window_at(x, y))})

    # ---- hotkey actions
    def _ask_note(self) -> None:
        self.suppress = True
        try:
            script = ('text returned of (display dialog "Why / what are you about to do?" '
                      'default answer "" with title "tasktrace note" '
                      'buttons {"Cancel", "Add note"} default button "Add note")')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            text = r.stdout.strip()
            if r.returncode == 0 and text:
                self.emit({"type": "note", "text": text[:500]})
        except Exception as exc:
            self.emit({"type": "error", "where": "note", "error": repr(exc)[:300]})
        finally:
            time.sleep(0.4)  # let focus return to the task before capturing again
            self.suppress = False

    def _mark_done(self) -> None:
        self.done_n += 1
        rel = "keyframes/done.png" if self.done_n == 1 else f"keyframes/done-{self.done_n}.png"
        self.emit({"type": "done_marker", "screenshot": rel})
        self.jobs.put(("done", rel))
        _notify("Marked as done")

    # ---- background threads
    def _poll_loop(self) -> None:
        prev = {w["id"]: w for w in self.windows}
        prev_bundle = self.front.get("bundle_id")
        prev_focus = None
        while not self.stop.is_set():
            try:
                wins, front = self._visible_windows(), self._front_app()
            except Exception:
                time.sleep(POLL_S)
                continue
            with self._win_lock:
                self.windows, self.front = wins, front
            for w in wins:  # new apps get asked to expose their contents as soon as they appear
                self._ensure_ax(w["pid"], self._bundle(w["pid"]))
            excluded = front.get("bundle_id") in self.excluded
            if excluded != self.in_excluded:
                self.in_excluded = excluded
                self.emit({"type": "paused" if excluded else "resumed", "reason": "excluded_app",
                           "app": front.get("name") if excluded else None})
            cur = {w["id"]: w for w in wins}
            if self.paused or self.suppress or excluded or front.get("pid") == self._own_pid:
                prev, prev_bundle = cur, front.get("bundle_id")
                time.sleep(POLL_S)
                continue
            for wid, w in cur.items():
                if wid not in prev:
                    self.emit({"type": "window", "change": "opened", **self._wpub(w)})
                    if self.keyframes:
                        self.jobs.put(("window", wid))
                elif w["title"] != prev[wid]["title"]:
                    self.emit({"type": "window", "change": "retitled", "from": prev[wid]["title"], **self._wpub(w)})
            for wid, w in prev.items():
                if wid not in cur:
                    self.emit({"type": "window", "change": "closed", **self._wpub(w)})
            if front.get("bundle_id") != prev_bundle:
                self.emit({"type": "app", "change": "activated", "app": front.get("name"),
                           "bundle_id": front.get("bundle_id")})
                prev_bundle = front.get("bundle_id")
            fw = next((w for w in wins if w["pid"] == front.get("pid")), None)
            fkey = (fw["id"], fw["title"]) if fw else None
            if fw and fkey != prev_focus:
                self.emit({"type": "window", "change": "focused", **self._wpub(fw)})
            prev_focus, prev = fkey, cur
            self._ui_tick += 1
            if self._ui_tick % UI_POLL_EVERY == 0 and time.monotonic() < self._ui_watch_until:
                self._poll_ui(front, fkey)
            time.sleep(POLL_S)

    def _poll_ui(self, front: dict, window_key) -> None:
        """Sample the focused window's contents and emit what changed since the last sample.

        This is what catches single-page apps: a modal appearing, a checkbox flipping, a wizard
        moving from 20% to 40% — none of which open or close a window.
        """
        pid = front.get("pid")
        if not pid or pid == self._own_pid:
            return
        try:
            root, gen = self._ui_root, self._ui_root_gen  # read once: the worker may replace them
            if root is None:
                app_el = AXUIElementCreateApplication(pid)
                AXUIElementSetMessagingTimeout(app_el, 0.25)
                root = _ax(app_el, "AXFocusedWindow") or _ax(app_el, "AXMainWindow")
            if root is None:
                return
            node = _snapshot(root, max_nodes=UI_NODES, depth=UI_DEPTH)
            if not node:  # the panel went away, or the app stopped answering: claim nothing
                self._ui_prev, self._ui_root = None, None
                return
            state = axcontext.ui_state(node, max_nodes=UI_NODES)
            key = (pid, window_key)
            if key != self._ui_key:
                self._ui_last_progress = None  # a different window is a fresh baseline
            else:
                for ch in axcontext.diff_ui_state(self._ui_prev, state):
                    # Toggles and progress are matched by label, so they survive a change of
                    # what we're watching. Dialogs are matched by presence, so they don't.
                    if ch["change"].startswith("dialog_") and gen != self._ui_prev_gen:
                        continue
                    if ch["change"] == "progress":
                        # Switching panels can make progress look like it appeared rather than
                        # moved. Remember the last value so it reads as a step, not a surprise.
                        if ch["to"] == self._ui_last_progress:
                            continue
                        ch["from"] = ch.get("from") or self._ui_last_progress
                        self._ui_last_progress = ch["to"]
                    self.emit({"type": "ui", **ch})
            self._ui_key, self._ui_prev, self._ui_prev_gen = key, state, gen
        except Exception as exc:
            self.emit({"type": "error", "where": "ui_poll", "error": repr(exc)[:300]})

    def _worker(self) -> None:
        while not (self.stop.is_set() and self.jobs.empty()):
            try:
                job = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if job[0] == "container":
                    _, el, ref, bounds = job
                    cc, watch = container_context(el, bounds)
                    if cc:
                        self.emit({"type": "container", "ref": ref, "context": cc})
                    # Watch the panel being worked in, not the whole window.
                    self._ui_root, self._ui_root_gen = watch, self._ui_root_gen + 1
                elif job[0] == "crop":
                    _, rect, rel = job
                    region = ",".join(str(v) for v in rect)
                    subprocess.run(["screencapture", "-x", "-R", region, str(self.run_dir / rel)],
                                   timeout=10, check=False, capture_output=True)
                elif job[0] == "window":
                    wid = job[1]
                    self.key_n += 1
                    rel = f"keyframes/{self.key_n:04d}.png"
                    subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), str(self.run_dir / rel)],
                                   timeout=10, check=False, capture_output=True)
                    if (self.run_dir / rel).exists():
                        self.emit({"type": "keyframe", "reason": "window_opened", "window_id": wid,
                                   "screenshot": rel})
                elif job[0] == "done":
                    subprocess.run(["screencapture", "-x", str(self.run_dir / job[1])],
                                   timeout=10, check=False, capture_output=True)
            except Exception as exc:
                self.emit({"type": "error", "where": "capture", "error": repr(exc)[:300]})

    # ---- lifecycle
    def start(self) -> None:
        """Install the event tap on the current (main) run loop and start the helper threads.

        Returns immediately. The caller keeps the run loop running: the CLI via run(),
        the menu-bar app via its own NSApplication loop.
        """
        self._started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.front = self._front_app()
        self.windows = self._visible_windows()
        for w in self.windows:
            self._ensure_ax(w["pid"], self._bundle(w["pid"]))
        self._initial = {"front_app": {k: v for k, v in self.front.items() if k != "pid"},
                         "windows": [self._wpub(w) for w in self.windows[:15]]}
        mask = 0
        for t in (*MOUSE_DOWN, *MOUSE_UP, Quartz.kCGEventKeyDown, Quartz.kCGEventScrollWheel):
            mask |= 1 << t
        self.tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                           Quartz.kCGEventTapOptionListenOnly, mask, self._on_event, None)
        if self.tap is None:
            self._out.close()
            raise PermissionError("Could not watch input. Turn on Input Monitoring and Accessibility, "
                                  "then restart the app.")
        self._src = CFMachPortCreateRunLoopSource(None, self.tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), self._src, kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        self._threads = [threading.Thread(target=self._poll_loop, daemon=True),
                         threading.Thread(target=self._worker, daemon=True)]
        for th in self._threads:
            th.start()

    def finish(self) -> Path:
        """Stop capturing, wait for pending screenshots, and write meta.json. Safe to call once."""
        self.stop.set()
        Quartz.CGEventTapEnable(self.tap, False)
        try:
            from CoreFoundation import CFRunLoopRemoveSource
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), self._src, kCFRunLoopCommonModes)
        except Exception:
            pass
        for th in self._threads:
            th.join(timeout=15)
        for pid, attrs in self._ax_enabled.items():
            restore_app_accessibility(pid, attrs)
        self._out.close()
        meta = {
            "name": self.name,
            "goal": self.goal,
            "started_at": self._started,
            "ended_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "recorder": f"tasktrace {__version__}",
            "host": {"os": f"macOS {platform.mac_ver()[0]}", "machine": platform.machine(),
                     "screens": _screens()},
            "initial": self._initial,
            "excluded_apps": sorted(self.excluded),
        }
        (self.run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return self.run_dir

    def run(self) -> None:
        """CLI: record until ctrl+opt+Q or ctrl+C."""
        self.start()
        old_handler = signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        _notify("Recording. ctrl+opt+Q to stop.")
        try:
            while not self.stop.is_set():
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.25, False)
        finally:
            signal.signal(signal.SIGINT, old_handler)
            self.finish()
            _notify("Recording stopped")


def new_run_dir(home: Path, name: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "task"
    run_dir = home / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def record_run(home: Path, name: str, goal: str = "", exclude: list[str] | None = None,
               crops: bool = True, keyframes: bool = True) -> Path:
    perms = check_permissions()
    missing = [k for k, v in perms.items() if v is False]
    if missing:
        raise PermissionError("Missing macOS permissions: " + ", ".join(missing)
                              + ". Run `tasktrace doctor --request`, grant them to your terminal app, "
                                "then restart the terminal.")
    run_dir = new_run_dir(home, name)
    Recorder(run_dir, name, goal, DEFAULT_EXCLUDED | set(exclude or []), crops, keyframes).run()
    return run_dir
