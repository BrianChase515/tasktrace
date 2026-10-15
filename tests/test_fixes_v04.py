"""Fixes from the first real recording (Chrome, 70 steps)."""

import json

from tasktrace.compile import compile_run
from tasktrace.drivers.fake import FakeDriver
from tasktrace.ocr import pick_label
from tasktrace.plan import make_plan, validate_plan
from tasktrace.replay import Executor, ScriptedUI

CHROME = {"name": "Google Chrome", "bundle_id": "com.google.Chrome", "pid": 700}
WIN = {"title": "Survey", "id": 5, "bounds": [0, 25, 1920, 1000]}


def _run(tmp_path, events):
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    return compile_run(run, use_ocr=False)


def test_dock_click_becomes_open_app(tmp_path):
    dock = {"app": {"name": "Dock", "bundle_id": "com.apple.dock"},
            "element": {"role": "AXDockItem", "subrole": "AXApplicationDockItem", "title": "Google Chrome",
                        "path": "AXApplication 'Dock' > AXList > AXDockItem 'Google Chrome'"}}
    t = _run(tmp_path, [
        {"t": 0, "type": "mouse_down", "button": "left", "x": 625, "y": 1026, "clicks": 1, "ctx": dock},
        {"t": 80, "type": "mouse_up", "button": "left", "x": 625, "y": 1026},
    ])
    step = t["steps"][0]
    assert step["action"] == "open_app" and step["app"] == "Google Chrome"
    assert "abs_xy" not in step["target"] and step["describe"] == "Open Google Chrome"
    plan = make_plan(t)
    assert validate_plan(plan) == []
    drv = FakeDriver()
    audit = Executor(plan, drv, ScriptedUI(), effect_timeout=0.1, poll=0.01).run()
    assert drv.log == ["open Google Chrome"] and audit["entries"][0]["outcome"] == "ok"


def test_unlabeled_field_named_after_question_clicked_first(tmp_path):
    question = {"app": CHROME, "window": WIN, "element": {"role": "AXStaticText",
                                                         "value": "Years of experience with Python?"}}
    field = {"app": CHROME, "window": WIN, "element": {}}
    events = [
        {"t": 0, "type": "mouse_down", "button": "left", "x": 900, "y": 300, "clicks": 1, "ctx": question},
        {"t": 60, "type": "mouse_up", "button": "left", "x": 900, "y": 300},
        {"t": 900, "type": "key", "keycode": 21, "mods": [], "chars": "4", "base": "4", "ctx": field},
    ]
    t = _run(tmp_path, events)
    p = t["parameters"][0]
    assert p["name"] == "years_of_experience_with_python" and p["kind"] == "number"
    assert 'the field for "Years of experience with Python?"' in t["steps"][1]["describe"]


def test_ocr_prefers_text_under_the_click_over_clipped_text():
    items = [
        {"text": "Submit applicatior", "conf": 0.9, "box": [0.6, 0.45, 0.4, 0.1]},  # runs off the right edge
        {"text": "Submit application", "conf": 0.9, "box": [0.3, 0.45, 0.4, 0.1]},  # under the click
    ]
    assert pick_label(items) == "Submit application"
    assert pick_label([{"text": "Next", "conf": 0.2, "box": [0.4, 0.4, 0.2, 0.2]}]) is None


def test_ask_step_pauses_for_the_person_and_feeds_later_steps():
    plan = {
        "schema": "tasktrace-plan/0.1", "plan_id": "demo", "version": 1, "parent": None,
        "author": {"kind": "ai", "name": "Test AI"}, "values": {},
        "steps": [
            {"id": 1, "action": "ask", "question": "Which report title?", "param": "title",
             "options": ["Q3", "Q4"]},
            {"id": 2, "action": "type", "text": "{title} forecast"},
        ],
    }
    assert validate_plan(plan) == []
    drv = FakeDriver()
    drv.focused = "Report title"
    ui = ScriptedUI(values={"title": "Q4"})
    audit = Executor(plan, drv, ui, effect_timeout=0.1, poll=0.01).run()
    assert ui.asked == ["title"]  # asked once, at the ask step, not up front
    assert audit["entries"][0]["outcome"] == "answered" and audit["entries"][0]["answer"] == "Q4"
    assert drv.fields["Report title"] == "Q4 forecast"


def test_ask_and_open_app_validation():
    base = {"schema": "tasktrace-plan/0.1", "plan_id": "x", "version": 1,
            "author": {"kind": "ai", "name": "A"}}
    errs = validate_plan({**base, "steps": [{"id": 1, "action": "ask"}, {"id": 2, "action": "open_app"}]})
    assert any("ask needs" in e for e in errs) and any("open_app needs app" in e for e in errs)
