"""Reading the logs back and saying, in plain words, what happened.

Installing is the easy half. The hard half is that DLSS quietly fails to start
in a lot of games and all the user sees is "nothing changed". The add-ons
write detailed logs; this turns them into an answer.

Which log matters depends on the route:

    feeder   dlss5-feed.log next to the game, plus host64/dlss5-feed-host.log
             on the 32-bit path where the real NGX work happens
    bridge   dlss5-bridge writes into ReShade.log
    native   nothing but ReShade.log - the add-on hooks the game's own calls
    upstream the same: neural-upstream shows its state in its overlay tab
    standalone LOCALAPPDATA/RHI/Logs/standalone-dlssnr.log - outside the game
             folder, and ONE file for every game the add-on ever ran in, so
             only its last session is read

A log older than the install is from a previous setup and is ignored rather
than reported as if it described the current one.

When there is no log at all, the folder itself is the evidence: a proxy DLL
that has vanished says "antivirus", an untouched folder says "not started
yet". "Not run yet, or ReShade never loaded" told nobody anything, and a real
bug report arrived carrying exactly that and nothing else.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FEED_LOG = "dlss5-feed.log"
HOST_LOG = Path("host64") / "dlss5-feed-host.log"
RESHADE_LOG = "ReShade.log"
MANIFEST = "dlss5-autopilot.json"

OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"

# What neural-upstream registers itself as (NAME in its addon.cpp) and the
# overlay tab it draws. Matched loosely as well, in case a later build
# renames it - but never on "neural", which our own add-on's name contains.
UPSTREAM_ADDON_NAME = "DLSS5 NR Pre-Upscale"
UPSTREAM_PANEL = "'NR Pre-Upscale' tab (neural-upstream)"
NATIVE_ADDON_NAME = "DLSS 5 Neural Rendering"


def _upstream_named(name: str) -> bool:
    low = name.strip().lower()
    return (low == UPSTREAM_ADDON_NAME.lower() or "pre-upscale" in low
            or "upstream" in low)


# What kibblerz's standalone-dlssnr registers as. Its NAME export carries
# the build ("Standalone DLSS-NR + SR 1.7.17-early-proxy", read from the
# 1.7.17 binary), so the match is on the prefix. It logs outside the game
# folder, one file for every game; a session starts with "... attached;".
STANDALONE_ADDON_NAME = "Standalone DLSS-NR + SR"
STANDALONE_PANEL = "'Standalone DLSS-NR + SR' add-on tab"
STANDALONE_LOG = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
                  / "RHI" / "Logs" / "standalone-dlssnr.log")
_STANDALONE_SESSION = " attached; requested profile="
# The add-on's own words for the runtime set it loads privately being
# incomplete (README troubleshooting table, and the binary's strings).
_STANDALONE_NO_RUNTIME = "required private runtime dependency missing"


def _standalone_named(name: str) -> bool:
    low = name.strip().lower()
    return "standalone dlss-nr" in low or "standalone-dlssnr" in low

# The shaders the feed actually runs. ReShade compiles every .fx in the
# folder, and the lumenite pack ships a dozen the feed never uses; a compile
# error in one of those is noise, not a failure.
FEED_SHADERS = ("dlss5_feed.fx", "lumenite_kernel.fx", "lumenite_quantmotion.fx")

_DEPTH_HINT = (
    "In the ReShade overlay open the Add-ons tab and look at the depth "
    "buffer list: one has to be selected. If none is, or it switches when "
    "you change display mode, try 'Use aspect ratio heuristics' set to off "
    "there. Borderless, display scaling and an in-game render scale below "
    "100% are the usual reason the buffer stops matching.")

_COMPILER_FIX = (
    "The game ships its own d3dcompiler_47.dll and it predates Shader Model "
    "5.1, so the neural pass never compiles - frames still flow, nothing "
    "changes on screen. Rename that file to d3dcompiler_47.dll.dlss5-off so "
    "Windows uses the System32 copy; the tool's next install does this by "
    "itself.")


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


def _manifest(install_dir: Path) -> dict:
    try:
        data = json.loads((install_dir / MANIFEST).read_text(encoding="utf8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _route(install_dir: Path) -> str:
    return _manifest(install_dir).get("path") or ""


def _fresh(path: Path, since: float) -> bool:
    """Is this log from the current install rather than an earlier one?"""
    try:
        return path.is_file() and path.stat().st_mtime >= since - 60
    except OSError:
        return False


def _addons(man: dict) -> list[str]:
    """The add-on files the install recorded - the ones antivirus goes for."""
    return [f for f in man.get("files") or []
            if isinstance(f, str) and f.lower().endswith((".addon64", ".addon32"))]


def _missing_addons(install_dir: Path, man: dict) -> list[str]:
    return [f for f in _addons(man) if not (install_dir / f).is_file()]


OPTI_LOG = "OptiScaler.log"


def _opti_log(install_dir: Path) -> Path | None:
    """OptiScaler writes next to itself by default, or under Logs/."""
    cands = [install_dir / OPTI_LOG]
    try:
        cands += sorted((install_dir / "Logs").glob("*.log"),
                        key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return next((c for c in cands if c.is_file()), None)


# When the game has no DLSS and OptiScaler is meant to hook its FSR/XeSS
# calls instead, the log must show those calls arriving. OptiScaler's LOG_*
# macros prefix every line with the C++ function name (SysUtils.h:
# `spdlog::info(__FUNCTION__ " " msg)`), so the hook functions themselves
# are the evidence. Phrases and where they come from, all under
# _research/forkrepo/OptiScaler/:
#   "context created"     inputs/FSR2_Dx12.cpp:442 / FSR3_Dx12.cpp:293 /
#                         FfxApiExe_Dx12.cpp:96 - an FSR context created
#                         through the hook (the game's call reached us)
#   "hk_ffxFsr2" / "hk_ffxFsr3" / "hk_ffxCreateContext" / "hk_xess"
#                         the hooked entry points' own function names
#   "XeSS Version:"       proxies/XeSS_Proxy.h:909, once libxess is wrapped
#   "libxess.dll found"   Config.cpp:1703/1709 "libxess.dll found in memory"
#                         / "found in game folder"
# and the two lines that say the hook can never land:
#   "libxess.dll not found!"  Config.cpp:1705
#   "disabling FSR2 hooks!"   inputs/FSR2_Dx12.cpp:976 "Katana Engine
#                             exports detected, disabling FSR2 hooks!"
# "Trying to hook FSR2 methods" (FSR2_Dx12.cpp:969) is NOT evidence: it is
# logged on every start whenever EnableFsr2Inputs is on, hooked or not.
_INPUT_SEEN = ("context created", "hk_ffxFsr2", "hk_ffxFsr3",
               "hk_ffxCreateContext", "hk_xess", "XeSS Version:",
               "libxess.dll found")
_INPUT_NEVER = ("libxess.dll not found!", "disabling FSR2 hooks!")


def _check_inputs(text: str, upscaler: str, rep: "Report") -> None:
    """Did the game's FSR/XeSS calls ever reach OptiScaler?"""
    if not upscaler:
        return
    name = "FSR" if upscaler == "fsr" else "XeSS"
    dead = [k for k in _INPUT_NEVER if k in text]
    if dead:
        rep.add(BAD, f"OptiScaler never saw the game's {name} calls.",
                f"The log says '{dead[0]}' - the game loads no "
                f"{name} runtime OptiScaler can hook, so it may link its own "
                f"statically. Try the feeder route.")
    elif not any(k in text for k in _INPUT_SEEN):
        rep.add(WARN, f"OptiScaler never saw the game's {name} calls.",
                f"No {name} context was created through OptiScaler. Make "
                f"sure {name} is selected in the game's own menu; if it is, "
                f"the game may load its own {name} statically and there is "
                f"nothing to hook - try the feeder route.")


def _analyse_optiscaler(install_dir: Path, rep: "Report", since: float,
                        man: dict | None = None) -> "Report":
    """The OptiScaler route has no ReShade: its own log says everything.

    The fork's DLSS-NR lines are unambiguous - "running at WxH" is success,
    "create failed" / "unavailable" / "did not run" name the reason. With an
    upscaler recorded in the manifest the input hook has to show up too.
    """
    p = _opti_log(install_dir)
    text = _tail(p) if p else ""
    if not text:
        rep.add(BAD, "No OptiScaler log from this install yet.",
                "Either the game has not been run since installing, or "
                "OptiScaler is not loading at all - check that the proxy DLL "
                "sits next to the executable the game actually launches, and "
                "that antivirus did not quarantine it.")
        rep.verdict = "Not run yet, or OptiScaler never loaded."
        return rep
    rep.ran = True
    try:
        rep.log_time = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %H:%M")
    except OSError:
        pass
    if since and not _fresh(p, since):
        rep.add(WARN, "The log predates the current install.",
                "Play once and check again.")
    _check_inputs(text, str((man or {}).get("upscaler") or ""), rep)
    lines = text.splitlines()
    nr = [ln for ln in lines if "DLSS-NR" in ln or "dlssnr" in ln.lower()]
    running = [ln for ln in nr if "running at" in ln]
    failed = [ln for ln in nr if any(k in ln for k in (
        "create failed", "unavailable", "did not run", "not found beside",
        "would not load", "disabling for this session", "refused"))]
    if "forwarder loaded" in text:
        rep.add(OK, "OptiScaler loaded and found the neural-rendering forwarder.")
    if running:
        rep.add(OK, "Neural rendering is running.", running[-1].strip()[-160:])
        rep.verdict = "Working."
    elif failed:
        rep.add(BAD, "Neural rendering did not start.", failed[-1].strip()[-220:])
        if "refuse" in failed[-1] or "unavailable" in failed[-1]:
            rep.add(INFO, "OptiScaler needs driver 616.56 or newer, and a "
                          "nvngx_dlssnr build for your card (the tool picks "
                          "one). If it keeps refusing, the native or "
                          "renodx-dlss route is one click away.")
        rep.verdict = "OptiScaler loaded, but the model refused or failed."
    elif nr:
        rep.add(WARN, "OptiScaler mentions neural rendering but never reports "
                      "it running.", nr[-1].strip()[-160:])
        rep.verdict = "Inconclusive - open the overlay (Insert) and read the "\
                      "status under the Neural Rendering checkbox."
    else:
        rep.add(WARN, "OptiScaler ran, but neural rendering was never asked for.",
                "Press Insert in game and tick Neural Rendering; the tool "
                "writes Enabled=true, but a hand-edited OptiScaler.ini can "
                "override it.")
        rep.verdict = "OptiScaler loaded; neural rendering not switched on."
    return rep


def _explain_no_log(install_dir: Path, man: dict, rep: Report,
                    stale_reshade: bool) -> Report:
    """No current log: read the folder instead and name the likeliest cause.

    The order matters. A missing proxy DLL or add-on explains everything
    downstream, so it wins; a folder that is intact and has no ReShade.log at
    all means nothing has loaded ReShade since the install, and the hints go
    to why that can be; a ReShade.log older than the manifest means the
    install came after the last run.
    """
    rep.ran = False
    proxy = man.get("proxy") or ""
    exe = man.get("exe") or "the game's executable"
    app = "app" if man.get("kind") == "video" else "game"
    missing = _missing_addons(install_dir, man)

    if proxy and not (install_dir / proxy).is_file():
        rep.add(BAD, f"ReShade's {proxy} is gone from the folder.",
                "The install wrote it and it is no longer there: antivirus "
                "quarantined it, or the game verified its files and removed "
                "it. Restore it from quarantine (and exclude the folder), "
                "then install again.")
        rep.verdict = f"ReShade's {proxy} is missing from the folder - reinstall."
        return rep

    if missing:
        rep.add(BAD, f"Add-on missing from the folder: {', '.join(missing)}.",
                "The install wrote it and it is no longer there - almost "
                "always antivirus quarantine. Restore it, exclude the folder, "
                "and install again.")
        rep.verdict = "An add-on was quarantined - reinstall."
        return rep

    if stale_reshade:
        rep.add(WARN, "ReShade.log is older than the install.",
                f"The {app} was last run before this install, so nothing "
                f"has loaded the new files yet. Play once and check again.")
        rep.verdict = "Installed after the last run - play once and check again."
        return rep

    rep.add(WARN, f"The {app} has not been started since the install.",
            "ReShade writes ReShade.log the moment it loads, and there is "
            "none in the folder. All the files are still in place.")
    rep.add(INFO, f"If you DID start it, it launches something other than "
                  f"{exe}.",
            "A launcher or a different executable in another folder does not "
            "pick up the files here. Point the tool at the folder holding the "
            "executable that actually runs.")
    if proxy:
        alt = "d3d11.dll" if proxy.lower() == "dxgi.dll" else "dxgi.dll"
        rep.add(INFO, f"Or the {app} ignores {proxy}.",
                f"Some load the graphics DLLs in a way that skips {proxy}. "
                f"Try the {alt} proxy name in the settings and install again.")
    rep.verdict = f"Not started since the install - run the {app} once, then check again."
    return rep


def _shader_failures(rtext: str, provider_tech: str, rep: Report) -> None:
    """ReShade's "Failed to compile/load" lines, sorted by whether they matter.

    ReShade compiles every .fx it finds. The feed needs three of them plus
    whichever provides motion vectors; a failure anywhere else is reported
    once, as information, so a broken lumenite_RTAO.fx does not read as a
    broken install.
    """
    essential = set(FEED_SHADERS)
    if provider_tech:
        essential.add(provider_tech.lower() + ".fx")
    others: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"Failed to (compile|load) ([^\n]{0,200})", rtext):
        what = m.group(2).strip()
        fm = re.search(r"([^\\/'\"]+\.(?:fxh?|addon64|addon32|dll))\b", what)
        name = fm.group(1) if fm else what[:80]
        key = (m.group(1), name.lower())
        if key in seen:
            continue
        seen.add(key)
        if name.lower().endswith((".fx", ".fxh")) and name.lower() not in essential:
            others.append(name)
            continue
        rep.add(BAD, f"ReShade failed to {m.group(1)}: {name}")
    if others:
        rep.add(INFO, f"{len(others)} other shader{'s' if len(others) != 1 else ''} "
                      f"failed to compile - not used by the feed, ignore.",
                ", ".join(others))


def analyse(install_dir: Path) -> Report:
    """Read whatever logs apply to this install and explain the outcome."""
    rep = Report()
    since = _installed_at(install_dir)
    man = _manifest(install_dir)
    rep.route = man.get("path") or ""
    if rep.route == "optiscaler":
        return _analyse_optiscaler(install_dir, rep, since, man)

    feed = install_dir / FEED_LOG
    host = install_dir / HOST_LOG
    reshade = install_dir / RESHADE_LOG

    # Which log can possibly describe THIS install is decided by the route,
    # not by timestamps: reinstalling bumps the manifest and would make every
    # existing log look stale, while a feeder log left behind after switching
    # to native would otherwise be reported as if it were current.
    feeder_route = rep.route not in ("native", "bridge", "renodx", "upstream",
                                     "standalone")
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

    # A ReShade.log from before the install, with no newer log beside it,
    # describes the previous setup - it is not evidence that this one ran.
    stale_reshade = bool(rtext) and bool(since) and not _fresh(reshade, since)
    if stale_reshade and not (text or htext):
        rtext = ""

    for p, t in ((feed, text), (reshade, rtext), (host, htext)):
        if t:
            try:
                rep.log_time = datetime.fromtimestamp(p.stat().st_mtime)\
                    .strftime("%d %b %H:%M")
            except OSError:
                pass
            break

    # Another NGX hook in the folder is a conflict whatever the logs say.
    try:
        from . import installer as _inst
        hooks = _inst.other_ngx_hooks(install_dir, rep.route) \
            if rep.route != "optiscaler" \
            else [n for n in _inst.other_ngx_hooks(install_dir)
                  if n.lower() not in ("optiscaler.ini", "nvngx.dll_dlssnr.dll")]
    except Exception:
        hooks = []
    if hooks:
        rep.add(WARN, "Another DLSS hook shares this folder: " + ", ".join(hooks[:5]),
                "OptiScaler, a frame-gen unlocker or another RenoDX build "
                "rewrites the same NGX calls as the DLSS 5 add-on. Flicker "
                "and greyed-out frame-gen multipliers are the usual result. "
                "Try one at a time.")

    if not (text or rtext or htext):
        return _explain_no_log(install_dir, man, rep, stale_reshade)

    rep.ran = True

    # Files that went missing after the install are worth saying even when
    # a log exists: the log is from before the quarantine.
    for f in _missing_addons(install_dir, man):
        rep.add(BAD, f"Add-on missing from the folder: {f}.",
                "It was installed and is no longer there - antivirus "
                "quarantine, most likely. Restore it and install again.")

    # The motion-vector provider is named several times; the last is real.
    prov = re.findall(
        r"DLSS5_MV_PROVIDER=(\d+) \(([^)]+)\) -> (\S+) \(([^)]+)\)", text) if text else []
    provider_tech = prov[-1][2] if prov and prov[-1][2] != "none" else ""

    # ReShade attaching to a D3D9 device while the install is for DXGI means
    # the app renders with D3D9 here: a video player on EVR, or a game whose
    # settings put it in D3D9 mode. The feed has nothing to hook.
    d3d9_only = bool(rtext) and "Direct3DCreate9" in rtext \
        and "CreateSwapChain" not in rtext

    # --- what loaded ----------------------------------------------------
    if rtext:
        loaded = []
        for m in re.finditer(r'Registered add-on "([^"]+)" v(\S+)', rtext):
            rep.add(OK, f"ReShade loaded add-on: {m.group(1)} {m.group(2)}")
            loaded.append(m.group(1))

        # ReShade loads every .addon64 in the folder. The feeder and the
        # bridge each establish a DLSS contract of their own, so both at once
        # is not a slow path - it is two things fighting, and the game can die
        # before it ever creates a swapchain.
        # Did OUR add-on load? Everything downstream assumes it did. On the
        # renodx route it is ShortFuse's "RenoDX DLSS"; on the others the
        # "DLSS 5 Neural Rendering" add-on. A folder full of other RenoDX
        # add-ons (an HDR mod, another DLSS build) shows up here as a list
        # of things that loaded while ours is missing - and the person reads
        # "add-ons loaded" as "working" (Cyberpunk 2077, issue #3).
        if rep.route == "renodx":
            want = "RenoDX DLSS"
        elif rep.route == "upstream":
            want = UPSTREAM_ADDON_NAME
        elif rep.route == "standalone":
            want = STANDALONE_ADDON_NAME
        else:
            want = NATIVE_ADDON_NAME
        if rep.route == "upstream":
            ours = [n for n in loaded if _upstream_named(n)]
        elif rep.route == "standalone":
            ours = [n for n in loaded if _standalone_named(n)]
        else:
            ours = [n for n in loaded if n.strip().lower() == want.lower()]
        # The feed log names the add-on it found; that counts as loaded too
        # (ReShade.log can be truncated to the tail that fits).
        if not ours and "DLSS 5 add-on: renodx" in (text or ""):
            ours = [want]
        others = [n for n in loaded if n not in ours and "Feed" not in n
                  and "Bridge" not in n]
        # With the feed loaded, the feed's own log is the judge of the add-on
        # (it names it, or says it is missing); this check is for the routes
        # where nothing else would notice.
        if loaded and not ours and rep.route != "optiscaler"                 and not any("Feed" in n for n in loaded):
            rep.add(BAD, f"The '{want}' add-on did not load.",
                    "ReShade registered " + (", ".join(others) if others else
                    "nothing else") + " but not the DLSS 5 add-on this route "
                    "needs. Check the .addon64 is still in the folder "
                    "(antivirus), then reinstall.")
        if ours and any(n.strip().lower() == "renodx dlss" for n in others):
            rep.add(BAD, "Two DLSS add-ons are loaded: ours and ShortFuse's "
                         "renodx-dlss.",
                    "Both hook the same NGX calls. Keep one: uninstall here, "
                    "delete the other .addon64, install again.")
        elif rep.route == "upstream" and any(
                n.strip().lower() == NATIVE_ADDON_NAME.lower() for n in others):
            rep.add(BAD, "Two NGX hooks are loaded: neural-upstream and the "
                         "renodx-dlss5 add-on.",
                    "neural-upstream runs the network itself; renodx-dlss5 "
                    "hooks the same EvaluateFeature call, so both rewrite it. "
                    "Install this route again - it removes "
                    "renodx-dlss5.addon64 - or switch to the native route.")
        elif rep.route != "upstream" and any(_upstream_named(n) for n in others):
            rep.add(BAD, "Two NGX hooks are loaded: the DLSS 5 add-on and "
                         "neural-upstream.",
                    "nvngx.dll.addon64 belongs to the neural-upstream route. "
                    "Install this route again - it moves that add-on aside - "
                    "or switch to the neural-upstream route.")
        elif rep.route == "standalone" and any(
                n.strip().lower() == NATIVE_ADDON_NAME.lower() for n in others):
            rep.add(BAD, "Two add-ons process the frame: standalone-dlssnr and "
                         "the renodx-dlss5 add-on.",
                    "standalone-dlssnr runs the network itself on its own NGX "
                    "session; renodx-dlss5 runs it again on the game's. Install "
                    "this route again - it removes renodx-dlss5.addon64 - or "
                    "switch route.")
        elif rep.route != "standalone" and any(_standalone_named(n) for n in others):
            rep.add(BAD, "Two add-ons process the frame: the DLSS 5 add-on and "
                         "standalone-dlssnr.",
                    "standalone-dlssnr.addon64 (with its nvngx.dll) belongs to "
                    "the standalone route. Install this route again - it moves "
                    "them aside - or switch to the standalone route.")
        elif others:
            rep.add(WARN, "Other ReShade add-ons are loaded: " + ", ".join(others),
                    "They share the swap chain with the DLSS 5 add-on. If the "
                    "picture flickers or nothing happens, move their .addon64 "
                    "files out of the folder and test with ours alone.")
        feeder_on = any("Feed" in n for n in loaded)
        bridge_on = any("Bridge" in n for n in loaded)
        if feeder_on and bridge_on:
            rep.add(BAD, "Both the feeder and the bridge add-on are loaded.",
                    "Only one route may be installed at a time. This is "
                    "usually an orphan from an earlier install that the "
                    "manifest never recorded. Uninstall, check no "
                    "dlss5-feed.addon64 or dlss5-bridge.addon64 is left in "
                    "the folder, then install again.")

        if d3d9_only and str(man.get("api") or "").upper() in ("DX11", "DX12"):
            rep.add(WARN, "ReShade attached to a Direct3D 9 device, not DXGI.",
                    "The app renders with D3D9 here (a video player on the EVR "
                    "renderer, or a game in D3D9 mode); the feed needs "
                    "D3D11/12. Switch the renderer to one that uses D3D11, or "
                    "the game to DX11/DX12, and play again.")

        # The game exiting before a swapchain exists means it never got to
        # rendering at all - nothing downstream of this is worth reading.
        if "Registered add-on" in rtext and "Exiting" in rtext \
                and "CreateSwapChain" not in rtext and "Presenting" not in rtext \
                and not d3d9_only:
            rep.add(BAD, "The game closed before it drew a single frame.",
                    "ReShade attached and the device was created, but no swap "
                    "chain ever was, so the game quit during start-up. That "
                    "points at something in the folder stopping it rather "
                    "than at the DLSS setup. Uninstall and check the game "
                    "starts on its own first.")
        if "Registered add-on" not in rtext:
            rep.add(BAD, "ReShade loaded no add-ons.",
                    "Add-on support requires the ReShade build WITH add-ons, "
                    "and AddonPath must point at the game folder.")
        _shader_failures(rtext, provider_tech, rep)
        if "untested build" in rtext:
            rep.add(WARN, "The add-on flagged your nvngx_dlssnr as an untested build.",
                    "It accepted it, but failures may be specific to that file.")
        if "focus window is the desktop window" in rtext:
            rep.add(INFO, "ReShade skipped a device whose window is the desktop.",
                    "Harmless: the app created a throwaway device before its "
                    "real one.")

    # --- the feeder path -------------------------------------------------
    if text:
        m = re.search(r"DLSS 5 add-on: \S+ (v[\d.]+) -- (\S+)", text)
        if m and m.group(2) == "classic":
            rep.add(INFO, f"DLSS 5 add-on {m.group(1)}, classic engine",
                    "An older add-on build. Feeder behaviour differs from the "
                    "newer 'v45+' engine.")

        # The feed reports its effects once per runtime, and the first
        # runtime in a process can be a throwaway that says MISSING while a
        # later one says found. Only the last describes reality.
        states = re.findall(r"DLSS5_Feed\.fx technique (found|MISSING)", text)
        if states:
            found = states[-1] == "found"
        elif "technique found" in text:
            found = True
        elif "is not loaded" in text:
            found = False
        else:
            found = None
        if found:
            rep.add(OK, "DLSS5_Feed.fx loaded and its textures were found.")
        elif found is False:
            rep.add(BAD, "DLSS5_Feed.fx never loaded.",
                    "Check the ReShade overlay for a compile error and that "
                    "reshade-shaders\\Shaders holds DLSS5_Feed.fx.")

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

        # Flat depth means ReShade handed the feed no depth buffer. A video
        # player has none to give, so there it is the expected state.
        depth = re.findall(r"Depth probe[^\n]*", text)
        if depth and "sampled depth is flat" in depth[-1]:
            if man.get("kind") == "video":
                rep.add(INFO, "No depth in a video player - expected.")
            else:
                rep.add(WARN, "ReShade is not giving the feed a depth buffer.",
                        "The sampled depth is flat. " + _DEPTH_HINT)

        # Deep Fried Chicken states, as the feed log reports them.
        dfc = re.findall(r"Deep Fried Chicken[^\n]*?\b(ARMED|DISARMED|CONFLICT|FAILED)\b", text)
        if dfc:
            state = dfc[-1]
            npass = re.findall(r"(\d+)\s*pass", text)
            if state == "ARMED":
                rep.add(OK, "Deep Fried Chicken is armed"
                            + (f" ({npass[-1]} passes)." if npass else "."))
            elif state == "CONFLICT":
                rep.add(BAD, "Deep Fried Chicken reports CONFLICT: another neural "
                             "add-on is loaded beside it.",
                        "Remove renodx-dlss5.addon64 / renodx-dlss.addon64 / "
                        "alexs-toolkit.addon64 from the folder (host64 on 32-bit) "
                        "and start again - with two, Chicken does nothing.")
            else:
                rep.add(BAD, f"Deep Fried Chicken reports {state}.",
                        "deep-fried-chicken.log next to it says why; quote it "
                        "with dlss5-feed.log when reporting.")
        elif man.get("consumer") == "dfc" and "Deep Fried Chicken: not present" in text:
            rep.add(BAD, "Deep Fried Chicken's files are not where the feed looks.",
                    "For a 32-bit game its three files belong in host64\\, "
                    "beside the helper; reinstall puts them there.")
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

    # The standalone add-on keeps a log of its own, outside the folder; it
    # says more than ReShade.log ever can on this route.
    if rep.route == "standalone":
        return _analyse_standalone(rep, since, bool(rtext))

    # --- the decisive part, from whichever log has it --------------------
    joined = "\n".join(x for x in (text, htext, rtext) if x)
    # The feeder's own crash handler: "### CRASH RECORDED ###  exception
    # 0xC0000005 at ... in Game.exe; this add-on was last doing: <step>" and
    # a dump path on the next line. Frames may have been delivered just
    # before, so this has to be read before "Working." is declared.
    rec = re.search(r"### CRASH RECORDED ###\s+exception (0x[0-9A-Fa-f]+)[^\n]*?"
                    r"last doing: ([^\n]+)", joined)
    if rec:
        dump = re.search(r"crash dump written: ([^\n]+?)\s+--", joined)
        rep.add(BAD, f"The feed recorded a crash ({rec.group(1)}) while "
                     f"{rec.group(2).strip()}.",
                "That is the feeder itself going down, not the install. Two "
                "things to try from the install page: another 'feeder build' "
                "from the list (the stable 0.7.0 is the long-tested 32-bit "
                "path), and a lower work resolution. Then report it to the "
                "DLSS5-Feeder project with this log"
                + (f" and the dump ({dump.group(1).strip()}; zip it, it "
                   f"shrinks to a few MB)" if dump else "") + ".")
        rep.verdict = "The feed crashed after starting - a feeder bug; try another feeder build."
        return rep
    crash = re.search(r"CreateFeature raised exception (0x[0-9A-Fa-f]+)", joined)
    ready = re.search(r"feature ready[:\s]", joined)
    delivered = re.findall(r"frame (\d+) (?:delivered|evaluated)", joined)
    perf = re.search(r"(\d+) frames: feed CPU ([\d.]+) ms/frame[^\n]*?"
                     r"([\d.]+) fps", joined)
    # The neural pass is cs_5_1. A game's bundled d3dcompiler_47.dll that
    # predates it fails the compile, and the feed keeps delivering frames
    # into a pass that does nothing - "Working." would be a lie.
    old_compiler = re.search(
        r"is too old for Shader Model 5\.1|rejects cs_5_1|"
        r"unrecognized compiler target 'cs_5_1'", joined)

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
        if old_compiler:
            rep.add(BAD, "The game's own d3dcompiler_47.dll is too old for "
                         "the neural pass.", _COMPILER_FIX)
            rep.verdict = ("Frames flow, but neural rendering is silently doing "
                           "nothing - old d3dcompiler_47.dll in the game folder.")
        else:
            rep.verdict = "Working."
    elif ready:
        rep.add(WARN, "The feature was created but no frames were delivered.",
                "Neural rendering may still be switched off in the DLSS 5 panel.")
        # The feeder builds its contract out of ReShade's depth buffer. If
        # ReShade has not selected one, everything above this point still
        # succeeds and no frame is ever produced - which is what a display
        # mode change can cause: ReShade matches a depth buffer to the back
        # buffer, and borderless, resolution scaling or a render scale below
        # 100% make the two disagree.
        rep.add(INFO, "If it is switched on and still does nothing, check the "
                      "depth buffer.", _DEPTH_HINT)
        rep.verdict = "Set up correctly, but not switched on yet."
    elif "failure: resource build" in joined:
        rep.add(BAD, "Building the feed resources failed.")
        rep.verdict = "DLSS never started."
    elif old_compiler:
        rep.add(BAD, "The game's own d3dcompiler_47.dll is too old for "
                     "the neural pass.", _COMPILER_FIX)
        rep.verdict = "The neural pass cannot compile - old d3dcompiler_47.dll in the game folder."
    elif rep.route in ("native", "bridge", "renodx", "upstream"):
        panel = ("RenoDX DLSS tab" if rep.route == "renodx"
                 else UPSTREAM_PANEL if rep.route == "upstream"
                 else "DLSS 5 Neural Rendering panel")
        rep.add(INFO, "This route leaves no frame log of its own.",
                f"Open the ReShade overlay and check the {panel}: it shows "
                f"the live state and whether it is switched on.")
        rep.verdict = (f"Add-ons loaded. Confirm in the {panel} - this "
                       f"route does not log frames.")
    else:
        rep.verdict = "Inconclusive - the feed did not get far enough to tell."

    return rep


def _analyse_standalone(rep: Report, since: float, reshade_ran: bool) -> Report:
    """Read the standalone add-on's own log and say what it got to.

    Phrases are the add-on's (README troubleshooting table and the strings
    in its 1.7.17 binary): "standalone contract ready" is the feature set
    created, "on-present frame N" is a frame through the pipeline,
    "standalone pipeline FAILED at <stage>" names the stage that died.
    """
    p = STANDALONE_LOG
    text = _tail(p, 200_000) if p.is_file() else ""
    if not text:
        rep.add(WARN if reshade_ran else INFO,
                "The add-on has not written its own log yet.",
                f"standalone-dlssnr writes {p} the moment it attaches. ReShade "
                f"loaded, so if the game ran and the file is not there, the "
                f"add-on never initialised: check standalone-dlssnr.addon64 "
                f"AND nvngx.dll are beside the executable (antivirus), then "
                f"install again." if reshade_ran else
                f"standalone-dlssnr writes {p} the moment it attaches; play "
                f"once and check again.")
        rep.verdict = ("Add-on loaded, but its own log has nothing yet - play "
                       "once and check again.")
        return rep
    try:
        rep.log_time = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %H:%M")
    except OSError:
        pass
    if since and not _fresh(p, since):
        rep.add(WARN, "The standalone-dlssnr log predates this install.",
                "It is one file for every game the add-on ran in, and it was "
                "last written before this install. Play once and check again.")
        rep.verdict = "Installed after the last run - play once and check again."
        return rep
    # One log for every game: only the last session can describe this one.
    cut = text.rfind(_STANDALONE_SESSION)
    if cut >= 0:
        text = text[text.rfind("\n", 0, cut) + 1:]

    if _STANDALONE_NO_RUNTIME in text:
        rep.add(BAD, "The add-on found no private runtime beside it.",
                "Its log says 'required private runtime dependency missing': "
                "nvngx.dll (the caller bridge) as well as the add-on, plus "
                "nvngx_dlssnr.dll and nvngx_dlss.dll, must all sit beside the "
                "executable. Installing again puts every one of them back; "
                "antivirus quarantine is the usual reason one is gone.")
        rep.verdict = "The add-on loaded but is missing a runtime file - reinstall."
        return rep
    if "NGX core: no _nvngx.dll found" in text:
        rep.add(BAD, "The add-on found no NGX core in the NVIDIA driver.",
                "It scans the driver store for _nvngx.dll and found none: "
                "not an NVIDIA driver, or a very old one. Update the driver.")
        rep.verdict = "No NGX core in the driver - update the NVIDIA driver."
        return rep

    if "same-frame VORT optical flow" in text:
        rep.add(OK, "Motion vectors: VORT optical flow is feeding the network.")
    elif "zero-motion" in text or "fallback guides" in text:
        rep.add(WARN, "Running on zero-motion guides - expect ghosting.",
                "The add-on did not get VORT and DLSS5_AIO_Feed.fx: check both "
                "are under reshade-shaders\\Shaders (vort_Motion.fx with its "
                "Includes folder) and that the ReShade overlay shows no "
                "compile error for them. Installing again puts them back.")
    if "DLSS-G runtime unavailable" in text:
        rep.add(INFO, "Frame generation is off: no usable nvngx_dlssg.dll.",
                "Neural rendering and DLAA/DLSS SR still run. The runtime "
                "needs an RTX 40 or 50 card; installing again fetches it "
                "when the mirror has one.")
    elif "falling back to real frames" in text or "frame generation disabled" in text:
        rep.add(WARN, "Frame generation failed and was switched off.",
                text[text.rfind("DLSS-G"):][:160].splitlines()[0]
                if "DLSS-G" in text else "")
    if "native presentation" in text and "failed" in text:
        rep.add(BAD, "The add-on's own output window could not be created.",
                "It presents through a topmost window of its own; that "
                "failed here. Try borderless instead of fullscreen, or the "
                "'Early proxy initialization' option in its add-on tab for a "
                "D3D12 game that hangs at start.")
    if "waiting for a valid" in text and "shared frame" in text:
        rep.add(WARN, "Vulkan: the add-on is waiting for a shared frame.",
                "ReShade's Vulkan layer must be active, and at least one "
                "effect loaded, for the frame handoff.")

    failed = re.findall(r"standalone pipeline FAILED at ([^\n]+)", text)
    contract = re.findall(r"standalone contract ready: ([^\n]+)", text)
    frames = re.findall(r"on-present frame (\d+):", text)
    if failed:
        rep.add(BAD, f"The pipeline failed at {failed[-1].strip()[:160]}.",
                "The add-on says which stage died; the usual ones are a "
                "nvngx_dlssnr build that does not match the card and a "
                "resolution change while it was running. Restart the game "
                "with the resolution set before loading gameplay.")
        rep.verdict = "The add-on's pipeline failed - see the stage it names."
    elif frames:
        rep.add(OK, f"Frames are going through the pipeline ({len(frames)} "
                    f"logged, last was frame {frames[-1]}).")
        if contract:
            rep.add(INFO, "Active contract: " + contract[-1].strip()[:200])
        rep.verdict = "Working."
    elif contract:
        rep.add(WARN, "The feature set was created but no frame was logged.",
                "Contract: " + contract[-1].strip()[:200] + ". Play a little "
                "longer, or press F10 to see whether the proxy window shows.")
        rep.verdict = "Set up, no frame through yet."
    else:
        rep.add(WARN, "The add-on attached but never reached a contract.",
                f"Open the {STANDALONE_PANEL} in the ReShade overlay: it "
                f"reports the stage it is at and why. The tail of its log "
                f"is in the bug report.")
        rep.verdict = "Inconclusive - the add-on attached but built nothing."
    return rep


# ---------------------------------------------------------------------------
# the bug report body
# ---------------------------------------------------------------------------

_RESHADE_KEEP = ("WARN", "ERROR", "Registered add-on", "CreateSwapChain",
                 "Direct3DCreate9", "Exiting")


def _last_lines(text: str, n: int, keep=None, width: int = 200) -> list[str]:
    lines = [ln.rstrip() for ln in text.splitlines()]
    if keep is not None:
        lines = [ln for ln in lines if keep(ln)]
    return [ln[:width] for ln in lines[-n:]]


def _block(title: str, lines: list[str], budget: int) -> str:
    """A fenced log excerpt that never exceeds its share of the report.

    GitHub's URL cap is the reason everything here is measured: over it, the
    report has to go through the clipboard, which loses people.
    """
    body = "\n".join(lines) if lines else "(none)"
    if len(body) > budget:
        body = "...\n" + body[-budget:].split("\n", 1)[-1]
    return f"\n**{title}**\n```\n{body}\n```\n"


def _presence(install_dir: Path, man: dict, route: str) -> list[str]:
    """One line per file that decides whether anything can load at all."""
    names: list[str] = []
    proxy = man.get("proxy")
    if proxy:
        names.append(proxy)
    names += _addons(man)
    if route == "standalone":
        names.append("nvngx.dll")
    if route != "optiscaler":
        names.append("ReShade.ini")
    names.append("nvngx_dlssnr.dll")
    out = []
    for n in dict.fromkeys(names):
        state = "present" if (install_dir / n).is_file() else "MISSING"
        out.append(f"- {n}: {state}")
    # The game's own compiler beside the exe is the cause of the silent
    # "frames flow, nothing happens" case; worth a line whenever it is there.
    if (install_dir / "d3dcompiler_47.dll").is_file():
        out.append("- d3dcompiler_47.dll: present (the game's own)")
    elif any(install_dir.glob("d3dcompiler_47.dll.*")):
        out.append("- d3dcompiler_47.dll: renamed aside")
    return out


def issue_body(version: str, gpu_name: str, sm, driver: str, game, route: str,
               last_diag, autopilot_tail: str, autopilot_log_path,
               install_dir, last_error: str = "") -> str:
    """The text of a bug report, with the evidence already in it.

    A report is only as good as what it carries. The machine, the verdict,
    which files are actually in the folder and the tail of each log the
    add-ons wrote answer the first five questions a maintainer would ask, so
    the reply can be a fix instead of "please attach ReShade.log".
    """
    diag = ""
    if last_diag is not None:
        try:
            diag = f"\n**Diagnosis**: {last_diag.verdict}\n" + "".join(
                f"- [{f_.level}] {f_.title}\n" for f_ in last_diag.findings)
        except Exception:
            diag = ""

    exe = getattr(getattr(game, "exe", None), "name", None) or "-"
    head = (
        "**What happened**\n\n\n"
        "**What I expected**\n\n\n"
        "---\n"
        f"- version: {version}\n"
        f"- gpu: {gpu_name} (sm_{sm}), driver {driver}\n"
        f"- game: {getattr(game, 'name', None) or '-'}\n"
        f"- exe: {exe}\n"
        f"- arch/api: {getattr(game, 'bit_label', None) or '-'} / "
        f"{getattr(game, 'api', None) or '-'}\n"
        f"- route: {route or '-'}\n"
        + diag[:1200])

    files = ""
    reshade = feed = opti = ""
    d = Path(install_dir) if install_dir else None
    if d is not None and d.is_dir():
        man = _manifest(d)
        files = "\n**Files in the folder**\n" + "\n".join(_presence(d, man, route)) + "\n"
        reshade = _tail(d / RESHADE_LOG, 250_000)
        feed = _tail(d / FEED_LOG, 100_000)
        if route == "optiscaler":
            p = _opti_log(d)
            opti = _tail(p, 100_000) if p else ""

    parts = [head, files]
    parts.append(_block("ReShade.log", _last_lines(
        reshade, 25, lambda ln: any(k in ln for k in _RESHADE_KEEP)), 1500))
    parts.append(_block("dlss5-feed.log", _last_lines(feed, 20), 1400))
    if route == "optiscaler":
        parts.append(_block("OptiScaler.log", _last_lines(opti, 20), 900))
    if route == "standalone":
        parts.append(_block("standalone-dlssnr.log",
                            _last_lines(_tail(STANDALONE_LOG, 100_000), 20), 900))
    if last_error:
        parts.append(f"\n**Last error**\n```\n{last_error[-900:]}\n```\n")
    parts.append(_block(
        f"autopilot.log (`{autopilot_log_path}`)",
        _last_lines(autopilot_tail or "", 15,
                    lambda ln: not re.search(r"scan \S+: \d+ found", ln)), 900))
    body = "".join(parts)
    return body[:6000]
