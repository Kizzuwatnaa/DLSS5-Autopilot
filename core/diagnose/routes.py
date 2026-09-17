r"""One reader per route: OptiScaler, Remix, upstream, standalone.

Each takes a half-filled Report and finishes it, because the
evidence that matters is different on every route.

Part of core/diagnose; see __init__.py.
"""
from __future__ import annotations
import itertools
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import model
from .model import *  # noqa: F401,F403
from .evidence import *  # noqa: F401,F403
from .process import _loaded_note


__all__ = [
    "_analyse_optiscaler", "_analyse_remix", "_analyse_standalone", "_analyse_upstream",
    "_check_inputs", "_last_run", "_opti_checklist", "_overlay_key",
    "_section"
]

def _last_run(text: str) -> str:
    """The last run in a log whose lines are stamped with a clock time.

    OptiScaler stamps every line "[HH:MM:SS.ffffff]" and appends run after
    run. It has no banner this can key on, but a clock that jumps a long way
    backwards can only mean a new run - which is enough to stop a line from
    a session two days ago being read as what just happened.

    A LONG way: several threads write this file and their lines arrive a few
    milliseconds out of order, so a small step backwards is ordinary and
    cutting there would throw away most of a healthy log.
    """
    cut, prev = 0, None
    for m in re.finditer(r"^\[(\d{2}):(\d{2}):(\d{2})", text, re.M):
        now = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        # A clock that goes back over midnight is one run crossing a day,
        # not two runs; cutting there would throw away everything before
        # midnight.
        wrapped = prev is not None and prev > 86_100 and now < 300
        if prev is not None and now < prev - 60 and not wrapped:
            cut = m.start()
        prev = now
    return text[cut:]


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
    text = _last_run(_tail(p)) if p else ""
    if not text:
        # An absent log is not proof of an absent OptiScaler. Whether one is
        # written is a setting, and one of the builds this tool installs
        # ships it off - a working install came back "never loaded" (#110).
        # Say what is actually known, and how to make the next run answer.
        proxy = str((man or {}).get("proxy") or "")
        proxy_there = bool(proxy) and (install_dir / proxy).is_file()
        logging_on = False
        ini_there = (install_dir / "OptiScaler.ini").is_file()
        try:
            _ini = (install_dir / "OptiScaler.ini").read_text(
                encoding="utf8", errors="replace")
            logging_on = bool(re.search(r"^\s*LogToFile\s*=\s*true\b",
                                        _ini, re.M | re.I))
        except OSError:
            pass
        if proxy_there and not ini_there:
            rep.add(BAD, "OptiScaler.ini is missing.",
                    f"The proxy this install wrote ({proxy}) is in the folder "
                    f"but OptiScaler's own settings file is not: the install "
                    f"writes it, so something has removed it since. Without "
                    f"it OptiScaler runs on its own defaults, and the "
                    f"neural-rendering and log settings this install wrote "
                    f"are gone. Install again.")
            rep.verdict = "OptiScaler.ini is missing - install again."
        elif proxy_there and not logging_on:
            rep.add(WARN, "OptiScaler's log is switched off.",
                    f"The proxy this install wrote ({proxy}) is in the folder, "
                    f"and OptiScaler.ini does not ask for a log file - one of "
                    f"the builds this tool installs ships that way. So a "
                    f"missing log says nothing about whether it loaded. "
                    f"Install again - the install sets [Log] LogToFile=true - "
                    f"play once, and press 'did it work?' again. If the game is running well, nothing is wrong: "
                    f"the log is how this tool checks, not how the feature "
                    f"works.")
            rep.verdict = ("OptiScaler's log is off - install again to switch it "
                           "on, then play once.")
            # Also read off an absent log (#171).
            rep.never_ran = True
        elif proxy_there:
            rep.add(WARN, "No OptiScaler log from this install yet.",
                    f"The proxy this install wrote ({proxy}) is in the folder "
                    f"and logging is on, so either the game has not been run "
                    f"since installing, or OptiScaler did not load - check "
                    f"that {proxy} sits next to the executable the game "
                    f"actually launches.")
            rep.verdict = "Not run yet, or OptiScaler did not load."
            # Rests on the absent log, like the feeder one: Windows' own
            # fault record for this game outranks it (#171).
            rep.never_ran = True
        else:
            rep.add(BAD, "No OptiScaler log, and no proxy in the folder.",
                    (f"The proxy this install wrote ({proxy}) is not beside "
                     if proxy else "The proxy this install wrote is not beside ")
                    + "the executable - check that it is next to the .exe the "
                      "game actually launches, and that antivirus did not "
                      "quarantine it.")
            rep.verdict = "OptiScaler is not in the game folder - install again."
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
    nr = [(i, ln) for i, ln in enumerate(lines)
          if "DLSS-NR" in ln or "dlssnr" in ln.lower()]
    # "running at WxH" is the base build's line when the model is created.
    # The forks also print the model's timing every frame it actually draws,
    # and on wilsjo2's after-RR path that dispatch line is the ONLY thing
    # written - the report called a thirteen-minute session with a dispatch
    # every frame "Inconclusive" (#81). A timing is the strongest proof there
    # is: the model cannot report a time for work it did not do.
    #
    # The word in front of the number is the fork author's, and it changes:
    #   "DlssNr_Dx12::Dispatch DLSS-NR cost: 7.41 ms total = 7.23 ms model"
    #   "DlssNr_Dx12::Dispatch DLSS-NR elapsed: 6.72 ms total, 6.60 ms model"
    # Matching "cost" called the second one - Spider-Man 2 dispatching every
    # frame on wilsjo2 - "never reports it running" (#168). Ask for a dispatch
    # and a duration instead, and let them name it what they like.
    failed = [x for x in nr if any(k in x[1] for k in (
        "create failed", "unavailable", "did not run", "not found beside",
        "would not load", "disabling for this session", "refused"))]
    # Settings echoed at startup ("DlssNr.Enabled: true") say what was asked
    # for, not what happened. Separating them keeps the "never ran" verdict
    # from sounding like the tool has no idea what went on.
    settings_only = [x for x in nr if re.search(r"DlssNr\.\w+:", x[1])]
    # Asking only for a duration made three other kinds of line proof that the
    # model ran: a create failure that reports how long it took, a setting
    # whose name happens to contain the word ("DlssNr.DispatchInterval: 16 ms")
    # and a dispatch that says it skipped. A line that is also a failure, also
    # a setting, or says it did nothing is not evidence of work done.
    _not_work = ("skip", "fail", "refus", "abort", "cancel", "no motion")
    running = [x for x in nr
               if x not in failed and x not in settings_only
               and not any(k in x[1].lower() for k in _not_work)
               and ("running at" in x[1]
                    or ("Dispatch" in x[1] and _DISPATCH_MS.search(x[1])))]
    if "forwarder loaded" in text:
        rep.add(OK, "OptiScaler loaded and found the neural-rendering forwarder.")
    # The game is running on Vulkan while this route was installed for D3D12.
    # OptiScaler hooks the game's own upscaler through NGX/D3D12; on Vulkan it
    # sees the swapchain and nothing else, so its overlay keeps saying "select
    # FSR or DLSS or XeSS as upscaler" however the game is set (Red Dead
    # Redemption 2 twice, issues #66 and #70 - the game has a renderer switch
    # and both people were on Vulkan).
    if "Vulkan is creating swapchain" in text \
            and str((man or {}).get("api") or "").upper() in ("DX11", "DX12"):
        rep.add(BAD, "The game is drawing with Vulkan, and this route was "
                     "installed for a Direct3D game.",
                "OptiScaler replaces the game's own upscaler through NGX, "
                "which is a Direct3D path: on Vulkan it never finds one, "
                "which is what its overlay means by 'select FSR or DLSS or "
                "XeSS as upscaler' even with DLSS already on in the game. "
                "Set the game's graphics settings to DirectX 12 and start it "
                "again. If it has to stay on Vulkan, switch the route to "
                "feeder - that goes through ReShade's Vulkan layer and does "
                "not touch the game's upscaler.")
        rep.verdict = ("The game is on Vulkan; the optiscaler route needs "
                       "a Direct3D renderer - or use the feeder route.")
        return rep
    # The game's own Present refusing the swapchain OptiScaler wrapped: the
    # engine gives up and the game closes before neural rendering ever
    # reports in (Assetto Corsa EVO, issue #58 - E_NOINTERFACE).
    pres = None
    for pres in re.finditer(r"Original present result:\s*([0-9A-Fa-f]{8})", text):
        pass
    if pres and not pres.group(1).startswith("0"):
        rep.add(BAD, "The game refused the swapchain OptiScaler wrapped "
                     f"(0x{pres.group(1).upper()}).",
                "OptiScaler replaces the swapchain to put its upscaler in the "
                "frame, and this game's own Present call would not accept it, "
                "so the engine stops. Two things in the game's settings, one at "
                "a time: the 'loads as' dropdown (winmm.dll instead of "
                "dxgi.dll, so OptiScaler enters later), then another entry in "
                "'optiscaler build'. If both close the same way, the feeder "
                "route goes through ReShade and does not replace the game's "
                "upscaler at all.")
        rep.verdict = ("The game refused OptiScaler's swapchain - try another "
                       "name in 'loads as', or the feeder route.")
        return rep
    # A failure logged AFTER the last "running at" is what the person saw
    # last: the model started and then stopped. Taking "running" first said
    # "Working." about a session that ended in a failure.
    if running and (not failed or failed[-1][0] < running[-1][0]):
        rep.add(OK, "Neural rendering is running.", running[-1][1].strip()[-160:])
        rep.verdict = "Working."
    elif failed:
        if running:
            rep.add(OK, "Neural rendering started.", running[-1][1].strip()[-160:])
        rep.add(BAD, "Neural rendering did not start.", failed[-1][1].strip()[-220:])
        if "refuse" in failed[-1][1] or "unavailable" in failed[-1][1]:
            rep.add(INFO, "OptiScaler needs driver 616.56 or newer, and a "
                          "nvngx_dlssnr build for your card (the tool picks "
                          "one). If it keeps refusing, the native or "
                          "renodx-dlss route is one click away.")
        rep.verdict = ("Neural rendering stopped after it started."
                       if running else
                       "OptiScaler loaded, but the model refused or failed.")
    elif nr and len(settings_only) == len(nr):
        # Every neural-rendering line is a setting being read back: the model
        # was switched on and then never asked to draw a frame. On this route
        # that has one usual cause - OptiScaler runs the model around the
        # game's own upscaler, so if the game's DLSS/FSR/XeSS is off, or the
        # game never reaches the point of running it, nothing dispatches and
        # the overlay sits on "waiting for the upscaler to run" (#85, #70).
        rep.add(BAD, "Neural rendering was switched on, but the model never "
                     "drew a frame.", settings_only[-1][1].strip()[-160:])
        rep.add(INFO, "OptiScaler runs the model around the game's own "
                      "upscaler. Turn DLSS (or FSR/XeSS) on in the game's "
                      "graphics menu and set it to anything but 'off' - with "
                      "no upscaler running there is nothing for neural "
                      "rendering to attach to, which is what the overlay "
                      "means by 'waiting for the upscaler to run'. If the "
                      "game has no upscaler at all, use the feeder route.")
        rep.verdict = ("Neural rendering never ran - the game's own upscaler "
                       "has to be on for this route.")
    elif nr:
        rep.add(WARN, "OptiScaler mentions neural rendering but never reports "
                      "it running.", nr[-1][1].strip()[-160:])
        rep.verdict = (f"Inconclusive - open the overlay "
                       f"({_overlay_key()}) and read the status under the "
                       f"Neural Rendering checkbox.")
        _loaded_note(install_dir, man, rep)
    else:
        rep.add(WARN, "OptiScaler ran, but neural rendering was never asked for.",
                f"Press {_overlay_key()} in game and tick Neural Rendering; "
                f"the tool writes Enabled=true, but a hand-edited "
                f"OptiScaler.ini can override it.")
        rep.verdict = "OptiScaler loaded; neural rendering not switched on."
        # ...and then say which of its four requirements are actually met on
        # this machine, rather than leaving the person to guess which one
        # broke. "OptiScaler does not engage" is six issues, four of them
        # still open, and every one of them was answered with advice instead
        # of with what the folder says.
        for line in _opti_checklist(install_dir, man or {}):
            rep.add(INFO, line)
    return rep


def _overlay_key(default: str = "Insert") -> str:
    """What this person actually has to press (#88 made it configurable)."""
    try:
        from .. import reshade_ini as _ri
        return _ri.overlay_key_name(default)
    except Exception:
        return default


def _opti_checklist(install_dir: Path, man: dict) -> list[str]:
    """The four things the OptiScaler route needs, each answered from disk.

    Every line is a fact read out of the folder, the ini or the driver -
    never an instruction. What to do about a missing one is already in the
    findings above it.
    """
    out: list[str] = []
    from .. import optiscaler as _opti

    # 1. Neural rendering switched on in the file OptiScaler actually reads.
    ini = install_dir / _opti.INI
    enabled = None
    try:
        text = ini.read_text(encoding="utf8", errors="replace")
        m = re.search(r"^\s*Enabled\s*=\s*(\S+)", _section(text, _opti.NR_SECTION),
                      re.M | re.I)
        enabled = m.group(1).strip().lower() if m else None
    except OSError:
        text = ""
    if enabled is None:
        out.append(f"{_opti.INI}: no [{_opti.NR_SECTION}] Enabled line "
                   f"({'the file is not there' if not ini.is_file() else 'the install writes one, so it has been changed since'}).")
    else:
        out.append(f"{_opti.INI}: [{_opti.NR_SECTION}] Enabled={enabled}.")

    # 2. The runtime, and the forwarder the model insists on being called
    #    through (its path has to contain "nvngx.dll"). The forwarder comes
    #    out of the OptiScaler package, so it is only "missing" if this
    #    install actually recorded writing it - a build that does not ship
    #    one must not be reported as broken.
    recorded = {str(f).replace("\\", "/").rsplit("/", 1)[-1].lower()
                for f in man.get("files") or [] if isinstance(f, str)}
    for name in ("nvngx_dlssnr.dll", _opti.FORWARDER):
        there = (install_dir / name).is_file()
        if there:
            out.append(f"{name}: present.")
        elif name.lower() in recorded or name == "nvngx_dlssnr.dll":
            out.append(f"{name}: MISSING - the install wrote it.")
        else:
            out.append(f"{name}: not in this folder (this build may not use one).")

    # 3. The game's own upscaler - the thing the model attaches to.
    up = str(man.get("upscaler") or "")
    out.append(f"the game's own upscaler, recorded at install: "
               f"{up or 'none recorded - the game was expected to use DLSS'}. "
               f"It has to be running for the model to have anything to "
               f"attach to.")

    # 4. The driver, which is where the model itself lives.
    try:
        from .. import gpu as _gpu
        drv = _gpu.driver_version()
        ok = _gpu.driver_at_least(_opti.DRIVER_MIN, drv)
        out.append(f"driver {drv or 'unknown'}: "
                   + ("new enough" if ok else
                      f"older than {_opti.DRIVER_MIN}, the first driver "
                      f"documented to carry the neural-rendering runtime"
                      if ok is False else
                      "could not be read"))
    except Exception:
        pass
    return out


def _section(text: str, name: str) -> str:
    """One [section] out of an ini, without the ones after it."""
    m = re.search(rf"^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", text,
                  re.M | re.S | re.I)
    return m.group(1) if m else ""


def _analyse_remix(install_dir: Path, rep: "Report", since: float,
                   man: dict) -> "Report":
    """Read the Remix runtime's own log, which is the only one that applies.

    Every phrase matched here was read out of this machine's runtime binary
    and confirmed in a real log; they come from Kim2091's gta4-atmos-dlss5
    fork (src/dxvk/rtx_render/rtx_neural_uplift.cpp and its NGX wrapper):

        [DLSS-NR] Loaded .trex\\nvngx_dlssnr.dll
        [DLSS-NR] Snippet initialized
        [DLSS-NR] Created the Neural Uplift feature (id 18, preset 0) at 1920x1080

    and, when it goes wrong, "nvngx_dlssnr.dll could not be loaded",
    "nvngx_dlssnr.dll not found", "snippet failed to load" and the
    caller-check line.
    """
    from .. import remix as _remix

    rx = man.get("remix") or {}
    key = str(rx.get("key") or "")
    conf = Path(rx.get("conf") or "")
    if conf and not conf.is_absolute():
        conf = install_dir / conf

    # The two things that are true whether or not the game has been run.
    trex = _remix.find_runtime(install_dir)
    if trex is None:
        rep.add(BAD, "The RTX Remix runtime is gone from this game.",
                "There is no '.trex' folder any more. The mod was removed or "
                "the game verified its files; nothing on this route can work "
                "without it.")
        rep.verdict = "No Remix runtime in the folder any more."
        return rep
    if not (trex / _remix.DLSSNR).is_file():
        rep.add(BAD, f"nvngx_dlssnr.dll is missing from {trex.name}.",
                "It was installed into the Remix runtime folder and is no "
                "longer there - almost always antivirus quarantine. Restore "
                "it, exclude the folder, and install again.")
    if not _remix.runtime_flavour(trex):
        rep.add(BAD, "This Remix runtime has no DLSS 5 neural pass.",
                "NVIDIA's own runtime has none, and neither does this one, so "
                "there is nothing to switch on. Turn on 'swap the Remix "
                "runtime' and install again to replace it with a community "
                "build that has the pass.")
        rep.verdict = "The Remix runtime here has no neural pass - use the swap option."
        return rep
    if key and conf.is_file() and not _remix.option_set(conf, key):
        rep.add(BAD, "Neural rendering is switched off in rtx.conf.",
                f"'{key}' is not in {conf.name} any more. The Remix menu "
                f"writes that file back when you save settings, so turning "
                f"the pass off in game (Alt+X -> Developer Settings Menu -> "
                f"Post-Processing) removes it. Turn it back on there, or "
                f"install again.")

    p = _remix.log_path(install_dir)
    text = _tail(p, 300_000)
    if not text:
        # A runtime we swapped in is the first thing to suspect when the
        # game stopped starting: it is a different d3d9.dll from the one
        # the mod was built and tested with, and a game that reaches Remix
        # through a translator of its own (d3d8to9, dgVoodoo) is the case
        # nobody upstream runs (#218, Max Payne).
        swapped = bool((man.get("components") or {}).get("remix_runtime"))
        rep.add(WARN, "The Remix runtime has not written a log yet.",
                f"It writes {Path(_remix.LOG)} the moment it starts, and "
                f"there is none."
                + ("" if swapped else
                   " Either the game has not been run since installing, or "
                   "Remix is not loading at all - check the game's own "
                   "d3d9.dll (the Remix bridge) is still beside the "
                   "executable."))
        if swapped:
            rep.add(BAD, "This install swapped the mod's own Remix runtime.",
                    "If the game stopped starting after the install, that is "
                    "the first thing to undo: the community runtime is a "
                    "different d3d9.dll from the one the mod ships, and a "
                    "game that reaches Remix through a translator of its own "
                    "(d3d8to9, dgVoodoo) is not a case it is tested on. "
                    "Press uninstall - the mod's runtime comes back - and "
                    "install again with 'swap the Remix runtime' off.")
            rep.verdict = ("The swapped Remix runtime is the first suspect - "
                           "uninstall puts the mod's own back.")
            # The game left no log of its own: the Windows fault record is
            # still allowed to rewrite this, as it is for every other
            # absent-log verdict.
            rep.never_ran = True
            return rep
        rep.verdict = "Not run yet, or the Remix runtime never loaded."
        rep.never_ran = True
        return rep
    rep.ran = True
    try:
        rep.log_time = datetime.fromtimestamp(p.stat().st_mtime)\
            .strftime("%d %b %H:%M")
    except OSError:
        pass
    if since and not _fresh(p, since):
        rep.add(WARN, "The Remix log predates the current install.",
                "Play once and check again.")

    if _remix.LOADED in text and "nvngx_dlssnr.dll" in text:
        rep.add(OK, "The runtime loaded nvngx_dlssnr.dll from the .trex folder.")
    if _remix.INITIALISED in text:
        rep.add(OK, "The DLSS-NR snippet initialised.")
    bad = [(k, fix) for k, fix in _remix.FAILURES if k in text]
    created = _remix.CREATED_RE.findall(text)
    if created:
        name, fid, preset, extent = created[-1]
        rep.add(OK, f"DLSS 5 is running inside Remix (feature {fid}).",
                f"{name.strip()} feature, preset {preset}"
                + (f", at {extent}" if extent else "") + ".")
        rep.verdict = "Working."
        return rep
    if bad:
        k, fix = bad[-1]
        rep.add(BAD, f"The neural pass did not start: {k}", fix)
        rep.verdict = "Remix ran, but the DLSS 5 snippet never started."
        return rep
    if "[DLSS-NR]" in text:
        rep.add(WARN, "Remix mentions DLSS-NR but never created the feature.",
                text[text.rfind("[DLSS-NR]"):][:200].splitlines()[0])
        rep.verdict = ("Inconclusive - open Alt+X -> Developer Settings Menu "
                       "-> Post-Processing and read the Neural Uplift line.")
        return rep
    rep.add(WARN, "The Remix runtime ran but says nothing about DLSS-NR.",
            f"With '{key or 'the enable key'}' set in rtx.conf the runtime "
            f"logs a [DLSS-NR] line on every start. Nothing here means this "
            f"runtime does not know the option - install again, or use the "
            f"'swap the Remix runtime' option.")
    rep.verdict = "Remix ran; the neural pass was never even attempted."
    return rep


def _analyse_upstream(rep: Report, rtext: str) -> Report:
    """Read the neural-upstream add-on's own lines out of ReShade.log.

    The route was answered with "this route leaves no frame log of its own"
    for three releases (issues #39, #51, #64) while the add-on was writing
    every step into ReShade.log under [NRPRE]: whether it is switched on,
    whether it found the game's DLSS call, what NGX answered, and a heartbeat
    per second with the state of the frame it is producing. All of it is
    here, so none of those questions has to go back to the person.
    """
    def last(pat: str):
        m = None
        for m in re.finditer(pat, rtext):
            pass
        return m

    on = last(r"\[NRPRE\] settings loaded: enabled=(\d)")
    hooks = re.findall(r"\[NRPRE\] hook \d+ on NVSDK_NGX_\w+_EvaluateFeature: OK", rtext)
    created = last(r"\[NRPRE\] CreateFeature id=18 -> res=(0x[0-9A-Fa-f]+)")
    snippet = "[NRPRE] 2b: snippet route" in rtext
    hb = last(r"\[NRPRE\] HB #(\d+) [^\n]*?finalvalid=(\d)[^\n]*?"
              r"erfail=(\d+) grfail=(\d+) passthru=(\d+)")
    expo = last(r"\[NRPRE\] HB #\d+ [^\n]*?meas=([\d.]+) valid=(\d)[^\n]*?"
                r"fromgame=(\d)")
    diag = last(r"\[NRPRE\] DIAG tick=\d+ run=(\d+) skip=(\d+)")
    net = last(r"\[NRPRE\] HB #\d+ [^\n]*?net=(\d+x\d+|0x0)")
    ev = last(r"\[NRPRE\] eval feat=\d+\s+render_subrect=(\d+x\d+)[^\n]*?"
              r"Output=(\d+x\d+)")
    # The last thing the add-on said. A device or swapchain re-creation as
    # the final word, with heartbeats before it, is the add-on going down
    # inside the rebuild - which is what a hang on loading a save looks like
    # from outside (S.T.A.L.K.E.R. 2, issue #64).
    # Closing the game ends the log the same way - the add-on says
    # "resetting state (device destroyed)" on the way out - so only a NEW
    # device or a rebuilt swapchain counts, and only when ReShade never got
    # to its own orderly shutdown afterwards.
    lines = re.findall(r"\[NRPRE\] ([^\n]*)", rtext)
    ended_on_reset = bool(lines) and (
        lines[-1].startswith("resetting state (new device)")
        or lines[-1].startswith("resetting state (swapchain recreated)"))
    if ended_on_reset:
        after = rtext[rtext.rfind("[NRPRE] " + lines[-1]):]
        if re.search(r"Unloading add-on|Exiting|Destroyed .* object|"
                     r"Uninstalling .* hook", after):
            ended_on_reset = False

    if hooks:
        rep.add(OK, f"The add-on hooked the game's DLSS call "
                    f"({len(hooks)} entry point{'s' if len(hooks) > 1 else ''}).")
    if created:
        code = created.group(1)
        if int(code, 16) == 1:
            rep.add(OK, "NGX created the neural feature.")
        elif snippet:
            rep.add(WARN, f"NGX refused the neural feature ({code}); the "
                          f"add-on ran its own snippet instead.",
                    "Its fallback path does the same work without asking the "
                    "driver for the feature, so this alone is not a fault - "
                    "though the one freeze traced on this route was on "
                    "a game that took it.")
        else:
            rep.add(BAD, f"NGX refused the neural feature ({code}).",
                    "The driver would not create the neural pass for this "
                    "game. Try the native or optiscaler route in the "
                    "dropdown; both reach the same network another way.")
            rep.verdict = f"The neural feature was refused by NGX ({code})."
            return rep

    if expo and expo.group(3) == "0" and float(expo.group(1) or 0) == 0.0:
        rep.add(WARN, "The game hands the add-on no exposure value.",
                "This route normalises the frame against the game's own "
                "exposure buffer and this game does not expose one, so it "
                "estimates instead - that is the route where the picture only "
                "gets darker. The native route runs after the game's tone "
                "mapping and does not need it.")

    if ev:
        rep.add(INFO, f"Neural pass at {ev.group(1)}, the game presents "
                      f"{ev.group(2)}.")

    if hb:
        n, valid, erfail, grfail, passthru = hb.groups()
        if int(passthru) > 0:
            rep.add(WARN, f"{passthru} of {n} frames went through untouched.",
                    "The add-on was in the frame and handed it on without "
                    "running the network - a few around a load or a menu is "
                    "normal.")
        if int(erfail) or int(grfail):
            rep.add(WARN, f"The add-on logged failures ({erfail} exposure, "
                          f"{grfail} guides).")
        if valid == "1":
            rep.add(OK, f"Neural rendering is running ({n} heartbeats"
                        + (f", {net.group(1)}" if net and net.group(1) != "0x0" else "")
                        + ").")
            # Frames went through, so nothing below about being switched off
            # or never hooked can be true any more - only what came after.
            if not ended_on_reset:
                rep.verdict = "Working."
                return rep
        else:
            rep.add(BAD, "The add-on is in the frame but produces nothing "
                         f"({n} heartbeats, no valid result).",
                    "Switch the route to native in the dropdown and install "
                    "again - it runs the same network after the game's own "
                    "upscaler instead of before it.")
            rep.verdict = "The add-on runs but never produces a frame."
            return rep

    if ended_on_reset:
        rep.add(BAD, "The add-on's log stops at a device re-creation "
                     f"({lines[-1][:60]}).",
                "The game threw its D3D12 device away and built a new one - "
                "loading a save, or changing a display setting - and the "
                "add-on wrote nothing after that. If the game hung or died "
                "at that moment, this is where. Nothing here can be set to "
                "avoid it: switch the route to native or optiscaler in the "
                "dropdown, or take nvngx.dll.addon64 out of the folder while "
                "you load. (If the game was only minimised or alt-tabbed "
                "when this was taken, it stops here for that reason and "
                "there is nothing wrong.)")
        rep.verdict = ("Neural rendering ran, then the add-on stopped at a "
                       "device re-creation.")
        return rep

    if on and on.group(1) == "0":
        rep.add(BAD, "Neural rendering is switched off.",
                "The add-on loaded with enabled=0. Open the ReShade overlay "
                f"({_overlay_key('Home')}) and switch it on in the "
                + UPSTREAM_PANEL + ".")
        rep.verdict = "Loaded, but switched off in the " + UPSTREAM_PANEL + "."
        return rep

    if not hooks:
        rep.add(BAD, "The add-on never found the game's DLSS call.",
                "This route rewrites the game's own DLSS; with no hook on it "
                "there is nothing to rewrite. Either the game does not ship "
                "DLSS, or it loads it after the add-on looked. The optiscaler "
                "route brings its own upscaler and does not need the game's.")
        rep.verdict = "The game's DLSS call was never hooked - nothing to run on."
        return rep

    if hb or (diag and int(diag.group(1)) > 0):
        rep.verdict = "Working."
        return rep

    rep.add(INFO, "The add-on loaded and set itself up, but never ran.",
            f"Open the ReShade overlay ({_overlay_key('Home')}), the "
            + UPSTREAM_PANEL +
            ", and check it is switched on; then play a few seconds and "
            "check again.")
    rep.verdict = "Loaded and set up; no neural frame yet."
    return rep


def _analyse_standalone(rep: Report, since: float, reshade_ran: bool,
                        install_dir: Path | None = None,
                        man: dict | None = None) -> Report:
    """Read the standalone add-on's own log and say what it got to.

    Phrases are the add-on's (README troubleshooting table and the strings
    in its 1.7.17 binary): "standalone contract ready" is the feature set
    created, "on-present frame N" is a frame through the pipeline,
    "standalone pipeline FAILED at <stage>" names the stage that died.
    """
    p = model.STANDALONE_LOG
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
        # A fresh ReShade.log is a record of this session, and this verdict is
        # built on it - so a fault record must not rewrite it into "nothing
        # here recorded the session".
        rep.never_ran = not reshade_ran
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
        # As above: a fresh ReShade.log is a record of this session.
        rep.never_ran = not reshade_ran
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
        # "Check both are under reshade-shaders\Shaders" is a question this
        # can answer itself: the install recorded what it wrote, so a shader
        # that is no longer there is a fact, not something to send somebody
        # looking for (#212 got the ask, with nothing in the report to say
        # which of the two it was).
        recorded, gone = _feed_shaders(install_dir, man)
        if gone:
            rep.add(BAD, f"{', '.join(gone)} "
                         f"{'is' if len(gone) == 1 else 'are'} gone from "
                         f"reshade-shaders\\Shaders.",
                    "The install wrote it there and it is no longer in the "
                    "folder - antivirus quarantine, or the shader pack was "
                    "tidied up. Without it the add-on runs on zero-motion "
                    "guides and the picture ghosts. Install again.")
            rep.verdict = ("The motion-vector shader was removed after the "
                           "install - install again.")
            return rep
        rep.add(WARN, "Running on zero-motion guides - expect ghosting.",
                "Both shaders are in the folder, so this is not a missing "
                "file: open the ReShade overlay and look for a compile error "
                "on DLSS5_AIO_Feed.fx or vort_Motion.fx (VORT needs its "
                "Includes folder), and check the effect search path covers "
                "reshade-shaders\\Shaders."
                if len(recorded) == 2 else
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
