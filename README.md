# tasktrace

**Record a task on your Mac once. Get one compact file an AI agent can understand and replicate.**

You do a task. tasktrace watches through macOS accessibility APIs and writes a single `trace.json`:
every click, keystroke and window change, with *what you clicked* (not just where), *what it caused*,
*which values change from run to run*, and *why* — in your own words. Hand that file to Claude (or serve it
over MCP) and the agent has what it needs to do the task itself.

Then close the loop: **any AI writes a plan, a local executor runs it while you watch, every step is
audited, and your corrections are triaged back to the AI** for the next version. The AI never needs
access to your mouse, so it works even with a locked-down corporate assistant.

Local-first. Nothing leaves your machine. Password fields are never recorded.

```
1. Click button "New Report" in Safari > Acme Portal - Reports -> opens "Report Wizard"
2. Click text field "Report title" in Safari > Report Wizard
3. Type {report_title} (e.g. "Q3 forecast") into text field "Report title" in Safari > Report Wizard
4. Press tab
5. Type {period_end} (e.g. "2026-09-30") into text field "Period end" in Safari > Report Wizard
6. Click password field "Approval PIN" in Safari > Report Wizard
7. Type [secret] into password field "Approval PIN" in Safari > Report Wizard
8. Click button "Create" in Safari > Report Wizard -> closes "Report Wizard" -> window becomes "Acme Portal - Q3 forecast"
9. Press cmd+s in Safari > Acme Portal - Q3 forecast
```

That's the trace's `outline`. Each step underneath carries the detail an agent needs to act:

```json
{
  "id": 1,
  "action": "click",
  "describe": "Click button \"New Report\" in Safari > Acme Portal - Reports -> opens \"Report Wizard\"",
  "target": {
    "element": { "role": "AXButton", "title": "New Report" },
    "path": "AXWindow 'Acme Portal - Reports' > AXWebArea > AXGroup 'toolbar' > AXButton 'New Report'",
    "ocr_label": "New Report",
    "rel_xy": [0.896, 0.134],
    "abs_xy": [1290, 142],
    "crop": "crops/0001.png"
  },
  "intent": "Starting from the Reports page; New Report is top right",
  "effect": { "opened": ["Report Wizard"], "settle_ms": 580 }
}
```

Full example: [`examples/sample-trace.json`](examples/sample-trace.json) (~2k tokens for a 9-step task).

## Why

Agents that operate computers fail for the same reason new hires do: nobody wrote down how the task
actually goes. Explaining it in a prompt is slow and lossy. Screen recordings are huge and ambiguous.
Raw click coordinates break the moment a window moves.

tasktrace sits in between: a structured, reviewable record designed to be *read by a model*.

| | Continuous screen memory (e.g. Screenpipe) | Record-and-replay (e.g. OpenAdapt) | **tasktrace** |
|---|---|---|---|
| Unit of capture | Everything, 24/7 | One workflow | One task run |
| Output | Searchable database | Compiled replay script | One trace file for an LLM to reason over |
| Who executes | Agent searches history | Deterministic replayer | Any agent (Claude, computer use, MCP) |
| Cause and effect | — | Postconditions | Per-step `effect` |
| Variables | — | Parameters | Inferred, confirmed by diffing runs |
| Your reasoning | — | — | `intent` notes via hotkey |

These tools are complementary. tasktrace's bet is that the **file format** is the product.

## What makes a trace useful

- **Ranked locators.** Every click records the accessibility element (role, title, ancestry path), the text
  visible at the click point (on-device OCR), the position relative to the window, and absolute
  coordinates — in that order of robustness. `replay_guidance` tells the agent to use them in that order.
  A click on a button's label resolves to the button itself, and clicks in the Dock become "Open <app>" steps.
- **Works inside Chrome and Electron apps.** Chromium browsers and Electron apps (Slack, VS Code, Notion…)
  hide their contents from accessibility tools unless asked. TaskTrace asks while it records or replays, so
  web buttons and form fields come through by name, then switches the setting back when it's done.
- **Cause and effect.** Window opens, closes, retitles and app switches are attributed to the step that
  caused them, so the agent can verify each step before moving on instead of clicking blind.
- **Parameters, not just values.** Text you type becomes a named parameter (`{report_title}`), typed as
  text/date/number/currency/email. A field with no label is named after the question you clicked just before. Record the task twice and `tasktrace diff` confirms which values really
  vary and which are constants, and marks steps that only happen sometimes as `optional`.
- **Intent.** Press `ctrl+opt+N` mid-task to add a note ("always use the last day of the quarter"). It
  attaches to the next step. Decision points are exactly what screen data can't show.
- **Success criteria.** Press `ctrl+opt+D` when you're done; tasktrace snapshots and OCRs the screen so the
  agent can check its own work.
- **Token-efficient.** Keystrokes collapse into typed strings (typos and backspaces resolved), repeated keys
  and scrolls merge, empty fields are dropped.

## Privacy

- Everything is stored locally in `~/TaskTraces` (override with `TASKTRACE_HOME`). No network calls.
- **Password fields** (`AXSecureTextField`) are detected at record time: keystrokes are stored as `•`
  and no screenshot is taken. The trace marks the step `secret`.
- **Password managers** (1Password, Bitwarden, Keychain Access, Passwords…) pause recording while in front,
  and their windows are never logged. Add your own with `--exclude <bundle id>`.
- **Redaction** scrubs API keys, JWTs, card numbers (Luhn-checked) and SSNs from every string in the trace;
  `--redact-emails` adds email addresses.
- `ctrl+opt+P` pauses and resumes at any time.

Review a trace before sharing it. Click crops are small, but they are screenshots.

## Download (no coding needed)

**[Download TaskTrace for Mac](https://github.com/BrianChase515/tasktrace/releases/download/latest/TaskTrace.dmg)**
· Apple silicon · macOS 13+ · [all builds](https://github.com/BrianChase515/tasktrace/releases)

1. Open the .dmg and drag **TaskTrace** into **Applications**.
2. Open it. macOS will say it can't verify the developer. Go to **System Settings → Privacy & Security**,
   scroll down, and click **Open Anyway**. (Once. This build isn't signed with a paid Apple Developer ID.)
3. TaskTrace walks you through its three permissions. For each, it opens the right Settings page; you
   switch TaskTrace on. Then it restarts itself. If a pop-up ever won't respond, use **◉ → Permissions**:
   each item there opens its Settings page directly.
4. Click **◉** in the menu bar → **Record a task…**. Do the task. Click **■ Stop recording**.

From the menu bar you can then:

- **Copy for AI** a recording, and paste it into any AI chat
- **Ask an AI to write a plan** (copies a request; paste it into ChatGPT, Copilot or Claude)
- **Run a plan from clipboard** (copy the AI's reply first) and approve each step in a pop-up
- **Copy fix request for AI** when something went wrong, so the AI sends back a corrected plan
- **Stop the run** at any moment from the menu or with **⌃⌥Q**, even while a step's pop-up is open
- **Move to Trash…** any recording you no longer want (with its plans and run history)

Everything is saved in `TaskTraces` in your home folder.

**Updates** install from inside the app: **◉ → Check for updates…** (it also checks on its own and asks
first, never during a recording or a run). Updates keep your permissions, because every build is signed
with the same TaskTrace certificate, and the updater refuses any download that isn't, or whose checksum
doesn't match.

## Command line

Requires macOS 13+ and Python 3.9+ (the Python that ships with macOS works; the MCP server needs 3.10+).

```bash
pip install "tasktrace[mcp,images] @ git+https://github.com/BrianChase515/tasktrace"
tasktrace doctor --request      # grant Accessibility, Input Monitoring, Screen Recording to your terminal
tasktrace record "q3-report" --goal "Create the quarterly forecast report in Acme Portal"
# ...do the task...  ctrl+opt+N note · ctrl+opt+D mark done · ctrl+opt+Q stop
tasktrace outline q3-report     # or open ~/TaskTraces/<run>/trace.json
```

Permissions apply to the app that runs Python (Terminal, iTerm, VS Code). Restart it after granting.

| Command | What it does |
|---|---|
| `tasktrace record [name] --goal "..."` | Record a run, then compile it |
| `tasktrace compile <run_dir> [--embed-crops]` | Rebuild `trace.json` from raw events |
| `tasktrace outline <run>` | Print parameters and the step outline |
| `tasktrace diff <runA> <runB> [--write]` | Learn variables vs constants from two runs |
| `tasktrace list` | List runs |
| `tasktrace mcp` | Serve traces to MCP clients |
| `tasktrace app` | Start the menu-bar app from your Python install |

## Use it with Claude

**Paste or attach** `trace.json` and say: *"Here's how I do this task. Do it for Q4, period end 2026-12-31."*

**Or connect over MCP** so Claude can browse your traces itself. Add to your MCP client config
(Claude Desktop: `claude_desktop_config.json`):

```json
{ "mcpServers": { "tasktrace": { "command": "tasktrace", "args": ["mcp"] } } }
```

Tools: `list_traces`, `get_outline`, `get_trace`, `get_step_image`. Read-only.

## The supervised loop: AI writes, a script runs, you judge, AI revises

Most AI assistants at work can't touch your computer, and you wouldn't want an unsupervised one to.
tasktrace splits the job so no single party has to be trusted with everything:

```
  trace ──> AUTHOR (any AI) ──> plan v1 ──> EXECUTOR (no AI, on your Mac) ──> audit
              ^                                   │  you confirm, skip, take over,
              │                                   │  or say "that was wrong because..."
              │                                   v
   plan v2 <── REVISER (any AI) <── review: each problem triaged and routed
                                       author · executor · human · re-record
```

| Role | Who | What it can do |
|---|---|---|
| Author / reviser | Any AI: Claude, Copilot, a local model | Reads a trace, writes a plan file. Text only. |
| Executor | `tasktrace replay` | Finds targets, clicks and types, checks each step's effect. No AI inside. |
| Operator | You | Gates steps, types secrets, takes over, writes corrections. Your word is ground truth. |
| Triage | `tasktrace review` | Classifies every problem and routes it to whoever can fix it. |

```bash
tasktrace plan q3-report --set report_title="Q4 forecast" --set period_end=2026-12-31
#   or let an AI write it:  tasktrace prompt author q3-report --as Copilot  (paste, save its JSON reply)
tasktrace validate plan.json
tasktrace replay plan.json                 # step mode: you approve each step
tasktrace review audits/<audit>.json --plan plan.json
tasktrace prompt revise plan.json audits/<audit>.json --as Copilot   # paste to get v2
tasktrace history plans/                   # who generated each version, and why it changed
```

What a session looks like (step 5's value was wrong, and the "Create" button had been renamed):

```
[5] Type {period_end} (e.g. "2026-09-30") into text field "Period end"  [value: '2026-12-31']
     found by element
     note: Always use the last day of the quarter
     [Enter] run   s skip   m I'll do it   q stop >
     look right? [Enter] yes, or type what went wrong > Period end must be the fiscal quarter end, 2027-01-31

[7] Your turn (secret: you type it, it is never recorded): Type [secret] into password field "Approval PIN"

[8] Click button "Create" -> closes "Report Wizard"  [consequential]
     found by rel_xy  <- position only, check the pointer target

$ tasktrace review audits/q3-report-v1-20261015-104512.json
Verdict: revise
  F1 [high] step 5 human_correction -> author  "Period end must be the fiscal quarter end, 2027-01-31"
  F2 [low]  step 8 fragile_locator  -> author
```

**Accountability is built into the files.** Every plan names its author (tool, AI model or human),
version, parent and a changelog where each change cites the review finding it fixes. Every audit records
the plan and author it ran, the operator, which locator matched, what was expected versus what happened,
before/after screenshots, and every human verdict.

**Safety rails.** You can stop a run at any moment: ⌃⌥Q or **■ Stop the run** in the app, ctrl+C in the
terminal. The click or keystroke in progress finishes and nothing after it runs; the audit still gets
written and says you stopped it. Step mode confirms every step. Watch mode still stops for anything that saves, submits,
sends, deletes, pays or creates, and for any target found only by position. A typo at a prompt re-asks; it
never means "go". Raw screen coordinates are off unless you pass `--allow-abs`. `--mode dry-run` checks
that every target can be found without touching anything. If an expected window doesn't appear, the run
stops instead of guessing.

**Asking instead of guessing.** A plan can include an `ask` step where the author wasn't sure:

```json
{"id": 4, "action": "ask", "question": "Which report title should I use?", "param": "title", "options": ["Q3", "Q4"]}
```

The run pauses there and asks you (a pop-up in the app, a prompt in the terminal). Later steps use your
answer as `{title}`, and the audit records the question and answer.

## How it works

```
recorder_macos.py     CGEventTap (listen-only) ─┐
                      AXUIElement at click/focus ├─> events.jsonl + crops/ + keyframes/
                      CGWindowList poller 5 Hz ─┘
compile.py            events ─> steps ─> effects ─> notes ─> parameters ─> redaction ─> trace.json
diff.py               align two traces ─> confirmed variables / constants / optional steps
mcp_server.py         trace.json ─> MCP tools
plan.py / prompts.py  trace ─> plan (deterministic, or any AI via prompt) with author + lineage
replay.py             plan ─> locator ladder ─> drivers/macos.py (AXPress, CGEvent) ─> audit
review.py             audit ─> findings routed to author / executor / human / trace
```

The raw run is kept, so traces can be recompiled as the compiler improves. The format is specified in
[`schema/trace.schema.json`](schema/trace.schema.json).

## Status and limitations

Alpha. The compiler, diff, redaction, MCP server and the whole plan → replay → audit → review loop are
covered by tests (`pytest`), the loop against a simulated app (`drivers/fake.py`). The recorder and the
macOS driver depend on what each app exposes through accessibility.

On a work machine, check your IT policy before running anything that records input or controls the
mouse and keyboard. Step mode's per-step approval makes that conversation easier.

- **Some web pages and custom-drawn apps** still expose little to accessibility tools; OCR and relative
  position carry more weight there. Firefox isn't switched on automatically yet.
- **System prompts** (like macOS asking to allow notifications) are left out of recordings on purpose.
- **Click crops are taken just after the click** (via `screencapture`), so a button that closes its window may
  not appear in its crop. The accessibility element still identifies it.
- **Window changes are polled** at 5 Hz, so `settle_ms` is approximate.
- **Spaces / full-screen switches** can show up as windows closing and opening.
- Keyboard layout: hotkeys use ANSI key codes.

## Roadmap

- ScreenCaptureKit ring buffer for true *before*-click crops
- Browser DOM capture (selectors) via an extension for web steps
- On-screen overlay for step mode (approve and comment without switching to the terminal)
- MCP tools for the loop, so an agent can request a replay and read its audit
- Windows recorder (UI Automation) emitting the same format

## Releases and signing

Every push to `main` builds `TaskTrace.app`, signs it, and runs checks on a real Mac before publishing:
a self-test, a dialog check that clicks every button the way a person would (launched normally and
directly), and an update check that installs the new build over itself and makes sure a build signed
by someone else, or a corrupted download, is refused. Only then is it published as the `latest`
release with `latest.json`, which the in-app updater reads.

macOS keeps permissions per signing identity, so builds are signed with a long-lived certificate kept in
the `TASKTRACE_SIGNING_P12` repository secret (a base64 `.p12`). Without that secret, builds are ad-hoc
signed and permissions reset on every update. The certificate's fingerprint is pinned in
`src/tasktrace/updater.py` as `STABLE_CERT`.

## Development

```bash
pip install -e ".[dev,mcp]"
python tests/fixtures/make_fixture.py   # regenerate the sample raw run
pytest
```

MIT licensed.
