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
from .evidence import _installed_at, _standalone_named, _upstream_named


__all__ = ["_loaded_note", "_foreign_hooks", "_name_foreign_hooks"]

# NGX loads the neural runtime when the feature is CREATED, not when the
# game starts. Not loaded at a sighting is what a session looks like before
# that moment, and what every session that never got there looks like -
# a consequence, not a missing file (#250).
_LOADS_ON_CREATE = ("nvngx_dlssnr.dll",)


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
               "Set up correctly, but not switched on")


def _name_foreign_hooks(rep: Report, foreign) -> Report:
    """Put a second DLSS hook into the verdict, not only under it."""
    if not foreign or not rep.verdict:
        return rep
    names = ", ".join(foreign[:3])
    if rep.verdict.startswith("Working"):
        # Frames came through with it loaded; it did not stop the pass.
        for f in rep.findings:
            if f.level == BAD and f.title.startswith("Another DLSS hook was loaded"):
                f.level = WARN
        return rep
    if rep.verdict.startswith(_CANNOT_SEE):
        rep.verdict = (f"Another DLSS hook was loaded beside ours ({names}) - "
                       f"move it out of the game folder and test with ours "
                       f"alone.")
    else:
        rep.verdict = (f"{rep.verdict.rstrip()} Another DLSS hook was loaded "
                       f"beside ours too ({names}) - test without it.")
    return rep
