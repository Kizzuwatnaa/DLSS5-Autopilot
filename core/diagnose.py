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

When there is no log at all, the folder itself is the evidence: a proxy DLL
that has vanished says "antivirus", an untouched folder says "not started
yet". "Not run yet, or ReShade never loaded" told nobody anything, and a real
bug report arrived carrying exactly that and nothing else.
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


def _analyse_optiscaler(install_dir: Path, rep: "Report", since: float) -> "Report":
    """The OptiScaler route has no ReShade: its own log says everything.

    The fork's DLSS-NR lines are unambiguous - "running at WxH" is success,
    "create failed" / "unavailable" / "did not run" name the reason.
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
        return _analyse_optiscaler(install_dir, rep, since)

    feed = install_dir / FEED_LOG
    host = install_dir / HOST_LOG
    reshade = install_dir / RESHADE_LOG

    # Which log can possibly describe THIS install is decided by the route,
    # not by timestamps: reinstalling bumps the manifest and would make every
    # existing log look stale, while a feeder log left behind after switching
    # to native would otherwise be reported as if it were current.
    feeder_route = rep.route not in ("native", "bridge", "renodx")
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
        hooks = _inst.other_ngx_hooks(install_dir) if rep.route != "optiscaler" \
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
        want = "RenoDX DLSS" if rep.route == "renodx" else "DLSS 5 Neural Rendering"
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
    elif rep.route in ("native", "bridge", "renodx"):
        panel = ("RenoDX DLSS tab" if rep.route == "renodx"
                 else "DLSS 5 Neural Rendering panel")
        rep.add(INFO, "This route leaves no frame log of its own.",
                f"Open the ReShade overlay and check the {panel}: it shows "
                f"the live state and whether it is switched on.")
        rep.verdict = (f"Add-ons loaded. Confirm in the {panel} - this "
                       f"route does not log frames.")
    else:
        rep.verdict = "Inconclusive - the feed did not get far enough to tell."

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
    if last_error:
        parts.append(f"\n**Last error**\n```\n{last_error[-900:]}\n```\n")
    parts.append(_block(
        f"autopilot.log (`{autopilot_log_path}`)",
        _last_lines(autopilot_tail or "", 15,
                    lambda ln: not re.search(r"scan \S+: \d+ found", ln)), 900))
    body = "".join(parts)
    return body[:6000]
