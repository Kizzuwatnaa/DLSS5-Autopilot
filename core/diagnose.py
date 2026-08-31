"""Reading dlss5-feed.log back and saying, in plain words, what happened.

Installing is the easy half. The hard half is that the DLSS feature quietly
fails to create in a lot of games and all the user sees is "nothing changed".
The add-on writes a detailed log; this turns it into an answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LOG = "dlss5-feed.log"
RESHADE_LOG = "ReShade.log"

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


def analyse(install_dir: Path) -> Report:
    """Read the logs next to the game and explain the outcome."""
    rep = Report()
    log = install_dir / LOG
    text = _tail(log)

    if not text.strip():
        rep.add(BAD, "No dlss5-feed.log in the game folder.",
                "The add-on never loaded. Either the game has not been run "
                "since installing, or ReShade is not being loaded at all - "
                "check that the proxy DLL sits next to the executable the "
                "game actually launches.")
        rep.verdict = "Not run yet, or ReShade never loaded."
        return rep

    rep.ran = True
    try:
        rep.log_time = datetime.fromtimestamp(log.stat().st_mtime)\
            .strftime("%d %b %H:%M")
    except OSError:
        pass

    # --- which add-on build answered -----------------------------------
    m = re.search(r"DLSS 5 add-on: \S+ (v[\d.]+) -- (\S+)", text)
    if m:
        engine = m.group(2)
        rep.add(INFO, f"DLSS 5 add-on {m.group(1)}, {engine} engine",
                "The 'classic' engine is an older add-on build; 'v45+' is the "
                "newer one. Feeder behaviour differs between them."
                if engine == "classic" else "")

    # --- shaders -------------------------------------------------------
    if "DLSS5_Feed.fx is not loaded" in text and "technique found" not in text:
        rep.add(BAD, "DLSS5_Feed.fx never loaded.",
                "ReShade could not compile or find the effect. Open the "
                "ReShade overlay and look for a compile error, and check that "
                "reshade-shaders\\Shaders contains DLSS5_Feed.fx.")
    elif "technique found" in text:
        rep.add(OK, "DLSS5_Feed.fx loaded and its textures were found.")

    # The add-on logs this line several times: once early, before ReShade has
    # compiled the effects (everything reads as missing), then again once the
    # runtime settles. Only the LAST one describes the real state.
    provider_lines = re.findall(
        r"DLSS5_MV_PROVIDER=(\d+) \(([^)]+)\) -> (\S+) \(([^)]+)\)", text)
    if provider_lines:
        _num, name, _tech, state = provider_lines[-1]
        if "enabled" in state:
            rep.add(OK, f"Motion vectors: {name} is enabled.")
        elif "not installed" in state:
            rep.add(BAD, f"Motion vectors: {name} is not installed.",
                    "Without a provider the feed has nothing to work with.")
        else:
            rep.add(BAD, f"Motion vectors: {name} is {state}.",
                    "Enable that technique in the ReShade overlay and make sure "
                    "it sits ABOVE 'DLSS 5 Feed' in the list.")

    # --- NGX session ----------------------------------------------------
    if "NVSDK_NGX_D3D12_Init -> 0x00000001" in text or \
       "NVSDK_NGX_D3D11_Init -> 0x00000001" in text:
        rep.add(OK, "NGX initialised successfully.")
    if "SuperSampling.Available=1" in text:
        rep.add(OK, "The driver reports DLSS as available.")
    elif "SuperSampling.Available=0" in text:
        rep.add(BAD, "The driver reports DLSS as NOT available.",
                "Usually a driver too old for this NGX runtime, or the game is "
                "running on the wrong GPU.")

    # --- the decisive part ----------------------------------------------
    crash = re.search(r"CreateFeature raised exception (0x[0-9A-Fa-f]+)", text)
    ready = re.search(r"feature ready:?\s*(\d+x\d+)?\s*(\w+)?", text)
    delivered = re.findall(r"frame (\d+) delivered", text)

    if crash:
        rep.add(BAD, f"Creating the DLSS feature crashed ({crash.group(1)}).",
                "This is the add-on and the nvngx_dlssnr build disagreeing. It "
                "is not something the install did wrong. Try a different "
                "combination: another renodx build, or another nvngx_dlssnr "
                "version that still supports your card.")
        rep.verdict = "DLSS never started - the add-on crashed creating the feature."
    elif delivered:
        rep.add(OK, f"Frames are being processed ({len(delivered)} 'delivered' "
                    f"lines, last was frame {delivered[-1]}).")
        rep.verdict = "Working."
    elif ready:
        rep.add(WARN, "The feature was created but no frames were delivered.",
                "Neural rendering may still be switched off in the DLSS 5 panel.")
        rep.verdict = "Set up correctly, but not switched on yet."
    elif "failure: resource build" in text:
        rep.add(BAD, "Building the feed resources failed.")
        rep.verdict = "DLSS never started."
    else:
        rep.verdict = "Inconclusive - the feed did not get far enough to tell."

    # --- motion vector sanity -------------------------------------------
    probes = re.findall(r"MV probe[^\n]*?(\d+)% non-zero", text)
    if probes:
        last = int(probes[-1])
        if last == 0:
            rep.add(WARN, "Motion vectors measured 0% non-zero.",
                    "If you were moving when this was logged, the provider is "
                    "not producing vectors and you will see smearing.")
        else:
            rep.add(OK, f"Motion vectors look alive ({last}% non-zero).")

    # --- resolution churn -------------------------------------------------
    builds = re.findall(r"building: (\d+x\d+)", text)
    if len(set(builds)) > 1:
        rep.add(WARN, f"The feature was rebuilt at {len(set(builds))} different "
                      f"resolutions ({', '.join(sorted(set(builds)))}).",
                "Changing resolution while neural rendering is on forces a "
                "rebuild and is a common cause of freezes. Set the resolution "
                "first, then turn it on.")

    # --- ReShade side ------------------------------------------------------
    rtext = _tail(install_dir / RESHADE_LOG, 200_000)
    if "untested build" in rtext:
        rep.add(WARN, "The add-on flagged your nvngx_dlssnr as an untested build.",
                "It still accepted it, but failures may be specific to that file.")
    for m in re.finditer(r"Failed to (compile|load) ([^\n]{0,90})", rtext):
        rep.add(BAD, f"ReShade failed to {m.group(1)}: {m.group(2).strip()}")

    if not rep.verdict:
        rep.verdict = "Inconclusive."
    return rep
