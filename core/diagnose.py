"""Reading the logs back and saying, in plain words, what happened.

Installing is the easy half. The hard half is that DLSS quietly fails to start
in a lot of games and all the user sees is "nothing changed". The add-ons
write detailed logs; this turns them into an answer.

Which log matters depends on the route:

    feeder   dlss5-feed.log next to the game, plus host64/dlss5-feed-host.log
             on the 32-bit path where the real NGX work happens
    bridge   dlss5-bridge writes into ReShade.log
    native   nothing but ReShade.log - the add-on hooks the game's own calls

A log older than the install is from a previous setup and is ignored rather
than reported as if it described the current one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FEED_LOG = "dlss5-feed.log"
HOST_LOG = Path("host64") / "dlss5-feed-host.log"
RESHADE_LOG = "ReShade.log"
MANIFEST = "dlss5-autopilot.json"

OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""


@dataclass
class Report:
    ran: bool = False
    verdict: str = ""
    route: str = ""
    findings: list[Finding] = field(default_factory=list)
    log_time: str = ""

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.findings.append(Finding(level, title, detail))


def _tail(path: Path, limit: int = 400_000) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read().decode("utf8", "replace")
    except OSError:
        return ""


def _installed_at(install_dir: Path) -> float:
    try:
        return (install_dir / MANIFEST).stat().st_mtime
    except OSError:
        return 0.0


def _route(install_dir: Path) -> str:
    try:
        data = json.loads((install_dir / MANIFEST).read_text(encoding="utf8"))
        return data.get("path") or ""
    except Exception:
        return ""


def _fresh(path: Path, since: float) -> bool:
    """Is this log from the current install rather than an earlier one?"""
    try:
        return path.is_file() and path.stat().st_mtime >= since - 60
    except OSError:
        return False


def analyse(install_dir: Path) -> Report:
    """Read whatever logs apply to this install and explain the outcome."""
    rep = Report()
    since = _installed_at(install_dir)
    rep.route = _route(install_dir)

    feed = install_dir / FEED_LOG
    host = install_dir / HOST_LOG
    reshade = install_dir / RESHADE_LOG

    # Which log can possibly describe THIS install is decided by the route,
    # not by timestamps: reinstalling bumps the manifest and would make every
    # existing log look stale, while a feeder log left behind after switching
    # to native would otherwise be reported as if it were current.
    feeder_route = rep.route not in ("native", "bridge")
    if feeder_route:
        text = _tail(feed)
        htext = _tail(host, 150_000)
    else:
        text = htext = ""
        if feed.is_file():
            rep.add(INFO, "An old dlss5-feed.log is still in the folder.",
                    "It is from a previous feeder install and says nothing "
                    "about this one, so it is ignored.")
    rtext = _tail(reshade, 250_000)

    if text and since and not _fresh(feed, since):
        rep.add(WARN, "The log predates the current install.",
                "You have reinstalled since this was written, so it may "
                "describe the previous setup. Play once and check again.")

    for p, t in ((feed, text), (reshade, rtext), (host, htext)):
        if t:
            try:
                rep.log_time = datetime.fromtimestamp(p.stat().st_mtime)\
                    .strftime("%d %b %H:%M")
            except OSError:
                pass
            break

    if not (text or rtext or htext):
        rep.add(BAD, "No log from this install yet.",
                "Either the game has not been run since installing, or "
                "ReShade is not loading at all - check that the proxy DLL sits "
                "next to the executable the game actually launches.")
        rep.verdict = "Not run yet, or ReShade never loaded."
        return rep

    rep.ran = True

    # --- what loaded ----------------------------------------------------
    if rtext:
        for m in re.finditer(r'Registered add-on "([^"]+)" v(\S+)', rtext):
            rep.add(OK, f"ReShade loaded add-on: {m.group(1)} {m.group(2)}")
        if "Registered add-on" not in rtext:
            rep.add(BAD, "ReShade loaded no add-ons.",
                    "Add-on support requires the ReShade build WITH add-ons, "
                    "and AddonPath must point at the game folder.")
        for m in re.finditer(r"Failed to (compile|load) ([^\n]{0,90})", rtext):
            rep.add(BAD, f"ReShade failed to {m.group(1)}: {m.group(2).strip()}")
        if "untested build" in rtext:
            rep.add(WARN, "The add-on flagged your nvngx_dlssnr as an untested build.",
                    "It accepted it, but failures may be specific to that file.")

    # --- the feeder path -------------------------------------------------
    if text:
        m = re.search(r"DLSS 5 add-on: \S+ (v[\d.]+) -- (\S+)", text)
        if m and m.group(2) == "classic":
            rep.add(INFO, f"DLSS 5 add-on {m.group(1)}, classic engine",
                    "An older add-on build. Feeder behaviour differs from the "
                    "newer 'v45+' engine.")

        if "is not loaded" in text and "technique found" not in text:
            rep.add(BAD, "DLSS5_Feed.fx never loaded.",
                    "Check the ReShade overlay for a compile error and that "
                    "reshade-shaders\\Shaders holds DLSS5_Feed.fx.")
        elif "technique found" in text:
            rep.add(OK, "DLSS5_Feed.fx loaded and its textures were found.")

        # This line is logged several times; only the last describes reality.
        prov = re.findall(
            r"DLSS5_MV_PROVIDER=(\d+) \(([^)]+)\) -> (\S+) \(([^)]+)\)", text)
        if prov:
            _n, name, _t, state = prov[-1]
            if "enabled" in state:
                rep.add(OK, f"Motion vectors: {name} is enabled.")
            elif "not installed" in state:
                rep.add(BAD, f"Motion vectors: {name} is not installed.")
            else:
                rep.add(BAD, f"Motion vectors: {name} is {state}.",
                        "Enable that technique in the ReShade overlay, ABOVE "
                        "'DLSS 5 Feed' in the list.")

        probes = re.findall(r"MV probe[^\n]*?(\d+)% non-zero", text)
        if probes:
            last = int(probes[-1])
            if last == 0:
                rep.add(WARN, "Motion vectors measured 0% non-zero.",
                        "If you were moving, the provider is producing nothing "
                        "and you will see smearing.")
            else:
                rep.add(OK, f"Motion vectors look alive ({last}% non-zero).")

        if "host spawned" in text:
            rep.add(OK, "The 32-bit helper process started.")
            if "host connected" in text:
                rep.add(OK, "The game and the helper are talking.")
            else:
                rep.add(BAD, "The helper started but never connected.")
            rep.add(INFO, "On 32-bit the DLSS 5 panel is in the separate "
                          "\"32-bit DLSS 5 Feeder\" window, not the game's "
                          "ReShade overlay.")

        builds = re.findall(r"building: (\d+x\d+)", text)
        if len(set(builds)) > 1:
            rep.add(WARN, f"Rebuilt at {len(set(builds))} different resolutions "
                          f"({', '.join(sorted(set(builds)))}).",
                    "Changing resolution while neural rendering is on forces a "
                    "rebuild and is a common cause of freezes.")

    # --- the decisive part, from whichever log has it --------------------
    joined = "\n".join(x for x in (text, htext, rtext) if x)
    crash = re.search(r"CreateFeature raised exception (0x[0-9A-Fa-f]+)", joined)
    ready = re.search(r"feature ready[:\s]", joined)
    delivered = re.findall(r"frame (\d+) (?:delivered|evaluated)", joined)
    perf = re.search(r"(\d+) frames: feed CPU ([\d.]+) ms/frame[^\n]*?"
                     r"([\d.]+) fps", joined)

    if "NVSDK_NGX" in joined and "-> 0x00000001" in joined:
        rep.add(OK, "NGX initialised successfully.")
    if "SuperSampling.Available=1" in joined:
        rep.add(OK, "The driver reports DLSS as available.")
    elif "SuperSampling.Available=0" in joined:
        rep.add(BAD, "The driver reports DLSS as NOT available.",
                "Usually a driver too old for this NGX runtime, or the game "
                "running on the wrong GPU.")

    if crash:
        rep.add(BAD, f"Creating the DLSS feature crashed ({crash.group(1)}).",
                "The add-on and the nvngx_dlssnr build disagree. Nothing the "
                "install did wrong - try another combination: a different "
                "renodx build, or another nvngx_dlssnr that still supports "
                "your card.")
        rep.verdict = "DLSS never started - the add-on crashed creating the feature."
    elif delivered:
        rep.add(OK, f"Frames are being processed ({len(delivered)} logged, "
                    f"last was frame {delivered[-1]}).")
        if perf:
            rep.add(INFO, f"{perf.group(1)} frames at {perf.group(3)} fps, "
                          f"{perf.group(2)} ms/frame spent on the feed.")
        rep.verdict = "Working."
    elif ready:
        rep.add(WARN, "The feature was created but no frames were delivered.",
                "Neural rendering may still be switched off in the DLSS 5 panel.")
        rep.verdict = "Set up correctly, but not switched on yet."
    elif "failure: resource build" in joined:
        rep.add(BAD, "Building the feed resources failed.")
        rep.verdict = "DLSS never started."
    elif rep.route in ("native", "bridge"):
        rep.add(INFO, "This route leaves no frame log of its own.",
                "Open the ReShade overlay and check the DLSS 5 Neural "
                "Rendering panel: it shows the live state and whether it is "
                "switched on.")
        rep.verdict = ("Add-ons loaded. Confirm in the DLSS 5 panel - this "
                       "route does not log frames.")
    else:
        rep.verdict = "Inconclusive - the feed did not get far enough to tell."

    return rep
