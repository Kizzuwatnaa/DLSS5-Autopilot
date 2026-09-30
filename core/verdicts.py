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
        # #364: the manifest said OptiScaler was installed and no proxy was
        # ever beside the executable - the package that was unpacked into
        # the folder held no OptiScaler.dll at all. Ours, and stage 1.
        # LIMIT: the first of these two is also printed when a proxy WAS
        # written and something removed it afterwards, which is stage 2's
        # shape (the person's antivirus). It is counted here because an
        # install of ours that wrote nothing looks exactly the same from
        # the folder, and blaming a virus scanner on a guess is the one
        # answer this project does not give.
        "OptiScaler is not in the game folder",
        "The release installed here was not an OptiScaler build",
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
        # #328: "The game loads d3d9.dll from another folder - ours is never reached."
        "from another folder - ours is never",
        "Not run yet, or the Remix runtime never",
        "It looks as though it ran and nothing this install wrote was loaded",
        "and no log was written - ReShade's Vulkan layer is not reaching the game",
        "and no log was written - the proxy is reached and ReShade is not",
        # 2.0.5: how Windows started the game kept the per-user layer out
        # (#400, #348, #238), and an engine a DXGI proxy never reaches (#403)
        "runs as administrator, so ReShade's Vulkan layer is skipped",
        "not DXGI - 'full rescan', then install again",
        # #479: the game drew with D3D9 on an install set up for D3D11/12
        "ReShade found a Direct3D 9 device, not the D3D11/12",
        "on a renderer this tool cannot reach",
        # the watcher saw another executable run (was unmapped until 2.0.8)
        ", and the install went beside",
        # 2.0.8, #577: the OptiScaler route's proxy was never loaded
        "was not loaded in it - try another 'loads as' name",
    )),
    ("4 the game refused the hook", "upstream", (
        "The game refused OptiScaler's swapchain",
        "The game refused the swapchain",
        "The game's DLSS call was never hooked",
    )),
    ("5 loaded, the feed never got going", "upstream", (
        # 2.0.7: DLSS was called and the bridge's log has no frame
        "The game called DLSS and the bridge delivered no frame",
        "Inconclusive - the feed did not get far enough",
        "Inconclusive - the add-on attached but built nothing",
        "It started and closed during start-up",
        "Remix ran; the neural pass was never even attempted",
        "Remix ran, but the DLSS 5 snippet never started",
        # 2.0.8: lunks' runtime says it skipped the pass, and why
        "Remix ran; the neural pass skipped its frames",
        "No Remix runtime",
        "The Remix runtime here has no neural pass",
        "The add-on runs but never produces a frame",
    )),
    ("6 set up, not switched on", "the person", (
        "Set up correctly, but not switched on yet",
        "Loaded and set up; no neural frame yet",
        # #390: switched on and hooked, and the game made no DLSS call
        # ("The game never called DLSS" before 2.0.6 - see RETIRED)
        "The game made no D3D12 DLSS call",
        # 2.0.7: the bridge attached and its log has no frame line
        "The bridge attached and delivered no frame - turn DLSS on",
        # #127: the bridge replaced the settings the install wrote
        "The bridge's substitute is off",
        "OptiScaler loaded; neural rendering not switched on",
        "The add-ons are loaded and the neural pass is switched off",
        "ReShade never gave it an effect runtime",
        # 2.0.8: lunks' runtime logged "NVIDIA DLSS-NR inactive:"
        "Remix ran with the neural pass switched off",
    )),
    ("7 loaded, and we cannot see", "us", (
        # 2.0.8: the watcher saw OptiScaler's proxy loaded and no log came
        "and no log was written.",
        "Inconclusive - open the overlay",
        "Add-ons loaded. Confirm in",
        "Add-ons loaded and the switch is on",
        "Frames reach the 64-bit helper, and only its own log",
        # 2.0.7: the bridge's own log counts its frames; whether the neural
        # pass drew them is in no log (look-in-the-overlay class, #352)
        "; whether the neural pass drew them is in no log",
        "The bridge attached and delivered no frame from the driver's",
        "The bridge's log has nothing from this launch",
        # upstream: a CreateFeature/eval line and no heartbeat after it
        "DLSS was called, and no neural frame followed",
        # 2.0.8: lunks' runtime logs at info level, and rtx.conf can raise
        # the level above it - a silent log is not a failed pass. Two of the
        # four "failed" remix records (#268 #281) were this, shared as failed.
        "Inconclusive - the Remix log shows no neural frame",
        # The report's own correction when the person said the game closed
        # itself or never started (diagnose.answered, #412): the logs could
        # not see why, and what to take out first is the answer.
        "Neural rendering ran, then the game closed itself",
        "this time the game never started - see below",
        "The game closed itself and nothing here recorded why",
        "Windows recorded the game faulting - see below",
        "The game never started with this install in",
    )),
    ("8 the add-on crashed", "upstream", (
        "The feed crashed after starting",
        # #482: the feeder's own wait for its 64-bit helper ran out
        "The feed stopped waiting for the 64-bit helper",
        "The crash is in the",
        "The 64-bit helper lost its graphics device",
        "its neural add-on never created the DLSS 5 feature",
        "the add-on crashed creating the feature",
        "It started, then the feed stopped",
        # 2.0.7: the bridge's own "stopped: ..." line, or its crash handler
        "It started, then the bridge stopped",
        "The bridge stopped before it delivered a frame",
        "It started, then the game crashed - see below",
        "The game crashed before the bridge delivered a frame",
        # #420, #20: Streamline's crash handler wrote a dump on this route
        "The game crashed with OptiScaler loaded",
    )),
    ("9 the model refused", "upstream", (
        "OptiScaler loaded, but the model refused or failed",
        "Neural rendering stopped after it started",
        "The neural feature was refused by NGX",
        # 2.0.8: NGX's own result, from feeder 1.18.0-beta
        "DLSS could not start inside this game's process",
        "NGX refused to create DLSS",
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
        # #439: the bridge route's own add-on under its 1.0.x name
        "An older bridge add-on was loaded beside ours",
        # #348: a ReShade d3d9.dll found before DXVK's
        "ReShade loaded as the game's d3d9.dll in front of DXVK",
        # 2.0.8: the feeder's helper-mode add-on beside the in-process one
        "Two feeder add-ons in one folder",
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
    # How the game was started, and a game read as the wrong renderer: the
    # next route goes in through the same start and the same wrong reading.
    "runs as administrator, so ReShade's Vulkan layer is skipped",
    "not DXGI - 'full rescan', then install again",
    "on a renderer this tool cannot reach",
    # #479: a D3D9 device on an install set up for D3D11/12 - the next route
    # goes in with the same wrong API (gate 2.0.6)
    "ReShade found a Direct3D 9 device, not the D3D11/12",
    # The game ran from another executable or folder: the next route would
    # go in beside the same one that does not start (gate 2.0.8)
    ", and the install went beside",
    # A proxy name the game does not load: the fix is another name on the
    # same route (gate 2.0.8)
    "try another 'loads as' name",
)

_SECOND_HOOK = "another dlss hook was loaded beside ours"


# Verdicts an older build printed and this one does not. Shared results and
# reports written by those builds still carry them, so they keep their
# stage; CHAIN holds only what the tool prints today (the gate checks it).
RETIRED = (
    ("6 set up, not switched on", "The game never called DLSS"),     # 2.0.5, #390
)


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
    for name, s in RETIRED:
        if s.lower() in low:
            return name, next(w for n, w, _s in CHAIN if n == name)
    return "unmapped: " + str(verdict)[:40], "?"


def not_a_route(verdict: str) -> bool:
    """A verdict about how the game was set up or started, not about the
    route: another route goes in the same way and meets the same thing."""
    low = str(verdict or "").lower()
    return any(s.lower() in low for s in NOT_A_ROUTE)


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


# The stages where the tool cannot tell from the logs whether the person saw
# DLSS 5, so a shared result has to take the person's word for it. Before
# 2.0.5 "share the result" wrote "failed" for every one of these: a route
# that logs no frames (renodx, native, bridge) could never share a success,
# and #414's "WORKED FINE" went into the list as a failure - 27 of the first
# 230 shared results were such an unseen outcome filed as failed.
# Stage 6 too: "not switched on" / "the game never called DLSS" is what the
# logs said when the report was read, not what the person saw after they
# switched it on. #437 (2.0.5, RDR2) filed "never called DLSS" as a failure
# and its reporter wrote "it appeared to work in game".
UNSEEN = ("6 set up, not switched on", "7 loaded, and we cannot see",
          "11 a second DLSS hook beside ours")
_PERSON_SAID_FAILED = (
    "Neural rendering ran, then the game closed itself",
    "this time the game never started - see below",
    "The game closed itself and nothing here recorded why",
    "Windows recorded the game faulting - see below",
    "The game never started with this install in",
    # #348: a ReShade d3d9.dll in front of DXVK - the route's chain was not
    # what the game ran, whatever the picture showed (gate 2.0.6)
    "ReShade loaded as the game's d3d9.dll in front of DXVK",
)


def outcome(verdict: str) -> str | None:
    """"worked", "failed", or None when only the person can say.

    Read by 'share the result' before it writes a record, and by the
    workflow that adds the records up, so the two agree on which verdicts
    are not an answer."""
    name = stage(verdict)[0]
    if name == "0 working":
        return "worked"
    # Stage 7 for the backlog, but written from the person's own "it closed
    # itself" / "it never started" (diagnose.answered): that answer is the
    # outcome, and asking "did the picture show?" after it asks twice.
    low = str(verdict or "").lower()
    if any(s.lower() in low for s in _PERSON_SAID_FAILED):
        return "failed"
    if name in UNSEEN or name.startswith("unmapped") or name == "no verdict":
        return None
    return "failed"
