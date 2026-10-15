"""The human-supervised loop end to end: trace -> plan -> replay -> audit -> review -> revise."""

import json
from pathlib import Path

import pytest

from tasktrace.cli import main
from tasktrace.compile import compile_run
from tasktrace.drivers.fake import FakeDriver
from tasktrace.plan import make_plan, missing_values, validate_plan
from tasktrace.prompts import author_prompt, revise_prompt
from tasktrace.replay import Executor, ScriptedUI, effect_ok
from tasktrace.review import lineage, triage

RUN = Path(__file__).resolve().parent / "fixtures" / "run_q3"
Q4 = {"report_title": "Q4 forecast", "period_end": "2026-12-31"}


@pytest.fixture(scope="module")
def trace():
    return compile_run(RUN, use_ocr=False)


@pytest.fixture
def plan(trace):
    return make_plan(trace, Q4, source="run_q3")


def run(plan, driver, ui=None, mode="step", tmp=None):
    ui = ui or ScriptedUI(on_handoff=lambda step: driver.human_types("Approval PIN", "****"))
    return Executor(plan, driver, ui, mode=mode, audit_dir=tmp, effect_timeout=0.2, poll=0.01).run()


def test_plan_from_trace(plan):
    assert validate_plan(plan) == []
    assert plan["values"] == Q4 and missing_values(plan) == []
    by_id = {s["id"]: s for s in plan["steps"]}
    assert by_id[3]["text"] == "{report_title}"
    assert by_id[7]["secret"] is True and "text" not in by_id[7]
    assert by_id[8]["consequential"] is True  # "Create"
    assert by_id[9]["consequential"] is True  # cmd+s
    assert by_id[1]["expect"] == {"opened": ["Report Wizard"]}
    assert plan["author"]["kind"] == "tool"


def test_happy_path(plan, tmp_path):
    drv = FakeDriver()
    audit = run(plan, drv, tmp=tmp_path)
    assert audit["result"] == "completed", audit["summary"]
    assert drv.fields == {"Report title": "Q4 forecast", "Period end": "2026-12-31", "Approval PIN": "****"}
    assert drv.windows[0]["title"] == "Acme Portal - Q4 forecast" and drv.saved
    pin = audit["entries"][6]
    assert pin["outcome"] == "handed_to_human" and "text_used" not in pin
    assert audit["plan"]["author"]["name"] == "tasktrace plan"
    assert (tmp_path / f"{audit['audit_id']}.json").exists()
    assert triage(audit, plan)["verdict"] == "pass"


def test_ui_changed_falls_back_and_is_flagged(plan):
    # The app renamed "Create" and OCR can't see it: the executor falls back to the
    # window-relative position, it still works, and review flags the fragile locator.
    drv = FakeDriver(rename={"Create": "Create report"}, ocr_text=False)
    audit = run(plan, drv)
    step8 = audit["entries"][7]
    assert step8["attempt"]["tried"] == ["element", "ocr", "rel_xy"]
    assert step8["outcome"] == "ok"
    rev = triage(audit, plan)
    f = [f for f in rev["findings"] if f["category"] == "fragile_locator"]
    assert f and f[0]["step"] == 8 and f[0]["route_to"] == "author"


def test_effect_mismatch_stops_and_routes_to_author(plan):
    drv = FakeDriver(broken={"New Report"})  # clicking does nothing
    audit = run(plan, drv)
    assert audit["entries"][0]["outcome"] == "effect_mismatch"
    assert audit["result"] == "stopped"
    assert all(e["outcome"] == "not_run" for e in audit["entries"][1:])
    rev = triage(audit, plan)
    assert rev["verdict"] == "revise"
    assert rev["findings"][0]["category"] == "effect" and "Nothing changed" in rev["findings"][0]["summary"]


def test_human_correction_is_ground_truth(plan):
    drv = FakeDriver()
    ui = ScriptedUI(
        answers={5: {"verdict": {"verdict": "rejected", "comment": "Period end must be the fiscal quarter end, 2027-01-31"}}},
        on_handoff=lambda s: drv.human_types("Approval PIN", "****"))
    audit = run(plan, drv, ui)
    assert audit["result"] == "completed_with_issues"
    rev = triage(audit, plan)
    top = rev["findings"][0]
    assert top["category"] == "human_correction" and top["severity"] == "high"
    assert top["human_comment"].endswith("2027-01-31") and top["text_used"] == "2026-12-31"
    prompt = revise_prompt(plan, audit, rev, "Copilot")
    assert '"version": 2' in prompt and "run_q3" in prompt and "Human comments are ground truth" in prompt


def test_human_can_stop_and_skip(plan):
    drv = FakeDriver()
    audit = run(plan, drv, ScriptedUI(answers={2: {"confirm": "skip"}, 4: {"confirm": "stop"}}))
    outcomes = [e["outcome"] for e in audit["entries"]]
    assert outcomes[:4] == ["ok", "skipped", "ok", "stopped"] and outcomes[4] == "not_run"
    assert triage(audit, plan)["routes"]["human"]


def test_watch_mode_only_gates_consequential(plan):
    drv = FakeDriver()
    asked = []

    class Spy(ScriptedUI):
        def confirm(self, step, desc, match):
            asked.append(step["id"])
            return "go"

    run(plan, drv, Spy(on_handoff=lambda s: drv.human_types("Approval PIN", "x")), mode="watch")
    assert asked == [8, 9]


def test_dry_run_touches_nothing(plan):
    drv = FakeDriver()
    audit = run(plan, drv, mode="dry-run")
    assert drv.log == [] and audit["result"] == "dry_run"
    assert audit["entries"][0]["outcome"] == "would_run"


def test_missing_values_are_asked(trace):
    p = make_plan(trace, {})
    drv = FakeDriver()
    ui = ScriptedUI(values=Q4, on_handoff=lambda s: drv.human_types("Approval PIN", "x"))
    audit = run(p, drv, ui)
    assert sorted(ui.asked) == ["period_end", "report_title"]
    assert audit["values_used"]["report_title"] == "Q4 forecast"


def test_validation_requires_lineage(plan):
    v2 = dict(plan, version=2, author={"kind": "ai", "name": "Copilot"})
    errs = validate_plan(v2)
    assert any("parent" in e for e in errs) and any("changelog" in e for e in errs)
    v2.update(parent=f"{plan['plan_id']}@1",
              changelog=[{"version": 2, "finding": "F1", "step": 5, "change": "use fiscal quarter end"}])
    assert validate_plan(v2) == []
    assert lineage([plan, v2]) == [
        "v1 by tasktrace plan (tool)",
        "v2 by Copilot (ai), from q3-report@1: 1 change(s) addressing F1",
    ]


def test_effect_ok_tolerates_new_values_in_titles():
    exp = {"closed": ["Report Wizard"], "retitled": [{"from": "A", "to": "Acme Portal - Q3 forecast"}]}
    obs = {"closed": ["Report Wizard"], "retitled": [{"from": "A", "to": "Acme Portal - Q4 forecast"}]}
    assert effect_ok(exp, obs) and not effect_ok(exp, {"closed": ["Report Wizard"]})


def test_terminal_ui_never_treats_a_typo_as_go():
    from tasktrace.replay import TerminalUI
    answers = iter(["yes please", "x", "q"])
    ui = TerminalUI(inp=lambda _: next(answers), out=lambda *_: None)
    assert ui.confirm({"id": 1}, "Click", None) == "stop"


def test_author_prompt_carries_trace_and_rules(trace):
    p = author_prompt(trace, Q4, "Copilot")
    assert "AUTHOR" in p and '"Copilot"' in p and "secret" in p and "Q4 forecast" in p


def test_cli_loop(tmp_path, capsys, monkeypatch):
    trace_path = tmp_path / "run" / "trace.json"
    trace_path.parent.mkdir()
    trace_path.write_text(json.dumps(compile_run(RUN, use_ocr=False)))
    assert main(["plan", str(trace_path), "--set", "report_title=Q4 forecast", "--set", "period_end=2026-12-31"]) == 0
    plan_path = trace_path.parent / "plans" / "q3-report-v1.json"
    assert main(["validate", str(plan_path)]) == 0
    assert main(["replay", str(plan_path), "--driver", "fake", "--mode", "dry-run"]) == 0
    audit_path = next((plan_path.parent / "audits").glob("*.json"))
    assert main(["review", str(audit_path), "--plan", str(plan_path)]) == 0
    assert "Verdict: dry_run" in capsys.readouterr().out
    assert main(["history", str(plan_path.parent)]) == 0
    assert "v1 by tasktrace plan" in capsys.readouterr().out
