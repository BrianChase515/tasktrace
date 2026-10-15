"""Container context and UI-state diffing, over plain node dicts.

The recorder snapshots a bounded piece of the macOS accessibility tree into `node`
dicts::

    {"role", "subrole", "role_description", "title", "description",
     "value", "placeholder", "identifier", "children": [...]}

Everything in this module works on those dicts alone, so the logic that decides
what a downstream model gets to see is platform-independent and testable off macOS.

Two jobs:

* `list_context` / `modal_context` turn the container around a click into the
  `container_context` object on a step — what the clicked thing sits *in*, not
  just the leaf that was clicked.
* `ui_state` / `diff_ui_state` summarise the live state of a window (dialogs,
  toggles, progress) so changes inside a single page can be attributed to the
  step that caused them.
"""

from __future__ import annotations

import re

LIST_ROLES = {"AXList", "AXTable", "AXOutline", "AXGrid"}
DIALOG_ROLES = {"AXSheet", "AXDrawer", "AXAlert"}
DIALOG_SUBROLES = {"AXDialog", "AXSystemDialog", "AXApplicationDialog", "AXAlert", "AXFloatingWindow"}
INTERACTIVE_ROLES = {
    "AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXPopUpButton", "AXRadioButton",
    "AXCheckBox", "AXButton", "AXLink", "AXMenuButton", "AXSlider", "AXSwitch", "AXToggle",
    "AXDisclosureTriangle", "AXIncrementor", "AXTab",
}
TOGGLE_ROLES = {"AXCheckBox", "AXRadioButton", "AXSwitch", "AXToggle", "AXDisclosureTriangle"}
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXSecureTextField"}
# Controls whose current value is part of the form's state, so a model can see what is already set.
VALUE_ROLES = TEXT_ROLES | TOGGLE_ROLES | {"AXPopUpButton", "AXMenuButton", "AXSlider", "AXIncrementor"}

MAX_SUMMARY_CHARS = 400
MAX_SIBLINGS = 14
PERCENT_RE = re.compile(r"\b\d{1,3}\s?(?:%|percent)\b", re.I)
STEP_RE = re.compile(r"\bstep\s+\d+\s+of\s+\d+\b", re.I)


# --------------------------------------------------------------------------- node helpers

def _s(node: dict, key: str) -> str:
    v = node.get(key)
    return str(v).strip() if isinstance(v, (str, int, float)) else ""


def is_secure(node: dict) -> bool:
    return "AXSecureTextField" in (node.get("subrole"), node.get("role"))


def node_label(node: dict) -> str:
    """The best human-readable name for a node, ignoring the text a person typed into it."""
    for key in ("title", "description", "placeholder", "help"):
        if _s(node, key):
            return _s(node, key)
    if node.get("role") not in TEXT_ROLES and _s(node, "value"):
        return _s(node, "value")
    return _s(node, "identifier")


def walk(node: dict | None, max_nodes: int = 400, depth: int = 12):
    """Breadth-first over a snapshot, bounded in both nodes and depth."""
    if not node:
        return
    queue, seen = [(node, 0)], 0
    while queue and seen < max_nodes:
        cur, d = queue.pop(0)
        yield cur
        seen += 1
        if d < depth:
            queue.extend((c, d + 1) for c in (cur.get("children") or []) if isinstance(c, dict))


def is_dialog(node: dict) -> bool:
    """Does this node look like a modal, dialog, sheet or wizard step container?"""
    if node.get("role") in DIALOG_ROLES or node.get("subrole") in DIALOG_SUBROLES:
        return True
    rd = _s(node, "role_description").lower()
    return "dialog" in rd or "modal" in rd or "alert" in rd


# --------------------------------------------------------------------------- summaries

def text_summary(node: dict, limit: int = MAX_SUMMARY_CHARS, max_nodes: int = 120) -> str:
    """All the text inside a node, in reading order: the whole card, not the 50px under the cursor."""
    parts, seen = [], set()
    for n in walk(node, max_nodes=max_nodes):
        bits = []
        if n.get("role") in ("AXStaticText", "AXHeading") and not is_secure(n):
            bits.append(_s(n, "value"))
        bits += [_s(n, "title"), _s(n, "description")]
        for b in bits:
            key = b.lower()
            if b and key not in seen and len(b) <= 200:
                seen.add(key)
                parts.append(b)
    out = " · ".join(parts)
    return out[:limit].rstrip(" ·") if len(out) > limit else out


def interactive_siblings(node: dict, limit: int = MAX_SIBLINGS, max_nodes: int = 300) -> list[dict]:
    """The form schema of a container: every interactive control in it, with its current value.

    Includes controls the person never touched, so a model can see the whole form.
    """
    out, seen = [], set()
    for n in walk(node, max_nodes=max_nodes):
        role = n.get("role")
        if role not in INTERACTIVE_ROLES and not is_secure(n):
            continue
        label = node_label(n)
        key = (role, label.lower())
        if key in seen:
            continue
        seen.add(key)
        item = {"role": "AXSecureTextField" if is_secure(n) else role}
        if label:
            item["title"] = label
        if not is_secure(n) and role in VALUE_ROLES and _s(n, "value"):
            item["value"] = _s(n, "value")[:80]
        if _s(n, "placeholder") and _s(n, "placeholder") != label:
            item["placeholder"] = _s(n, "placeholder")
        out.append(item)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- container context

def list_context(list_node: dict, item_index: int | None, item_node: dict | None) -> dict:
    """What a click inside an AXList/AXTable landed on: which row, of how many, and its full text."""
    children = [c for c in (list_node.get("children") or []) if isinstance(c, dict)]
    ctx: dict = {"kind": "list", "container": {"role": list_node.get("role")}}
    label = node_label(list_node)
    if label:
        ctx["container"]["title"] = label
    if children:
        ctx["total_items"] = len(children)
    if item_index is not None and item_index >= 0:
        ctx["list_item_index"] = item_index
    if item_node is not None:
        summary = text_summary(item_node)
        if summary:
            ctx["item_text_summary"] = summary
        item_controls = interactive_siblings(item_node, limit=6)
        if item_controls:
            ctx["item_controls"] = item_controls
    return ctx


def modal_context(node: dict | None, min_controls: int = 1) -> dict | None:
    """The form schema of the modal or form group a click happened inside.

    `min_controls` filters out containers that aren't really forms: a titled group holding one
    stray button says nothing a model can't already see on the step itself.
    """
    if not node:
        return None
    siblings = interactive_siblings(node)
    if len(siblings) < max(1, min_controls):
        return None
    ctx: dict = {"kind": "modal", "container": {"role": node.get("role")}}
    for key in ("subrole", "role_description"):
        if _s(node, key):
            ctx["container"][key] = _s(node, key)
    label = node_label(node)
    if label:
        ctx["container"]["title"] = label
    ctx["interactive_siblings"] = siblings
    prog = progress_strings(node)
    if prog:
        ctx["progress"] = prog[0]
    return ctx


def merge_context(list_ctx: dict | None, modal_ctx: dict | None) -> dict | None:
    """A click can be inside a list that is itself inside a modal; keep both."""
    if list_ctx and modal_ctx:
        merged = dict(list_ctx)
        merged["kind"] = "list_in_modal"
        merged["modal"] = {k: v for k, v in modal_ctx.items() if k != "kind"}
        return merged
    return list_ctx or modal_ctx


# --------------------------------------------------------------------------- live UI state

def progress_strings(node: dict, max_nodes: int = 300) -> list[str]:
    """Progress as a string: an AXProgressIndicator's value, or text like "40 percent"/"step 2 of 5"."""
    out = []
    for n in walk(node, max_nodes=max_nodes):
        if n.get("role") == "AXProgressIndicator":
            v = _s(n, "value") or _s(n, "description") or _s(n, "title")
            if v:
                out.append(v[:60])
            continue
        for key in ("value", "description", "title"):
            v = _s(n, key)
            if v and len(v) <= 60 and (PERCENT_RE.search(v) or STEP_RE.search(v)):
                out.append(v)
                break
    seen, uniq = set(), []
    for v in out:
        if v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    return uniq


def ui_state(node: dict | None, max_nodes: int = 400) -> dict:
    """A compact snapshot of what is on screen inside one window.

    Window opens and closes are already caught by the window poller; this covers the
    single-page case, where a modal appears, a checkbox flips or a wizard advances
    without any window ever changing.
    """
    dialogs: list[str] = []
    toggles: dict[str, str] = {}
    if node is None:
        return {"dialogs": [], "toggles": {}, "progress": []}
    for n in walk(node, max_nodes=max_nodes):
        if is_dialog(n):
            name = node_label(n) or n.get("role") or "dialog"
            if name not in dialogs:
                dialogs.append(name[:80])
        if n.get("role") in TOGGLE_ROLES:
            label = node_label(n) or n.get("role") or "toggle"
            val = _s(n, "value")
            if val:
                key, i = label[:80], 2
                while key in toggles and toggles[key] != val:
                    key, i = f"{label[:76]} #{i}", i + 1
                toggles[key] = val
    return {"dialogs": dialogs, "toggles": toggles, "progress": progress_strings(node, max_nodes)}


def diff_ui_state(prev: dict | None, cur: dict) -> list[dict]:
    """Changes between two `ui_state` snapshots, as events the compiler can attribute to a step."""
    if not prev:
        return []
    out = []
    for name in cur.get("dialogs", []):
        if name not in prev.get("dialogs", []):
            out.append({"change": "dialog_opened", "name": name})
    for name in prev.get("dialogs", []):
        if name not in cur.get("dialogs", []):
            out.append({"change": "dialog_closed", "name": name})
    pt, ct = prev.get("toggles", {}), cur.get("toggles", {})
    for label, val in ct.items():
        if label in pt and pt[label] != val:
            out.append({"change": "toggled", "element": label, "from": pt[label], "to": val})
    pp, cp = prev.get("progress", []), cur.get("progress", [])
    if cp and cp != pp:
        out.append({"change": "progress", "from": pp[0] if pp else None, "to": cp[0]})
    return out
