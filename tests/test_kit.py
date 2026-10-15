"""Scripts: the part that has loops in it.

A plan is a list, and a list cannot say "keep going until the form ends" or "try the next job".
These tests run an ordinary Python script — the kind an AI writes from a trace — against the
simulated app, including the cases that broke the plan: a form with a different number of pages
and different questions, and a list whose badge is only visible once a row is opened.
"""

import json

from tasktrace.answers import AnswerBank
from tasktrace.drivers.fake import FakeDriver
from tasktrace.kit import Session
from tasktrace.replay import ScriptedUI

CONTACT = [{"role": "AXTextField", "title": "First name*", "value": "Brian"},
           {"role": "AXTextField", "title": "Mobile phone number*", "value": ""}]
WORK = [{"role": "AXPopUpButton", "title": "Are you authorized to work in the US?*",
         "value": "Select an option", "options": ["Yes", "No"]}]
EXPERIENCE = [{"role": "AXTextField", "title": "Years of experience with CX*", "value": ""}]
NEW_ONE = [{"role": "AXTextField", "title": "What is your notice period?*", "value": ""}]


def _bank(tmp_path):
    bank = AnswerBank(tmp_path / "answers.json")
    bank.learn("Mobile phone number", "7275047083", "AXTextField")
    bank.learn("Are you authorized to work in the US?", "Yes", "AXPopUpButton")
    bank.learn("Years of experience with CX", "7", "AXTextField")
    return bank


def _apply_script(session):
    """The shape an AI writes from a trace: a loop, not a list of steps."""
    while session.running:
        page = session.form()
        for control in page.empty():
            session.answer(control)
        if page.button("Review your application", "Submit application"):
            return page
        nxt = page.button("Continue to next step", "Next")
        if nxt is None:
            session.give_up("no button to continue with")
            return page
        session.click(nxt)
    return session.form()


def _session(tmp_path, drv, ui=None, mode="watch"):
    return Session("Google Chrome", window="LinkedIn", mode=mode, driver=drv,
                   ui=ui or ScriptedUI(), audit_dir=tmp_path / "runs", answers=_bank(tmp_path),
                   name="apply")


def test_one_script_handles_forms_of_different_lengths(tmp_path):
    """The same script, two employers: three pages and one page. No edits in between."""
    for pages in ([CONTACT, WORK, EXPERIENCE], [CONTACT]):
        drv = FakeDriver()
        drv.load_form(pages, finish="Review your application")
        ui = ScriptedUI()
        with _session(tmp_path, drv, ui) as s:
            _apply_script(s)
        assert ui.asked == []  # every question was already answered, whatever the form's shape
        assert drv.form_pages[0][1]["value"] == "7275047083"
        assert s.audit["result"] == "ok"


def test_a_question_it_has_never_seen_rings_and_is_remembered(tmp_path):
    drv = FakeDriver()
    drv.load_form([WORK, NEW_ONE], finish="Review your application")
    bank = _bank(tmp_path)
    ui = ScriptedUI(values={"what_is_your_notice_period": "2 weeks"})
    with Session("Chrome", mode="watch", driver=drv, ui=ui, audit_dir=tmp_path / "runs",
                 answers=bank, name="apply") as s:
        _apply_script(s)
    assert ui.asked == ["what_is_your_notice_period"]
    assert drv.form_pages[1][0]["value"] == "2 weeks"
    assert bank.find("What is your notice period?")["answer"] == "2 weeks"  # never asked again
    assert [e["action"] for e in s.audit["entries"]].count("ask") == 1


def test_the_script_chooses_a_job_by_what_its_panel_says(tmp_path):
    drv = FakeDriver()
    drv.load_list([
        {"text": "Service Writer · Acme", "detail": "Promoted · 50 applicants",
         "button": "Easy Apply to Service Writer", "detail_only": True},
        {"text": "CX Strategist · Bitdefender", "detail": "Job match is high, we can help you stand out",
         "button": "Easy Apply to CX Strategist", "detail_only": True}])
    with _session(tmp_path, drv) as s:
        for job in s.rows():
            s.open(job)
            if not s.page_says("high match", "top applicant"):
                continue
            s.click(s.form().button("Easy Apply"))
            break
    assert drv.clicked_row["button"] == "Easy Apply to CX Strategist"


def test_anything_consequential_still_asks_the_person(tmp_path):
    drv = FakeDriver()
    drv.load_form([CONTACT], finish="Submit application")
    said_no = ScriptedUI(answers={1: {"confirm": "skip"}, 2: {"confirm": "skip"}, 3: {"confirm": "skip"}})
    with _session(tmp_path, drv, said_no) as s:
        s.click(s.form().button("Submit application"), consequential=True)
    assert "Submit application" not in drv.pressed  # refused, and the run still ends tidily
    assert s.audit["entries"][-1]["outcome"] == "skipped"


def test_stopping_ends_the_run_and_still_writes_the_audit(tmp_path):
    drv = FakeDriver()
    drv.load_form([CONTACT, WORK], finish="Review your application")
    with _session(tmp_path, drv) as s:
        s.note("about to work the form")
        s.stop("person")
        s.click(s.form().button("Continue to next step"))  # never reached
    assert s.audit["result"] == "stopped" and s.audit["stopped_by"] == "person"
    written = json.loads((tmp_path / "runs" / f"{s.audit_id}.json").read_text())
    assert written["entries"][0]["describe"] == "about to work the form"
    assert "Continue to next step" not in drv.pressed


def test_dry_run_looks_without_touching(tmp_path):
    drv = FakeDriver()
    drv.load_form([NEW_ONE], finish="Review your application")
    with _session(tmp_path, drv, mode="dry-run") as s:
        _apply_script(s)
    assert drv.form_pages[0][0]["value"] == "" and drv.pressed == []
    assert [e["outcome"] for e in s.audit["entries"]] == ["would_ask"]


def test_an_unexpected_screen_hands_over_rather_than_pressing_on(tmp_path):
    drv = FakeDriver()
    drv.load_form([CONTACT], advance="Nothing like this", finish="Nor this")
    handed = []
    ui = ScriptedUI(on_handoff=lambda s: handed.append(s["id"]))
    with _session(tmp_path, drv, ui) as s:
        page = s.form()
        if not page.button("Continue to next step"):
            s.give_up("this page has no continue button")
    assert handed and s.audit["entries"][-1]["outcome"] == "handed_to_human"


def test_the_shipped_linkedin_script_runs_end_to_end(tmp_path):
    """The example in examples/ is run here, so it cannot quietly rot."""
    from pathlib import Path

    from tasktrace import kit

    drv = FakeDriver()
    drv.load_list([
        {"text": "Service Writer · Acme", "detail": "Promoted · 50 applicants",
         "button": "Easy Apply to Service Writer", "detail_only": True},
        {"text": "CX Strategist · Bitdefender", "detail": "Job match is high, we can help you stand out",
         "button": "Easy Apply to CX Strategist", "detail_only": True}])
    drv.load_form([CONTACT, WORK, NEW_ONE], finish="Review your application")
    drv.load_list(drv.list_rows)
    bank = _bank(tmp_path)
    ui = ScriptedUI(values={"what_is_your_notice_period": "2 weeks"})
    kit.configure(mode="watch", _driver=drv, ui=ui, answers=bank, audit_dir=tmp_path / "runs")
    root = Path(__file__).resolve().parents[1]
    audit = kit.run_script(root / "examples" / "linkedin_easy_apply.py")

    assert drv.clicked_row["button"] == "Easy Apply to CX Strategist"  # skipped the unbadged one
    filled = {c["title"]: c["value"] for page in drv.form_pages for c in page if c.get("value")}
    assert filled["Mobile phone number*"] == "7275047083"  # from the bank
    assert filled["Are you authorized to work in the US?*"] == "Yes"
    assert filled["What is your notice period?*"] == "2 weeks"  # asked once, now remembered
    assert ui.asked == ["what_is_your_notice_period"]
    assert drv.pressed == ["Continue to next step", "Continue to next step", "Review your application"]
    assert audit["result"] == "ok"
    kit._defaults.update({"_driver": None, "ui": None, "answers": None})


def test_a_script_crash_is_recorded_instead_of_vanishing(tmp_path):
    drv = FakeDriver()
    drv.load_form([CONTACT])
    try:
        with _session(tmp_path, drv) as s:
            raise ValueError("the page was not what I expected")
    except ValueError:
        pass
    assert s.audit["result"] == "error"
    assert "not what I expected" in s.audit["entries"][-1]["describe"]
