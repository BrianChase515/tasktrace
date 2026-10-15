"""tasktrace command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compile import GUIDANCE_MODES, compile_run, write_trace
from .diff import apply_diff, diff_traces
from .paths import home, list_runs, resolve_trace


def _compile_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-ocr", action="store_true", help="skip on-device OCR of click crops")
    p.add_argument("--embed-crops", action="store_true", help="embed small JPEG thumbnails in trace.json")
    p.add_argument("--redact-emails", action="store_true", help="also redact email addresses")
    p.add_argument("--guidance", choices=GUIDANCE_MODES, default="synthesize",
                   help="instructions for the AI that reads the trace. synthesize (default): write a "
                        "generalized script from it. replay: follow it step by step. both: include each")


def _do_compile(run_dir: Path, args, output: Path | None = None) -> Path:
    trace = compile_run(run_dir, use_ocr=not args.no_ocr, embed_crops=args.embed_crops,
                        redact_emails=args.redact_emails, guidance=args.guidance)
    out = write_trace(trace, output or run_dir / "trace.json")
    size = out.stat().st_size
    print(f"Wrote {out}")
    print(f"  {len(trace['steps'])} steps, {len(trace['parameters'])} parameters, "
          f"{trace.get('redactions', {}).get('total', 0)} redactions, ~{size // 4:,} tokens")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tasktrace",
                                 description="Record a task once; get one trace an AI agent can replicate.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record a task demonstration (macOS)")
    r.add_argument("name", nargs="?", default="task", help="short name for the task")
    r.add_argument("--goal", default="", help="one sentence on what the task accomplishes")
    r.add_argument("--exclude", action="append", default=[], metavar="BUNDLE_ID",
                   help="never record while this app is in front (repeatable)")
    r.add_argument("--no-crops", action="store_true", help="don't screenshot around clicks")
    r.add_argument("--no-keyframes", action="store_true", help="don't screenshot windows as they open")
    r.add_argument("--no-compile", action="store_true", help="keep the raw run only")
    _compile_flags(r)

    c = sub.add_parser("compile", help="turn a raw run folder into trace.json")
    c.add_argument("run_dir")
    c.add_argument("-o", "--output", help="output path (default: <run_dir>/trace.json)")
    _compile_flags(c)

    o = sub.add_parser("outline", help="print a trace's one-line-per-step outline")
    o.add_argument("trace")

    d = sub.add_parser("diff", help="compare two runs of the same task to find variables vs constants")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--write", action="store_true", help="update trace A with the findings")

    sub.add_parser("list", help="list recorded runs")
    doc = sub.add_parser("doctor", help="check macOS permissions")
    doc.add_argument("--request", action="store_true", help="prompt for missing permissions")
    sub.add_parser("mcp", help="run the MCP server (stdio)")
    sub.add_parser("app", help="start the menu-bar app (macOS)")

    # ---- the human-supervised loop: plan -> replay -> review -> revise
    pl = sub.add_parser("plan", help="turn a trace into a version-1 plan (no AI needed)")
    pl.add_argument("trace")
    pl.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="parameter value")
    pl.add_argument("-o", "--output")

    va = sub.add_parser("validate", help="check a plan before running it")
    va.add_argument("plan")

    pr = sub.add_parser("prompt", help="print a prompt so any AI can author or revise a plan")
    prs = pr.add_subparsers(dest="role", required=True)
    pa = prs.add_parser("author", help="write a plan from a trace")
    pa.add_argument("trace")
    pa.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    pa.add_argument("--as", dest="who", default="<your name>", help="name the AI should sign as")
    pv = prs.add_parser("revise", help="write the next plan version from an audit")
    pv.add_argument("plan")
    pv.add_argument("audit")
    pv.add_argument("--as", dest="who", default="<your name>")

    rp = sub.add_parser("replay", help="run a plan here while you watch; writes an audit")
    rp.add_argument("plan")
    rp.add_argument("--mode", choices=["step", "watch", "dry-run"], default="step",
                    help="step: confirm every step (default). watch: confirm consequential steps only. "
                         "dry-run: only check every target can be found")
    rp.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    rp.add_argument("--driver", default="auto", choices=["auto", "macos", "fake"])
    rp.add_argument("--allow-abs", action="store_true", help="allow raw screen coordinates as a last resort")
    rp.add_argument("--audit-dir", help="default: an audits/ folder next to the plan")
    rp.add_argument("--answers", help="answer bank to use (default: answers.json in your TaskTrace folder)")
    rp.add_argument("--quiet", action="store_true", help="no sound when the run needs you")

    an = sub.add_parser("answers", help="questions you've already answered, reused by form_loop steps")
    an.add_argument("--learn-from", metavar="TRACE", action="append", default=[],
                    help="harvest question/answer pairs from a recording (repeatable)")
    an.add_argument("--set", action="append", default=[], metavar="QUESTION=ANSWER")
    an.add_argument("--forget", metavar="QUESTION")
    an.add_argument("--ask-always", metavar="QUESTION", help="always stop and ask this one")
    an.add_argument("--path", help="answer bank file (default: answers.json in your TaskTrace folder)")

    rv = sub.add_parser("review", help="triage an audit and route each problem")
    rv.add_argument("audit")
    rv.add_argument("--plan", help="the plan that was run (adds target details)")

    hi = sub.add_parser("history", help="show who generated each plan version in a folder")
    hi.add_argument("folder")

    args = ap.parse_args(argv)

    def _values(pairs: list[str]) -> dict:
        out = {}
        for p in pairs:
            k, _, v = p.partition("=")
            out[k.strip()] = v
        return out

    def _load(path: str) -> dict:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))

    def _write(obj: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    if args.cmd == "plan":
        from .plan import make_plan, missing_values
        tp = resolve_trace(args.trace)
        plan = make_plan(_load(str(tp)), _values(args.set), source=tp.parent.name)
        out = _write(plan, Path(args.output) if args.output else tp.parent / "plans" / f"{plan['plan_id']}-v1.json")
        print(f"Wrote {out} ({len(plan['steps'])} steps)")
        miss = missing_values(plan)
        if miss:
            print("Values still needed (pass --set, or you'll be asked at replay): " + ", ".join(miss))
        return 0

    if args.cmd == "validate":
        from .plan import validate_plan
        errs = validate_plan(_load(args.plan))
        print("OK" if not errs else "\n".join(f"- {e}" for e in errs))
        return 0 if not errs else 1

    if args.cmd == "prompt":
        from .prompts import author_prompt, revise_prompt
        from .review import triage
        if args.role == "author":
            print(author_prompt(_load(str(resolve_trace(args.trace))), _values(args.set), args.who))
        else:
            plan, audit = _load(args.plan), _load(args.audit)
            print(revise_prompt(plan, audit, triage(audit, plan), args.who))
        return 0

    if args.cmd == "answers":
        from .answers import AnswerBank, default_path, normalize
        bank = AnswerBank(Path(args.path).expanduser() if args.path else default_path(home()))
        for t in args.learn_from:
            n = bank.seed_from_trace(_load(str(resolve_trace(t))))
            print(f"Learned {n} answers from {t}")
        for pair in args.set:
            q, _, a = pair.partition("=")
            bank.learn(q.strip(), a.strip())
            print(f"Saved: {q.strip()}")
        if args.ask_always:
            bank.learn(args.ask_always, "", ask_always=True)
            print(f"Will always ask: {args.ask_always}")
        if args.forget:
            key = normalize(args.forget)
            before = len(bank.entries)
            bank.entries = [e for e in bank.entries if e.get("key") != key]
            bank.save()
            print(f"Forgot {before - len(bank.entries)} answer(s)")
        if not (args.learn_from or args.set or args.forget or args.ask_always):
            for e in bank.entries:
                flag = "  [always asks]" if e.get("ask_always") else ""
                print(f"  {e['question']}\n      -> {e.get('answer', '')!r}{flag}")
            print(f"\n{len(bank.entries)} answers in {bank.path}")
        return 0

    if args.cmd == "replay":
        from .answers import AnswerBank, default_path
        from .drivers import get_driver
        from .plan import validate_plan
        from .replay import Executor, TerminalUI
        plan_path = Path(args.plan).expanduser()
        plan = _load(str(plan_path))
        errs = validate_plan(plan)
        if errs:
            print("Plan failed validation, not running it:\n" + "\n".join(f"- {e}" for e in errs), file=sys.stderr)
            return 1
        plan.setdefault("values", {}).update(_values(args.set))
        author = plan.get("author") or {}
        print(f"Plan {plan['plan_id']} v{plan['version']} by {author.get('name')} ({author.get('kind')}), "
              f"{len(plan['steps'])} steps, mode={args.mode}")
        audit_dir = Path(args.audit_dir) if args.audit_dir else plan_path.parent / "audits"
        if args.quiet:
            from . import alert
            alert.set_enabled(False)
        bank = AnswerBank(Path(args.answers).expanduser() if args.answers else default_path(home()))
        ex = Executor(plan, get_driver(args.driver), TerminalUI(), mode=args.mode, audit_dir=audit_dir,
                      allow_abs=args.allow_abs, answers=bank)
        audit = ex.run()
        print(f"\nResult: {audit['result']}  {audit['summary']['outcomes']}")
        print(f"Audit: {audit_dir / (audit['audit_id'] + '.json')}")
        print(f"Next: tasktrace review {audit_dir / (audit['audit_id'] + '.json')} --plan {plan_path}")
        return 0

    if args.cmd == "review":
        from .review import triage
        audit = _load(args.audit)
        rep = triage(audit, _load(args.plan) if args.plan else None)
        out = _write(rep, Path(args.audit).with_name(Path(args.audit).stem + ".review.json"))
        print(f"Verdict: {rep['verdict']}   ({rep['next']})")
        for f in rep["findings"]:
            extra = f"  \"{f['human_comment']}\"" if f.get("human_comment") else ""
            print(f"  {f['id']} [{f['severity']}] step {f['step']} {f['category']} -> {f['route_to']}: "
                  f"{f['summary']}{extra}")
        print(f"Wrote {out}")
        return 0

    if args.cmd == "history":
        from .review import lineage
        plans = [json.loads(p.read_text(encoding="utf-8")) for p in Path(args.folder).expanduser().glob("*.json")]
        plans = [p for p in plans if str(p.get("schema", "")).startswith("tasktrace-plan")]
        print("\n".join(lineage(plans)) or "No plans found.")
        return 0

    if args.cmd in ("record", "doctor", "app") and sys.platform != "darwin":
        print("Recording needs macOS. You can still compile, diff and serve traces here.", file=sys.stderr)
        return 2

    if args.cmd == "record":
        from .recorder_macos import record_run
        print("Recording. Do the task, then press ctrl+opt+Q (or ctrl+C here).")
        print("  ctrl+opt+N  add a note (why)   ctrl+opt+D  mark done   ctrl+opt+P  pause/resume")
        try:
            run_dir = record_run(home(), args.name, args.goal, args.exclude,
                                 crops=not args.no_crops, keyframes=not args.no_keyframes)
        except PermissionError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Saved raw run to {run_dir}")
        if not args.no_compile:
            _do_compile(run_dir, args)
        return 0

    if args.cmd == "compile":
        _do_compile(Path(args.run_dir).expanduser(), args, Path(args.output) if args.output else None)
        return 0

    if args.cmd == "outline":
        t = json.loads(resolve_trace(args.trace).read_text(encoding="utf-8"))
        print(f"# {t['task'].get('name')}" + (f": {t['task']['goal']}" if t["task"].get("goal") else ""))
        for p in t.get("parameters", []):
            print(f"  param {p['name']} ({p['kind']}, {p['status']})" + (f" e.g. {p['example']!r}" if p.get("example") else ""))
        print("\n".join(t.get("outline", [])))
        return 0

    if args.cmd == "diff":
        pa, pb = resolve_trace(args.a), resolve_trace(args.b)
        a, b = (json.loads(p.read_text(encoding="utf-8")) for p in (pa, pb))
        report = diff_traces(a, b)
        print(json.dumps(report, indent=2))
        if args.write:
            write_trace(apply_diff(a, report), pa)
            print(f"Updated {pa}")
        return 0

    if args.cmd == "list":
        for row in list_runs():
            print(json.dumps(row))
        return 0

    if args.cmd == "doctor":
        from .recorder_macos import check_permissions
        res = check_permissions(request=args.request)
        for k, v in res.items():
            print(f"  {k:18} {'ok' if v else 'MISSING' if v is False else 'unknown'}")
        if not all(v is not False for v in res.values()):
            print("Grant these to your terminal app in System Settings > Privacy & Security, then restart it.")
            return 1
        return 0

    if args.cmd == "app":
        from .app import main as run_app
        run_app()
        return 0

    if args.cmd == "mcp":
        from .mcp_server import main as serve
        serve()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
