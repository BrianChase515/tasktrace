# Changelog

## 0.4.1

- **Stop a run at any time**: ⌃⌥Q or "■ Stop the run" in the menu-bar app (it also closes an open step
  pop-up), or ctrl+C in the terminal. The step in progress finishes its current action, nothing after it
  runs, waits for an app to respond are cut short, and the audit records who stopped it.
- A deliberate stop is reported as "Stopped", not as a failure that needs fixing.
- **Delete recordings** from the app: "Move to Trash…" under each recorded task. It can be restored from the Trash.

## 0.4.0

Fixes from the first real-world recording (a 70-step task in Chrome).

**Recording**
- Chrome, Edge, Brave, Arc and other Chromium browsers, plus Electron apps, now expose their page and app
  contents while TaskTrace records or replays, so web buttons and fields are captured by name instead of
  as an unnamed "scroll area". The setting is switched back afterwards.
- A click on a button's or link's text resolves to the button or link itself.
- Clicks on the Dock, the menu bar and popovers are attributed to the right app instead of the window underneath.
- macOS notification prompts are left out of recordings.
- Screenshots around each click are wider (360x110), and on-screen text that's cut off at the edge loses to
  text under the click point. This fixes labels like "Submit applicatior".

**Compiling**
- Dock clicks become `open_app` steps ("Open Google Chrome").
- Typed values in unlabeled fields are named after the question clicked just before
  (`years_of_experience_with_python` instead of `text_21`), shown as `target.field_hint`.

**Plans and replay**
- New `open_app` step.
- New `ask` step: when a plan's author isn't sure, the run pauses and asks the person; later steps use the answer.
- The macOS driver switches on accessibility for the app it's replaying in, and restores it when done.

## 0.3.0

- TaskTrace menu-bar app with permission walkthrough, recorded-tasks menu, Copy for AI, run a plan from
  the clipboard, step approvals as pop-ups, and plain-language results.
- Automatic .dmg build on GitHub with its own Python inside.
- Runs on the Python 3.9 that ships with macOS.

## 0.2.0

- Plans with authorship and version history, the replay executor with human approval and audit log,
  and review that routes each problem to whoever can fix it.

## 0.1.0

- Recorder, trace compiler, run comparison, redaction, MCP server for reading traces.
