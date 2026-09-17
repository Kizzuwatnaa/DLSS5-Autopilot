"""Where on the chain a diagnosis verdict stands, and who can act on it.

install -> nothing of ours loaded -> the game refused the hook -> loaded but
the feed never got going -> set up but not switched on -> loaded and we
cannot see -> the add-on crashed -> the model refused -> the driver's
runtime faulted -> working.

One list, read by two things that must never disagree: the corpus tool that
says which kind of failure to fix next (_tools/stuck_games.py), and the
autopilot pass that decides whether another route is worth a try.
The verdicts are matched by their own printed words; a verdict nobody added
here is "unmapped", which the corpus tool reports and the pass treats as "do
not guess".
"""
from __future__ import annotations

# (stage, who can fix it, fragments of the printed verdict)
# Every fragment is a piece of a string core/diagnose (or the crash override
# in ui/ctl_game.py) really prints - a fragment nothing prints matched
# nothing for a whole release, and "ReShade's" alone matched #238's Vulkan
# layer answer as antivirus removal. Keep them as long as it takes to be
# about one verdict.
CHAIN = (
    ("1 the install stopped", "us", (
        "The install never finished",
        "The install crashed",
        "The install stopped",
        "The drive was full",
        "Nothing is installed in this folder",
        "No install record here and no ReShade.log",
    )),
    ("2 a file went missing after it", "the person's antivirus", (
        "is missing from the folder - reinstall",      # ReShade's proxy, DXVK
        "ReShade's Vulkan layer is not registered",
        "ReShade Vulkan layer is not registered",
        "Files the install wrote are gone",
        "The motion-vector shader was removed",
        "OptiScaler.ini is missing",
    )),
    ("3 nothing of ours loaded", "us", (
        "Not started since the install",
        "Installed after the last run",
        # "did not load" today, "never loaded" in the reports before it
        "Not run yet, or OptiScaler",
        "DXVK ran and ReShade did not",
        "It started, and nothing here recorded the session",
        "The install went beside a launcher",
        "is running and has loaded nothing from this folder",
        "and loaded nothing from this folder",
        "Not run yet, or the Remix runtime never",
        "It looks as though it ran and nothing this install wrote was loaded",
        "and no log was written - ReShade's Vulkan layer is not reaching the game",
        "and no log was written - the proxy is reached and ReShade is not",
    )),
    ("4 the game refused the hook", "upstream", (
        "The game refused OptiScaler's swapchain",
        "The game refused the swapchain",
        "The game's DLSS call was never hooked",
    )),
    ("5 loaded, the feed never got going", "upstream", (
        "Inconclusive - the feed did not get far enough",
        "Inconclusive - the add-on attached but built nothing",
        "It started and closed during start-up",
        "Remix ran; the neural pass was never even attempted",
        "Remix ran, but the DLSS 5 snippet never started",
        "No Remix runtime",
        "The Remix runtime here has no neural pass",
        "The add-on runs but never produces a frame",
    )),
    ("6 set up, not switched on", "the person", (
        "Set up correctly, but not switched on yet",
        "Loaded and set up; no neural frame yet",
        "OptiScaler loaded; neural rendering not switched on",
        "ReShade never gave it an effect runtime",
    )),
    ("7 loaded, and we cannot see", "us", (
        "Inconclusive - open the overlay",
        "Add-ons loaded. Confirm in",
        "Frames reach the 64-bit helper, and only its own log",
    )),
    ("8 the add-on crashed", "upstream", (
        "The feed crashed after starting",
        "The crash is in the",
        "The 64-bit helper lost its graphics device",
        "its neural add-on never created the DLSS 5 feature",
        "the add-on crashed creating the feature",
        "It started, then the feed stopped",
    )),
    ("9 the model refused", "upstream", (
        "OptiScaler loaded, but the model refused or failed",
        "Neural rendering stopped after it started",
        "The neural feature was refused by NGX",
    )),
    ("10 the driver's runtime", "NVIDIA", (
        "Driver 616.64+ faults",
        "The driver has no DLSS 5 entry point",
        "Every DLSS evaluate faults",
    )),
    ("0 working", "-", ("Working",)),
    # Last: an appended "...beside ours too" keeps the stage its own verdict
    # names; only the replaced "we cannot see" verdicts land here (#250).
    ("11 a second DLSS hook beside ours", "the person", (
        "Another DLSS hook was loaded beside ours",
    )),
)

# The stages where the route itself is what failed, so a different route
# can rescue the game. Not the ones the person has to act on (switch it on,
# remove the other hook), not the ones where we cannot tell, and not an
# install that never finished - that is fixed by installing again.
ROUTE_FAILED = ("3 nothing of ours loaded", "4 the game refused the hook",
                "5 loaded, the feed never got going", "8 the add-on crashed",
                "9 the model refused")

# Verdicts inside those stages that another route does not change: a
# launcher is fixed by picking the executable that draws, and a feed that
# "did not get far enough to tell" is a cannot-tell answer - #142's shape,
# a game closed in the middle of its shader compile (9 of 84 reports).
NOT_A_ROUTE = (
    "The install went beside a launcher",
    "Inconclusive - the feed did not get far enough",
)

_SECOND_HOOK = "another dlss hook was loaded beside ours"


def stage(verdict: str) -> tuple[str, str]:
    """(stage, who can fix it) for a printed verdict."""
    if not str(verdict or "").strip():
        return "no verdict", "-"
    low = str(verdict).lower()
    # The hook's own verdict first: it names the other hook's modules, and a
    # name that happens to hold a fragment of an earlier stage re-staged it.
    if low.startswith(_SECOND_HOOK):
        return CHAIN[-1][0], CHAIN[-1][1]
    # An appended "Another DLSS hook ... too (names)" keeps the stage of the
    # verdict in front of it; the names in it are not read.
    low = low.split(" " + _SECOND_HOOK)[0]
    for name, who, starts in CHAIN:
        for s in starts:
            if s.lower() in low:
                return name, who
    return "unmapped: " + str(verdict)[:40], "?"


def route_failed(verdict: str) -> bool:
    """Is this a verdict another route could change?

    Not while a second DLSS hook sits beside ours: switching routes leaves
    it there, and the person is told to test without it first."""
    low = str(verdict or "").lower()
    if _SECOND_HOOK in low or any(s.lower() in low for s in NOT_A_ROUTE):
        return False
    return stage(verdict)[0] in ROUTE_FAILED


def why_next(verdict: str, route: str) -> str:
    """One short line for a toast: what went wrong with this route."""
    name = stage(verdict)[0]
    return {
        "3 nothing of ours loaded": f"nothing of the {route} route loaded in the game",
        "4 the game refused the hook": f"the game refused the {route} hook",
        "5 loaded, the feed never got going": f"the {route} route did not get the neural pass going",
        "8 the add-on crashed": f"the {route} add-on crashed",
        "9 the model refused": f"the model refused on the {route} route",
    }.get(name, "")
