# Changelog

## Unreleased

TaskTrace's output is aimed at an AI that writes a *script* for the task, not one that replays
your clicks. Four changes, all in what gets captured and how the trace asks to be used:

- **A click now records what it was inside, not just what it hit.** Clicking a row in a list
  records which row, out of how many, and everything that row said (job title, company, location —
  not the 50 pixels under the pointer). Clicking inside a modal or form records every field in it,
  with its current value, including the ones you never had to touch. New `container_context` on the step.
- **Click crops follow the element's own bounds**, so OCR reads whole words. Labels used to come
  back sliced in half ("asy Apply") because the screenshot was a fixed box centred on the pointer.
- **Changes inside a single page are now effects.** A modal appearing, a checkbox flipping, a wizard
  going from 20% to 40%: none of these open a window, so none of them used to be recorded. The focused
  window's contents are sampled for a few seconds after each input and the difference is attributed
  to the step that caused it.
- **New guidance for the AI reading the trace**: compile this demonstration into a generalized script
  (loops over list rows, a loop over wizard pages, wildcard matchers, human detours pruned) instead of
  replaying it click for click. `tasktrace compile --guidance replay|both` keeps the old instructions
  for the local executor loop.

## 0.5.4

- **Fixed: permission setup could loop forever.** macOS ties each permission switch to one exact app
  signature, so switches flipped for an older TaskTrace build still looked ON but didn't count, and
  restarting couldn't fix it. Setup now clears TaskTrace's own stale entries first, then asks fresh.
  Builds now share one signature, so this is a one-time step.
- After a permission restart, TaskTrace says which permissions are still off instead of starting the
  whole setup again, and Accessibility is checked the moment you click Done.

## 0.5.3

- **Fixed: pop-ups ignored every click on first launch** (you had to force-quit). The keyboard listener
  behind the ⌃⌥Q stop shortcut (added in 0.4.1) made TaskTrace's pop-ups ignore mouse clicks on macOS 26,
  even when it only listened to other apps. The shortcut is gone for runs: stop with **■ Stop the run** in
  the menu, or the Stop button every step pop-up has. (⌃⌥Q still stops a *recording*.)
- The build now launches the app fresh with no permissions, clicks through its first pop-ups from outside
  like a real mouse, and refuses to publish if they don't respond.

## 0.5.2

- First build signed with the TaskTrace certificate: from here on, updates keep your permissions.
- Pop-ups: the build's click test caught a pop-up ignoring clicks when another app was in front. Went back
  to the bring-to-front call that passed every test on macOS 26, and dropped the app-mode switching.

## 0.5.1

- **In-app updates**: "Check for updates…" in the menu, plus a quiet check at launch and every 6 hours
  (never during a recording or a run). Downloads are checked against their published checksum and must be
  signed with the TaskTrace certificate before they replace the app.
- **Permissions survive updates**: builds are signed with a long-lived TaskTrace certificate, so macOS
  recognizes each update as the same app.
- **Dialogs**: TaskTrace now becomes a regular app while a pop-up is open and uses the newer way to come
  to the front on macOS 14 and later, keeps pop-ups above other windows and on every Space, and no longer
  hides itself between pop-ups.
- **Permissions menu**: each permission has its own item that opens its Settings page directly, with no
  pop-up involved, plus "Restart TaskTrace".
- **Build checks**: every build clicks through its own dialogs on a real Mac, tests an update end to end,
  and is only published if everything passes.

## 0.4.1

- **Stop a run at any time**: "■ Stop the run" in the menu-bar app (it also closes an open step
  pop-up), or ctrl+C in the terminal. (A ⌃⌥Q shortcut was removed in 0.5.3.) The step in progress finishes its current action, nothing after it
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
