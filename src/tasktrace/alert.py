"""A sound when the run needs the person.

A supervised run is only supervised if the person notices it has stopped. They are allowed to
look away: the point of the loop is that the machine does the dull part. So whenever the run
pauses for an answer, a sound plays.
"""

from __future__ import annotations

import subprocess
import sys

SOUNDS = {
    "ask": "/System/Library/Sounds/Ping.aiff",      # your turn: a question is waiting
    "problem": "/System/Library/Sounds/Basso.aiff",  # something went wrong
    "done": "/System/Library/Sounds/Glass.aiff",     # the run finished
}
_enabled = True


def set_enabled(on: bool) -> None:
    """Turn sounds off for this process (--quiet, or a run nobody is watching)."""
    global _enabled
    _enabled = bool(on)


def attention(kind: str = "ask") -> bool:
    """Play a short system sound. Never raises, never blocks the run."""
    if not _enabled or sys.platform != "darwin":
        return False
    path = SOUNDS.get(kind, SOUNDS["ask"])
    try:
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def say(text: str) -> bool:
    """Speak one short line. Used for the question itself when the person is across the room."""
    if not _enabled or sys.platform != "darwin" or not text:
        return False
    try:
        subprocess.Popen(["say", str(text)[:200]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
