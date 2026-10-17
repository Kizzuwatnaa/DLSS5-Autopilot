r"""What the bridge's own log says about the launch a report reads.

The bridge route was answered "this route logs no frames, so the panel is
the only live picture" in every report it ever sent (#27 #127 #198 #217
#287 #352), while the bridge wrote dlss5-bridge.log beside itself the whole
time: the build, the first frame it delivered, a frame-rate line every 600
frames, and the reason when it turns itself off. Nothing read it.

The file is emptied when the bridge attaches, so it holds one launch - but
not necessarily the launch ReShade.log's last session describes: a launch
in which the bridge did not load leaves the older file behind. It is tied
to ReShade's session by the two logs' own clocks, never by the files'
times.

Part of core/diagnose; see __init__.py.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import *  # noqa: F401,F403
from .helper import _clock


__all__ = ["BridgeRun", "bridge_run", "read_bridge", "_bridge_excerpt",
           "_bridge_session", "_bridge_text", "_bridge_verdict"]

# BridgeFail's one word for what failed, three times in a row, in words.
_FAIL_WORDS = {"evaluate": "evaluates", "command list": "command lists",
               "resource build": "resource builds", "HDR10 decode": "HDR10 decodes"}

# How far after ReShade's "Registered add-on" line the bridge may write
# "attached.": it registers inside DllMain and writes the line a moment
# later, so anything outside this window is another launch.
_TIE_BEFORE, _TIE_AFTER = 5.0, 120.0


@dataclass
class BridgeRun:
    version: str = ""
    attached: str = ""            # the "attached." line itself
    frames: int = 0               # the highest count the log carries
    first: str = ""               # the first "delivered" line
    fps: str = ""                 # from the last frame-rate line
    unfocused: bool = False       # that line says the window was not in focus
    stop: str = ""                # why it stopped after its last frame, or ""
    stop_line: str = ""
    module: str = ""              # "it faulted in <module>" beside the stop
    path: str = "bridge"          # the stop's own tag: bridge, synth or vkmirror
    crashed: bool = False         # the crash handler's block is the stop
    called: bool = False          # the log shows the game calling DLSS
    recorded: int = 0             # Vulkan mirror frames recorded, not yet delivered
    refused: str = ""             # NGX's answer to CreateFeature, when it refused
    failed: list = field(default_factory=list)   # evaluate failures after it


def _bridge_text(path: Path, head: int = 65_536, tail: int = 1_000_000) -> str:
    """The file, or its first and last parts: the "attached." line is the
    first line and the frames and the stop are at the end."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size <= head + tail:
                return f.read().decode("utf8", "replace")
            first = f.read(head).decode("utf8", "replace").rsplit("\n", 1)[0]
            f.seek(size - tail)
            last = f.read().decode("utf8", "replace").split("\n", 1)[-1]
            return first + "\n...\n" + last
    except OSError:
        return ""


def _bridge_session(text: str) -> str:
    """From the last "attached." line on; "" without one."""
    last = None
    for last in _BRIDGE_ATTACHED.finditer(text or ""):
        pass
    return text[last.start():] if last is not None else ""


def read_bridge(text: str) -> BridgeRun | None:
    """Parse one bridge session. None when there is no "attached." line."""
    text = _drop_carried(_bridge_session(text))
    att = _BRIDGE_ATTACHED.search(text)
    if att is None:
        return None
    run = BridgeRun(version=att.group(2), attached=att.group(0).strip())
    run.called = bool(_BRIDGE_CALLED.search(text))
    refused = list(_BRIDGE_CREATE_FAILED.finditer(text))
    if refused:
        run.refused = f"{refused[-1].group(1)}, {refused[-1].group(2).rstrip(',')}"
    last_end = -1
    for m in _BRIDGE_FRAME.finditer(text):
        if m.group(3):
            # recorded before its evaluate ran: not a delivered frame (gate
            # 2.0.7 pass 3); delivery is the "so far" count on that path
            run.recorded = max(run.recorded, int(m.group(3)))
            continue
        n = int(m.group(1) or m.group(2))
        run.frames = max(run.frames, n)
        last_end = m.end()
        if not run.first and m.group(1):
            s = text.rfind("\n", 0, m.start()) + 1
            e = text.find("\n", m.end())
            run.first = text[s:e if e >= 0 else None].strip()
    stats = list(_BRIDGE_STATS.finditer(text))
    if stats:
        run.fps = stats[-1].group(2).replace(",", ".")
        e = text.find("\n", stats[-1].end())
        run.unfocused = "WINDOW NOT IN FOCUS" in text[stats[-1].end():e if e >= 0 else None]
    # After the last frame line - or, with none, after the attached line.
    after = text[last_end if last_end >= 0 else att.end():]
    first_stop = None
    hits = [m for m in (pat.search(after) for pat in _BRIDGE_STOPS) if m]
    # BridgeDisable's own "stopped: <why>" names the reason in words; the
    # exception line before it carries the code, which goes beside it.
    words = next((m for m in hits if m.re is _BRIDGE_STOPS[0]), None)
    if words is not None:
        # Only this launch's own lines: the next launch reprints the last
        # crash under "The previous run crashed", and its code is not this
        # stop's (gate 2.0.7).
        codes = re.findall(r"exception (0x[0-9A-Fa-f]{8})", _own(after[:words.start()]))
        code = codes[-1] if codes else ""
        why = words.group(1)
        # BridgeFail passes one word ("evaluate", "command list"), and its
        # counter is shared by all four kinds: three failures of any of them
        # in a row turn it off, the last one named (gate 2.0.7).
        if why in _FAIL_WORDS:
            fails = list(_BRIDGE_FAILED.finditer(after[:words.start()]))
            why = (f"it failed three times in a row, the last in its {why}"
                   + (f" ({fails[-1].group(1)}, {fails[-1].group(2).rstrip(',')})"
                      if fails and why == "evaluate" else ""))
        first_stop = (words, why + (f" ({code})" if code and code not in why else ""))
    elif hits:
        m = min(hits, key=lambda h: h.start())
        first_stop = (m, m.group(1))
    crash = _BRIDGE_CRASH.search(after)
    if crash and (first_stop is None or crash.start() < first_stop[0].start()):
        who = (crash.group(2) or "").strip()
        who = re.split(r"[\\/]", who)[-1] if who else ""
        first_stop = (crash, f"exception {crash.group(1)}" + (f" in {who}" if who else ""))
        run.crashed = True
    if first_stop is not None:
        m, why = first_stop
        # the bridge's own pointers to lines above it mean nothing in a
        # headline of ours (gate 2.0.7 pass 3)
        why = re.sub(r" -- the results are named above --|;? the lines above say why", "", why)
        run.stop = why.strip().rstrip(".")
        s = after.rfind("\n", 0, m.start()) + 1
        e = after.find("\n", m.start())
        run.stop_line = after[s:e if e >= 0 else None].strip()
        tag = re.search(r"\[(bridge|synth|vkmirror)\]", run.stop_line)
        run.path = tag.group(1) if tag else "bridge"
        # The fault's owner: logged just before a BridgeDisable, just after
        # the synthetic path's own stop line (gate 2.0.7).
        owners = list(_BRIDGE_FAULTED.finditer(after[:m.start()]))
        near = _BRIDGE_FAULTED.search(after[m.end():m.end() + 600])
        pick = owners[-1] if owners else near
        if pick is not None and not run.crashed:
            run.module = re.split(r"[\\/]", pick.group(1).strip())[-1]
        # "the D3D12 evaluate raised exception 0x.. after 1234 delivered
        # frames": a count the frame lines had not reached yet
        cnt = re.search(r"after (\d+) delivered frames", run.stop_line)
        if cnt:
            run.frames = max(run.frames, int(cnt.group(1)))
    run.failed = [f"{m.group(1)} {m.group(2)}".rstrip(",")
                  for m in _BRIDGE_FAILED.finditer(after)]
    return run


def _drop_carried(text: str) -> str:
    """The launch without the previous run's crash, which the bridge reprints
    at attach: one clock on its first line, the old block's own clocks on the
    rest, and a blank line after it. Its exception code is not this launch's
    (gate 2.0.7 pass 3)."""
    # Line by line: a regex for "up to the first blank line" backtracked
    # without end on a log with no blank line after the block.
    lines = (text or "").split("\n")
    start = next((i for i, ln in enumerate(lines) if "The previous run crashed" in ln), None)
    if start is None:
        return text or ""
    end = next((i for i in range(start + 1, min(len(lines) - 1, start + 62))
                if not lines[i].strip()), None)
    if end is None:
        return text or ""
    return "\n".join(lines[:start] + lines[end + 1:])


def _own(text: str) -> str:
    """The lines this launch wrote: a reprinted line from the previous run
    carries that run's clock after this one's."""
    return "\n".join(ln for ln in (text or "").splitlines()
                     if not re.match(r"^\S+\s+\d\d:\d\d:\d\d\.\d{3}\s", ln)
                     and "The previous run crashed" not in ln)


def _quote(line: str, width: int = 160) -> str:
    """The bridge's own line, without its clock and the sentence it adds to
    every stop, and marked when cut."""
    said = re.sub(r"^[\d:.]+\s+", "", line or "")
    said = re.split(r"\. (?:The game renders normally|This backend stops here|The game keeps"
                    r"|The game's own DLSS was forwarded)", said)[0].rstrip(".")
    return said if len(said) <= width else said[:width].rsplit(" ", 1)[0] + "..."


def _tied(att: str, rtext: str) -> bool:
    """Was the bridge's "attached." line written in ReShade's last session?

    Read off the two logs' own clocks: ReShade's "Registered add-on "DLSS 5
    Bridge" line, the newest one, and the bridge's first line just after it.
    A ReShade session without that line (the bridge did not load, or the
    tail no longer holds it) ties nothing."""
    m = _BRIDGE_ATTACHED.search(att or "")
    t = _clock(m.group(1)) if m else None
    reg = None
    for reg in _BRIDGE_REGISTERED.finditer(rtext or ""):
        pass
    r = _clock(reg.group(1)) if reg is not None else None
    if t is None or r is None:
        return False
    if t < r - 43200:
        t += 86400                          # past midnight
    elif t > r + 43200:
        t -= 86400
    return r - _TIE_BEFORE <= t <= r + _TIE_AFTER


def bridge_run(install_dir: Path, rtext: str) -> BridgeRun | None:
    """The bridge's log for the launch ReShade's last session describes, or
    None: no log, no session in it, or one from another launch."""
    run = read_bridge(_bridge_text(Path(install_dir) / BRIDGE_LOG))
    if run is None or not _tied(run.attached, rtext):
        return None
    return run


def _bridge_verdict(rep: Report, run: BridgeRun, panel: str, rtext: str,
                    synthetic: bool = False) -> bool:
    """Say what the bridge's log says; True when that is the verdict.

    Delivered frames are not "Working.": the bridge hands each one to
    NVIDIA's DLSS call, where the DLSS 5 add-on takes it, and whether the
    neural pass then changed the picture is written in neither log. The
    counts are lower bounds: the log has a line at frame 1 and one every
    600 after it."""
    n = run.frames
    frames = f"at least {n} frame{'' if n == 1 else 's'}"
    own_panel = "DLSS 5 Bridge panel in the overlay"
    if n:
        rep.add(OK, f"The bridge delivered {frames} in this launch"
                    + (f", {run.fps} fps at its last count" if run.fps else "") + ".",
                f"Read from {BRIDGE_LOG} (dlss5-bridge {run.version}). The "
                f"bridge began that file when ReShade loaded it, in the launch "
                f"of ReShade.log's last session."
                + (" The last frame-rate line was taken with the game window "
                   "out of focus." if run.unfocused else ""))
    else:
        rep.add(INFO, f"The bridge attached in this launch (dlss5-bridge "
                      f"{run.version}); its log has no delivered-frame line.",
                "It writes a line at the first frame it delivers (on the "
                "Vulkan mirror, at the first frame it records), so none went "
                "through it in this launch.")
    if run.crashed:
        rep.add(BAD, (f"The game crashed after the bridge delivered {frames}: {run.stop}." if n else
                      f"The game crashed before the bridge delivered a frame: {run.stop}."),
                f"The bridge's crash handler wrote this in {BRIDGE_LOG}. It "
                f"records any crash in the game's process, and the module "
                f"named is where the fault was - not always the bridge. "
                f"'report a bug' carries the lines.")
        rep.verdict = ("It started, then the game crashed - see below." if n else
                       "The game crashed before the bridge delivered a frame - see below.")
        return True
    if run.stop:
        after = {"synth": "The bridge's substitute stopped delivering there, and "
                          "the game draws on without the pass.",
                 "vkmirror": "The Vulkan mirror stood down there for the rest of "
                             "the session; the game's own DLSS is what is on screen."
                 }.get(run.path, "The bridge turned itself off there, and the game "
                                 "draws without the pass from then on.")
        rep.add(BAD, (f"The bridge stopped after delivering {frames}: {run.stop}." if n else
                      f"The bridge stopped before it delivered a frame: {run.stop}."),
                f"Its own line: '{_quote(run.stop_line)}'. {after}"
                + (f" It faulted in {run.module}." if run.module else "")
                + f" 'report a bug' carries the last lines of {BRIDGE_LOG}. The "
                  f"feeder route does not go through the bridge.")
        rep.verdict = ("It started, then the bridge stopped - see why below."
                       if n else
                       "The bridge stopped before it delivered a frame - see why below.")
        return True
    if run.failed:
        last = run.failed[-1].split(" ", 1)
        rep.add(WARN, f"The bridge's evaluate failed {len(run.failed)} "
                      f"time{'s' if len(run.failed) > 1 else ''} "
                      + ("after its last frame line" if n else "in this launch")
                      + f" ({', '.join(last)}).",
                "Three failures in a row turn the bridge off; this log has no "
                "such stop, so it kept running. A frame whose evaluate failed "
                "gets no neural output.")
    if not n and run.refused:
        rep.add(BAD, f"NVIDIA's runtime would not create the game's DLSS feature ({run.refused}).",
                "The bridge asks NGX for the feature the game asked for and has "
                "nothing to deliver into without it. The DLSS 5 add-on is not "
                "involved yet at that point; the driver and the nvngx_dlss build "
                "the game loads are.")
    if not n and run.recorded:
        rep.add(INFO, f"The Vulkan mirror recorded at least {run.recorded} "
                      f"frame{'' if run.recorded == 1 else 's'}; none is counted as "
                      f"delivered yet.",
                "It counts delivered frames every 600, after their evaluate "
                "finished.")
    if not n:
        # The "no frame log" line is gone for this launch, so the verdict
        # cannot say the route logs none (gate 2.0.7).
        rep.verdict = (f"The game called DLSS and the bridge delivered no frame - "
                       f"the {own_panel} says why, and 'report a bug' carries "
                       f"{BRIDGE_LOG}."
                       if run.called else
                       f"The bridge attached and delivered no frame from the "
                       f"driver's optical flow - the {own_panel} says why."
                       if synthetic else
                       # Not "turn DLSS on": it was on for #637, with Ray
                       # Reconstruction - which replaces Super Resolution,
                       # the one call the bridge takes over.
                       f"The bridge attached and delivered no frame - its log "
                       f"shows no DLSS Super Resolution call from the game. In "
                       f"the game's menu turn DLSS on and, if it has Ray "
                       f"Reconstruction, turn that off, then check the "
                       f"{own_panel}.")
        return True
    rep.verdict = (f"The bridge delivered {frames}; whether the neural pass "
                   f"drew them is in no log - switch the pass off and on in "
                   f"the {panel} and compare the picture.")
    return True


def _bridge_excerpt(text: str, n: int = 6, budget: int = 900,
                    width: int = 200) -> list[str]:
    """The lines a report carries: the "attached." line, the first and last
    frame lines, the last three frame-rate lines, every stop, failure and
    crash line, and the tail - in the log's order, inside the budget. The
    replay rebuilds the file from exactly these, so the verdict it reads is
    the one the machine read."""
    lines = [ln.rstrip() for ln in _bridge_session(text).splitlines()]
    if not lines:
        # A file with no "attached." line is still a file: its last lines,
        # rather than "(none)", which reads as no log at all (#155).
        return [ln.rstrip()[:width] for ln in (text or "").splitlines()
                if ln.strip()][-8:]
    found = [(i, _BRIDGE_FRAME.search(ln)) for i, ln in enumerate(lines)]
    frames = [i for i, m in found if m and (m.group(1) or m.group(3))]
    so_far = [i for i, m in found if m and m.group(2)]
    stats = [i for i, ln in enumerate(lines) if _BRIDGE_STATS.search(ln)]
    stops = [i for i, ln in enumerate(lines)
             if any(p.search(ln) for p in _BRIDGE_STOPS) or _BRIDGE_FAULTED.search(ln)]
    failed = [i for i, ln in enumerate(lines) if _BRIDGE_FAILED.search(ln)]
    # what made "the game called DLSS" true, or the replay says "turn it on"
    called = [i for i, ln in enumerate(lines)
              if _BRIDGE_CALLED.search(ln) and not _BRIDGE_FAILED.search(ln)]
    crash = [j for i, ln in enumerate(lines) if "### CRASH RECORDED ###" in ln
             for j in range(i, min(i + 4, len(lines)))]
    # Most important first: what is dropped over budget is the end of this,
    # so the oldest tail lines go first and the attached line last. The
    # crash block and the stop lines come before the failures: six failure
    # lines once pushed the crash's exception line out, and the replay read
    # "delivered" where the machine read "stopped" (gate 2.0.7).
    # The highest frame count goes before them, or a long stop line pushed
    # it out and the replay read "before it delivered a frame".
    firm = ([0] + frames[-1:] + so_far[-1:] + crash[:4] + stops[-4:] + frames[:1]
            + called[-1:] + failed[-2:] + stats[-1:])
    keep = list(dict.fromkeys(firm + stats[-3:-1]
                              + list(range(len(lines) - 1, max(0, len(lines) - n) - 1, -1))))

    def size(idx):
        return len("\n".join(lines[i][:width] for i in idx))
    while len(keep) > 1 and size(sorted(keep)) > budget:
        keep.pop()
    return [lines[i][:width] for i in sorted(keep)]
