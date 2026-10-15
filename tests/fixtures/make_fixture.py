"""Generate the sample raw run used by the tests and the README example.

It mirrors what the macOS recorder writes for this demonstration:
create a quarterly report in a web portal (Safari), including a password-field PIN.

    python tests/fixtures/make_fixture.py
"""

import json
from pathlib import Path

HERE = Path(__file__).parent / "run_q3"

SAFARI = {"name": "Safari", "bundle_id": "com.apple.Safari", "pid": 501}
MAIN = {"title": "Acme Portal - Reports", "id": 101, "bounds": [0, 25, 1440, 875]}
WIZ = {"title": "Report Wizard", "id": 102, "bounds": [420, 200, 600, 420]}

events = []


def ev(**kw):
    events.append(kw)


def ctx(win, element=None):
    c = {"app": SAFARI, "window": win}
    if element:
        c["element"] = element
    return c


def click(t, x, y, win, element, crop=None, clicks=1):
    down = dict(t=t, type="mouse_down", button="left", x=x, y=y, clicks=clicks, mods=[], ctx=ctx(win, element))
    if crop:
        down["crop"] = crop
    ev(**down)
    ev(t=t + 70, type="mouse_up", button="left", x=x, y=y)


KEYCODES = {c: i for i, c in enumerate("asdfhgzxcv")}  # cosmetic; compiler uses chars


def type_text(t, text, win, element, secure=False, step=110):
    for ch in text:
        if ch == "\b":
            ev(t=t, type="key", keycode=51, mods=[], chars="\x7f", base="\x7f", ctx=ctx(win, element))
        else:
            k = dict(t=t, type="key", keycode=KEYCODES.get(ch.lower(), 0),
                     mods=["shift"] if ch.isupper() else [], ctx=ctx(win, element))
            if secure:
                k.update(secure=True, chars="•")
            else:
                k.update(chars=ch, base=ch.lower())
            ev(**k)
        t += step
    return t


new_report = {"role": "AXButton", "title": "New Report", "frame": [1240, 128, 110, 30],
              "path": "AXWindow 'Acme Portal - Reports' > AXWebArea > AXGroup 'toolbar' > AXButton 'New Report'"}
title_field = {"role": "AXTextField", "title": "Report title", "placeholder": "e.g. Q1 forecast",
               "frame": [560, 288, 320, 24],
               "path": "AXWindow 'Report Wizard' > AXGroup 'details' > AXTextField 'Report title'"}
period_field = {"role": "AXTextField", "title": "Period end", "placeholder": "YYYY-MM-DD",
                "frame": [560, 330, 160, 24],
                "path": "AXWindow 'Report Wizard' > AXGroup 'details' > AXTextField 'Period end'"}
pin_field = {"role": "AXTextField", "subrole": "AXSecureTextField", "title": "Approval PIN",
             "frame": [560, 410, 160, 24],
             "path": "AXWindow 'Report Wizard' > AXGroup 'approval' > AXTextField 'Approval PIN'"}
create_btn = {"role": "AXButton", "title": "Create", "frame": [900, 566, 90, 28],
              "path": "AXWindow 'Report Wizard' > AXButton 'Create'"}

ev(t=0, type="window", change="focused", app="Safari", title=MAIN["title"], window_id=101, bounds=MAIN["bounds"])
ev(t=800, type="note", text="Starting from the Reports page; New Report is top right")
click(2000, 1290, 142, MAIN, new_report, crop="crops/0001.png")
ev(t=2650, type="window", change="opened", app="Safari", title=WIZ["title"], window_id=102, bounds=WIZ["bounds"])
ev(t=2650, type="window", change="focused", app="Safari", title=WIZ["title"], window_id=102, bounds=WIZ["bounds"])
ev(t=2900, type="keyframe", reason="window_opened", window_id=102, screenshot="keyframes/0001.png")

click(4100, 720, 300, WIZ, title_field, crop="crops/0002.png")
t = type_text(4500, "Q3 forcast\b\b\b\becast", WIZ, title_field)  # typo fixed with backspaces
ev(t=t + 200, type="key", keycode=48, mods=[], chars="\t", base="\t", ctx=ctx(WIZ, title_field))
ev(t=t + 500, type="note", text="Always use the last day of the quarter")
t = type_text(t + 700, "2026-09-30", WIZ, period_field)

click(t + 800, 640, 422, WIZ, pin_field)  # no crop: recorder never screenshots password fields
t = type_text(t + 1300, "4821", WIZ, pin_field, secure=True)

click(t + 900, 940, 580, WIZ, create_btn, crop="crops/0004.png")
tc = t + 900
ev(t=tc + 600, type="window", change="closed", app="Safari", title=WIZ["title"], window_id=102, bounds=WIZ["bounds"])
ev(t=tc + 600, type="window", change="retitled", app="Safari", title="Acme Portal - Q3 forecast",
   window_id=101, bounds=MAIN["bounds"], **{"from": MAIN["title"]})
ev(t=tc + 600, type="window", change="focused", app="Safari", title="Acme Portal - Q3 forecast",
   window_id=101, bounds=MAIN["bounds"])

main2 = dict(MAIN, title="Acme Portal - Q3 forecast")
ev(t=tc + 2500, type="key", keycode=1, mods=["cmd"], chars="s", base="s", ctx=ctx(main2))
ev(t=tc + 4000, type="done_marker", screenshot="keyframes/done.png")
ev(t=tc + 4600, type="key", keycode=8, mods=["ctrl"], chars="\x03", base="c", ctx=ctx(main2))  # ctrl+C to stop

meta = {
    "name": "q3-report",
    "goal": "Create the quarterly forecast report in Acme Portal",
    "started_at": "2026-10-15T14:30:00+00:00",
    "ended_at": "2026-10-15T14:30:22+00:00",
    "recorder": "tasktrace 0.1.0",
    "host": {"os": "macOS 15.6", "machine": "arm64", "screens": [{"frame": [0, 0, 1440, 900], "scale": 2.0}]},
    "initial": {"front_app": {"name": "Safari", "bundle_id": "com.apple.Safari"},
                "windows": [{"app": "Safari", "title": MAIN["title"], "window_id": 101, "bounds": MAIN["bounds"]}]},
    "excluded_apps": ["com.1password.1password"],
}

HERE.mkdir(parents=True, exist_ok=True)
(HERE / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
with open(HERE / "events.jsonl", "w") as f:
    for e in sorted(events, key=lambda e: e["t"]):
        f.write(json.dumps(e, ensure_ascii=False) + "\n")

# Placeholder crops so the image paths in the trace resolve (the real recorder captures the screen).
try:
    from PIL import Image, ImageDraw

    for rel, label in (("crops/0001.png", "New Report"), ("crops/0002.png", "Report title"),
                       ("crops/0004.png", "Create"), ("keyframes/0001.png", "Report Wizard"),
                       ("keyframes/done.png", "Acme Portal - Q3 forecast  Saved")):
        p = HERE / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        im = Image.new("RGB", (400, 160), (246, 246, 248))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([60, 50, 340, 110], radius=10, fill=(0, 113, 227))
        d.text((80, 72), label, fill=(255, 255, 255))
        im.save(p)
except ImportError:
    pass

print(f"wrote {len(events)} events to {HERE}")
