"""Click-test TaskTrace's dialogs the way a person would. Run by the build, inside the built app:

    open -W TaskTrace.app --args --ui-check /path/to/result.json

For each scenario it shows a dialog, finds the button on screen, posts a real mouse click on it,
and records how many clicks it took for the dialog to respond (1 is right; more means the first
clicks were swallowed). It compares the old dialog code with the current one.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time

import Quartz
from AppKit import NSAlert, NSApplication
from ApplicationServices import AXUIElementCreateApplication, AXUIElementSetMessagingTimeout
from PyObjCTools import AppHelper

from .recorder_macos import _ax, _ax_frame, _ax_str

MAX_CLICKS = 3


def legacy_alert(title: str, text: str = "", buttons=("OK",)) -> int:
    """The dialog code TaskTrace shipped up to 0.4.1, kept here only to compare against."""
    app = NSApplication.sharedApplication()
    app.activateIgnoringOtherApps_(True)
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(text)
    for b in buttons:
        a.addButtonWithTitle_(b)
    choice = int(a.runModal()) - 1000
    app.hide_(None)
    return choice


def _find_button(title: str):
    app_el = AXUIElementCreateApplication(os.getpid())
    AXUIElementSetMessagingTimeout(app_el, 2.0)
    stack = list(_ax(app_el, "AXWindows") or [])
    seen = 0
    while stack and seen < 500:
        el = stack.pop()
        seen += 1
        if _ax_str(el, "AXRole") == "AXButton" and _ax_str(el, "AXTitle") == title:
            return _ax_frame(el)
        stack.extend(_ax(el, "AXChildren") or [])
    return None


def _click(x: float, y: float) -> None:
    for t in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        ev = Quartz.CGEventCreateMouseEvent(None, t, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.05)


def _front_app_name() -> str:
    from AppKit import NSWorkspace
    a = NSWorkspace.sharedWorkspace().frontmostApplication()
    return str(a.localizedName()) if a else ""


def run_scenario(name: str, show, button: str) -> dict:
    """show() displays one dialog on the main thread and returns the button index."""
    state = {"done": False, "clicks": 0, "found": False, "aborted": False}

    def clicker():
        deadline = time.monotonic() + 6
        frame = None
        while time.monotonic() < deadline and not state["done"]:
            frame = _find_button(button)
            if frame:
                break
            time.sleep(0.2)
        if not frame:
            state["aborted"] = True
            AppHelper.callAfter(NSApplication.sharedApplication().stopModalWithCode_, 900)
            return
        state["found"] = True
        state["front_app_before_click"] = _front_app_name()
        shots = os.environ.get("TASKTRACE_UICHECK_SHOTS")
        if shots:
            safe = "".join(c if c.isalnum() else "-" for c in name)
            subprocess.run(["screencapture", "-x", os.path.join(shots, f"{safe}.png")], capture_output=True)
        time.sleep(0.4)
        for _ in range(MAX_CLICKS):
            if state["done"]:
                return
            state["clicks"] += 1
            _click(frame[0] + frame[2] / 2, frame[1] + frame[3] / 2)
            time.sleep(1.2)
        if not state["done"]:
            state["aborted"] = True
            AppHelper.callAfter(NSApplication.sharedApplication().stopModalWithCode_, 900)

    th = threading.Thread(target=clicker, daemon=True)
    th.start()
    choice = show()
    state["done"] = True
    th.join(timeout=5)
    ok = state["found"] and not state["aborted"] and state["clicks"] == 1
    return {"name": name, "ok": ok, "clicks": state["clicks"], "choice": choice,
            "button_found": state["found"], "gave_up": state["aborted"],
            "front_app_before_click": state.get("front_app_before_click")}


def _bring_other_app_forward() -> None:
    subprocess.run(["open", "-a", "TextEdit"], capture_output=True, timeout=20)
    time.sleep(2.5)


def run_all(out_path: str) -> None:
    from .app import alert

    results = []

    def scen(name, show, button):
        try:
            results.append(run_scenario(name, show, button))
        except Exception as exc:  # keep going; report it
            results.append({"name": name, "ok": False, "error": repr(exc)[:300]})

    # Old code: a dialog right after another dialog (the first-launch welcome, then the permissions ask).
    scen("old: first dialog", lambda: legacy_alert("Welcome", "", ("Got it",)), "Got it")
    scen("old: next dialog", lambda: legacy_alert("TaskTrace needs your OK", "", ("Set up", "Not now")),
         "Not now")
    _bring_other_app_forward()
    scen("old: another app in front", lambda: legacy_alert("Step 3", "", ("Run it", "Stop")), "Run it")

    # Current code, same situations.
    scen("new: first dialog", lambda: alert("Welcome", "", ("Got it",))[0], "Got it")
    scen("new: next dialog", lambda: alert("TaskTrace needs your OK", "", ("Set up", "Not now"))[0], "Not now")
    scen("new: dialog with a text box", lambda: alert("Record a task", "", ("Start recording", "Cancel"),
                                                      field="")[0], "Start recording")
    _bring_other_app_forward()
    scen("new: another app in front", lambda: alert("Step 3", "", ("Run it", "Stop"))[0], "Run it")
    scen("new: next dialog after that", lambda: alert("Step 4", "", ("Run it", "Stop"))[0], "Stop")

    from ApplicationServices import AXIsProcessTrusted
    pre_post = getattr(Quartz, "CGPreflightPostEventAccess", None)
    report = {"macos": platform.mac_ver()[0],
              "can_read_ui": bool(AXIsProcessTrusted()),
              "can_post_clicks": bool(pre_post()) if pre_post else None,
              "scenarios": results,
              "current_code_ok": all(r.get("ok") for r in results if r["name"].startswith("new"))}
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)
    subprocess.run(["osascript", "-e", 'tell application "TextEdit" to quit saving no'], capture_output=True)


def main(out_path: str) -> None:
    def watchdog():
        time.sleep(150)
        with open(out_path, "w") as f:
            json.dump({"error": "timed out"}, f)
        os._exit(2)

    threading.Thread(target=watchdog, daemon=True).start()
    NSApplication.sharedApplication()

    def go():
        try:
            run_all(out_path)
        finally:
            os._exit(0)

    AppHelper.callLater(1.5, go)
    AppHelper.runEventLoop()
