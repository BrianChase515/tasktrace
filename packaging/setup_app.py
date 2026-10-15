"""Build TaskTrace.app with py2app. Run from this folder on a Mac:

    pip install py2app ..   # installs tasktrace and its dependencies
    python make_icon.py
    python setup_app.py py2app

The app bundles its own Python, so people who download it never install anything.
"""

from setuptools import setup

import tasktrace

VERSION = tasktrace.__version__

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "TaskTrace.icns",
    "packages": ["tasktrace", "rumps"],
    "includes": [
        "objc", "AppKit", "Foundation", "Quartz", "ApplicationServices", "CoreFoundation",
        "Vision", "PyObjCTools.AppHelper",
    ],
    "excludes": ["tkinter", "pytest", "mcp", "PIL", "jsonschema", "unittest"],
    "plist": {
        "CFBundleName": "TaskTrace",
        "CFBundleDisplayName": "TaskTrace",
        "CFBundleIdentifier": "io.github.brianchase515.tasktrace",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,  # menu-bar app: no Dock icon
        "LSMinimumSystemVersion": "13.0",
        "NSHumanReadableCopyright": "MIT License",
        "NSAppleEventsUsageDescription": "TaskTrace shows notes and notifications with AppleScript.",
    },
}

setup(
    name="TaskTrace",
    version=VERSION,
    app=["app_main.py"],
    options={"py2app": OPTIONS},
)
