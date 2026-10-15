import json

import pytest

from tasktrace.appcore import parse_json_reply, plan_dir_for, plan_filename, result_summary, run_label, trace_for_ai
from tasktrace.compile import compile_run
from tasktrace.drivers.fake import FakeDriver
from tasktrace.plan import make_plan
from tasktrace.replay import Executor, ScriptedUI
from tasktrace.review import triage

from test_loop import Q4, RUN


def test_parse_reply_with_chatter_and_fences():
    reply = 'Sure! Here is the plan:\n```json\n{"schema": "tasktrace-plan/0.1", "steps": []}\n```\nLet me know.'
    assert parse_json_reply(reply)["schema"] == "tasktrace-plan/0.1"
    assert parse_json_reply('{"a": 1}') == {"a": 1}
    assert parse_json_reply('blah {"a": {"b": 2}} blah')["a"]["b"] == 2


@pytest.mark.parametrize("bad", ["", "no json here", "{not json}"])
def test_parse_reply_errors_are_plain(bad):
    with pytest.raises(ValueError) as e:
        parse_json_reply(bad)
    assert "clipboard" in str(e.value).lower()


def test_trace_for_ai_strips_thumbnails():
    t = compile_run(RUN, use_ocr=False, embed_crops=True)
    text = trace_for_ai(t)
    assert "data:image" not in text and text.startswith("Here is a recording")
    json.loads(text[text.index("{"):])


def test_run_label():
    assert run_label({"run": "20261015-143000-q3", "goal": "Create the quarterly report"}) == \
        "Create the quarterly report  (10/15 14:30)"
    assert run_label({"run": "x", "goal": "a" * 60}).endswith("…")


def test_plan_paths(tmp_path):
    (tmp_path / "run1").mkdir()
    assert plan_dir_for({"source_trace": "run1"}, tmp_path) == tmp_path / "run1" / "plans"
    assert plan_dir_for({"source_trace": "missing"}, tmp_path) == tmp_path / "plans"
    assert plan_dir_for({"source_trace": ".."}, tmp_path) == tmp_path / "plans"
    assert plan_dir_for({"source_trace": "../etc"}, tmp_path) == tmp_path / "plans"
    assert plan_filename({"plan_id": "q3 report/../x", "version": 2}) == "q3-report-x-v2.json"  # path tricks are flattened


def _run(**driver_kw):
    trace = compile_run(RUN, use_ocr=False)
    plan = make_plan(trace, Q4)
    drv = FakeDriver(**driver_kw)
    ui = ScriptedUI(on_handoff=lambda s: drv.human_types("Approval PIN", "x"))
    audit = Executor(plan, drv, ui, effect_timeout=0.2, poll=0.01).run()
    return audit, triage(audit, plan)


def test_result_summary_pass():
    title, body, fix = result_summary(*_run())
    assert title.startswith("Done") and "9 of 9" in body and not fix


def test_result_summary_offers_fix_in_plain_words():
    title, body, fix = result_summary(*_run(broken={"New Report"}))
    assert fix and "Step 1" in body and "Run a plan from clipboard" in body
