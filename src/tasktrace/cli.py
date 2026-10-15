"""tasktrace command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compile import compile_run, write_trace
from .diff import apply_diff, diff_traces
from .paths import home, list_runs, resolve_trace


def _compile_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-ocr", action="store_true", help="skip on-device OCR of click crops")
    p.add_argument("--embed-crops", action="store_true", help="embed small JPEG thumbnails in trace.json")
    p.add_argument("--redact-emails", action="store_true", help="also redact email addresses")


def _do_compile(run_dir: Path, args, output: Path | None = None) -> Path:
    trace = compile_run(run_dir, use_ocr=not args.no_ocr, embed_crops=args.embed_crops,
                        redact_emails=args.redact_emails)
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

    args = ap.parse_args(argv)

    if args.cmd in ("record", "doctor") and sys.platform != "darwin":
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

    if args.cmd == "mcp":
        from .mcp_server import main as serve
        serve()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
