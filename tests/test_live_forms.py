"""Working a form nobody has seen before.

The first real run of a plan failed in the way a step list always will: the second employer's
Easy Apply asked different questions, in a different order, over a different number of pages,
and a plan that says "type the city into the third field" has nothing useful to do.

So the executor reads the page in front of it: fills what the person has answered before, stops
and asks about anything new, and remembers the answer so it never asks twice.
"""

import json

from tasktrace.answers import AnswerBank, entities, is_essay, normalize
from tasktrace.drivers.fake import FakeDriver
from tasktrace.plan import validate_plan
from tasktrace.replay import Executor, ScriptedUI

PLAN = {"schema": "tasktrace-plan/0.1", "plan_id": "apply", "version": 1,
        "author": {"kind": "ai", "name": "test"}, "values": {}}


def _plan(*steps):
    return {**PLAN, "steps": [{"id": i, **s} for i, s in enumerate(steps, 1)]}


# --------------------------------------------------------------------------- the answer bank

def test_the_same_question_is_only_ever_asked_once():
    bank = AnswerBank()
    bank.learn("How many years of experience do you have with CX?", "7", "AXTextField")
    # Same question, different employer's wording.
    assert bank.find("How many years of experience do you have with CX?*")["answer"] == "7"
    assert bank.find("How many years of experience do you have with CX")["answer"] == "7"


def test_a_question_about_one_company_never_answers_for_another():
    bank = AnswerBank()
    bank.learn("Why do you want to work at Bitdefender?", "Because of their endpoint research.")
    assert bank.find("Why do you want to work at SquareTrade?") is None  # different company
    assert bank.find("Why do you want to work at Bitdefender?") is None  # essay: always re-asked
    assert entities("Why do you want to work at SquareTrade?") == {"SquareTrade"}


def test_essay_questions_are_flagged_and_short_ones_are_not():
    assert is_essay("Why do you want to work here?")
    assert is_essay("Tell us about a time you disagreed with a manager")
    assert not is_essay("Years of experience with Salesforce")
    assert normalize("Please share your compensation expectation *") == "share your compensation expectation"


def test_the_bank_is_built_from_a_recording(tmp_path):
    trace = {"steps": [{"id": 1, "container_context": {"interactive_siblings": [
        {"role": "AXTextField", "title": "First name*", "value": "Brian"},
        {"role": "AXPopUpButton", "title": "Phone country code *", "value": "United States (+1)"},
        {"role": "AXPopUpButton", "title": "Race/Ethnicity *", "value": "Select an option"},
        {"role": "AXButton", "title": "Continue to next step"}]}}]}
    bank = AnswerBank(tmp_path / "answers.json")
    assert bank.seed_from_trace(trace) == 2  # not the button, not the unanswered dropdown
    assert bank.find("First name")["answer"] == "Brian"
    assert json.loads((tmp_path / "answers.json").read_text())["answers"]


def test_seeding_ignores_the_browser_and_bare_tick_boxes(tmp_path):
    trace = {"steps": [{"id": 1, "container_context": {"interactive_siblings": [
        {"role": "AXTextField", "title": "Address and search bar", "value": "linkedin.com/jobs"},
        {"role": "AXCheckBox", "title": "Yes", "value": "1"},
        {"role": "AXTextField", "title": "Inbox (18,501) - Gmail - Memory usage - 421 MB", "value": "False"},
        {"role": "AXTextField", "title": "Notice period*", "value": "2 weeks"}]}}]}
    bank = AnswerBank(tmp_path / "a.json")
    assert bank.seed_from_trace(trace) == 1  # only the real question
    assert bank.find("Notice period") and bank.find("Yes") is None
    assert bank.find("Address and search bar") is None


def test_a_partly_typed_value_is_replaced_by_the_full_one(tmp_path):
    def trace(value):
        return {"steps": [{"id": 1, "container_context": {"interactive_siblings": [
            {"role": "AXComboBox", "title": "Location (city) *", "value": value}]}}]}
    bank = AnswerBank(tmp_path / "a.json")
    bank.seed_from_trace(trace("Winston S"))  # mid-autocomplete
    bank.seed_from_trace(trace("Winston-Salem, North Carolina, United States"))
    assert bank.find("Location (city)")["answer"] == "Winston-Salem, North Carolina, United States"


# --------------------------------------------------------------------------- the form loop

PAGE_1 = [{"role": "AXTextField", "title": "First name*", "value": "Brian"},  # already filled by the site
          {"role": "AXTextField", "title": "Mobile phone number*", "value": ""}]
PAGE_2 = [{"role": "AXPopUpButton", "title": "Are you authorized to work in the US?*", "value": "Select an option",
           "options": ["Yes", "No"]},
          {"role": "AXTextField", "title": "How many years of experience do you have with CX?*", "value": ""}]
PAGE_3 = [{"role": "AXTextField", "title": "Why do you want to work at Bitdefender?", "value": ""}]


def _bank(tmp_path):
    bank = AnswerBank(tmp_path / "answers.json")
    bank.learn("Mobile phone number", "7275047083", "AXTextField")
    bank.learn("Are you authorized to work in the US?", "Yes", "AXPopUpButton")
    bank.learn("How many years of experience do you have with CX?", "7", "AXTextField")
    return bank


def test_a_form_it_has_answers_for_runs_without_interrupting(tmp_path):
    drv = FakeDriver()
    drv.load_form([PAGE_1, PAGE_2], finish="Review your application")
    ui = ScriptedUI()
    plan = _plan({"action": "form_loop", "advance": ["Continue to next step"],
                  "finish": ["Review your application"]})
    assert validate_plan(plan) == []
    audit = Executor(plan, drv, ui, mode="watch", answers=_bank(tmp_path)).run()

    entry = audit["entries"][0]
    assert entry["outcome"] == "form_complete"
    assert ui.asked == []  # nothing needed a person
    assert [f["question"] for f in entry["pages"][0]["filled"]] == ["Mobile phone number*"]
    assert drv.form_pages[1][0]["value"] == "Yes"  # the dropdown was set, not typed over
    assert drv.form_pages[0][0]["value"] == "Brian"  # a field the site filled is left alone


def test_a_new_question_stops_the_run_and_is_remembered(tmp_path):
    drv = FakeDriver()
    drv.load_form([PAGE_2, PAGE_3], finish="Review your application")
    bank = _bank(tmp_path)
    ui = ScriptedUI(values={"why_do_you_want_to_work_at_bitdefender": "Their endpoint research is the best."})
    plan = _plan({"action": "form_loop", "finish": ["Review your application"]})
    audit = Executor(plan, drv, ui, mode="watch", answers=bank).run()

    entry = audit["entries"][0]
    assert entry["outcome"] == "form_complete"
    assert ui.asked == ["why_do_you_want_to_work_at_bitdefender"]  # only the one it couldn't know
    asked = entry["pages"][1]["asked"][0]
    assert asked["reusable"] is False  # an essay about this employer is never reused
    assert bank.find("Why do you want to work at Bitdefender?") is None
    # ...but a factual question answered once is never asked again:
    bank.learn("What is your notice period?", "2 weeks", "AXTextField")
    assert bank.find("What is your notice period?*")["answer"] == "2 weeks"


def test_an_unanswered_question_hands_over_rather_than_guessing(tmp_path):
    drv = FakeDriver()
    drv.load_form([PAGE_3], finish="Review your application")
    handed = []
    ui = ScriptedUI(values={}, on_handoff=lambda s: handed.append(s["id"]))
    audit = Executor(_plan({"action": "form_loop", "finish": ["Review your application"]}),
                     drv, ui, mode="watch", answers=_bank(tmp_path)).run()
    assert audit["entries"][0]["outcome"] == "handed_to_human" and handed == [1]
    assert audit["entries"][0]["pages"][0]["problems"][0]["problem"] == "unanswered"


def test_a_form_that_stops_moving_does_not_loop_forever(tmp_path):
    drv = FakeDriver()
    drv.load_form([PAGE_1, PAGE_2], finish="Never appears")
    drv.press = lambda control: True  # the button responds but the page never changes
    audit = Executor(_plan({"action": "form_loop", "advance": ["Continue to next step"],
                            "finish": ["Never appears"], "max_pages": 4}),
                     drv, ScriptedUI(), mode="watch", answers=_bank(tmp_path)).run()
    assert audit["entries"][0]["outcome"] in ("handed_to_human", "skipped")
    assert len(audit["entries"][0]["pages"]) <= 4


def test_dry_run_reports_what_it_would_ask_without_touching_anything(tmp_path):
    drv = FakeDriver()
    drv.load_form([PAGE_3], finish="Review your application")
    audit = Executor(_plan({"action": "form_loop", "finish": ["Review your application"]}),
                     drv, ScriptedUI(), mode="dry-run", answers=_bank(tmp_path)).run()
    page = audit["entries"][0]["pages"][0]
    assert page["would_ask"] == ["Why do you want to work at Bitdefender?"]
    assert drv.form_pages[0][0]["value"] == ""  # nothing was filled in


# --------------------------------------------------------------------------- choosing a row

JOBS = [
    {"text": "Service Writer · Acme · Durham, NC · Promoted", "button": "Easy Apply to Service Writer"},
    {"text": "CX Strategist · Bitdefender · Remote · Top applicant · Easy Apply",
     "button": "Easy Apply to CX Strategist"},
    {"text": "Support Lead · Zendesk · Remote · High match", "button": "Easy Apply to Support Lead"},
    {"text": "Already applied: Analyst · SquareTrade · High match", "button": "Easy Apply to Analyst"},
]


def test_it_picks_a_job_by_what_the_card_says_not_by_position():
    drv = FakeDriver()
    drv.load_list(JOBS)
    plan = _plan({"action": "pick_list_item", "param": "job",
                  "require_any": ["High match", "Top applicant"],
                  "exclude_any": ["Already applied"],
                  "press": {"role": "AXButton", "title_prefix": "Easy Apply"}})
    assert validate_plan(plan) == []
    audit = Executor(plan, drv, ScriptedUI(), mode="watch").run()

    entry = audit["entries"][0]
    assert entry["outcome"] == "ok"
    assert drv.clicked_row["button"] == "Easy Apply to CX Strategist"  # first badged row
    assert [m["index"] for m in entry["matched"]] == [1, 2]  # the applied-to one is excluded
    assert "Top applicant" in entry["chose"]["text"]


def test_no_badged_job_hands_over_instead_of_applying_to_anything():
    drv = FakeDriver()
    drv.load_list([JOBS[0]])
    handed = []
    Executor(_plan({"action": "pick_list_item", "require_any": ["High match"],
                            "press": {"title_prefix": "Easy Apply"}}),
                     drv, ScriptedUI(on_handoff=lambda s: handed.append(s["id"])), mode="watch").run()
    assert handed == [1] and drv.clicked_row is None


def test_plan_validation_asks_for_what_these_steps_need():
    errs = validate_plan(_plan({"action": "pick_list_item"}, {"action": "form_loop"}))
    assert any("pick_list_item needs" in e for e in errs)
    assert any("form_loop needs" in e for e in errs)


def test_sound_never_breaks_a_run():
    from tasktrace import alert
    alert.set_enabled(False)
    assert alert.attention("ask") is False and alert.say("hello") is False
    alert.set_enabled(True)
    assert alert.attention("nonexistent-kind") in (True, False)  # unknown kind falls back, never raises
