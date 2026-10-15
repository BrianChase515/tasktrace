"""MCP server: lets Claude (or any MCP client) read your recorded traces directly.

Add to your MCP client config, e.g. Claude Desktop:

    {"mcpServers": {"tasktrace": {"command": "tasktrace", "args": ["mcp"]}}}

Read-only. Nothing leaves your machine except what the client asks for.
"""

from __future__ import annotations

import json

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Image, MCPServer as _Server
except ImportError:
    try:  # SDK 1.x
        from mcp.server.fastmcp import FastMCP as _Server, Image
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("The MCP server needs the mcp extra: pip install 'tasktrace[mcp]'") from exc

from .paths import list_runs, resolve_trace

mcp = _Server("tasktrace", instructions=(
    "Recorded demonstrations of tasks the user did on their Mac. Start with list_traces, then get_outline. "
    "Use get_trace for locators, container context and effects, and follow the guidance it carries: "
    "automation_synthesis_guidance (compile the demonstration into a generalized script) or, on older "
    "traces, replay_guidance (follow it step by step)."))


def _load(trace: str) -> tuple[dict, object]:
    path = resolve_trace(trace)
    return json.loads(path.read_text(encoding="utf-8")), path.parent


@mcp.tool()
def list_traces(limit: int = 20) -> list[dict]:
    """List recorded task demonstrations, newest first: run name, goal, step count, parameters."""
    return list_runs(limit)


@mcp.tool()
def get_outline(trace: str) -> str:
    """One line per step for a recorded task. The cheapest way to understand what was done."""
    t, _ = _load(trace)
    head = f"Task: {t['task'].get('name')}" + (f" - {t['task']['goal']}" if t["task"].get("goal") else "")
    params = ", ".join(f"{p['name']} ({p['kind']})" for p in t.get("parameters", []))
    return "\n".join([head, f"Parameters: {params or 'none'}", *t.get("outline", [])])


@mcp.tool()
def get_trace(trace: str, include_thumbnails: bool = False) -> dict:
    """The full compiled trace: steps with locators, effects, parameters, success criteria and replay guidance."""
    t, _ = _load(trace)
    if not include_thumbnails:
        for s in t.get("steps", []):
            (s.get("target") or {}).pop("thumbnail", None)
    return t


@mcp.tool()
def get_step_image(trace: str, step: int) -> Image:
    """The screenshot crop around a step's click point, to see what the target looked like."""
    t, run_dir = _load(trace)
    st = next((s for s in t.get("steps", []) if s["id"] == step), None)
    crop = ((st or {}).get("target") or {}).get("crop")
    if not crop:
        raise ValueError(f"Step {step} has no screenshot crop")
    return Image(path=str(run_dir / crop))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
