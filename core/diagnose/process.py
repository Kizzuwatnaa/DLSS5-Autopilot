r"""What was in the game's process: ours, what was not yet, and whose else.

Two questions a log cannot answer alone. Which of our files the game
really had loaded - read out of the process by core/watch.py - and which
other DLSS hook ReShade loaded into the same session beside ours.

The second one was a warning under a verdict that said "Add-ons loaded,
confirm in the overlay" (#250: DLSS 5 Swapper's overlay beside
neural-upstream, #55 before it). People act on the verdict, not on the
warnings under it, so a second hook in the frame is now said IN it.

Part of core/diagnose; see __init__.py.
"""
from __future__ import annotations
import re
from datetime import datetime
from pathlib import Path

from .model import *  # noqa: F401,F403
from .evidence import _installed_at, _standalone_named, _upstream_named, TAIL_RESHADE


__all__ = ["_loaded_note", "_foreign_hooks", "_name_foreign_hooks"]

# NGX loads the neural runtime when the feature is CREATED, not when the
# game starts. Not loaded at a sighting is what a session looks like before
# that moment, and what every session that never got there looks like -
# a consequence, not a missing file (#250).
_LOADS_ON_CREATE = ("nvngx_dlssnr.dll",)


# The clock of a line that belongs to a LAUNCH: "04:12:59:122 [55536] | INFO |
# Registered add-on ...", or ReShade's own first line. No date in it. Not any
# line: the log's last one is written when the game closes, and measuring
# from it called every session longer than two minutes "started again".
_LOG_CLOCK = re.compile(r"^(\d{2}):(\d{2}):(\d{2}):\d+ [^\n]*"
                        r"(?:Initializing crosire's ReShade|Registered add-on)", re.M)

# A launch later than the sighting by less than this is the same launch seen
# from its two ends: the snapshot is taken as the game comes up and ReShade
# registers its add-ons a moment afterwards (#250: 49 s apart). Beyond it,
# the game was started again after the snapshot was taken (#287: the log ends
# with a launch three minutes after it).
_SAME_LAUNCH_S = 120


def _sighting_is_older_than_the_last_launch(install_dir: Path, at) -> bool:
    """Did the game start again after this snapshot was taken?

    The module list is the strongest evidence the diagnosis has - it is the
    running game rather than a log - but only about the run it was taken
    from. #287's was taken at 04:10, between a launch at 04:09 and the one
    the log ends with at 04:12, and every line built from it still opened
    "When it last ran": a finished launch read out as the current one, its
    add-ons reported missing from a session that was over.

    ReShade's own clock carries no date, so this compares times of day and
    says nothing when they are hours apart rather than guessing a day.
    """
    try:
        if not at:
            return False
        log = install_dir / "ReShade.log"
        if not log.is_file():
            return False
        # A launch after the snapshot wrote the log after it. A log last
        # written before it is an earlier day's, whatever its clock reads -
        # and today's list is then the only evidence there is.
        if log.stat().st_mtime <= float(at):
            return False
        text = log.read_text(encoding="utf8", errors="replace")[-TAIL_RESHADE:]
        marks = _LOG_CLOCK.findall(text)
        if not marks:
            return False
        h, m, s = (int(x) for x in marks[-1])
        seen_at = datetime.fromtimestamp(float(at))
        last = seen_at.replace(hour=h, minute=m, second=s, microsecond=0)
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    gap = (last - seen_at).total_seconds()
    # Only a gap on the same day, and only a plausible one: a log whose last
    # line reads hours after the snapshot is a log from another day.
    return _SAME_LAUNCH_S < gap < 3 * 3600


# A file this tool writes -> what its add-on calls itself when ReShade
# registers it. A file that is not here is never taken for registered.
_REGISTERS_AS = (("bridge", ("dlss 5 bridge",)), ("feed", ("dlss 5 feed",)),
                 ("renodx-dlss5.", ("dlss 5 neural rendering",)),
                 ("renodx-dlss.", ("renodx dlss",)),
                 ("nvngx.dll.addon", ("pre-upscale", "upstream")),
                 ("standalone-dlssnr", ("standalone dlss-nr",)),
                 ("rtx40mfg", ("mfg unlock",)))


def _loaded_note(install_dir: Path, man: dict, rep: Report) -> None:
    """Say what the process really had in it, where the log cannot.

    Three routes write no frame log at all and OptiScaler's log stops short
    of saying whether the model ran, so 14 reports were answered "open the
    overlay and read it yourself". Half of that question - is the add-on
    even in the process, is the runtime beside it - the module list answers
    outright, and it does not need the person to go and look.
    """
    try:
        from .. import watch
        seen = watch.settle(
            watch.last_sighting(install_dir, _installed_at(install_dir)),
            man.get("files"))
        proxy = str(man.get("proxy") or "")
        also = ()
        if man.get("dxvk"):
            from .. import dxvk as _dx
            also = _dx.ALL_FILES
        need = [n for n in seen.get("missing") or []
                if watch.essential(n, proxy, also)]
    except Exception:
        return
    if not seen or seen.get("refused"):
        return
    if _sighting_is_older_than_the_last_launch(install_dir, seen.get("at")):
        return
    # The session's own log outranks an early look (#352): ReShade registered
    # both add-ons and ran them for eleven minutes, and two lines under that
    # the snapshot - taken as the process came up - said they were not loaded.
    # File by file: the bridge registering says nothing about the feed, and an
    # HDR mod or the MFG unlock registering says nothing about either.
    reg = [f.title.lower() for f in rep.findings
           if f.level == OK and f.title.startswith("ReShade loaded add-on")]

    def registered(n: str) -> bool:
        said = next((t for k, t in _REGISTERS_AS if k in n), ())
        return any(s in t for s in said for t in reg)
    need = [n for n in need if not (str(n).lower().endswith((".addon64", ".addon32"))
                                    and registered(str(n).lower()))]
    later = [n for n in need if str(n).lower() in _LOADS_ON_CREATE]
    need = [n for n in need if n not in later]
    loaded = [n for n in (seen.get("ours") or [])]
    if not loaded and not need and not later:
        return
    when = datetime.fromtimestamp(seen.get("at", 0)).strftime("%d %b %H:%M")
    if loaded:
        rep.add(OK, f"When it last ran ({when}), the process had "
                    f"{', '.join(loaded)} loaded.",
                "Read out of the running game, not out of a log: those are "
                "in it. What the overlay still answers is whether the model "
                "is switched on and drawing.")
    if need:
        # Only what the game has to load. The rest of a package is loaded
        # on demand, and "...and not amd_fidelityfx_vk.dll" under a DX12
        # game read as the fault (#231).
        rep.add(WARN, (f"...and not {', '.join(need)}."
                       if loaded else
                       f"When it last ran ({when}), the process did not have "
                       f"{', '.join(need)} loaded."),
                "Written here, and not in the process when it last ran.")
    if later:
        rep.add(INFO, f"{', '.join(later)} was not loaded yet when the game "
                      f"was seen.",
                "NVIDIA's runtime loads it when the neural feature is "
                "created, not when the game starts. A look taken before that, "
                "or a session that never created the feature, both show this "
                "- on its own it is not a missing file.")


# What a registered add-on's name has to carry to be a DLSS hook, rather
# than an HDR mod, a limiter or a shader tool. The RTX 40 MFG unlock this
# tool installs ("Universal RTX 40 MFG Unlock V1.2") carries none of them.
_HOOK_WORDS = re.compile(r"dlss|\bngx\b|nvngx|neural|optiscaler", re.I)


def _route_owns(name: str, route: str) -> bool:
    """Is this add-on one the route itself installs into the game?"""
    low = name.strip().lower()
    native = low == NATIVE_ADDON_NAME.lower()
    if route == "feeder":
        return "feed" in low or native
    if route == "bridge":
        return "bridge" in low or native
    if route == "native":
        return native
    if route == "renodx":
        return low == "renodx dlss"
    if route == "upstream":
        return _upstream_named(name)
    if route == "standalone":
        return _standalone_named(name)
    return False


def _foreign_hooks(loaded, route: str) -> list[str]:
    """The DLSS hooks ReShade loaded in this session that the route did not."""
    return list(dict.fromkeys(
        n for n in loaded
        if _HOOK_WORDS.search(n) and not _route_owns(n, route)))


# Verdicts that only say "we cannot see from here". A second hook in the
# frame is a better answer than any of them, so it replaces them; every
# other verdict names something the logs do show, and keeps it.
_CANNOT_SEE = ("Add-ons loaded", "Inconclusive", "Loaded and set up",
               "Set up correctly, but not switched on",
               # 2.0.7: the bridge's own log counted frames and nothing more
               "The bridge delivered at least")


def _name_foreign_hooks(rep: Report, foreign) -> Report:
    """Put a second DLSS hook into the verdict, not only under it."""
    if not foreign or not rep.verdict:
        return rep
    names = ", ".join(foreign[:3])
    if rep.verdict.startswith("Working"):
        # Frames came through with it loaded; it did not stop the pass.
        for f in rep.findings:
            if f.level == BAD and f.title.startswith(("Another DLSS hook was loaded",
                                                      "Another DLSS tool's add-on")):
                f.level = WARN
        return rep
    if rep.verdict.startswith(_CANNOT_SEE) and all("DX11 Bridge" in n for n in foreign):
        # The bridge route's own add-on under its 1.0.x name: every ReShade
        # route now moves it aside on install, so that is the step (#439).
        rep.verdict = (f"An older bridge add-on was loaded beside ours ({names}, "
                       f"dlss5-dx11-bridge.addon64) - install again and it is "
                       f"moved aside.")
    elif rep.verdict.startswith(_CANNOT_SEE):
        rep.verdict = (f"Another DLSS hook was loaded beside ours ({names}) - "
                       f"move it out of the game folder and test with ours "
                       f"alone.")
    else:
        rep.verdict = (f"{rep.verdict.rstrip()} Another DLSS hook was loaded "
                       f"beside ours too ({names}) - test without it.")
    return rep
