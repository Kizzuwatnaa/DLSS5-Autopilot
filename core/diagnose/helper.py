r"""The 64-bit helper a 32-bit game's feed hands its frames to.

On a 32-bit game DLSS does not run in the game at all: dlss5-feed.addon32
ships every frame over a pipe to host64\dlss5-feed-host64.exe, and the
neural add-on and NVIDIA's runtime run in THAT process. So a picture that
freezes while the game's own feed log looks healthy is the helper's story,
and only the helper's log tells it.

#252 (World of Warcraft 3.3.5a, feeder 1.16.0-beta.2): the feed shipped
frames at 135 fps, the helper's Present failed with 0x887A0005 three
seconds after DLSS was created, and its neural add-on never created the
feature. The diagnosis said "Inconclusive", and the report did not even
carry the helper's log. Every phrase read here is in the 1.16.0-beta.2
helper binary; "frame N evaluated" and a clean exit are in the owner's own
working Bayonetta session, which has none of the fault lines.

Part of core/diagnose; see __init__.py.
"""
from __future__ import annotations
import re

from .model import *  # noqa: F401,F403


__all__ = ["_helper_session", "_helper_excerpt", "_helper_verdict", "_fed_the_helper"]

_HOST_BANNER = re.compile(r"^[\d:.]+\s+dlss5-feed-host\S*\s", re.M)
# DXGI_ERROR_DEVICE_REMOVED. Any other Present code is left alone: the
# helper says a failed present of its own window does not touch the feed.
_REMOVED = re.compile(r"\[host\] Present failed (0x887A0005)", re.I)
_OUTCOME = re.compile(r"\[host\] neural consumer outcome: ([^\n]+)")
_KEEP = re.compile(r"DLSS 5 add-on|NGX_D3D12_Init|NeuralUplift|Present failed|"
                   r"in a row|consumer outcome|feature ready|evaluated|"
                   r"CRASH|stack|\] exit|lost|pipe closed")


def _helper_session(text: str) -> str:
    """The helper's last run, cut at its own banner when the log has several."""
    last = None
    for last in _HOST_BANNER.finditer(text or ""):
        pass
    return text[last.start():] if last is not None else (text or "")


def _helper_excerpt(text: str, n: int = 14) -> list[str]:
    """The helper's lines a report carries: the ones a rule here reads."""
    lines = [ln.rstrip()[:200] for ln in _helper_session(text).splitlines()
             if _KEEP.search(ln)]
    return lines[-n:]


def _consumer(htext: str) -> str:
    f = re.search(r"\[host\] DLSS 5 add-on file: (\S+)", htext)
    v = re.search(r"\[host\] DLSS 5 add-on: (v[\d.]+)", htext)
    return " ".join(x.group(1) for x in (f, v) if x) or "the neural add-on"


def _helper_verdict(rep: Report, htext: str) -> bool:
    """A fault the helper itself logged, named - True when it set the verdict."""
    h = _helper_session(htext)
    if not h:
        return False
    removed = _REMOVED.search(h)
    outcome = None
    for outcome in _OUTCOME.finditer(h):
        pass
    said = outcome.group(1) if outcome else ""
    uplift = re.findall(r"NeuralUplift=(\d)", h)
    off = bool(uplift) and uplift[-1] == "0"
    who = _consumer(h)
    # "(ReShade.log is unavailable)" is the helper saying it could not look,
    # not that nothing was there.
    no_nr = not off and "unavailable" not in said \
        and ("did not intercept" in said or "feature 18 failed" in said)
    if not removed and not no_nr:
        return False
    if removed:
        rep.add(BAD, f"The 64-bit helper lost its graphics device (Present "
                     f"failed {removed.group(1)}).",
                "0x887A0005 is Windows' 'device removed': the Direct3D 12 "
                "device the helper runs DLSS on is gone, and the frames it "
                "hands back to the game stop changing - a frozen picture. "
                "It happened in host64\\dlss5-feed-host64.exe, where " + who +
                " and NVIDIA's runtime run, not in the game or its ReShade. "
                "If the game plays again with neural rendering off, the "
                "neural pass is what takes the device down: install again "
                "with another build in 'dlss5 add-on', and send "
                "host64\\dlss5-feed-host.log to the DLSS5-Feeder project.")
    if no_nr:
        rep.add(BAD, "The neural add-on in the helper never created the "
                     "DLSS 5 feature.",
                f"The helper's own check says: {said.strip()[:120]}. DLSS "
                f"itself was set up, so what reaches the game is plain DLAA "
                f"with no neural pass. {who} is the add-on that did not "
                f"answer; host64\\ReShade.log says what it did instead.")
    rep.verdict = ("The 64-bit helper lost its graphics device, so the "
                   "picture stops changing - try another 'dlss5 add-on' build."
                   if removed else
                   "DLSS runs in the 64-bit helper, but its neural add-on "
                   "never created the DLSS 5 feature - try another 'dlss5 "
                   "add-on' build.")
    return True


def _fed_the_helper(rep: Report, text: str) -> bool:
    """The feed shipped frames to the helper, and its log can see no further."""
    perf = None
    for perf in re.finditer(r"\[feed32\] (\d+) frames: feed CPU [\d.]+ ms/frame"
                            r"[^\n]*?\(([\d.]+) fps\)[^\n]*pipe write", text or ""):
        pass
    if perf is None:
        return False
    probe = bool(re.search(r"present probe: 0 presents / \d+ frames fed", text)) \
        and "the game kept presenting" in text
    rep.add(WARN, f"The game is handing its frames to the 64-bit helper "
                  f"({perf.group(1)} frames at {perf.group(2)} fps).",
            "On a 32-bit game DLSS runs in host64\\dlss5-feed-host64.exe. "
            "This log sees the frames go out and nothing after that: whether "
            "a neural frame comes back is in the helper's own log, "
            "host64\\dlss5-feed-host.log."
            + (" The 'present probe: 0 presents' lines do not measure the "
               "helper either - they count the game's own presents at one "
               "Vulkan entry point, and the same log says the game kept "
               "presenting while they read 0." if probe else ""))
    rep.verdict = ("Frames reach the 64-bit helper, and only its own log "
                   "says what came back - look in host64\\dlss5-feed-host.log.")
    return True
