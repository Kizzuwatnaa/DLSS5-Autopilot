"""What the game page's one card says about the last session.

Pure: the page draws it, the watcher and "did it work?" both feed it, and a
test can read every state without a window. Nothing here words a verdict -
the headline is the diagnosis' own sentence, the findings are its own, the
offered route is the one the watcher kept with the answer. What is added is
the number the session cost, when the logs carried one.

States:
  running  the watcher saw the game start and has not seen it close
  ok       the diagnosis says Working
  failed   the logs show a route that did not work
  unsure   the logs cannot tell (verdicts.outcome is None): the person
           watched the game, so the card does not call it a failure
"""
from __future__ import annotations

from . import verdicts

DOT = "  ·  "


def stat_line(m) -> str:
    """What the session cost, from what was measured - or "".

    OptiScaler logs the model's own cost per frame; the feeder logs a frame
    rate and a running frame count. The cost lines count readings, not
    frames, so only the feeder's number is printed as frames. A work area
    the add-on's config did not confirm is the slider's, not a measurement."""
    if m is None:
        return ""
    bits = []
    ms = getattr(m, "model_ms", None)
    fps = getattr(m, "fps", None)
    if ms is not None:
        bits.append(f"{ms:.1f} ms of model a frame")
    elif fps:
        bits.append(f"{fps:.0f} fps")
    res = getattr(m, "resolution", 0)
    if bits and res and getattr(m, "from_config", True):
        bits.append(f"{int(res)}% work area")
    frames = getattr(m, "frames", 0)
    if bits and fps and frames:
        bits.append(f"{int(frames):,} frames")
    return DOT.join(bits)


def state_of(result: dict) -> str:
    """ok / failed / unsure for a diagnosis result."""
    if result.get("ok"):
        return "ok"
    if verdicts.outcome(str(result.get("title", ""))) is None:
        return "unsure"
    return "failed"


def build(result, entry, measured, running: str = "") -> dict | None:
    """The card, or None when there is nothing to say.

    `result` is the page's result dict (only kind "diagnosis" is a session),
    `entry` the verdict kept for this folder (it carries the offered route),
    `measured` an autotune.Measured or None, `running` the name of the game
    while the watcher has seen it start and not close."""
    if running:
        return {"state": "running", "headline": f"{running} is running", "stat": "",
                "findings": [], "next": "", "why": ""}
    if not isinstance(result, dict) or result.get("kind") != "diagnosis":
        return None
    state = state_of(result)
    nxt = why = ""
    if state == "failed" and isinstance(entry, dict):
        nxt = str(entry.get("next") or "")
        why = str(entry.get("why") or "") if nxt else ""
    return {"state": state, "headline": str(result.get("title", "")),
            "stat": stat_line(measured) if state == "ok" else "",
            "findings": list(result.get("findings") or []), "next": nxt, "why": why}
