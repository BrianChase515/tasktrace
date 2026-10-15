"""The three macOS permissions tasktrace needs, and how to ask for them nicely.

Asking for a permission adds the app to its list in System Settings automatically,
so nobody has to find the app with the "+" button. We then open the exact settings
page so the person only has to flip one switch.
"""

from __future__ import annotations

import subprocess

import Quartz

PERMISSIONS = {
    "accessibility": {
        "title": "Accessibility",
        "why": "to read which button or field you clicked, and to press it when replaying",
        "pane": "Privacy_Accessibility",
        "tcc": "Accessibility",
    },
    "input_monitoring": {
        "title": "Input Monitoring",
        "why": "to notice your clicks and keystrokes while recording (password fields are never recorded)",
        "pane": "Privacy_ListenEvent",
        "tcc": "ListenEvent",
    },
    "screen_recording": {
        "title": "Screen Recording",
        "why": "to take small screenshots around each click and read on-screen text",
        "pane": "Privacy_ScreenCapture",
        "tcc": "ScreenCapture",
    },
}
ORDER = ["accessibility", "input_monitoring", "screen_recording"]


def _accessibility() -> bool:
    from ApplicationServices import AXIsProcessTrusted
    return bool(AXIsProcessTrusted())


def status() -> dict:
    """{name: True | False | None}. None means this macOS can't report it."""
    pre_sc = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    pre_in = getattr(Quartz, "CGPreflightListenEventAccess", None)
    return {
        "accessibility": _accessibility(),
        "input_monitoring": bool(pre_in()) if pre_in else None,
        "screen_recording": bool(pre_sc()) if pre_sc else None,
    }


def missing() -> list[str]:
    st = status()
    return [k for k in ORDER if st.get(k) is False]


def bundle_id() -> str | None:
    try:
        from AppKit import NSBundle
        b = NSBundle.mainBundle().bundleIdentifier()
        return str(b) if b else None
    except Exception:
        return None


def clear_stale(name: str) -> bool:
    """Remove TaskTrace's existing entry for this permission so macOS asks fresh.

    macOS ties each permission switch to one exact app signature. A switch flipped for an older
    TaskTrace build still shows as ON in System Settings but doesn't count for this build, and
    restarting can't fix that. Clearing the entry lets the person switch on the current app.
    Only TaskTrace's own entry is touched."""
    bid = bundle_id()
    if not bid or not bid.startswith("io.github.brianchase515.tasktrace"):
        return False  # not the installed app (e.g. run from a terminal): leave the terminal's entries alone
    r = subprocess.run(["/usr/bin/tccutil", "reset", PERMISSIONS[name]["tcc"], bid], capture_output=True)
    return r.returncode == 0


def request(name: str) -> None:
    """Ask macOS for one permission. Shows the system prompt and lists the app in Settings."""
    try:
        if name == "accessibility":
            from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        elif name == "input_monitoring" and hasattr(Quartz, "CGRequestListenEventAccess"):
            Quartz.CGRequestListenEventAccess()
        elif name == "screen_recording" and hasattr(Quartz, "CGRequestScreenCaptureAccess"):
            Quartz.CGRequestScreenCaptureAccess()
    except Exception:
        pass


def open_settings(name: str) -> None:
    pane = PERMISSIONS[name]["pane"]
    subprocess.Popen(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"])


def request_all(open_panes: bool = True) -> dict:
    for name in missing():
        request(name)
        if open_panes:
            open_settings(name)
    return status()
