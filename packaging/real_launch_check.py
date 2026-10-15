"""Launch the real app the way a person does, with NO permissions granted, and click through its
first-launch pop-ups from outside the app, like a real mouse. Must run before any permission is
granted to TaskTrace on the build machine. The build shell running this script is allowed to click.

    python real_launch_check.py dist/TaskTrace.app out_dir
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import Quartz
from AppKit import NSRunningApplication
from ApplicationServices import AXUIElementCreateApplication, AXUIElementSetMessagingTimeout

from tasktrace.recorder_macos import _ax, _ax_frame, _ax_str

BUNDLE = "io.github.brianchase515.tasktrace"


def app_pid():
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE)
    return int(apps[0].processIdentifier()) if apps and len(apps) else None


def buttons(pid):
    el = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(el, 3.0)
    found, stack, n = {}, list(_ax(el, "AXWindows") or []), 0
    while stack and n < 800:
        e = stack.pop()
        n += 1
        if _ax_str(e, "AXRole") == "AXButton" and _ax_str(e, "AXTitle"):
            found[_ax_str(e, "AXTitle")] = _ax_frame(e)
        stack.extend(_ax(e, "AXChildren") or [])
    return found


def responsive(pid) -> bool:
    el = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(el, 3.0)
    return _ax(el, "AXRole") is not None


def screen_windows():
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        b = w.get(Quartz.kCGWindowBounds) or {}
        if b.get("Width", 0) < 60 or b.get("Height", 0) < 40:
            continue
        out.append({"owner": str(w.get(Quartz.kCGWindowOwnerName) or ""), "layer": int(w.get(Quartz.kCGWindowLayer, 0)),
                    "name": str(w.get(Quartz.kCGWindowName) or ""),
                    "bounds": [int(b.get("X", 0)), int(b.get("Y", 0)), int(b.get("Width", 0)), int(b.get("Height", 0))]})
    return out[:15]


def click(x, y):
    for t in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, t, (x, y), 0))
        time.sleep(0.05)


def main(app: str, out: str) -> int:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    state = Path.home() / "TaskTraces" / ".app-state.json"
    state.unlink(missing_ok=True)  # behave like a first launch
    report = {"steps": []}
    shot = lambda name: subprocess.run(["screencapture", "-x", str(out / f"{name}.png")], capture_output=True)

    subprocess.run(["open", "-n", app], check=True)
    pid = None
    for _ in range(40):
        pid = app_pid()
        if pid:
            break
        time.sleep(0.5)
    report["pid"] = pid
    if not pid:
        report["error"] = "app did not start"
        (out / "real-launch.json").write_text(json.dumps(report, indent=1))
        return 1

    ok = True
    for want in ("Got it", "Not now"):
        step = {"button": want}
        frame = None
        for _ in range(40):  # up to ~20 s for the pop-up
            try:
                frame = buttons(pid).get(want)
            except Exception as exc:
                step["lookup_error"] = repr(exc)[:200]
            if frame:
                break
            time.sleep(0.5)
        step["found"] = bool(frame)
        step["windows_on_screen"] = screen_windows()
        shot(f"before-{want.replace(' ', '-')}")
        if not frame:
            step["buttons_seen"] = list(buttons(pid).keys()) if responsive(pid) else "app not responding"
            ok = False
            report["steps"].append(step)
            break
        clicks = 0
        while clicks < 3:
            clicks += 1
            click(frame[0] + frame[2] / 2, frame[1] + frame[3] / 2)
            time.sleep(1.5)
            if want not in buttons(pid):
                break
        step["clicks"] = clicks
        step["closed"] = want not in buttons(pid)
        step["responsive_after"] = responsive(pid)
        shot(f"after-{want.replace(' ', '-')}")
        report["steps"].append(step)
        if not step["closed"]:
            ok = False
            # Where is the app stuck? A stack sample of every thread.
            subprocess.run(["sample", str(pid), "3", "-file", str(out / "sample.txt")], capture_output=True)
            report["sample"] = "sample.txt"
            break

    report["ok"] = ok
    report["responsive_at_end"] = responsive(pid)
    (out / "real-launch.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    subprocess.run(["kill", str(pid)])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
