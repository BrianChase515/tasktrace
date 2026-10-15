"""Context capture: container_context, element-aligned crops, in-page effects, synthesis guidance.

The four gaps that stopped a downstream coding AI turning a trace into a real script:
tunnel vision on the leaf element, OCR labels sliced in half, no effect inside a single-page
app, and guidance telling the AI to replay rather than compile.
"""

import json
from pathlib import Path

import pytest

from tasktrace import axcontext
from tasktrace.cli import main
from tasktrace.compile import compile_run
from tasktrace.images import crop_rect, point_in_crop
from tasktrace.ocr import pick_label

ROOT = Path(__file__).resolve().parents[1]
CHROME = {"name": "Google Chrome", "bundle_id": "com.google.Chrome", "pid": 700}
WIN = {"title": "Jobs | LinkedIn", "id": 9, "bounds": [0, 25, 1920, 1055]}


def _run(tmp_path, events, **kw):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    return compile_run(run, use_ocr=False, **kw)


def _click(t, x, y, element, ref=None, crop=None, rect=None):
    down = {"t": t, "type": "mouse_down", "button": "left", "x": x, "y": y, "clicks": 1,
            "ctx": {"app": CHROME, "window": WIN, "element": element}}
    if ref is not None:
        down["ref"] = ref
    if crop:
        down["crop"], down["crop_rect"] = crop, rect
    return [down, {"t": t + 70, "type": "mouse_up", "button": "left", "x": x, "y": y}]


# --------------------------------------------------------------------------- 1. container context

JOB_CARD = {
    "role": "AXGroup",
    "children": [
        {"role": "AXStaticText", "value": "Senior Backend Engineer"},
        {"role": "AXStaticText", "value": "Boost Engine Tech"},
        {"role": "AXStaticText", "value": "Durham, NC (Hybrid)"},
        {"role": "AXButton", "title": "Easy Apply"},
    ],
}
JOB_LIST = {"role": "AXList", "title": "Job search results", "children": [{} for _ in range(25)]}

APPLY_MODAL = {
    "role": "AXGroup", "subrole": "AXApplicationDialog", "title": "Apply to Boost Engine Tech",
    "children": [
        {"role": "AXProgressIndicator", "value": "20 percent"},
        {"role": "AXTextField", "title": "Email address", "value": "brian@example.com"},
        {"role": "AXComboBox", "title": "Phone country code", "value": "United States (+1)"},
        {"role": "AXTextField", "title": "Mobile phone number", "placeholder": "e.g. 555 0100"},
        {"role": "AXCheckBox", "title": "Follow Boost Engine Tech", "value": "1"},
        {"role": "AXButton", "title": "Continue to next step"},
    ],
}


def test_list_click_records_which_row_and_the_whole_card():
    ctx = axcontext.list_context(JOB_LIST, 2, JOB_CARD)
    assert ctx["kind"] == "list" and ctx["list_item_index"] == 2 and ctx["total_items"] == 25
    summary = ctx["item_text_summary"]
    for bit in ("Senior Backend Engineer", "Boost Engine Tech", "Durham, NC (Hybrid)"):
        assert bit in summary  # the whole card, not the fragment under the pointer
    assert ctx["item_controls"] == [{"role": "AXButton", "title": "Easy Apply"}]


def test_modal_click_records_the_form_schema_including_untouched_fields():
    ctx = axcontext.modal_context(APPLY_MODAL)
    assert ctx["kind"] == "modal" and ctx["progress"] == "20 percent"
    by_title = {c.get("title"): c for c in ctx["interactive_siblings"]}
    assert by_title["Email address"]["value"] == "brian@example.com"  # pre-filled, never clicked
    assert by_title["Mobile phone number"]["placeholder"] == "e.g. 555 0100"
    assert by_title["Phone country code"]["role"] == "AXComboBox"


def test_a_group_with_one_stray_control_is_not_a_form():
    thin = {"role": "AXGroup", "title": "toolbar", "children": [{"role": "AXButton", "title": "Share"}]}
    assert axcontext.modal_context(thin, min_controls=2) is None
    assert axcontext.modal_context(thin)["interactive_siblings"][0]["title"] == "Share"


def test_secure_fields_are_listed_but_never_valued():
    modal = {"role": "AXGroup", "subrole": "AXDialog", "title": "Sign in", "children": [
        {"role": "AXTextField", "subrole": "AXSecureTextField", "title": "Password", "value": "hunter2"}]}
    sib = axcontext.modal_context(modal)["interactive_siblings"][0]
    assert sib == {"role": "AXSecureTextField", "title": "Password"}


def test_container_context_reaches_the_step_and_the_outline(tmp_path):
    events = _click(0, 640, 420, {"role": "AXButton", "title": "Easy Apply"}, ref=1)
    events.append({"t": 20, "type": "container", "ref": 1,
                   "context": axcontext.list_context(JOB_LIST, 2, JOB_CARD)})
    trace = _run(tmp_path, events)
    cc = trace["steps"][0]["container_context"]
    assert cc["list_item_index"] == 2 and "Senior Backend Engineer" in cc["item_text_summary"]
    assert "[list item 3 of 25]" in trace["outline"][0]


def test_list_inside_a_modal_keeps_both(tmp_path):
    merged = axcontext.merge_context(axcontext.list_context(JOB_LIST, 0, JOB_CARD),
                                     axcontext.modal_context(APPLY_MODAL))
    assert merged["kind"] == "list_in_modal"
    assert merged["list_item_index"] == 0
    assert any(c["title"] == "Email address" for c in merged["modal"]["interactive_siblings"])


# --------------------------------------------------------------------------- 2. unclipped OCR

def test_crop_follows_the_element_not_the_pointer():
    # "Easy Apply" button, clicked near its left edge: the old fixed box centred on the pointer
    # started mid-word, which is where "asy Apply" came from.
    rect = crop_rect(620, 430, [604, 418, 120, 32])
    assert rect[0] <= 604 - 15 + 1 and rect[0] + rect[2] >= 604 + 120 + 15 - 1
    assert rect[2] >= 240  # minimum width around the click
    assert rect[0] <= 620 <= rect[0] + rect[2]


def test_crop_of_a_huge_container_stays_around_the_pointer():
    rect = crop_rect(1500, 300, [0, 100, 1900, 800])
    assert rect[2] <= 900 and rect[3] <= 260
    assert rect[0] <= 1500 <= rect[0] + rect[2] and rect[1] <= 300 <= rect[1] + rect[3]


def test_crop_without_an_element_rect_falls_back_to_the_pointer():
    rect = crop_rect(500, 500, None)
    assert rect[0] < 500 < rect[0] + rect[2] and rect[1] < 500 < rect[1] + rect[3]


def test_ocr_label_is_read_at_the_pointer_not_the_crop_centre():
    point = point_in_crop(620, 430, crop_rect(620, 430, [604, 418, 120, 32]))
    assert point[0] < 0.4  # clicked left of centre, because the crop covers the whole button
    items = [  # the button's own label sits left in the crop; a neighbour sits dead centre
        {"text": "Easy Apply", "conf": 0.9, "box": [0.05, 0.35, 0.3, 0.3]},
        {"text": "Save", "conf": 0.9, "box": [0.42, 0.35, 0.2, 0.3]},
    ]
    assert pick_label(items, point=point) == "Easy Apply"
    assert pick_label(items) == "Save"  # centre-based picking is what used to go wrong


# --------------------------------------------------------------------------- 3. single-page effects

def test_dialog_toggle_and_progress_become_effects(tmp_path):
    events = _click(0, 640, 420, {"role": "AXButton", "title": "Easy Apply"})
    events.append({"t": 600, "type": "ui", "change": "dialog_opened",
                   "name": "Apply to Boost Engine Tech"})
    events += _click(4000, 700, 880, {"role": "AXStaticText", "value": "Follow Boost Engine Tech"})
    events.append({"t": 4400, "type": "ui", "change": "toggled",
                   "element": "Follow Boost Engine Tech", "from": "1", "to": "0"})
    events += _click(8000, 900, 940, {"role": "AXButton", "title": "Continue to next step"})
    events.append({"t": 8500, "type": "ui", "change": "progress", "from": "20 percent",
                   "to": "40 percent"})
    trace = _run(tmp_path, events)

    assert trace["steps"][0]["effect"]["dialog_opened"] == ["Apply to Boost Engine Tech"]
    assert 'shows "Apply to Boost Engine Tech"' in trace["outline"][0]
    assert trace["steps"][1]["effect"]["toggled"] == [
        {"element": "Follow Boost Engine Tech", "from": "1", "to": "0"}]
    assert '"Follow Boost Engine Tech" becomes unchecked' in trace["outline"][1]
    assert trace["steps"][2]["effect"]["progress"] == {"from": "20 percent", "to": "40 percent"}
    assert "progress 20 percent -> 40 percent" in trace["outline"][2]


def test_ui_state_diff_detects_what_the_window_poller_cannot():
    before = axcontext.ui_state({"role": "AXWindow", "children": [
        {"role": "AXCheckBox", "title": "Follow Boost Engine Tech", "value": "1"},
        {"role": "AXProgressIndicator", "value": "20 percent"}]})
    after = axcontext.ui_state({"role": "AXWindow", "children": [
        {"role": "AXCheckBox", "title": "Follow Boost Engine Tech", "value": "0"},
        {"role": "AXProgressIndicator", "value": "40 percent"},
        APPLY_MODAL]})
    changes = {c["change"]: c for c in axcontext.diff_ui_state(before, after)}
    assert changes["toggled"]["from"] == "1" and changes["toggled"]["to"] == "0"
    assert changes["progress"]["to"] == "40 percent"
    assert changes["dialog_opened"]["name"] == "Apply to Boost Engine Tech"
    assert axcontext.diff_ui_state(None, after) == []  # no baseline, nothing claimed


# --------------------------------------------------------------------------- 4. guidance

def test_guidance_defaults_to_synthesis(tmp_path):
    trace = _run(tmp_path, _click(0, 10, 10, {"role": "AXButton", "title": "Go"}))
    g = trace["automation_synthesis_guidance"]
    assert "replay_guidance" not in trace
    for phrase in ("TRACE-TO-CODE COMPILER", "PRUNE", "GENERALIZE", "while", "for` loop"):
        assert phrase in g


def test_guidance_modes(tmp_path):
    events = _click(0, 10, 10, {"role": "AXButton", "title": "Go"})
    assert "replay_guidance" in _run(tmp_path, events, guidance="replay")
    both = _run(tmp_path, events, guidance="both")
    assert "replay_guidance" in both and "automation_synthesis_guidance" in both


def test_cli_guidance_flag_and_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    events = _click(0, 640, 420, {"role": "AXButton", "title": "Easy Apply", "frame": [604, 418, 120, 32]},
                    ref=1)
    events.append({"t": 20, "type": "container", "ref": 1, "context": axcontext.merge_context(
        axcontext.list_context(JOB_LIST, 2, JOB_CARD), axcontext.modal_context(APPLY_MODAL))})
    events.append({"t": 600, "type": "ui", "change": "dialog_opened", "name": "Apply to Boost"})
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))

    out = tmp_path / "trace.json"
    assert main(["compile", str(run), "-o", str(out), "--no-ocr", "--guidance", "both"]) == 0
    trace = json.loads(out.read_text())
    jsonschema.validate(trace, json.loads((ROOT / "schema" / "trace.schema.json").read_text()))
    assert trace["steps"][0]["container_context"]["kind"] == "list_in_modal"
