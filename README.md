# tasktrace

**Record a task on your Mac once. Get one compact file an AI agent can understand and replicate.**

You do a task. tasktrace watches through macOS accessibility APIs and writes a single `trace.json`:
every click, keystroke and window change, with *what you clicked* (not just where), *what it caused*,
*which values change from run to run*, and *why* — in your own words. Hand that file to Claude (or serve it
over MCP) and the agent has what it needs to do the task itself.

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
- **Cause and effect.** Window opens, closes, retitles and app switches are attributed to the step that
  caused them, so the agent can verify each step before moving on instead of clicking blind.
- **Parameters, not just values.** Text you type becomes a named parameter (`{report_title}`), typed as
  text/date/number/currency/email. Record the task twice and `tasktrace diff` confirms which values really
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

## Quickstart

Requires macOS 13+ and Python 3.10+.

```bash
pip install "tasktrace[mcp,images] @ git+https://github.com/<you>/tasktrace"
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

## Use it with Claude

**Paste or attach** `trace.json` and say: *"Here's how I do this task. Do it for Q4, period end 2026-12-31."*

**Or connect over MCP** so Claude can browse your traces itself. Add to your MCP client config
(Claude Desktop: `claude_desktop_config.json`):

```json
{ "mcpServers": { "tasktrace": { "command": "tasktrace", "args": ["mcp"] } } }
```

Tools: `list_traces`, `get_outline`, `get_trace`, `get_step_image`. Read-only.

## How it works

```
recorder_macos.py     CGEventTap (listen-only) ─┐
                      AXUIElement at click/focus ├─> events.jsonl + crops/ + keyframes/
                      CGWindowList poller 5 Hz ─┘
compile.py            events ─> steps ─> effects ─> notes ─> parameters ─> redaction ─> trace.json
diff.py               align two traces ─> confirmed variables / constants / optional steps
mcp_server.py         trace.json ─> MCP tools
```

The raw run is kept, so traces can be recompiled as the compiler improves. The format is specified in
[`schema/trace.schema.json`](schema/trace.schema.json).

## Status and limitations

Alpha. The compiler, diff, redaction and MCP server are covered by tests (`pytest`); the recorder is
macOS-only and depends on what each app exposes through accessibility.

- **Electron and some web apps** expose thin accessibility trees; OCR and relative position carry more weight there.
- **Click crops are taken just after the click** (via `screencapture`), so a button that closes its window may
  not appear in its crop. The accessibility element still identifies it.
- **Window changes are polled** at 5 Hz, so `settle_ms` is approximate.
- **Spaces / full-screen switches** can show up as windows closing and opening.
- Keyboard layout: hotkeys use ANSI key codes.

## Roadmap

- ScreenCaptureKit ring buffer for true *before*-click crops
- Browser DOM capture (selectors) via an extension for web steps
- Replay harness that drives a trace through Claude computer use and scores it against `success_criteria`
- Windows recorder (UI Automation) emitting the same format

## Development

```bash
pip install -e ".[dev,mcp]"
python tests/fixtures/make_fixture.py   # regenerate the sample raw run
pytest
```

MIT licensed.
