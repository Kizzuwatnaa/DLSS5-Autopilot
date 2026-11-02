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

from pathlib import Path

from .model import *  # noqa: F401,F403
from .evidence import _same_launch


__all__ = ["_helper_session", "_helper_excerpt", "_helper_verdict", "_fed_the_helper",
           "_helper_same_launch"]

_HOST_BANNER = re.compile(r"^[\d:.]+\s+dlss5-feed-host\S*\s", re.M)
# DXGI_ERROR_DEVICE_REMOVED. Any other Present code is left alone: the
# helper says a failed present of its own window does not touch the feed.
_REMOVED = re.compile(r"\[host\] Present failed (0x887A0005)", re.I)
_OUTCOME = re.compile(r"\[host\] neural consumer outcome: ([^\n]+)")
_KEEP = re.compile(r"DLSS 5 add-on|NGX_D3D12_Init|NeuralUplift|Present failed|"
                   r"in a row|consumer outcome|feature ready|evaluated|"
                   r"CRASH|EXCEPTION|evaluate raised|stack|\] exit|lost|"
                   r"pipe closed")
# "evaluate raised" is half of the driver-fault rule (chain.py); #917's
# report carried the stack line without it and replayed to another answer.


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
    # "(ReShade.log is unavailable)" - "unknown (this process's ReShade.log
    # could not be opened ...)" from 1.16.0-beta.5 - is the helper saying it
    # could not look, not that nothing was there.
    no_nr = not off and "unavailable" not in said and "could not be opened" not in said \
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


def _clock(stamp: str) -> float | None:
    """Seconds since midnight from the logs' "01:20:34.841" stamp."""
    m = re.match(r"(\d\d):(\d\d):(\d\d)[.:](\d{3})", stamp or "")
    if not m:
        return None
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def _helper_same_launch(text: str, htext: str, feed: Path, host: Path) -> bool:
    """Is the helper's log from the launch the feed log's last session is?

    Read off the logs' own clocks: the helper's first stamp has to fall
    within three minutes after the feed's last "host spawned" line. The
    files' times drift apart while the person plays on - the feed log keeps
    growing and the helper's stops once it is up - so on a long session
    the time check threw away the helper's log of that very launch (gate
    2.0.6, the twin of #482's). Without both stamps, the files' times."""
    spawn = None
    for spawn in re.finditer(r"^(\S+)\s+\[feed32\] host spawned", text or "", re.M):
        pass
    # The helper's LAST run, and only when its start is there: a log with
    # a helper respawned after a lost device holds two runs, and a tail cut
    # to 150 KB or a report's excerpt starts somewhere in the middle - the
    # first stamp is then not the start (gate 2.0.7).
    run = _helper_session(htext)
    first = (re.search(r"^(\S+)\s+\[host\]", run, re.M)
             if _HOST_BANNER.match(run) else None)
    lo = _clock(spawn.group(1)) if spawn else None
    t0 = _clock(first.group(1)) if first else None
    if lo is None or t0 is None:
        return _same_launch(feed, host)
    if t0 < lo - 43200:
        t0 += 86400                        # past midnight
    return lo - 10 <= t0 <= lo + 180


def _gave_up_early(rep: Report, text: str, htext: str, stale: bool = False) -> bool:
    """The feed stopped waiting for the helper, and the helper came up after.

    "host lost: pipe never appeared" is the feed's own timeout. On #482 the
    helper was spawned at 01:20:34.8, the feed gave up at 01:20:49.9, and the
    helper's NGX_D3D12_Init succeeded at 01:20:52.8 - it was starting, not
    gone. The answer is the feeder's wait, not the helper's log, which the
    report already carries.
    """
    lost = re.search(r"^(\S+)\s+\[feed32\] host lost: pipe never appeared", text or "", re.M)
    if not lost:
        return False
    gave_up = _clock(lost.group(1))
    spawn = None
    for spawn in re.finditer(r"^(\S+)\s+\[feed32\] host spawned", text or "", re.M):
        pass
    # Is the helper's log from this launch? Its first stamp has to fall
    # between the spawn and two minutes after the feed gave up - read off
    # the logs' own clocks, because the feed log keeps growing while the
    # person plays on and the files' times drift apart (gate 2.0.6).
    first = re.search(r"^(\S+)\s+\[host\]", htext or "", re.M)
    t0 = _clock(first.group(1)) if first else None
    lo = _clock(spawn.group(1)) if spawn else gave_up
    if t0 is not None and lo is not None and gave_up is not None:
        if t0 < lo - 43200:
            t0 += 86400                    # past midnight
        if not (lo - 10 <= t0 <= gave_up + 120):
            stale, htext = True, ""
    both = (htext or "") + "\n" + (text or "")
    ready = None
    for m in re.finditer(r"^(\S+)\s+\[host\] NVSDK_NGX_D3D12_Init -> 0x0*1 \(Success\)",
                         both, re.M):
        t = _clock(m.group(1))
        if t is None or gave_up is None:
            continue
        if t < gave_up - 43200:
            t += 86400                     # past midnight
        # Within two minutes of giving up: anything later is another start.
        if gave_up <= t <= gave_up + 120:
            ready = t
            break
    started = _clock(spawn.group(1)) if spawn else None
    waited = (f" {gave_up - started:.0f} s after starting it"
              if started is not None and gave_up is not None and gave_up >= started else "")
    if ready is not None:
        took = (f", {ready - started:.0f} s after it was started"
                if started is not None and ready >= started else "")
        rep.add(BAD, f"The feed stopped waiting for the 64-bit helper{waited}, "
                     f"and the helper finished starting later{took}.",
                "The helper's own log shows NVIDIA's runtime coming up after the "
                "feed had already given up on it ('host lost: pipe never "
                "appeared'), so the game carried on without the pass. That "
                "wait is inside DLSS5-Feeder, not in this install. Start the "
                "game again, and if it keeps happening, try another "
                "build in 'feeder build' and send both logs to the "
                "DLSS5-Feeder project.")
        rep.verdict = ("The feed stopped waiting for the 64-bit helper while it "
                       "was still starting - a feeder timing bug; start the game "
                       "again or try another feeder build.")
        return True
    rep.add(BAD, f"The feed stopped waiting for the 64-bit helper{waited} "
                 f"('host lost: pipe never appeared').",
            "The helper was started and never opened its connection in time, "
            "so the game carried on without the pass. "
            + ("Its own log, host64\\dlss5-feed-host.log, is from an earlier "
               "launch: the helper wrote nothing this time, which points at "
               "something stopping it from starting at all."
               if stale else
               "Its own log, host64\\dlss5-feed-host.log, says how far it got."
               if not htext else
               "Its own log shows no NVIDIA runtime coming up after the feed "
               "gave up."))
    rep.verdict = ("The feed stopped waiting for the 64-bit helper before it "
                   "started - see below.")
    return True


def _fed_the_helper(rep: Report, text: str, htext: str = "",
                    stale: bool = False) -> bool:
    """The feed shipped frames to the helper, and its log can see no further.

    `stale`: the helper's log is there but from an earlier launch."""
    if _gave_up_early(rep, text, htext, stale):
        return True
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
