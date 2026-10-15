"""Apply to LinkedIn jobs that say you're a strong match.

Written from a recording of Brian doing it once (tasktrace trace "Linkedin", 2026-10-15).
This is an ordinary Python script: no AI runs here, nothing leaves the machine. It reads the
screen through the accessibility tree, so it works on forms it has never seen — which is the
whole point, because every employer's Easy Apply asks different things.

    tasktrace run examples/linkedin_easy_apply.py --mode watch

Start with LinkedIn's job search open in Chrome. The script:

  1. Opens jobs one at a time and reads the panel, because the "job match is high" and "you'd be
     a top applicant" badges are in the panel, not on the card.
  2. Works the application form, however many pages it has, filling each field from the answer
     bank (`tasktrace answers`) and ringing a bell for anything it has not been asked before.
  3. Stops before submitting and waits for you. Submitting is never automatic.

Edit WANTED, SKIP and MAX_JOBS to change what it goes for.
"""

from tasktrace.kit import Session

WANTED = ["job match is high", "top applicant"]      # what a worthwhile job's panel says
SKIP = ["no longer accepting", "applied", "application submitted"]
MAX_JOBS = 12                 # how many to open before giving up for now
ADVANCE = ["Continue to next step", "Next", "Continue"]
FINISH = ["Review your application", "Submit application"]
MAX_PAGES = 15                # a form longer than this is not a form, it is a loop


def choose_a_job(s: Session) -> bool:
    """Open jobs until one reads well, then press its Easy Apply button."""
    for job in s.rows()[:MAX_JOBS]:
        card = job.get("text") or ""
        if any(word in card.lower() for word in SKIP):
            continue
        s.open(job)
        if not s.page_says(*WANTED):
            continue
        if s.page_says(*SKIP):
            continue
        apply_button = s.find("AXButton", starts_with="Easy Apply")
        if apply_button is None:
            s.note(f"badged but no Easy Apply button: {card[:80]}")
            continue
        s.note(f"applying to: {card[:120]}")
        return s.click(apply_button)
    s.give_up(f"opened {MAX_JOBS} jobs and none said {' or '.join(WANTED)}")
    return False


def work_the_form(s: Session) -> bool:
    """Fill in whatever this employer asks, page after page, until the end of the form."""
    last_progress = None
    for _ in range(MAX_PAGES):
        if not s.running:
            return False
        page = s.form()
        for control in page.empty():
            if s.answer(control) is None:        # the person declined to answer
                s.give_up(f"unanswered: {control.get('title')}")
                return False
        if page.button(*FINISH):
            return True
        nxt = page.button(*ADVANCE)
        if nxt is None:
            s.give_up("no button to continue with — an upload, or a page I don't understand")
            return False
        s.click(nxt)
        s.settle()
        if s.form().progress == page.progress == last_progress:
            s.give_up(f"the form stopped moving at {page.progress}")
            return False
        last_progress = page.progress
    s.give_up(f"still going after {MAX_PAGES} pages")
    return False


def main() -> None:
    with Session("Google Chrome", window="LinkedIn", name="linkedin-easy-apply") as s:
        if not choose_a_job(s):
            return
        s.settle(1.0)
        if not work_the_form(s):
            return
        # Review first: the last page lists everything that is about to be sent.
        review = s.form().button("Review your application")
        if review:
            s.click(review)
            s.settle(1.0)
        # Always asks, whatever the mode. Nothing after this can be taken back.
        if s.click(s.form().button("Submit application"), consequential=True):
            s.wait_for("application was sent", "your application was sent", timeout=15)
            s.click(s.find("AXButton", label="Not now"))


if __name__ == "__main__":
    main()
