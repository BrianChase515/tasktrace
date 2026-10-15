"""TaskTrace menu-bar app.

Everything the command line does, behind a menu-bar icon and plain dialogs:
record a task, copy it for any AI, run a plan an AI wrote (pasted from the
clipboard) while you approve each step, and copy a fix request back to the AI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import rumps
from AppKit import (
    NSAlert,
    NSApplication,
    NSBundle,
    NSRunningApplication,
    NSPasteboard,
    NSPasteboardTypeString,
    NSTextField,
)
from Foundation import NSURL, NSFileManager, NSMakeRect
from PyObjCTools import AppHelper

from . import __version__, permissions, updater
from .appcore import REPO_URL, parse_json_reply, plan_dir_for, plan_filename, result_summary, run_label, trace_for_ai
from .compile import compile_run, write_trace
from .paths import home, list_runs
from .plan import make_plan, validate_plan
from .prompts import author_prompt, revise_prompt
from .replay import Executor
from .review import triage

TITLE_IDLE = "◉"
TITLE_REC = "● REC"
TITLE_PAUSED = "❚❚ REC"
TITLE_BUSY = "▶ Running"
TITLE_STOPPING = "■ Stopping…"
STATE_FILE = ".app-state.json"


# --------------------------------------------------------------------------- dialogs

def on_main(fn, *args, **kwargs):
    """Run fn on the main thread and wait for its result (AppKit UI must live there)."""
    if threading.current_thread() is threading.main_thread():
        return fn(*args, **kwargs)
    box: dict = {}
    done = threading.Event()

    def call():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover
            box["error"] = exc
        finally:
            done.set()

    AppHelper.callAfter(call)
    done.wait()
    if "error" in box:
        raise box["error"]
    return box["value"]


_MODAL_LEVEL = 8       # NSModalPanelWindowLevel
_ALL_SPACES = (1 << 0) | (1 << 8)  # show on every Space, including over full-screen apps


def _activate(app) -> None:
    """Bring TaskTrace forward so its pop-up takes the first click.

    Tested on macOS 26 by clicking real pop-ups: the older activateIgnoringOtherApps: still brings a
    menu-bar app forward when another app is in front; the newer cooperative activate() often doesn't,
    and then the pop-up ignores clicks. Use the older call, plus NSRunningApplication's for good measure."""
    app.activateIgnoringOtherApps_(True)
    try:
        NSRunningApplication.currentApplication().activateWithOptions_(1 << 1)  # ignoring other apps
    except Exception:
        pass


def alert(title: str, text: str = "", buttons=("OK",), field: str | None = None,
          placeholder: str = "") -> tuple[int, str | None]:
    """A native dialog. Returns (index of the button clicked, text typed if there was a field).

    A menu-bar-only app isn't brought forward automatically, and on recent macOS a pop-up from an
    app that isn't in front can ignore clicks. So bring TaskTrace forward first, keep the pop-up
    above other windows and on every Space, and don't hide the app afterwards (hiding made the
    next pop-up start in the background).
    """
    app = NSApplication.sharedApplication()
    _activate(app)
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(text)
    for b in buttons:
        a.addButtonWithTitle_(b)
    tf = None
    if field is not None:
        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 24))
        tf.setStringValue_(field)
        tf.setPlaceholderString_(placeholder)
        a.setAccessoryView_(tf)
        a.window().setInitialFirstResponder_(tf)
    win = a.window()
    win.setLevel_(_MODAL_LEVEL)
    win.setCollectionBehavior_(_ALL_SPACES)
    win.center()
    win.makeKeyAndOrderFront_(None)
    win.orderFrontRegardless()
    choice = int(a.runModal()) - 1000  # NSAlertFirstButtonReturn == 1000
    if choice < 0:  # dismissed by the stop hotkey: treat as the last button, which is always the safe one
        choice = len(buttons) - 1
    typed = str(tf.stringValue()) if tf is not None else None
    return choice, typed


def copy_text(text: str) -> None:
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def paste_text() -> str:
    return str(NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString) or "")


def notify(title: str, text: str) -> None:
    try:
        rumps.notification("TaskTrace", title, text)
    except Exception:  # running outside the .app bundle has no notification identity
        safe = (title + ": " + text).replace('"', "'")
        subprocess.Popen(["osascript", "-e", f'display notification "{safe}" with title "TaskTrace"'])


def reveal(path: Path) -> None:
    subprocess.Popen(["open", "-R", str(path)])


class DialogUI:
    """The executor's questions, as dialogs. Called from the replay thread."""

    def __init__(self, stopping=lambda: False):
        self.stopping = stopping  # true once the person asked to stop the run

    def ask_value(self, name: str, question: str | None = None, options: list | None = None) -> str:
        title = question or f"What should “{name}” be this time?"
        text = ("Options: " + ", ".join(options)) if options else "This value can change from run to run."
        _, v = on_main(alert, title, text, ("Use this answer",), field="")
        return (v or "").strip()

    def confirm(self, step, desc, match):
        lines = [desc]
        if step.get("note"):
            lines.append(f"Your note when recording: {step['note']}")
        if match and match.strategy in ("rel_xy", "abs_xy"):
            lines.append("TaskTrace could only find this by its position on screen. "
                         "If the window moved, choose “I'll do it”.")
        title = f"Step {step['id']}" + (": this one saves or submits something" if step.get("consequential") else "")
        i, _ = on_main(alert, title, "\n\n".join(lines), ("Run it", "Skip", "I'll do it", "Stop"))
        return ["go", "skip", "manual", "stop"][i]

    def handoff(self, step, reason):
        why = ("It's a password field, so TaskTrace never types or stores it." if reason.startswith("secret")
               else reason[:1].upper() + reason[1:] + ".")
        i, _ = on_main(alert, f"Step {step['id']}: your turn",
                       f"{step.get('describe') or step.get('ask', '')}\n\n{why} Do it now, then click Done.",
                       ("Done", "Skip", "Stop"))
        return ["done", "skip", "stop"][i]

    def on_failure(self, step, entry):
        what = {"locator_failed": "TaskTrace couldn't find what to click on screen.",
                "effect_mismatch": "The step ran, but what should have happened next didn't.",
                "error": "Something went wrong running this step."}.get(entry.get("outcome"), "This step didn't work.")
        i, _ = on_main(alert, f"Step {step['id']} didn't work",
                       f"{step.get('describe', '')}\n\n{what}", ("Try again", "I'll do it", "Skip", "Stop"))
        return ["retry", "manual", "skip", "stop"][i]

    def verdict(self, step, entry):
        i, text = on_main(alert, f"Step {step['id']}: did that look right?",
                          f"{entry.get('describe', '')}\n\nIf something's off, say what. "
                          "Your words go straight to the AI that fixes the plan.",
                          ("Looks right", "Wrong, keep going", "Wrong, stop here"), field="",
                          placeholder="What went wrong? (optional if it looks right)")
        text = (text or "").strip()
        if self.stopping():
            return None  # the run was stopped while this was open; nothing to record
        if i == 0:
            return {"verdict": "approved", **({"comment": text} if text else {})}
        return {"verdict": "rejected", "comment": text or "(no details given)", "stop": i == 2}


# --------------------------------------------------------------------------- the app

class TaskTraceApp(rumps.App):
    def __init__(self):
        super().__init__("TaskTrace", title=TITLE_IDLE, quit_button=None)
        self.rec = None
        self.busy = False
        self.executor = None
        # No keyboard shortcut for stopping a run: any NSEvent key monitor (even one that only watches
        # other apps) made TaskTrace's pop-ups ignore mouse clicks on macOS 26. Stop from the menu or
        # with the Stop button that every step pop-up has.
        self._runs_sig = None
        self._rebuild()
        self._ticker = rumps.Timer(self._tick, 0.5)
        self._ticker.start()
        self._first = rumps.Timer(self._first_launch, 1.0)
        self._first.start()
        self._updating = False
        self._update_timer = rumps.Timer(lambda _: self.check_for_updates(None, quiet=True), 6 * 3600)
        self._update_timer.start()

    # ---- state & menu
    def _state(self) -> dict:
        try:
            return json.loads((home() / STATE_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, **kw) -> None:
        st = {**self._state(), **kw}
        home().mkdir(parents=True, exist_ok=True)
        (home() / STATE_FILE).write_text(json.dumps(st))

    def _recent_menu(self) -> rumps.MenuItem:
        root = rumps.MenuItem("Recorded tasks")
        rows = [r for r in list_runs(30) if r.get("compiled")][:12]
        if not rows:
            root.add(rumps.MenuItem("Nothing recorded yet"))
            return root
        for row in rows:
            d = home() / row["run"]
            item = rumps.MenuItem(run_label(row))
            item.update([
                rumps.MenuItem("Copy for AI", callback=lambda _, d=d: self.copy_for_ai(d)),
                rumps.MenuItem("Ask an AI to write a plan…", callback=lambda _, d=d: self.ask_ai_for_plan(d)),
                rumps.MenuItem("Repeat it now…", callback=lambda _, d=d: self.replay_recording(d)),
                rumps.separator,
                rumps.MenuItem("Show in Finder", callback=lambda _, d=d: reveal(d / "trace.json")),
                rumps.MenuItem("Move to Trash…", callback=lambda _, d=d, r=row: self.delete_recording(d, r)),
            ])
            root.add(item)
        return root

    def _rebuild(self) -> None:
        self.menu.clear()
        items = []
        if self.rec:
            paused = self.rec.paused
            items += [
                rumps.MenuItem("■ Stop recording", callback=self.stop_recording),
                rumps.MenuItem("Add a note…  (⌃⌥N)", callback=self.add_note),
                rumps.MenuItem("Mark done  (⌃⌥D)", callback=self.mark_done),
                rumps.MenuItem("Resume" if paused else "Pause  (⌃⌥P)", callback=self.toggle_pause),
            ]
        elif self.busy:
            items += [
                rumps.MenuItem("■ Stop the run", callback=self.stop_run),
                rumps.MenuItem("Running a plan: answer the pop-ups as they appear"),
            ]
        else:
            items.append(rumps.MenuItem("● Record a task…", callback=self.record))
        items += [
            rumps.separator,
            self._recent_menu(),
            rumps.MenuItem("Run a plan from clipboard…", callback=None if (self.rec or self.busy) else self.run_from_clipboard),
            rumps.separator,
            self._permissions_menu(),
            rumps.MenuItem("Open recordings folder", callback=lambda _: self._open_folder()),
            rumps.MenuItem("Check for updates…", callback=self.check_for_updates),
            rumps.MenuItem("How it works", callback=lambda _: subprocess.Popen(["open", REPO_URL])),
            rumps.separator,
            rumps.MenuItem(f"Quit TaskTrace {__version__}", callback=lambda _: rumps.quit_application()),
        ]
        self.menu.update(items)
        self._runs_sig = self._runs_signature()

    def _permissions_menu(self) -> rumps.MenuItem:
        st = permissions.status()
        miss = [n for n in permissions.ORDER if st.get(n) is False]
        root = rumps.MenuItem("Permissions: all set" if not miss else "⚠ Permissions")
        for name in permissions.ORDER:
            ok = st.get(name)
            mark = "✓" if ok else ("✗" if ok is False else "?")
            root.add(rumps.MenuItem(f"{mark} {permissions.PERMISSIONS[name]['title']}: open settings",
                                    callback=lambda _, n=name: self._open_permission(n)))
        root.add(rumps.separator)
        root.add(rumps.MenuItem("Walk me through it…", callback=self.setup_permissions))
        root.add(rumps.MenuItem("Restart TaskTrace (applies new permissions)", callback=lambda _: self._relaunch()))
        return root

    def _open_permission(self, name: str) -> None:
        permissions.request(name)  # lists TaskTrace on that page if it isn't there yet
        permissions.open_settings(name)

    def _runs_signature(self):
        return tuple(r["run"] for r in list_runs(30) if r.get("compiled"))

    def _open_folder(self) -> None:
        home().mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(home())])

    def _tick(self, _) -> None:
        if self.rec is not None:
            if self.rec.stop.is_set():  # stopped with ⌃⌥Q
                self._finish_recording()
                return
            want = TITLE_PAUSED if self.rec.paused else TITLE_REC
            if self.title != want:
                self.title = want
                self._rebuild()
        elif not self.busy and self._runs_signature() != self._runs_sig:
            self._rebuild()

    # ---- first launch & permissions
    def _first_launch(self, timer) -> None:
        timer.stop()
        if not self._state().get("welcomed"):
            alert("TaskTrace is running",
                  "Look for ◉ at the top right of your screen. That's TaskTrace.\n\n"
                  "Record a task once, then hand it to any AI, or have TaskTrace repeat it while you watch. "
                  "Everything stays on this Mac.", ("Got it",))
            self._save_state(welcomed=True)
        if permissions.missing():
            self.setup_permissions(None, first_launch=True)
        self.check_for_updates(None, quiet=True)

    def setup_permissions(self, _, first_launch: bool = False) -> bool:
        miss = permissions.missing()
        if not miss:
            if not first_launch:
                alert("You're all set", "TaskTrace has everything it needs.")
            self._save_state(perm_restart=False)
            return True
        names = "\n".join(f"• {permissions.PERMISSIONS[n]['title']}: {permissions.PERMISSIONS[n]['why']}"
                          for n in miss)
        if self._state().get("perm_restart"):
            # We just restarted to apply permissions and some are still off. Say so plainly; don't loop.
            self._save_state(perm_restart=False)
            i, _ = alert("Some permissions are still off",
                         f"{names}\n\nIf the switch already looks on in System Settings, it belongs to an "
                         "older copy of TaskTrace. Set up will clear that and ask again.",
                         ("Set up", "Not now"))
        else:
            i, _ = alert("TaskTrace needs your OK",
                         f"macOS asks you to approve a few things first:\n\n{names}\n\n"
                         "For each one, TaskTrace opens the right page in System Settings. Just switch TaskTrace on.",
                         ("Set up", "Not now"))
        if i != 0:
            return False
        needs_restart = False
        for name in miss:
            p = permissions.PERMISSIONS[name]
            permissions.clear_stale(name)  # a switch left over from an older build doesn't count
            permissions.request(name)
            permissions.open_settings(name)
            while True:
                i, _ = alert(f"Switch on TaskTrace under {p['title']}",
                             f"In the System Settings window that just opened, find TaskTrace in the "
                             f"{p['title']} list and switch it on. macOS may ask for your password.\n\n"
                             "If it was already on, TaskTrace just turned that old setting off because it "
                             "belonged to an earlier copy. Switch it on again.\n\n"
                             "Not in the list? Click Show me again.",
                             ("Done", "Show me again", "Skip"))
                if i == 1:
                    permissions.request(name)
                    permissions.open_settings(name)
                    continue
                if i == 0 and name == "accessibility" and not permissions.status().get("accessibility"):
                    j, _ = alert("Accessibility still looks off",
                                 "macOS doesn't show TaskTrace as switched on yet. Check the switch next to "
                                 "TaskTrace, then click Done.", ("Check again", "Skip"))
                    if j == 0:
                        continue
                if name != "accessibility":
                    needs_restart = True  # macOS applies these two only after a restart
                break
        if needs_restart:
            i, _ = alert("Restart TaskTrace to finish",
                         "macOS applies Input Monitoring and Screen Recording when TaskTrace restarts. "
                         "It takes a second.", ("Restart now", "Later"))
            if i == 0:
                self._save_state(perm_restart=True)
                self._relaunch()
        self._rebuild()
        return not permissions.missing()

    def _relaunch(self) -> None:
        path = str(NSBundle.mainBundle().bundlePath())
        if path.endswith(".app"):
            subprocess.Popen(["/bin/sh", "-c", f'sleep 1; open "{path}"'])
        else:
            alert("Quit and reopen", "Quit TaskTrace and start it again to apply the new permissions.")
        rumps.quit_application()

    def _require(self, *needed: str) -> bool:
        miss = [n for n in permissions.missing() if n in needed]
        return True if not miss else self.setup_permissions(None)

    # ---- updates
    def check_for_updates(self, _, quiet: bool = False) -> None:
        if self._updating:
            return

        def work():
            try:
                info = updater.latest_info()
            except Exception as exc:
                if not quiet:
                    AppHelper.callAfter(alert, "Couldn't check for updates", str(exc))
                return
            AppHelper.callAfter(self._offer_update, info, quiet)

        threading.Thread(target=work, daemon=True).start()

    def _offer_update(self, info: dict, quiet: bool) -> None:
        if not updater.is_newer(info["version"], __version__):
            if not quiet:
                alert("TaskTrace is up to date", f"You have version {__version__}, the newest.")
            return
        if self.rec is not None or self.busy:
            return  # never interrupt a recording or a run; the next check will offer it again
        if quiet and self._state().get("skip_version") == info["version"]:
            return
        keeps = info.get("signing") == "stable"
        i, _ = alert(f"TaskTrace {info['version']} is available",
                     f"You have {__version__}. Updating takes a few seconds and restarts TaskTrace."
                     + ("\n\nYour permissions carry over." if keeps else
                        "\n\nmacOS may ask you to switch the permissions on again after this one."),
                     ("Update now", "Later", "Skip this version"))
        if i == 2:
            self._save_state(skip_version=info["version"])
        if i != 0:
            return
        self._updating = True
        app_path = Path(str(NSBundle.mainBundle().bundlePath()))
        notify("Updating", f"Installing TaskTrace {info['version']}…")

        def work():
            try:
                updater.install(info, app_path)
            except Exception as exc:
                AppHelper.callAfter(self._update_failed, str(exc))
                return
            AppHelper.callAfter(self._relaunch)

        threading.Thread(target=work, daemon=True).start()

    def _update_failed(self, err: str) -> None:
        self._updating = False
        alert("The update didn't install", f"{err}\n\nTaskTrace is unchanged. You can also download the "
              f"newest version from {REPO_URL}/releases")

    # ---- stopping a run
    def stop_run(self, _) -> None:
        ex = self.executor
        if ex is None or ex.stopping:
            return
        ex.request_stop("person")
        self.title = TITLE_STOPPING
        app = NSApplication.sharedApplication()
        if app.modalWindow() is not None:  # a step's pop-up is open: close it
            app.abortModal()
        notify("Stopping", "Nothing else will run. The step in progress finishes first.")

    # ---- managing recordings
    def delete_recording(self, run_dir: Path, row: dict) -> None:
        if self.busy:
            alert("A plan is running", "Stop the run first, then delete the recording.")
            return
        i, _ = alert(f"Move “{run_label(row)}” to the Trash?",
                     "This removes the recording and any plans, runs and reviews made from it. "
                     "You can restore it from the Trash.", ("Move to Trash", "Cancel"))
        if i != 0:
            return
        ok, _, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(run_dir)), None, None)
        if not ok:
            alert("Couldn't move it to the Trash", str(err))
        self._rebuild()

    # ---- recording
    def record(self, _) -> None:
        if not self._require("accessibility", "input_monitoring", "screen_recording"):
            return
        i, goal = alert("Record a task",
                        "Say what you're about to do in a few words, then do it like you normally would.\n\n"
                        "While recording:\n"
                        "⌃⌥N  add a note about why you're doing something\n"
                        "⌃⌥D  mark the task done\n"
                        "⌃⌥Q  stop (or use the menu)\n\n"
                        "Password fields and password managers are never recorded.",
                        ("Start recording", "Cancel"), field="", placeholder="e.g. Create the quarterly report")
        if i != 0:
            return
        goal = (goal or "").strip() or "Untitled task"
        from .recorder_macos import DEFAULT_EXCLUDED, Recorder, new_run_dir
        run_dir = new_run_dir(home(), goal)
        rec = Recorder(run_dir, goal, goal, set(DEFAULT_EXCLUDED))
        try:
            rec.start()
        except PermissionError as exc:
            shutil.rmtree(run_dir, ignore_errors=True)
            alert("TaskTrace can't record yet", str(exc))
            self.setup_permissions(None)
            return
        self.rec = rec
        self.title = TITLE_REC
        self._rebuild()

    def stop_recording(self, _) -> None:
        if self.rec is not None:
            self._finish_recording()

    def add_note(self, _) -> None:
        if self.rec is not None:
            threading.Thread(target=self.rec._ask_note, daemon=True).start()

    def mark_done(self, _) -> None:
        if self.rec is not None:
            self.rec._mark_done()

    def toggle_pause(self, _) -> None:
        if self.rec is not None:
            self.rec.paused = not self.rec.paused
            self.rec.emit({"type": "paused" if self.rec.paused else "resumed", "reason": "user"})
            self.title = TITLE_PAUSED if self.rec.paused else TITLE_REC
            self._rebuild()

    def _finish_recording(self) -> None:
        rec, self.rec = self.rec, None
        self.title = TITLE_IDLE
        self._rebuild()
        try:
            run_dir = rec.finish()
            trace = compile_run(run_dir)
            write_trace(trace, run_dir / "trace.json")
        except Exception:
            alert("Something went wrong saving the recording", traceback.format_exc()[-1500:])
            return
        self._rebuild()
        n, params = len(trace.get("steps", [])), trace.get("parameters", [])
        extra = (f"\n\nValues that may change next time: " + ", ".join(p["name"] for p in params)) if params else ""
        i, _ = alert(f"Saved: {n} steps", f"“{trace['task'].get('goal') or trace['task']['name']}”{extra}",
                     ("Copy for AI", "Show in Finder", "Done"))
        if i == 0:
            self.copy_for_ai(run_dir)
        elif i == 1:
            reveal(run_dir / "trace.json")

    # ---- handing work to an AI
    def _load_trace(self, run_dir: Path) -> dict | None:
        try:
            return json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            alert("Couldn't open that recording", str(run_dir))
            return None

    def copy_for_ai(self, run_dir: Path) -> None:
        trace = self._load_trace(run_dir)
        if trace:
            copy_text(trace_for_ai(trace))
            notify("Copied", "Paste it into any AI chat.")

    def ask_ai_for_plan(self, run_dir: Path) -> None:
        trace = self._load_trace(run_dir)
        if not trace:
            return
        copy_text(author_prompt(trace, {}, "<your AI's name>"))
        alert("Copied a request for your AI",
              "1. Paste it into your AI (ChatGPT, Copilot, Claude…). Tell it any values for this run.\n"
              "2. It replies with a plan. Copy its whole reply.\n"
              "3. Come back here and choose “Run a plan from clipboard”.")

    def run_from_clipboard(self, _) -> None:
        try:
            plan = parse_json_reply(paste_text())
        except ValueError as exc:
            alert("No plan found", str(exc))
            return
        errs = validate_plan(plan)
        if errs:
            i, _ = alert("That plan needs fixing first", "\n".join(f"• {e}" for e in errs[:8]),
                         ("Copy problems for the AI", "OK"))
            if i == 0:
                copy_text("TaskTrace rejected your plan. Fix these problems and send the full corrected plan "
                          "JSON:\n" + "\n".join(f"- {e}" for e in errs))
            return
        path = plan_dir_for(plan, home()) / plan_filename(plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
        self._start_replay(plan, path)

    def replay_recording(self, run_dir: Path) -> None:
        trace = self._load_trace(run_dir)
        if not trace:
            return
        plan = make_plan(trace, {}, source=run_dir.name)
        path = run_dir / "plans" / plan_filename(plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
        self._start_replay(plan, path)

    # ---- replay
    def _start_replay(self, plan: dict, plan_path: Path) -> None:
        if not self._require("accessibility"):
            return
        a = plan.get("author") or {}
        i, _ = alert(f"Run “{plan.get('goal') or plan.get('plan_id')}”?",
                     f"{len(plan.get('steps', []))} steps, written by {a.get('name')} (version {plan.get('version')}).\n\n"
                     "Step by step: you approve every step and say if anything looks wrong.\n"
                     "Watch: it runs on its own and only stops for important steps.\n"
                     "Check only: makes sure everything can be found, without clicking anything.",
                     ("Step by step", "Watch", "Check only", "Cancel"))
        if i == 3:
            return
        mode = ["step", "watch", "dry-run"][i]
        self.busy = True
        self.title = TITLE_BUSY
        self._rebuild()
        threading.Thread(target=self._replay_thread, args=(plan, plan_path, mode), daemon=True).start()

    def _replay_thread(self, plan: dict, plan_path: Path, mode: str) -> None:
        try:
            from .drivers.macos import MacDriver

            class AppDriver(MacDriver):
                def prepare(self, step):  # activating apps is main-thread work
                    on_main(MacDriver.prepare, self, step)

            from .answers import AnswerBank, default_path
            from .paths import home

            audit_dir = plan_path.parent / "audits"
            # The answer bank is the person's own file: questions they have answered before,
            # so a form_loop step only interrupts for something genuinely new.
            ex = Executor(plan, AppDriver(), None, mode=mode, audit_dir=audit_dir,
                          answers=AnswerBank(default_path(home())))
            ex.ui = DialogUI(stopping=lambda: ex.stopping)
            self.executor = ex
            audit = ex.run()
            review = triage(audit, plan)
            (audit_dir / f"{audit['audit_id']}.review.json").write_text(
                json.dumps(review, indent=1, ensure_ascii=False), encoding="utf-8")
            AppHelper.callAfter(self._replay_done, plan, audit, review, audit_dir)
        except Exception:
            err = traceback.format_exc()[-1500:]
            AppHelper.callAfter(self._replay_failed, err)

    def _replay_done(self, plan, audit, review, audit_dir: Path) -> None:
        self.busy = False
        self.executor = None
        self.title = TITLE_IDLE
        self._rebuild()
        title, body, offer_fix = result_summary(audit, review)
        buttons = ("Copy fix request for AI", "Show files", "Done") if offer_fix else ("Show files", "Done")
        i, _ = alert(title, body, buttons)
        choice = buttons[i]
        if choice.startswith("Copy fix"):
            copy_text(revise_prompt(plan, audit, review, "<your AI's name>"))
            notify("Fix request copied", "Paste it into the AI that wrote the plan.")
        elif choice == "Show files":
            reveal(audit_dir / f"{audit['audit_id']}.json")

    def _replay_failed(self, err: str) -> None:
        self.busy = False
        self.executor = None
        self.title = TITLE_IDLE
        self._rebuild()
        alert("TaskTrace hit a bug while running the plan", f"{err}\n\nPlease report it at {REPO_URL}/issues")


def self_test() -> int:
    """Used by the build to prove the bundled app can load everything it needs."""
    import tasktrace.drivers.macos  # noqa: F401
    import tasktrace.recorder_macos  # noqa: F401
    from tasktrace import ocr  # noqa: F401
    print(f"tasktrace {__version__} self-test ok; permissions: {permissions.status()}")
    return 0


def main() -> None:
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    if "--ui-check" in sys.argv:
        from .uicheck import main as ui_check
        ui_check(sys.argv[sys.argv.index("--ui-check") + 1])
        return
    TaskTraceApp().run()


if __name__ == "__main__":
    main()
