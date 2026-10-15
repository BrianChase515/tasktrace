"""Compile a raw run folder (meta.json + events.jsonl) into one trace.json.

Platform-independent: everything macOS-specific happened at record time,
except optional OCR of the click crops, which is skipped elsewhere.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import SCHEMA_VERSION, __version__, images, ocr
from .redact import Redactor

EFFECT_WINDOW_MS = 3000  # how long after a step we attribute screen changes to it
TYPING_GAP_MS = 5000  # pause that splits one typing run into two
SCROLL_MERGE_MS = 1500
DOUBLE_CLICK_MS = 600
DRAG_PX = 6

SPECIAL_KEYS = {
    36: "return", 76: "enter", 48: "tab", 51: "delete", 117: "forward_delete", 53: "escape",
    123: "left", 124: "right", 125: "down", 126: "up", 115: "home", 119: "end",
    116: "page_up", 121: "page_down", 49: "space",
}
MOD_ORDER = ["ctrl", "opt", "shift", "cmd"]
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXSecureTextField"}
ROLE_WORDS = {
    "AXButton": "button", "AXTextField": "text field", "AXTextArea": "text area",
    "AXSecureTextField": "password field", "AXSearchField": "search field", "AXCheckBox": "checkbox",
    "AXRadioButton": "radio button", "AXPopUpButton": "dropdown", "AXComboBox": "combo box",
    "AXMenuItem": "menu item", "AXMenuBarItem": "menu", "AXMenuButton": "menu button", "AXLink": "link",
    "AXCell": "cell", "AXRow": "row", "AXTab": "tab", "AXTabGroup": "tab group", "AXStaticText": "text",
    "AXImage": "image", "AXSlider": "slider", "AXList": "list", "AXTable": "table", "AXOutline": "outline",
    "AXToolbar": "toolbar", "AXWebArea": "web page", "AXGroup": "area", "AXScrollArea": "scroll area",
    "AXWindow": "window", "AXDisclosureTriangle": "disclosure triangle", "AXIncrementor": "stepper",
}
VERBS = {"click": "Click", "double_click": "Double-click", "triple_click": "Triple-click",
         "right_click": "Right-click"}
TOGGLE_WORDS = {"1": "checked", "0": "unchecked", "true": "checked", "false": "unchecked"}


def _toggle_word(v) -> str:
    return TOGGLE_WORDS.get(str(v).strip().lower(), str(v))

SYNTHESIS_GUIDANCE = (
    "This is ground-truth telemetry from one human demonstration of a task on macOS, captured "
    "through the accessibility API. You are a TRACE-TO-CODE COMPILER, not a playback bot: your job "
    "is to write a standalone, generalized automation script for this task, not to repeat these "
    "exact clicks. Clicking the recorded positions on a live app will click the wrong rows, repeat "
    "the person's detours, and break the first time a form has a different number of pages. "
    "(1) Read `outline` first, then `steps`, and infer the SEMANTIC WORKFLOW: what the person was "
    "trying to achieve at each stage, not the literal input sequence. "
    "(2) PRUNE. Drop exploratory detours (opening something, finding nothing useful, backing out) "
    "and accidental repeats: clicks less than 1000 ms apart on the same element or container where "
    "the first produced no `effect`. A step with no `effect` that is immediately undone is noise. "
    "(3) GENERALIZE. Treat the strings in `target.element.title`, `target.path` and "
    "`container_context` as one instance, not as constants. Match on stable prefixes, roles and "
    "accessible names (e.g. a button whose name starts with \"Easy Apply\"), never on a row index, "
    "a pixel position or a full path copied verbatim. Use `parameters` for every value that varies; "
    "steps marked `secret` were typed into password fields and never recorded, so your script must "
    "prompt for them. "
    "(4) WRITE REAL CONTROL FLOW. `container_context.list_item_index` / `total_items` mean the "
    "person acted on one row of a list: emit a `for` loop over the rows with a predicate drawn from "
    "`item_text_summary`, not a click on that index. Repeated state-advancing clicks (the same "
    "\"Next\"/\"Continue\" button while `effect.progress` moves 0% -> 20% -> 40%) mean a `while` "
    "loop: at each turn read the visible form, fill the required fields, press the advance button, "
    "and stop at the terminal action (e.g. \"Submit application\") or when progress stops changing. "
    "`container_context.interactive_siblings` is the form schema of that page, including fields the "
    "person never had to touch, so your script can handle pages that ask for more than this run did; "
    "`form_same_as_step: N` means that form is unchanged since step N, so read it there. "
    "(5) VERIFY, DON'T ASSUME. After each action, check the corresponding `effect` "
    "(windows opened/closed/retitled, `dialog_opened`, `toggled`, `progress`) before continuing, and "
    "fail loudly rather than guessing. `settle_ms` is roughly how long the app took to respond. "
    "(6) `intent` is the person's own explanation of why a step happens; it outranks your inference. "
    "The task is done when `success_criteria` holds. Steps marked `optional` did not happen in every run. "
    "Locators, in order of robustness: target.element (role + title/description, scoped by "
    "target.path and the step's window), then target.ocr_label, then target.rel_xy "
    "(fraction of the window), and target.abs_xy only as a last resort."
)

REPLAY_GUIDANCE = (
    "This is a recorded demonstration of a task on macOS. To replicate it: "
    "(1) Read `outline` first, then `steps`. "
    "(2) Substitute `parameters` with the values for this run; ask if a value isn't given. "
    "Steps marked `secret` were typed into password fields and were never recorded; ask the person. "
    "(3) Find each target using the most robust locator available, in order: "
    "target.element (accessibility role + title/description, scoped by target.path and the step's window), "
    "then target.ocr_label (text visible at the click point), then target.rel_xy (fraction of the window's "
    "width/height), and target.abs_xy only if the screen layout matches environment.screens. "
    "(4) After each step, confirm its `effect` (windows opened/closed/retitled) before continuing; "
    "if the effect doesn't happen, stop and re-check rather than guessing. "
    "(5) `intent` is the person's own explanation of why a step happens; it outranks your inference. "
    "(6) The task is complete when `success_criteria` holds. Steps marked `optional` did not occur in every run."
)


# --------------------------------------------------------------------------- loading

def load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    run_dir = Path(run_dir)
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    events = []
    with open(run_dir / "events.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # a truncated last line after a crash shouldn't sink the run
    events.sort(key=lambda e: e.get("t", 0))  # stable: keeps write order for equal timestamps
    return meta, events


# --------------------------------------------------------------------------- small helpers

def _el(ctx: dict | None) -> dict:
    return (ctx or {}).get("element") or {}


def is_secure(el: dict) -> bool:
    return "AXSecureTextField" in (el.get("subrole"), el.get("role"))


def element_label(el: dict, ocr_label: str | None = None) -> str | None:
    for k in ("title", "description"):
        if el.get(k):
            return el[k]
    if el.get("value") and el.get("role") not in TEXT_ROLES:
        return el["value"]
    for k in ("placeholder", "help", "identifier"):
        if el.get(k):
            return el[k]
    return ocr_label


def _element_key(ctx: dict | None) -> tuple:
    el, win = _el(ctx), (ctx or {}).get("window") or {}
    return (win.get("id"), el.get("role"), el.get("title"), el.get("description"),
            el.get("identifier"), tuple(el.get("frame") or ()))


def _combo(mods: list[str], key: str) -> str:
    return "+".join([m for m in MOD_ORDER if m in mods] + [key])


def _key_name(ev: dict) -> str:
    kc = ev.get("keycode")
    if kc in SPECIAL_KEYS:
        return SPECIAL_KEYS[kc]
    base = (ev.get("base") or ev.get("chars") or "").lower()
    return base if base and base.isprintable() else f"key{kc}"


def _printable(ev: dict) -> bool:
    ch = ev.get("chars") or ""
    return bool(ch) and ch.isprintable()


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s[:40] or "value"


def classify(text: str) -> str:
    t = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}\.\d{1,2}\.\d{2,4}", t):
        return "date"
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", t):
        return "email"
    if re.fullmatch(r"https?://\S+", t):
        return "url"
    if re.fullmatch(r"[$€£¥]\s?-?[\d,]+(\.\d+)?|-?[\d,]+(\.\d+)?\s?(USD|EUR|GBP)", t):
        return "currency"
    if re.fullmatch(r"-?[\d,]*\.?\d+%?", t):
        return "number"
    return "text"


def prune(obj):
    """Drop None/empty values so the trace stays small."""
    if isinstance(obj, dict):
        out = {k: prune(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [prune(v) for v in obj if v not in (None, "", [], {})]
    return obj


# --------------------------------------------------------------------------- step building

class _Builder:
    def __init__(self):
        self.steps: list[dict] = []
        self.typing: dict | None = None
        self.pending: dict[str, dict] = {}

    def flush(self):
        if self.typing:
            self.steps.append(self.typing)
            self.typing = None

    def add(self, step: dict):
        self.flush()
        self.steps.append(step)

    def key(self, keys: str, ev: dict):
        self.flush()
        prev = self.steps[-1] if self.steps else None
        if prev and prev["action"] == "key" and prev["keys"] == keys and ev["t"] - prev["t_end"] < 2000:
            prev["times"] = prev.get("times", 1) + 1
            prev["t_end"] = ev["t"]
            return
        self.steps.append({"action": "key", "keys": keys, "t_start": ev["t"], "t_end": ev["t"],
                           "ctx": ev.get("ctx")})


def build_steps(events: list[dict]) -> list[dict]:
    b = _Builder()
    for ev in events:
        typ, t = ev.get("type"), ev.get("t", 0)

        if typ == "mouse_down":
            b.flush()
            b.pending[ev.get("button", "left")] = ev

        elif typ == "mouse_up":
            down = b.pending.pop(ev.get("button", "left"), None)
            if down is None:
                continue
            base = {"t_start": down["t"], "t_end": t, "ctx": down.get("ctx"), "x": down["x"], "y": down["y"],
                    "crop": down.get("crop"), "crop_rect": down.get("crop_rect"),
                    "ref": down.get("ref"), "mods": down.get("mods") or []}
            if ((ev["x"] - down["x"]) ** 2 + (ev["y"] - down["y"]) ** 2) ** 0.5 > DRAG_PX:
                b.add({**base, "action": "drag", "to": [ev["x"], ev["y"]]})
                continue
            right = down.get("button") == "right" or (down.get("button") == "left" and "ctrl" in base["mods"])
            clicks = down.get("clicks", 1) or 1
            prev = b.steps[-1] if (b.steps and b.typing is None) else None
            if (clicks >= 2 and not right and prev and prev["action"] in ("click", "double_click")
                    and abs(prev["x"] - down["x"]) <= DRAG_PX and abs(prev["y"] - down["y"]) <= DRAG_PX
                    and down["t"] - prev["t_end"] <= DOUBLE_CLICK_MS):
                prev["action"] = "double_click" if clicks == 2 else "triple_click"
                prev["t_end"] = t
                continue
            b.add({**base, "action": "right_click" if right else "click"})

        elif typ == "key":
            mods, kc = ev.get("mods") or [], ev.get("keycode")
            if {"cmd", "ctrl"} & set(mods):
                b.add({"action": "hotkey", "keys": _combo(mods, _key_name(ev)), "t_start": t, "t_end": t,
                       "ctx": ev.get("ctx")})
            elif kc == 51:  # delete: edit the text being typed, if any
                if b.typing and b.typing["text"]:
                    b.typing["text"] = b.typing["text"][:-1]
                    b.typing["t_end"] = t
                else:
                    b.key("delete", ev)
            elif kc in SPECIAL_KEYS and kc != 49:
                b.key(_combo([m for m in mods if m in ("shift", "opt")], SPECIAL_KEYS[kc]), ev)
            elif _printable(ev):
                ctx = ev.get("ctx")
                k = _element_key(ctx)
                secure = bool(ev.get("secure")) or is_secure(_el(ctx))
                if not (b.typing and b.typing["_key"] == k and t - b.typing["t_end"] <= TYPING_GAP_MS):
                    b.flush()
                    b.typing = {"action": "type", "text": "", "t_start": t, "t_end": t, "ctx": ctx,
                                "secure": secure, "_key": k}
                b.typing["text"] += ev["chars"]
                b.typing["t_end"] = t
                b.typing["secure"] = b.typing["secure"] or secure

        elif typ == "scroll":
            b.flush()
            prev = b.steps[-1] if b.steps else None
            wid = ((ev.get("ctx") or {}).get("window") or {}).get("id")
            if (prev and prev["action"] == "scroll" and t - prev["t_end"] <= SCROLL_MERGE_MS
                    and ((prev.get("ctx") or {}).get("window") or {}).get("id") == wid):
                prev["dx"] += ev.get("dx", 0)
                prev["dy"] += ev.get("dy", 0)
                prev["t_end"] = t
            else:
                b.add({"action": "scroll", "dx": ev.get("dx", 0), "dy": ev.get("dy", 0), "x": ev.get("x"),
                       "y": ev.get("y"), "t_start": t, "t_end": t, "ctx": ev.get("ctx")})
    b.flush()

    # Stopping with ctrl+C in the terminal leaves a stray last step.
    while b.steps and b.steps[-1]["action"] == "hotkey" and b.steps[-1]["keys"] in ("ctrl+c", "ctrl+opt+q"):
        b.steps.pop()
    return b.steps


# --------------------------------------------------------------------------- effects

def attach_containers(steps: list[dict], events: list[dict]) -> None:
    """Match each click to the description of the list row or modal it happened inside.

    The recorder walks the container tree on a worker thread and emits it separately, keyed to
    the click by `ref`, so a slow accessibility tree can never stall the event tap.
    """
    by_ref = {e["ref"]: e["context"] for e in events
              if e.get("type") == "container" and e.get("ref") is not None and e.get("context")}
    for s in steps:
        ctx = by_ref.get(s.get("ref"))
        if ctx:
            s["container"] = ctx


def dedupe_forms(steps: list[dict]) -> None:
    """Say "same form as step N" rather than repeating a form that hasn't changed.

    Working through a wizard means a dozen clicks inside one form; spelling out every field each
    time is most of the file's size and tells the reader nothing new.
    """
    seen: dict[str, int] = {}
    for s in steps:
        cc = s.get("container_context") or {}
        for blk in (cc, cc.get("modal") or {}):
            sib = blk.get("interactive_siblings")
            if not sib:
                continue
            key = json.dumps(sib, sort_keys=True, ensure_ascii=False)
            if key in seen:
                blk.pop("interactive_siblings")
                blk["form_same_as_step"] = seen[key]
            else:
                seen[key] = s["id"]


def attach_effects(steps: list[dict], events: list[dict]) -> None:
    changes = [e for e in events if e.get("type") in ("window", "app", "ui")]
    keyframes = {e.get("window_id"): e.get("screenshot") for e in events if e.get("type") == "keyframe"}
    for i, s in enumerate(steps):
        nxt = steps[i + 1]["t_start"] if i + 1 < len(steps) else float("inf")
        lo, hi = s["t_start"], min(s["t_end"] + EFFECT_WINDOW_MS, nxt)
        win = ((s.get("ctx") or {}).get("window") or {})
        app = ((s.get("ctx") or {}).get("app") or {})
        eff: dict = {"opened": [], "closed": [], "retitled": []}
        last_t = None
        for e in changes:
            if not lo < e.get("t", 0) <= hi:
                continue
            title = e.get("title") or f"(untitled {e.get('app') or 'window'})"
            if e["type"] == "window":
                ch = e.get("change")
                if ch == "opened":
                    eff["opened"].append(title)
                    if keyframes.get(e.get("window_id")):
                        eff["screenshot"] = keyframes[e.get("window_id")]
                elif ch == "closed":
                    eff["closed"].append(title)
                elif ch == "retitled":
                    eff["retitled"].append({"from": e.get("from"), "to": e.get("title")})
                elif ch == "focused":
                    if e.get("title") != win.get("title"):
                        eff["focused"] = title
                    else:
                        continue
            elif e["type"] == "ui":
                # Inside a single-page app nothing opens a window; this is what changed on the page.
                ch = e.get("change")
                if ch == "dialog_opened" and e.get("name"):
                    eff.setdefault("dialog_opened", []).append(e["name"])
                elif ch == "dialog_closed" and e.get("name"):
                    eff.setdefault("dialog_closed", []).append(e["name"])
                elif ch == "toggled":
                    eff.setdefault("toggled", []).append({"element": e.get("element"),
                                                          "from": e.get("from"), "to": e.get("to")})
                elif ch == "progress":
                    eff["progress"] = {"from": e.get("from"), "to": e.get("to")}
                else:
                    continue
            elif e["type"] == "app":
                if e.get("app") and e.get("app") != app.get("name"):
                    eff["app"] = e.get("app")
                else:
                    continue
            last_t = e["t"]
        if eff.get("focused") in eff["opened"]:
            eff.pop("focused")
        if last_t is not None:
            eff["settle_ms"] = max(0, last_t - s["t_end"])
            s["effect"] = eff


def attach_notes(steps: list[dict], events: list[dict]) -> list[str]:
    """A note describes what you're about to do, so it attaches to the next step."""
    loose = []
    for n in (e for e in events if e.get("type") == "note" and e.get("text")):
        target = next((s for s in steps if s["t_start"] >= n["t"]), steps[-1] if steps else None)
        if target is None:
            loose.append(n["text"])
        else:
            target.setdefault("intent", []).append(n["text"])
    return loose


# --------------------------------------------------------------------------- output

def describe(step: dict) -> str:
    a, tgt = step["action"], step.get("target") or {}
    el = tgt.get("element") or {}
    label = element_label(el, tgt.get("ocr_label"))
    role = ROLE_WORDS.get(el.get("subrole") or "") or ROLE_WORDS.get(el.get("role") or "", "element")
    if label:
        thing = f'{role} "{label}"'
    elif tgt.get("abs_xy"):
        thing = f"{role} at ({tgt['abs_xy'][0]}, {tgt['abs_xy'][1]})"
    else:
        thing = role

    if a == "open_app":
        return f"Open {step.get('app')}"
    if a in VERBS:
        text = f"{VERBS[a]} {thing}"
    elif a == "drag":
        text = f"Drag {thing} to ({tgt['to_xy'][0]}, {tgt['to_xy'][1]})"
    elif a == "type":
        if step.get("secret"):
            text = f"Type [secret] into {thing}"
        elif step.get("param"):
            if thing == "element" and tgt.get("field_hint"):
                thing = f'the field for "{tgt["field_hint"]}"'
            text = f'Type {{{step["param"]}}} (e.g. "{step.get("text", "")}") into {thing}'
        else:
            text = f'Type "{step.get("text", "")}" into {thing}'
    elif a in ("key", "hotkey"):
        text = f"Press {step['keys']}" + (f" x{step['times']}" if step.get("times", 1) > 1 else "")
    elif a == "scroll":
        dy = (step.get("scroll") or {}).get("dy", 0)
        text = f"Scroll {'up' if dy > 0 else 'down'} {abs(dy)}"
    else:
        text = a

    where = " > ".join(x for x in (step.get("app"), step.get("window")) if x)
    if where and a not in ("key",):
        text += f" in {where}"
    eff = step.get("effect") or {}
    if eff.get("opened"):
        text += " -> opens " + ", ".join(f'"{w}"' for w in eff["opened"])
    if eff.get("closed"):
        text += " -> closes " + ", ".join(f'"{w}"' for w in eff["closed"])
    for r in eff.get("retitled") or []:
        text += f' -> window becomes "{r.get("to")}"'
    for d in eff.get("dialog_opened") or []:
        text += f' -> shows "{d}"'
    for d in eff.get("dialog_closed") or []:
        text += f' -> dismisses "{d}"'
    for tg in eff.get("toggled") or []:
        text += f' -> "{tg.get("element")}" becomes {_toggle_word(tg.get("to"))}'
    if eff.get("progress"):
        p = eff["progress"]
        text += f" -> progress {p.get('from') or '?'} -> {p.get('to')}"
    if eff.get("app"):
        text += f" -> switches to {eff['app']}"
    if (step.get("container_context") or {}).get("list_item_index") is not None:
        cc = step["container_context"]
        n = f" of {cc['total_items']}" if cc.get("total_items") else ""
        text += f" [list item {cc['list_item_index'] + 1}{n}]"
    return text


def _out_step(i: int, s: dict, run_dir: Path, use_ocr: bool, embed: bool) -> dict:
    ctx = s.get("ctx") or {}
    el, app, win = _el(ctx), ctx.get("app") or {}, ctx.get("window") or {}
    out: dict = {"id": i, "t_ms": int(s["t_start"]), "action": s["action"],
                 "app": app.get("name"), "window": win.get("title")}
    tgt: dict = {}
    if el:
        keep = ("role", "subrole", "title", "description", "placeholder", "identifier", "help")
        tgt["element"] = {k: el.get(k) for k in keep}
        if not is_secure(el):
            tgt["element"]["value"] = el.get("value")
        tgt["path"] = el.get("path")
    if s.get("x") is not None and s["action"] != "type":
        x, y = s["x"], s["y"]
        bx, by, bw, bh = (win.get("bounds") or [0, 0, 0, 0])
        if bw > 0 and bh > 0:
            tgt["rel_xy"] = [round((x - bx) / bw, 3), round((y - by) / bh, 3)]
        fr = el.get("frame")
        if fr:
            tgt["in_element_xy"] = [x - fr[0], y - fr[1]]
        tgt["abs_xy"] = [x, y]
        if s.get("to"):
            tgt["to_xy"] = s["to"]
    crop = s.get("crop")
    if crop and (run_dir / crop).exists():
        tgt["crop"] = crop
        rect = s.get("crop_rect")
        if rect:
            tgt["crop_rect"] = rect
        if use_ocr:
            # The crop is cut to the element, not centred on the pointer, so say where the
            # pointer was: OCR should return the label it landed on, whole.
            point = images.point_in_crop(s.get("x") or 0, s.get("y") or 0, rect)
            tgt["ocr_label"] = ocr.label_near_point(run_dir / crop, point)
        if embed:
            tgt["thumbnail"] = images.thumbnail_b64(run_dir / crop)
    out["target"] = tgt
    if s.get("container"):
        out["container_context"] = s["container"]

    if s["action"] == "type":
        if s.get("secure"):
            out["secret"] = True
        else:
            out["text"] = s["text"]
    elif s["action"] in ("key", "hotkey"):
        out["keys"] = s["keys"]
        if s.get("times", 1) > 1:
            out["times"] = s["times"]
    elif s["action"] == "scroll":
        out["scroll"] = {"dx": s.get("dx", 0), "dy": s.get("dy", 0)}
    if s.get("intent"):
        out["intent"] = " / ".join(s["intent"])
    if s.get("effect"):
        out["effect"] = s["effect"]
    if s["action"] == "click" and el.get("role") == "AXDockItem" and el.get("title"):
        # Clicking an app in the Dock means "open this app"; positions in the Dock are meaningless.
        out.update(action="open_app", app=el["title"], window=None,
                   target={"element": {"role": "AXDockItem", "title": el["title"]}})
        out.pop("container_context", None)  # the Dock's own list says nothing about the task
    return out


QUESTION_GAP_MS = 8000


def _assign_parameters(steps: list[dict]) -> list[dict]:
    """Name each typed value after its field, or after the question clicked just before typing."""
    params, used = [], set()
    last_click: dict | None = None
    for st in steps:
        if st["action"] in VERBS:
            last_click = st
            continue
        if st["action"] != "type":
            continue
        tgt = st.get("target") or {}
        label = element_label(tgt.get("element") or {}, tgt.get("ocr_label"))
        if not label and last_click and last_click.get("window") == st.get("window") \
                and st["t_ms"] - last_click["t_ms"] <= QUESTION_GAP_MS:
            ltgt = last_click.get("target") or {}
            label = element_label(ltgt.get("element") or {}, ltgt.get("ocr_label"))
            if label:
                st.setdefault("target", {})["field_hint"] = label
        label = label or f"text_{st['id']}"
        name = base = _slug(label)
        n = 2
        while name in used:
            name, n = f"{base}_{n}", n + 1
        used.add(name)
        st["param"] = name
        secret = bool(st.get("secret"))
        params.append({"name": name, "kind": "secret" if secret else classify(st.get("text", "")),
                       "example": None if secret else st.get("text"), "step": st["id"], "status": "guess"})
    return params


def refresh_text(trace: dict) -> None:
    head = ("id", "t_ms", "action", "describe")
    for i, st in enumerate(trace["steps"]):
        st["describe"] = describe(st)
        trace["steps"][i] = {**{k: st[k] for k in head if k in st},
                             **{k: v for k, v in st.items() if k not in head}}
    trace["outline"] = [f"{s['id']}. {s['describe']}" + ("  [optional]" if s.get("optional") else "")
                        for s in trace["steps"]]


GUIDANCE_MODES = ("synthesize", "replay", "both")


def _guidance(mode: str) -> dict:
    """Which instructions the consuming AI gets. Synthesis is the default: write code, don't replay."""
    out = {}
    if mode in ("synthesize", "both"):
        out["automation_synthesis_guidance"] = SYNTHESIS_GUIDANCE
    if mode in ("replay", "both"):
        out["replay_guidance"] = REPLAY_GUIDANCE
    return out or {"automation_synthesis_guidance": SYNTHESIS_GUIDANCE}


def compile_run(run_dir: Path, *, use_ocr: bool = True, embed_crops: bool = False,
                redact_emails: bool = False, guidance: str = "synthesize") -> dict:
    run_dir = Path(run_dir)
    meta, events = load_run(run_dir)
    raw = build_steps(events)
    attach_containers(raw, events)
    attach_effects(raw, events)
    loose_notes = attach_notes(raw, events)
    steps = [_out_step(i, s, run_dir, use_ocr, embed_crops) for i, s in enumerate(raw, 1)]
    dedupe_forms(steps)
    params = _assign_parameters(steps)

    # Success criteria: where things ended up, plus the "done" snapshot if one was taken.
    focus = [e for e in events if e.get("type") == "window" and e.get("change") == "focused"]
    last = steps[-1] if steps else {}
    sc: dict = {
        "final_app": focus[-1].get("app") if focus else last.get("app"),
        "final_window": focus[-1].get("title") if focus else last.get("window"),
        "last_effect": last.get("effect"),
    }
    done = [e for e in events if e.get("type") == "done_marker"]
    if done:
        shot = done[-1].get("screenshot")
        sc["done_marker"] = {"t_ms": done[-1]["t"], "screenshot": shot}
        if use_ocr and shot and (run_dir / shot).exists():
            sc["visible_text"] = ocr.read_lines(run_dir / shot)

    apps, seen = [], set()
    for s in raw:
        a = (s.get("ctx") or {}).get("app") or {}
        if a.get("name") and a["name"] not in seen:
            seen.add(a["name"])
            apps.append({"name": a["name"], "bundle_id": a.get("bundle_id")})

    gaps = [{"t_ms": e["t"], "reason": e.get("reason"), "app": e.get("app")}
            for e in events if e.get("type") == "paused"]
    host = meta.get("host") or {}
    trace = {
        "schema": SCHEMA_VERSION,
        "generator": f"tasktrace {__version__}",
        "task": {
            "name": meta.get("name") or run_dir.name,
            "goal": meta.get("goal"),
            "recorded_at": meta.get("started_at"),
            "duration_s": round(max((e.get("t", 0) for e in events), default=0) / 1000, 1),
            "notes": loose_notes,
            "gaps": gaps,
        },
        "environment": {"os": host.get("os"), "machine": host.get("machine"),
                        "screens": host.get("screens"), "apps_used": apps},
        **_guidance(guidance),
        "parameters": params,
        "outline": [],
        "steps": steps,
        "success_criteria": sc,
    }
    refresh_text(trace)
    red = Redactor(emails=redact_emails)
    trace = red.walk(trace)
    trace["redactions"] = {"total": red.total, "by_kind": red.counts}
    trace = prune(trace)
    for key in ("parameters", "outline", "steps", "success_criteria"):
        trace.setdefault(key, [] if key != "success_criteria" else {})
    return trace


def write_trace(trace: dict, path: Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(trace, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
