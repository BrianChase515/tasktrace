import copy
import json
from pathlib import Path

import pytest

from tasktrace.cli import main
from tasktrace.compile import build_steps, classify, compile_run
from tasktrace.diff import apply_diff, diff_traces
from tasktrace.redact import Redactor

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "tests" / "fixtures" / "run_q3"


@pytest.fixture(scope="module")
def trace():
    return compile_run(RUN, use_ocr=False)


def test_actions_are_collapsed(trace):
    assert [s["action"] for s in trace["steps"]] == [
        "click", "click", "type", "key", "type", "click", "type", "click", "hotkey"]


def test_typo_corrections_apply(trace):
    assert trace["steps"][2]["text"] == "Q3 forecast"


def test_click_has_ranked_locators(trace):
    tgt = trace["steps"][0]["target"]
    assert tgt["element"] == {"role": "AXButton", "title": "New Report"}
    assert tgt["abs_xy"] == [1290, 142]
    assert tgt["rel_xy"] == [0.896, 0.134]
    assert tgt["in_element_xy"] == [50, 14]
    assert tgt["crop"] == "crops/0001.png"


def test_cause_and_effect(trace):
    assert trace["steps"][0]["effect"]["opened"] == ["Report Wizard"]
    eff = trace["steps"][7]["effect"]
    assert eff["closed"] == ["Report Wizard"]
    assert eff["retitled"][0]["to"] == "Acme Portal - Q3 forecast"


def test_notes_attach_to_next_step(trace):
    assert trace["steps"][0]["intent"].startswith("Starting from the Reports page")
    assert trace["steps"][4]["intent"] == "Always use the last day of the quarter"


def test_secrets_never_recorded(trace):
    pin = trace["steps"][6]
    assert pin["secret"] is True and "text" not in pin
    assert "crop" not in trace["steps"][5]["target"]
    assert "4821" not in json.dumps(trace)


def test_parameters(trace):
    p = {x["name"]: x for x in trace["parameters"]}
    assert p["report_title"]["kind"] == "text" and p["report_title"]["example"] == "Q3 forecast"
    assert p["period_end"]["kind"] == "date"
    assert p["approval_pin"]["kind"] == "secret" and "example" not in p["approval_pin"]


def test_stop_keystroke_dropped(trace):
    assert trace["steps"][-1]["keys"] == "cmd+s"


def test_schema_valid(trace):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schema" / "trace.schema.json").read_text())
    jsonschema.validate(trace, schema)


def test_double_click_merges():
    ctx = {"app": {"name": "Finder"}, "window": {"title": "Docs", "id": 1, "bounds": [0, 0, 800, 600]}}
    evs = [
        {"t": 0, "type": "mouse_down", "button": "left", "x": 100, "y": 100, "clicks": 1, "ctx": ctx},
        {"t": 60, "type": "mouse_up", "button": "left", "x": 100, "y": 100},
        {"t": 200, "type": "mouse_down", "button": "left", "x": 101, "y": 100, "clicks": 2, "ctx": ctx},
        {"t": 260, "type": "mouse_up", "button": "left", "x": 101, "y": 100},
    ]
    steps = build_steps(evs)
    assert [s["action"] for s in steps] == ["double_click"]


def test_drag():
    evs = [
        {"t": 0, "type": "mouse_down", "button": "left", "x": 10, "y": 10, "clicks": 1},
        {"t": 400, "type": "mouse_up", "button": "left", "x": 300, "y": 40},
    ]
    s = build_steps(evs)[0]
    assert s["action"] == "drag" and s["to"] == [300, 40]


def test_redactor():
    r = Redactor()
    s = r.scrub("key sk-ant-abcdefghijklmnop1234 card 4111 1111 1111 1111 ssn 123-45-6789 order 1234567890123")
    assert "sk-ant" not in s and "4111" not in s and "123-45-6789" not in s
    assert "1234567890123" in s  # fails Luhn, left alone
    assert r.counts == {"api_key": 1, "card": 1, "ssn": 1}


def test_redacts_typed_secrets_in_plain_fields(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    ctx = {"app": {"name": "Notes"}, "element": {"role": "AXTextArea", "title": "Body"}}
    evs = [{"t": i * 50, "type": "key", "keycode": 0, "mods": [], "chars": c, "base": c, "ctx": ctx}
           for i, c in enumerate("token sk-abcdefghijklmnopqrstu")]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in evs))
    t = compile_run(run, use_ocr=False)
    assert "sk-abc" not in json.dumps(t)
    assert set(t["redactions"]["by_kind"]) == {"api_key"}


def test_classify():
    assert classify("2026-09-30") == "date"
    assert classify("$1,200.50") == "currency"
    assert classify("42") == "number"
    assert classify("a@b.co") == "email"
    assert classify("Q3 forecast") == "text"


def test_diff_confirms_variables_and_constants(trace):
    b = copy.deepcopy(trace)
    b["steps"][2]["text"] = "Q4 forecast"  # title changes per run; date stays the same
    report = diff_traces(trace, b)
    assert [v["param"] for v in report["variables"]] == ["report_title"]
    assert [c["param"] for c in report["constants"]] == ["period_end"]
    updated = apply_diff(trace, report)
    names = {p["name"]: p for p in updated["parameters"]}
    assert names["report_title"]["status"] == "confirmed"
    assert "period_end" not in names
    assert updated["steps"][4]["constant"] is True
    assert updated["task"]["runs_compared"] == 2


def test_diff_marks_optional_steps(trace):
    b = copy.deepcopy(trace)
    del b["steps"][3]  # the Tab key press didn't happen in run B
    report = diff_traces(trace, b)
    assert report["only_in_a"] == [4]
    assert apply_diff(trace, report)["steps"][3]["optional"] is True


def test_cli_compile_and_outline(tmp_path, capsys):
    out = tmp_path / "trace.json"
    assert main(["compile", str(RUN), "-o", str(out), "--no-ocr", "--embed-crops"]) == 0
    t = json.loads(out.read_text())
    pytest.importorskip("PIL")
    assert t["steps"][0]["target"]["thumbnail"].startswith("data:image/jpeg;base64,")
    assert main(["outline", str(out)]) == 0
    assert 'opens "Report Wizard"' in capsys.readouterr().out
