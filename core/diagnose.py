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
    remix    rtx-remix/logs/remix-dxvk.log - the Remix runtime's own log. No
             ReShade is involved at all on that route, so ReShade.log and the
             feed log say nothing about it.

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

# A DLSS-NR dispatch line with a duration in it, whatever the fork calls the
# number ("cost:", "elapsed:", ...). See the running/failed split below.
_DISPATCH_MS = re.compile(r"\d+(?:[.,]\d+)?\s*ms\b")

_DEPTH_HINT = (
    "In the ReShade overlay open the Add-ons tab and look at the depth "
    "buffer list: one has to be selected. If none is, or it switches when "
    "you change display mode, try 'Use aspect ratio heuristics' set to off "
    "there. Borderless, display scaling and an in-game render scale below "
    "100% are the usual reason the buffer stops matching. If one is selected "
    "and the depth is still flat, tick 'Copy depth buffer before clear "
    "operations' on the same tab - Mass Effect Legendary Edition needs it.")

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
    # This verdict rests on there being no log at all: whoever has better
    # evidence that the game ran (Windows' fault record) must replace it.
    never_ran: bool = False

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.findings.append(Finding(level, title, detail))


def _area(size: str) -> int:
    try:
        w, h = size.split("x")
        return int(w) * int(h)
    except (ValueError, AttributeError):
        return 0


def _biggest(sizes) -> str:
    """The largest "WxH" in the set - the one a full-screen present uses."""
    return max(sizes, key=_area) if sizes else ""


def _near(a: str, b: str, tol: float = 0.10) -> bool:
    """Is `a` within `tol` of `b` in area, without being the same size?

    A bordered window is the display minus a title bar and a frame: 1920x1071
    against 1920x1080 is 0.8% smaller. A genuinely different resolution (a
    minimized 160x28, or a real mode change) is nowhere near.
    """
    ab, bb = _area(a), _area(b)
    if not ab or not bb or a == b:
        return False
    return (1.0 - tol) <= (ab / bb) < 1.0


def _tail(path: Path, limit: int = 400_000) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read().decode("utf8", "replace")
    except OSError:
        return ""


# ReShade writes one line per launch into the same ReShade.log and never
# truncates it, so a tail can hold several sessions at once (issue #22: the
# same add-on reported five times). Everything the diagnosis reads out of
# that file - which add-ons loaded, which errors were raised, whether a
# swapchain was ever created - is only true of ONE run, and mixing runs is
# how a build the person replaced days ago still shows up as "loaded"
# (Detroit: Become Human, issue #63, reported 0.14.0-beta.5 and 0.13.1-beta.1
# side by side). This line starts every session; the last one starts the run
# that matters.
_RESHADE_SESSION = "Initializing crosire's ReShade"


def _last_session(text: str) -> str:
    """The last ReShade session in a log that may hold several."""
    at = text.rfind(_RESHADE_SESSION)
    # A tail can cut into the middle of a session, and then there is no
    # marker to find - the whole tail is the best evidence there is.
    return text[at:] if at > 0 else text


# The same for the feeder's own log and its 32-bit helper's: each run starts
# with "dlss5-feed 0.14.0-beta.5 (built ...) attached." The standalone
# add-on's log was already read this way; the feeder's was not, so a crash
# from a run two days ago could still be reported as what just happened.
_FEED_SESSION = re.compile(r"^[\d:.]+\s+dlss5-feed\S*\s[^\n]*attached\.", re.M)


def _last_feed_session(text: str) -> str:
    last = None
    for last in _FEED_SESSION.finditer(text):
        pass
    return text[last.start():] if last is not None and last.start() > 0 else text


def _attached(text: str) -> bool:
    """Did the feed add-on say it was loaded? Its own session marker, so a
    build named something new is still recognised - and "detached" is not it."""
    return bool(_FEED_SESSION.search(text or ""))


# Everything the feed can say about the picture starts from the effect runtime
# ReShade hands it. These are the lines that can only exist once it has one,
# whatever the build calls the runtime itself.
_FEED_GOT_RUNTIME = re.compile(
    r"effect runtime|runtime \w+ initialis|effects:|technique|building:"
    r"|feature ready|session ready|frame \d+", re.I)


def _same_launch(feed: Path, reshade: Path, tol: float = 300.0) -> bool:
    """Were these two logs written by the same run of the game?

    The feed log's last session and ReShade's last session come out of two
    files, and nothing else in the report ties them together: an add-on
    removed between two launches would otherwise be reported as loaded, on
    the strength of the older file. Unreadable or absent: say yes, and let
    the finding that reads the text decide.
    """
    try:
        return abs(feed.stat().st_mtime - reshade.stat().st_mtime) <= tol
    except OSError:
        return True


# Both the feeder's crash handler and its evaluate guard print the module
# chain that led to a fault, innermost first:
#   "crash stack, by module (innermost first): Game.exe <- KERNEL32.DLL"
#   "evaluate fault stack, by module (innermost first): D3D12Core.dll <- ..."
# The chain is the only thing in the log that says WHOSE bug it is. Taking
# the first rule that matched instead sent Detroit: Become Human away as "a
# feeder bug; try another feeder build" when the innermost frame was the
# game's own executable and no add-on appeared in the chain at all (#63).
_OURS_IN_STACK = ("dlss5-feed.addon64", "dlss5-feed.addon32",
                  "dlss5-feed-host64.exe", "dlss5-feed-host32.exe",
                  "renodx-dlss5.addon64", "renodx-dlss.addon64",
                  "dlss5-bridge.addon64", "dlss5-dx11-bridge.addon64",
                  "nvngx.dll.addon64", "standalone-dlssnr.addon64")
# The graphics runtime, reached through our add-ons. A fault here is the
# driver's or Direct3D's, and the chain below it says which of ours called
# in. D3D12Core.dll is Microsoft's, not NVIDIA's - the wording says so.
_NGX_IN_STACK = ("nvngx_dlssnr.dll", "_nvngx.dll", "nvngx.dll",
                 "nvngx_dlss.dll", "D3D12Core.dll")


def _fault_chain(text: str, kind: str = r"(?:crash|evaluate fault)") -> list[str]:
    """The modules of the last fault stack in a feeder log, innermost first."""
    last = None
    for last in re.finditer(kind + r" stack, by module \(innermost first\):"
                                   r"\s*([^\n]+)", text):
        pass
    if last is None:
        return []
    return [p.strip() for p in last.group(1).split("<-") if p.strip()]


def _in(name: str, group: tuple[str, ...]) -> bool:
    return any(name.lower() == n.lower() for n in group)


def _family(name: str) -> str:
    """'DLSS 5 Feed 0.14.0-beta.5' -> 'dlss 5 feed'.

    The feeder puts its build in the add-on name, so two builds register
    under two different names. Without the build they are one add-on, and
    two of them in one session is two files fighting over the same frame.
    """
    low = re.sub(r"\s+v?\d+[\d.]*(?:-[A-Za-z]+[\d.]*)?\s*$", "", name.strip().lower())
    return low.strip() or name.strip().lower()


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


# The add-ons are what antivirus goes for most often, but they are not the
# only files whose absence explains everything after it. Somebody who has
# installed three times and still has no nvngx_dlssnr.dll and no shader
# in the folder does not need "install again" a fourth time -
# something is removing them (GTA IV, issue #84).
# Only files that cannot go missing on their own. ReShade.ini is the one
# file ReShade itself rewrites, and the one people are told to delete to
# reset their settings, so it is NOT here - listing it turned "you reset
# your settings" into "antivirus quarantined your install" and returned
# early over every rule below it, the 32-bit Vulkan layer clash included.
_CORE_NAMES = ("nvngx_dlssnr.dll",)


def _dlss_mod():
    """core.dlss, imported where it is used, like this file's other siblings:
    dlss imports installer, and installer imports this module."""
    from . import dlss
    return dlss


def _missing_core(install_dir: Path, man: dict) -> list[str]:
    """Recorded files that the install wrote and are no longer there."""
    out = []
    for f in man.get("files") or []:
        if not isinstance(f, str):
            continue
        base = f.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not (base.endswith((".addon64", ".addon32", ".fx"))
                or base in _CORE_NAMES):
            continue
        if not (install_dir / f).is_file():
            out.append(f)
    return out


OPTI_LOG = "OptiScaler.log"


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
                "so the engine stops. Two things on the install page, one at "
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
        from . import reshade_ini as _ri
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
    from . import optiscaler as _opti

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
        from . import gpu as _gpu
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
    from . import remix as _remix

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
                "there is nothing to switch on. Install again with 'swap the "
                "Remix runtime' ticked to replace it with a community build "
                "that has the pass.")
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
        rep.add(WARN, "The Remix runtime has not written a log yet.",
                f"It writes {Path(_remix.LOG)} the moment it starts. Either "
                f"the game has not been run since installing, or Remix is not "
                f"loading at all - check the game's own d3d9.dll (the Remix "
                f"bridge) is still beside the executable.")
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


# What installer.VULKAN_LAYER writes into the manifest where a proxy DLL name
# would go. Spelled out here so diagnose does not import the installer.
VULKAN_LAYER = "(vulkan layer)"


def _layer_state(man: dict) -> tuple[bool, bool]:
    """(any ReShade layer active, one this game's architecture can load)."""
    try:
        from . import vulkan
    except ImportError:                       # not Windows
        return True, True
    x64 = man.get("bitness") != 32
    return (vulkan.existing_registration() is not None,
            vulkan.registered_for(x64) is not None)


def _layer_gone(man: dict) -> tuple[str, str, str] | None:
    """(title, detail, verdict) when the Vulkan layer cannot reach this game."""
    any_layer, mine = _layer_state(man)
    bits = 32 if man.get("bitness") == 32 else 64
    if mine:
        return None
    if any_layer:
        return ("A ReShade Vulkan layer is registered, but not the "
                f"{bits}-bit one this game needs.",
                f"A {bits}-bit game can only load the {bits}-bit ReShade "
                f"layer. Install again with this tool - it registers the "
                f"missing one beside the other.",
                f"The {bits}-bit ReShade Vulkan layer is not registered - "
                f"install again.")
    return ("ReShade's Vulkan layer is not registered any more.",
            "This game reaches ReShade as a Vulkan layer - a registry entry, "
            "not a file in the folder. Something removed or disabled it: "
            "ReShade's own installer with Vulkan unticked, a cleanup tool, or "
            "another user account. Install again with this tool.",
            "ReShade's Vulkan layer is not registered - install again.")


def _dxvk_files(man: dict) -> list[str]:
    """The DXVK DLLs this install wrote. Taken from the recorded file list,
    not from the manifest's api: by the time the manifest is written the
    game has been re-labelled Vulkan, which says nothing about whether DXVK
    came in as d3d9.dll or as dxgi.dll + d3d11.dll."""
    if not man.get("dxvk"):
        return []
    from . import dxvk as _dxvk
    written = {str(f).replace("\\", "/").lower()
               for f in man.get("files") or [] if isinstance(f, str)}
    return [n for n in _dxvk.ALL_FILES if n in written]


def _dxvk_gone(install_dir: Path, man: dict) -> list[str]:
    """DXVK files the install recorded that are no longer in the folder."""
    return [n for n in _dxvk_files(man) if not (install_dir / n).is_file()]


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
    missing = _missing_core(install_dir, man)

    if proxy == VULKAN_LAYER:
        # Never a file. Until 1.6.1 this went through the "gone from the
        # folder" branch below and every Vulkan-layer install - which since
        # 1.6.0 is every DirectX 9 game, through DXVK - was told antivirus
        # had eaten a file that never existed.
        gone = _layer_gone(man)
        if gone is not None:
            rep.add(BAD, gone[0], gone[1])
            rep.verdict = gone[2]
            return rep
    elif proxy and not (install_dir / proxy).is_file():
        rep.add(BAD, f"ReShade's {proxy} is gone from the folder.",
                "The install wrote it and it is no longer there: antivirus "
                "quarantined it, or the game verified its files and removed "
                "it. Restore it from quarantine (and exclude the folder), "
                "then install again.")
        rep.verdict = f"ReShade's {proxy} is missing from the folder - reinstall."
        return rep

    dxvk_gone = _dxvk_gone(install_dir, man)
    if dxvk_gone:
        rep.add(BAD, f"DXVK is gone from the folder: {', '.join(dxvk_gone)}.",
                "The install put DXVK there so the game renders on Vulkan and "
                "ReShade can reach it as a layer. Without it the game is back "
                "on DirectX and nothing loads. Antivirus quarantine or the "
                "game verifying its files removes it - restore it (and "
                "exclude the folder), then install again.")
        rep.verdict = "DXVK is missing from the folder - reinstall."
        return rep

    try:
        from . import dxvk as _dxvk
        exe_path = Path(str(man.get("exe") or ""))
        since = _installed_at(install_dir)
        # Dated, like every other log this file reads: one left by a run
        # BEFORE this install - or by a game that ships its own DXVK - says
        # nothing about whether the game has been started since.
        dxvk_logs = [n for n in _dxvk.logs_for(exe_path if exe_path.name else None)
                     if _fresh(install_dir / n, since)]
    except Exception:
        dxvk_logs = []

    # Read above the rule below rather than beside its own: a missing file
    # returns, and DXVK's log is the harder evidence.
    if missing and not (dxvk_logs and proxy == VULKAN_LAYER):
        # Stands aside for the DXVK-log and name-clash rules: a shader lost
        # to the game's own file verification would otherwise bury the
        # 32-bit layer answer (#31) under "restore your files". Same shape
        # as the ReShade.ini regression named above _CORE_NAMES.
        many = len(missing) > 1
        rep.add(BAD, f"Written by the install and no longer in the folder: "
                     f"{', '.join(missing[:5])}"
                     f"{', ...' if len(missing) > 5 else ''}.",
                "The install put "
                + ("these files" if many else "it")
                + " there and "
                + ("they are" if many else "it is")
                + " gone - a missing dll is almost always antivirus "
                  "quarantine; a missing shader is more often the game's own "
                  "launcher verifying its files, or a mod manager tidying up. "
                  "Installing again "
                  "writes the same files for the same thing to remove: "
                  "restore them from quarantine first (Windows Security -> "
                  "Protection history -> Restore), add this folder to the "
                  "exclusions, and only then install again.")
        rep.verdict = ("Files the install wrote are gone from the folder - "
                       "restore them and exclude the folder before "
                       "reinstalling.")
        return rep

    # Same guard as the rule above, and for the same reason: DXVK's own
    # log proves the game HAS been started since the install, so "play once
    # and check again" is not just unhelpful there, it is untrue.
    if stale_reshade and not (dxvk_logs and proxy == VULKAN_LAYER):
        rep.add(WARN, "ReShade.log is older than the install.",
                f"The {app} was last run before this install, so nothing "
                f"has loaded the new files yet. Play once and check again.")
        rep.verdict = "Installed after the last run - play once and check again."
        rep.never_ran = True
        return rep

    # DXVK writes its own log beside the game the moment it loads. One of
    # those next to a missing ReShade.log settles the question the hints
    # below can only guess at: the game DID run, DXVK DID load, and the
    # ReShade layer did not - so telling this person the game was never
    # started, or that it might not be on Vulkan, is plainly wrong (Call of
    # Juarez: Gunslinger, issue #31, three rounds of that answer).
    if dxvk_logs and proxy == VULKAN_LAYER:
        bits = 32 if man.get("bitness") == 32 else 64
        # 1.7.3 got this far and said "install again to rewrite the layer",
        # which did not help, because the layer was there and being thrown
        # away: both of ReShade's manifests call themselves VK_LAYER_reshade
        # and the Vulkan loader keeps a name only once - it kept the 64-bit
        # one and then refused it as the wrong bit-type. Its own trace in a
        # 32-bit process is where that came from (#31, four rounds; #2).
        clash = None
        if bits == 32:
            try:
                from . import vulkan as _vk
                clash = _vk.name_clash()
            except Exception:
                clash = None
        if clash is not None:
            rep.add(BAD, "The 32-bit ReShade layer carries the layer name "
                         "the 64-bit one uses, so the Vulkan loader "
                         "throws it away.",
                    f"{dxvk_logs[0]} proves the game ran and DXVK translated "
                    f"it, and there is still no ReShade.log. Both of "
                    f"ReShade's layer manifests are called "
                    f"VK_LAYER_reshade, and an implicit layer name may only "
                    f"appear once: the loader keeps the 64-bit one, then "
                    f"refuses it because a 32-bit game cannot load it. "
                    f"Install again with this version - it gives the 32-bit "
                    f"layer its own name.")
            rep.verdict = ("The 32-bit Vulkan layer was being discarded as a "
                           "duplicate name - install again to rewrite it.")
            return rep
        rep.add(BAD, "DXVK ran, and ReShade did not.",
                f"{dxvk_logs[0]} was written since the install, so the {app} "
                f"was started and DXVK loaded and translated it to Vulkan - "
                f"but ReShade writes ReShade.log the moment it loads, and "
                f"there is none, so ReShade is not in the chain. The "
                f"likeliest reason is its {bits}-bit Vulkan layer, whose "
                f"registration is per user and per architecture: install "
                f"again to write it, and if the game still starts without a "
                f"ReShade.log, say so - a {bits}-bit layer that registers "
                f"and does not load is worth knowing about.")
        rep.verdict = (f"DXVK ran and ReShade did not - install again to "
                       f"rewrite the {bits}-bit Vulkan layer.")
        return rep

    rep.add(WARN, f"The {app} has not been started since the install.",
            "ReShade writes ReShade.log the moment it loads, and there is "
            "none in the folder. All the files are still in place.")
    rep.add(INFO, f"If you DID start it, it launches something other than "
                  f"{exe}.",
            "A launcher or a different executable in another folder does not "
            "pick up the files here. Point the tool at the folder holding the "
            "executable that actually runs.")
    if proxy == VULKAN_LAYER:
        rep.add(INFO, f"Or the {app} is not running on Vulkan.",
                "ReShade reaches this install as a Vulkan layer, so the game "
                "(or emulator) has to render with Vulkan - check its renderer "
                "setting. A game that shipped through DXVK does; an emulator "
                "left on OpenGL or D3D does not, and no ReShade.log appears.")
    elif proxy and proxy.lower() == "opengl32.dll":
        rep.add(INFO, f"Or the {app} does not render with OpenGL.",
                "This install went in as opengl32.dll, which only loads when "
                "the game draws with OpenGL. Unity and other engines name "
                "OpenGL among their backends yet draw with Direct3D on "
                "Windows, and then nothing here is ever loaded. Pick "
                "DirectX 11 or DirectX 12 in the 'graphics api' dropdown on the "
                "install page and install again.")
    elif proxy:
        alt = "d3d11.dll" if proxy.lower() == "dxgi.dll" else "dxgi.dll"
        rep.add(INFO, f"Or the {app} ignores {proxy}.",
                f"Some load the graphics DLLs in a way that skips {proxy}. "
                f"Try the {alt} proxy name in the settings and install again.")
    rep.verdict = f"Not started since the install - run the {app} once, then check again."
    # Said in a way the caller can act on: Windows' own fault record for this
    # executable is proof the game DID start, and it outranks "there is no
    # log" (#171 - GTA5.exe faulted eleven minutes before the report was
    # written, and the answer told the person to run the game once).
    rep.never_ran = True
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

    # The install itself did not finish. The installer records that, and the
    # folder then holds whatever arrived before it stopped - which is why a
    # download cut short in the middle (a reset connection, issue #74) came
    # back here as a game that would not work, with the parts that never
    # downloaded reported as "MISSING" as if antivirus had eaten them.
    # Nothing below this can mean anything until the install is finished.
    if man.get("complete") is False:
        from . import net as _net
        if _net.DISK_FULL_NOTE in (man.get("notes") or []):
            rep.add(BAD, "The install stopped because the drive was full.",
                    "Whatever had not been written yet is missing, which is "
                    "why files are listed as gone. Free up a few hundred MB "
                    "on the game's drive and on the one %LOCALAPPDATA% is on, "
                    "then press INSTALL again.")
            rep.verdict = "The drive was full - free up space and install again."
            return rep
        stopped = next((n[len(_net.STOP_NOTE):] for n in (man.get("notes") or [])
                        if isinstance(n, str) and n.startswith(_net.STOP_NOTE)), "")
        if stopped:
            rep.add(BAD, "The install was stopped before it finished.",
                    f"It said: {stopped} Whatever came before that step is in "
                    f"place; nothing after it was written.")
            rep.verdict = "The install stopped for a reason of its own - see below."
            return rep
        # An uninstall that could not remove everything records itself the
        # same way, and telling that person to install again is the opposite
        # of what they need. Its own note says which it was.
        if "uninstall left" in " ".join(man.get("notes") or []):
            rep.add(BAD, "The uninstall did not finish.",
                    "Some files could not be removed because the game or its "
                    "launcher still had them open. Close the game and press "
                    "uninstall again.")
            rep.verdict = ("The uninstall left files behind - close the game "
                           "and uninstall again.")
            return rep
        # Only files that were written and have since gone can be named:
        # what a cut-off download never fetched was never recorded.
        missing = [f for f in (man.get("files") or [])
                   if isinstance(f, str) and not (install_dir / f).is_file()]
        rep.add(BAD, "The install did not finish.",
                "This folder was set up part of the way and the install "
                "stopped - the install log on this page says why, and it is "
                "usually a download that was cut off. "
                + (f"Written and now gone: {', '.join(missing[:4])}"
                   f"{', ...' if len(missing) > 4 else ''}. " if missing else "")
                + "Press INSTALL again (whatever downloaded is kept, so it "
                  "carries on), or 'uninstall' first if you would rather "
                  "start from a clean folder.")
        rep.verdict = "The install never finished - install again."
        return rep

    if rep.route == "optiscaler":
        return _analyse_optiscaler(install_dir, rep, since, man)
    if rep.route == "remix":
        return _analyse_remix(install_dir, rep, since, man)

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
        text = _last_feed_session(_tail(feed))
        htext = _last_feed_session(_tail(host, 150_000))
    else:
        text = htext = ""
        if feed.is_file():
            rep.add(INFO, "An old dlss5-feed.log is still in the folder.",
                    "It is from a previous feeder install and says nothing "
                    "about this one, so it is ignored.")
    # Only the last launch describes what the person just saw. Everything
    # before it belongs to an install that may not even be this route.
    rtext = _last_session(_tail(reshade, 250_000))

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
        seen_addons: set[str] = set()
        for m in re.finditer(r'Registered add-on "([^"]+)" v(\S+)', rtext):
            # ReShade.log accumulates one line per session; five launches
            # showed the same add-on five times in a report (issue #22).
            key = m.group(1) + m.group(2)
            if key in seen_addons:
                continue
            seen_addons.add(key)
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
        # Two builds of one add-on in the same launch. ReShade loads every
        # .addon64 in the folder, so a copy left behind under another name -
        # renamed by hand to keep it, or dropped in by another mod - is
        # loaded beside ours and establishes a second contract on the same
        # frame. The build is part of the name, so this only shows up here.
        families: dict[str, list[str]] = {}
        for n in loaded:
            families.setdefault(_family(n), []).append(n)
        for names in families.values():
            # Only worth saying about an add-on this tool installs: two
            # builds of somebody else's add-on are their business, and
            # claiming a DLSS-contract fight there would be made up.
            if len(names) > 1 and any(n in ours or "Feed" in n or "Bridge" in n
                                      for n in names):
                rep.add(BAD, f"{len(names)} builds of the same add-on are "
                             f"loaded: " + ", ".join(names) + ".",
                        "ReShade loads every .addon64 in the game folder. Two "
                        "of them process the same frame and fight over the "
                        "same DLSS contract, which ends in a freeze or a "
                        "crash. One of them is this tool's - look in the game "
                        "folder for the other .addon64 (an older build kept "
                        "under another name) and move it out, then start the "
                        "game again.")

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
                    "renderer, or a game that links D3D11 but draws with "
                    "D3D9); the feed needs D3D11/12. Either switch the game "
                    "or player to D3D11/12, or set 'graphics api' to DirectX 9 "
                    "on the install page and install again - the game then "
                    "goes through DXVK like any DirectX 9 title.")

        # The game exiting before a swapchain exists means it never got to
        # rendering at all - nothing downstream of this is worth reading.
        # "CreateSwapChain"/"Presenting" are the DXGI spellings. On Vulkan -
        # a native Vulkan game, or any game sent through DXVK - ReShade logs
        # vkCreateSwapchainKHR instead, so looking only for the DXGI ones
        # called every Vulkan session a game that never drew a frame, right
        # next to "frames are being processed". Seen on Bayonetta via DXVK.
        # And whatever the API: a frame the feed delivered was drawn. OpenGL
        # has no swap-chain line of either kind, and Octowow (#156) - running,
        # frames delivered at 3440x1440 - was told it closed before it drew
        # anything, above the finding that said frames were processed.
        drew = bool(re.search(r"frame \d+ (?:delivered|evaluated)", text or ""))
        if "Registered add-on" in rtext and "Exiting" in rtext \
                and "CreateSwapChain" not in rtext and "Presenting" not in rtext \
                and "vkCreateSwapchainKHR" not in rtext \
                and not d3d9_only and not drew:
            rep.add(BAD, "The game closed before it drew a single frame.",
                    "ReShade attached and the device was created, but no swap "
                    "chain ever was, so the game quit during start-up. That "
                    "points at something in the folder stopping it rather "
                    "than at the DLSS setup. Uninstall and check the game "
                    "starts on its own first.")
        # A log long enough to be cut down to its tail can easily lose the
        # registration lines while every line an add-on WROTE is still
        # there: Starfield's 400 KB ReShade.log reported "no add-ons" next
        # to 34621 heartbeats from the add-on that had loaded (#51). ReShade
        # prefixes an add-on's own lines with its name in brackets, and a
        # line like that is proof enough on its own.
        wrote = re.search(r"\|\s*(?:INFO|WARN|ERROR)\s*\|\s*\[[^\]]+\]", rtext)
        # And the add-on keeps a log of its own. ReShade is the only thing
        # that loads it, so a feed log from this session says the add-on was
        # loaded even when the ReShade tail no longer holds a line about it:
        # Web of Shadows had a folder full of shader packs, the compile lines
        # pushed the registrations out of the tail, and the answer was "no
        # add-ons loaded" directly above a feed that had attached and hooked
        # the game (#164). The add-on's own word beats a cut log.
        # Two things this must NOT do. It must not speak for a different
        # launch: the feed log's last session and ReShade's last session are
        # read from two files, so an add-on removed between launches would be
        # reported as loaded from the older feed log. And it must not speak
        # for an older install - that is what the warning above it is for.
        attached = bool(_attached(text) or _attached(htext))
        if attached and since and not _fresh(feed, since):
            attached = False
        if attached and not _same_launch(feed, reshade):
            attached = False
        # The standalone route has no feed log; its add-on keeps one of its own,
        # in LOCALAPPDATA, and ReShade is equally the only thing that loads it.
        # Without this the same cut tail says "no add-ons" to that route.
        if not attached and rep.route == "standalone":
            try:
                stext = _tail(STANDALONE_LOG, 150_000)
            except OSError:
                stext = ""
            attached = _STANDALONE_SESSION in stext
            if attached and since and not _fresh(STANDALONE_LOG, since):
                attached = False
        if "Registered add-on" not in rtext and not wrote and attached:
            rep.add(OK, "The add-on loaded - it wrote its own log in the "
                        "session this report reads.",
                    "ReShade's own log no longer holds the registration line "
                    "(a long log is read from its tail), but nothing except "
                    "ReShade loads this add-on.")
        if "Registered add-on" not in rtext and not wrote and not attached:
            rep.add(BAD, "ReShade loaded no add-ons.",
                    "Add-on support requires the ReShade build WITH add-ons, "
                    "and AddonPath must point at the game folder.")
            if (man.get("proxy") or "").lower() == "opengl32.dll":
                rep.add(INFO, "Or this log is from another program's ReShade.",
                        "This install went in as opengl32.dll, which only "
                        "loads when the game draws with OpenGL; Unity and "
                        "other engines name OpenGL among their backends yet "
                        "draw with Direct3D on Windows. Pick DirectX 11 or "
                        "DirectX 12 in the 'graphics api' dropdown on the "
                        "install page and install again.")
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
        # "MISSING" right after a runtime starts is often just "not compiled
        # yet": the feed says so ("has not resolved yet ... waiting 10 s")
        # and decides later. A game that closes inside those ten seconds
        # never got the later answer - Half Sword (#142) died of "out of
        # video memory" one second in and was told the shader never loaded,
        # and that its motion-vector shader was not installed, over a file
        # list showing both in place.
        last_missing = max((m.end() for m in re.finditer(
            r"DLSS5_Feed\.fx technique MISSING", text)), default=-1)
        tail = text[last_missing:] if last_missing >= 0 else ""
        pending = found is False and "DLSS5_Feed.fx has not resolved yet" in tail \
            and "is not loaded" not in tail and "technique found" not in tail \
            and not re.search(r"frame \d+ (?:delivered|evaluated)", tail)
        if found:
            rep.add(OK, "DLSS5_Feed.fx loaded and its textures were found.")
        elif pending:
            rep.add(WARN, "The game closed while ReShade was still compiling "
                          "the effects.",
                    "The feed waits a few seconds for DLSS5_Feed.fx to compile "
                    "before it calls it missing, and the session ended inside "
                    "that wait - so nothing here says the shader is broken. "
                    + ("Start" if re.search(r"frame \d+ (?:delivered|evaluated)", text) else
                       "If the game closed on its own, it closed before the "
                       "feed handed DLSS 5 a single frame: start")
                    + " it again and give it that time, and check it "
                    "starts without the install if it closes again.")
        elif found is False:
            rep.add(BAD, "DLSS5_Feed.fx never loaded.",
                    "Check the ReShade overlay for a compile error and that "
                    "reshade-shaders\\Shaders holds DLSS5_Feed.fx.")

        if prov and not pending:
            _n, name, _t, state = prov[-1]
            if "enabled" in state:
                rep.add(OK, f"Motion vectors: {name} is enabled.")
            elif "not installed" in state:
                # Issue #101: the report said "not installed" over a file
                # list that showed the shader sitting in the folder, and a
                # ReShade.log line saying a DIFFERENT technique was enabled.
                # The add-on means "the technique this build reads is not
                # switched on", which is not the same as a missing file -
                # and the two together read as the tool contradicting
                # itself. Look before repeating it.
                # prov's third group is the technique, "Name@file.fx".
                shader = (_t or "").split("@", 1)[1] if "@" in (_t or "") else ""
                on_disk = bool(shader) and (
                    install_dir / "reshade-shaders" / "Shaders" / shader).is_file()
                if on_disk:
                    rep.add(BAD, f"Motion vectors: {name} is installed but "
                                 f"not switched on.",
                            f"{shader} is in reshade-shaders\\Shaders, so "
                            f"nothing is missing - the technique itself is "
                            f"off, or another motion-vector technique is on "
                            f"in its place. This build of DLSS5_Feed.fx reads "
                            f"{name} and nothing else. Open the ReShade "
                            f"overlay, tick {name} and put it ABOVE "
                            f"'DLSS 5 Feed' in the list, or install again to "
                            f"have the tool write the order itself.")
                else:
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
            rep.add(INFO, "On 32-bit the DLSS 5 page in the game's overlay "
                          "drives the 64-bit helper; the helper's own window "
                          "is there too, but do not alt-tab to it while "
                          "playing - that minimizes the game and tears the "
                          "feature down.")
        # A minimized window has a 160x28 client area on Windows, so the swap
        # chain comes back that size and every DLSS create against it fails
        # with InvalidParameter until the window is restored. Found on
        # Bayonetta: the game is exclusive fullscreen, the 32-bit panel is a
        # separate window, so alt-tabbing to reach the panel minimized the
        # game - the act of opening the panel was tearing the feature down.
        # Worth separating from a real resolution change: the advice differs.
        builds = re.findall(r"building: (\d+)x(\d+)", text)
        sizes = {f"{w}x{h}" for w, h in builds}
        tiny = sorted(s for s in sizes
                      if int(s.split("x")[0]) < 640 or int(s.split("x")[1]) < 360)
        rest = sorted(sizes.difference(tiny))
        # The feed builds at whatever size the swap chain reports. In a
        # bordered window that is the CLIENT area - a few pixels short of the
        # display - and the neural result then never lands on screen:
        # everything reports success and the picture is untouched, at any
        # setting. Measured on Bayonetta, where three builds at 1920x1071 did
        # nothing and the first build at a true 1920x1080 worked at once.
        _big = _biggest(rest)
        short = sorted(s for s in rest if _near(s, _big))
        if short:
            rep.add(WARN, f"The game was presenting a few pixels short of its "
                          f"display size ({', '.join(short)} against {_big}).",
                    "That is a bordered window: the swap chain is the client "
                    "area, and the neural result does not land on the screen "
                    "- everything reports success and nothing changes, "
                    "whatever you set. Use borderless or true fullscreen so "
                    "the game presents at the full display size, then switch "
                    "neural rendering on (F6).")
        if tiny:
            rep.add(WARN, f"The game window was minimized while the feed was "
                          f"running (rebuilt at {', '.join(tiny)}).",
                    "DLSS cannot be created that small, so every attempt fails "
                    "until the window comes back. Alt-tabbing out of an "
                    "exclusive-fullscreen game minimizes it - and on 32-bit "
                    "the DLSS 5 panel is a separate window, so opening it does "
                    "exactly that. The panel's settings are saved, so set them "
                    "once, then restart and play without alt-tabbing - or run "
                    "the game windowed / borderless.")
        if len(rest) > 1 and not short:
            rep.add(WARN, f"Rebuilt at {len(rest)} different resolutions "
                          f"({', '.join(rest)}).",
                    "Changing resolution while neural rendering is on forces a "
                    "rebuild and is a common cause of freezes.")

    # The standalone add-on keeps a log of its own, outside the folder; it
    # says more than ReShade.log ever can on this route.
    if rep.route == "standalone":
        return _analyse_standalone(rep, since, bool(rtext))

    # neural-upstream writes its whole run into ReShade.log under [NRPRE].
    # Older builds of the add-on wrote nothing, and those still fall through
    # to the "confirm it in the overlay" answer below.
    if rep.route == "upstream" and "[NRPRE]" in rtext:
        return _analyse_upstream(rep, rtext)

    # --- the decisive part, from whichever log has it --------------------
    joined = "\n".join(x for x in (text, htext, rtext) if x)
    # The feeder's own crash handler: "### CRASH RECORDED ###  exception
    # 0xC0000005 at ... in Game.exe; this add-on was last doing: <step>" and
    # a dump path on the next line. Frames may have been delivered just
    # before, so this has to be read before "Working." is declared.
    # Driver 616.64+ with renodx-dlss5 4.6/4.7: the helper (or the feed)
    # catches an access violation inside D3D12Core.dll on every evaluate and
    # the game simply never gets a neural frame. The feeder's 0.14 builds
    # print the module chain; the chain is the signature.
    # 0.14.0-beta.5's 64-bit feed log puts the module chain on the line
    # after "evaluate raised" without naming D3D12Core.dll on the first
    # (issue #37); the host log names it on both. The chain alone is the
    # signature, so the first line only has to be the access violation.
    drv = re.search(r"evaluate raised 0xC0000005", joined)
    if drv and re.search(r"D3D12Core\.dll\s*<-\s*nvngx_dlssnr\.dll\s*<-\s*_nvngx\.dll\s*<-\s*renodx-dlss5", joined):
        have = str((man.get("components") or {}).get("renodx") or "")
        pinned = have.startswith("4.5")
        _m = man or {}
        _sa = _dlss_mod().standalone_fits(str(_m.get("api") or ""),
                                          _m.get("bitness"))
        rep.add(BAD, "Every DLSS evaluate faults inside NVIDIA's NGX runtime "
                     "(D3D12Core.dll <- nvngx_dlssnr.dll <- _nvngx.dll <- "
                     "renodx-dlss5).",
                "This is NVIDIA driver 616.64 or newer: the driver routes the "
                "feature into the runtime itself and the renodx-dlss5 add-on "
                "does not survive it (measured by the feeder's author, "
                "DLSS5-Feeder #54). "
                + (f"This install already carries {have}, the build that "
                   f"passes on most games there, so on this one the fault is "
                   f"in the driver's runtime itself and no renodx-dlss5 "
                   f"build this tool can install changes it. "
                   if pinned else
                   "Install again: the tool pins the add-on to 4.55 on these "
                   "drivers, which passes on most games. ")
                + ("Worth trying before the driver: the standalone route, "
                   "which runs its own feed and never goes through "
                   "renodx-dlss5, and for games that ship DLSS the bridge "
                   "route, which works around the fault in memory. Rolling "
                   "the driver back to 616.56 is the surer test."
                   if _sa else
                   "The bridge route works around it in memory for games that "
                   "ship DLSS; otherwise rolling the driver back to 616.56 is "
                   "the answer."))
        # The shared results are what put standalone first here: the same
        # game failed three times on the feeder route on 616.92 and then
        # worked twice on standalone, from the same person. The fault lives
        # in renodx-dlss5's path through the driver, and standalone does not
        # take that path - so it is the answer that needs no rollback.
        _alt = "try the standalone route, or " if _sa else ""
        rep.verdict = (f"Driver 616.64+ faults inside NGX even with renodx-dlss5 "
                       f"{have} - {_alt}roll the driver back to 616.56."
                       if pinned else
                       "Driver 616.64+ faults with renodx-dlss5 4.6/4.7 - install "
                       "again (the tool pins 4.55)"
                       + (", or try the standalone route." if _sa else "."))
        return rep
    # An external frame pacer between the feed and the screen. The feeder
    # measures it (presents against frames fed) and NVIDIA Smooth Motion is
    # the usual one: it presents interpolated frames the feed never made, and
    # on Vulkan the two together take the game down (issue #63).
    pacer = re.search(r"an external frame pacer is presenting this swapchain:"
                      r"\s*([^\n]*)", joined)
    if pacer:
        rep.add(WARN, "Something else is pacing the frames this game presents.",
                "The feed counted more presents than frames it fed ("
                + pacer.group(1).strip()[:120] +
                "). NVIDIA Smooth Motion does exactly this. Turn it off for "
                "this game in the NVIDIA app (Graphics -> this game -> Smooth "
                "Motion) and start it again: where a game draws with Vulkan, "
                "this combination has been reported to take it down.")

    rec = re.search(r"### CRASH RECORDED ###\s+exception (0x[0-9A-Fa-f]+)[^\n]*?"
                    r"last doing: ([^\n]+)", joined)
    if rec:
        dump = re.search(r"crash dump written: ([^\n]+?)\s+--", joined)
        where = (f" and the dump ({dump.group(1).strip()}; zip it, it shrinks "
                 f"to a few MB)" if dump else "")
        # Whose crash is it? The feeder's handler catches every crash in the
        # process, its own and the game's alike, and prints the module chain.
        chain = _fault_chain(joined, "crash")
        inner = chain[0] if chain else ""
        mine = [c for c in chain if _in(c, _OURS_IN_STACK)]
        pacer_note = (" The feed also measured an external frame pacer on "
                      "this swapchain (above): that is the first thing to "
                      "turn off." if pacer else "")
        exe_name = Path(str(man.get("exe") or "")).name.lower()
        inner_is_game = bool(exe_name) and inner.lower() == exe_name
        if chain and inner_is_game and not mine:
            # The innermost frame is the game, and nothing of ours appears
            # anywhere below it. The feeder only recorded what it saw.
            rep.add(BAD, f"The game crashed ({rec.group(1)}), and the feed "
                         f"recorded it while {rec.group(2).strip()}.",
                    "The stack the feeder wrote down is "
                    + " <- ".join(chain[:4]) +
                    " - the game's own code, with no add-on of ours in it. "
                    "Nothing of ours raised this fault, so another feeder "
                    "build is unlikely to change it." + pacer_note +
                    " Next, one at a time: turn off any overlay that draws "
                    "in this game (Smooth Motion, the NVIDIA overlay, "
                    "Discord, RivaTuner), and if it still happens, uninstall "
                    "here and confirm the game is stable on its own.")
            rep.verdict = ("The crash is in the game's own code - no add-on "
                           "in the stack.")
        elif chain and _in(inner, _NGX_IN_STACK):
            _m = man or {}
            _sa = _dlss_mod().standalone_fits(str(_m.get("api") or ""),
                                              _m.get("bitness"))
            rep.add(BAD, f"The crash ({rec.group(1)}) is inside the "
                         f"Direct3D 12 / NGX runtime ({inner}), reached "
                         f"through "
                    + (mine[0] if mine else "the DLSS 5 add-on") + ".",
                    "The stack is " + " <- ".join(chain[:5]) + ". The feed "
                    "asked the runtime for a neural frame and the runtime "
                    "faulted, so no feeder build changes it. Try another "
                    "'DLSS 5 add-on' build from the install page, and if the "
                    "driver is 616.64 or newer, "
                    + ("try the standalone route (it does not load "
                       "renodx-dlss5), or " if _sa else "")
                    + "roll it back to 616.56.")
            rep.verdict = ("The crash is in the graphics runtime, not in the "
                           "feed - try another DLSS 5 add-on build.")
        else:
            named = (f" The stack is {' <- '.join(chain[:4])}." if chain else "")
            rep.add(BAD, f"The feed recorded a crash ({rec.group(1)}) while "
                         f"{rec.group(2).strip()}.",
                    "That is the feeder itself going down, not the install."
                    + named + pacer_note + " Two things to try from the "
                    "install page: another 'feeder build' from the list (the "
                    "stable release is the long-tested 32-bit path), and a "
                    "lower work resolution. Then report it to the "
                    "DLSS5-Feeder project with this log" + where + ".")
            rep.verdict = ("The feed crashed after starting - a feeder bug; "
                           "try another feeder build.")
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

    # A failed hook of NVSDK_NGX_..._EvaluateFeature. What it means depends on
    # the driver first and on the line second, and it took three readings to
    # get that order right:
    #  - #75, driver 610.60: a driver from before DLSS 5, and the verdict
    #    "update the driver" was right.
    #  - #127, driver 616.92: the same line was answered "update", on a
    #    driver newer than the one asked for.
    #  - On 616.64 the add-on hooks the plain EvaluateFeature and logs
    #    "Failed to find ..._C" for the variant beside it, in sessions that
    #    then deliver frames (the owner's own video-player and mirror logs).
    #    So on a driver that carries DLSS 5 the line is not a driver verdict.
    # The driver's age is therefore read whenever the line is there, and it
    # decides; the line only decides when the driver cannot be read.
    hookfail = re.search(
        r"vtable::Hook\(Failed to find (NVSDK_NGX_\w+_EvaluateFeature\w*)",
        rtext or "")
    # "hooked" directly after the name today; tolerate a space in case a
    # later add-on build logs it that way.
    plain_hooked = re.search(
        r"vtable::Hook\(NVSDK_NGX_\w+_EvaluateFeature\w*\s*hooked", rtext or "")
    drv = ""
    old_driver = None
    if hookfail:
        try:
            from . import gpu as _gpu
            drv = _gpu.driver_version() or ""
            if drv:
                ok = _gpu.driver_at_least("616.56", drv)
                old_driver = None if ok is None else not ok
        except Exception:
            pass
    too_old = old_driver is True or (old_driver is None and not plain_hooked)
    # ...and only when nothing else worked: one failed hook on a D3D11
    # variant, while the D3D12 path ran fine and delivered frames, would
    # otherwise turn "Working." into "update your driver".
    if hookfail and too_old and not delivered and not ready and not crash:
        rep.add(BAD, (f"This machine's driver, {drv}, is older than DLSS 5 "
                      f"itself."
                      if old_driver else
                      "The driver's NGX runtime does not export the call the "
                      "add-on hooks."),
                f"The add-on logged 'Failed to find {hookfail.group(1)}'. "
                "616.56 is the first driver documented to carry the "
                "neural-rendering runtime"
                + (f", and this machine reports {drv} - if that is the "
                   f"driver the game ran with, nothing else in this folder "
                   f"can make up for it. Update the graphics driver and "
                   f"install again." if old_driver else
                   ", and this machine's driver version could not be read - "
                   "if it is older than that, nothing else in this folder can "
                   "make up for it. Check the driver is 616.56 or newer."))
        rep.verdict = ("The driver has no DLSS 5 entry point - update the "
                       "graphics driver." if old_driver else
                       "The add-on found no DLSS 5 entry point to hook - check "
                       "the driver is 616.56 or newer.")
        return rep
    if hookfail and old_driver is False and not plain_hooked \
            and not delivered and not ready and not crash:
        # A driver that carries DLSS 5, and nothing of this entry point was
        # hooked at all: that is a real inability to intercept, just not a
        # question of the driver's age. Said as what it is, and the rules
        # below still get their turn.
        rep.add(WARN, f"The add-on could not hook {hookfail.group(1)}.",
                f"On driver {drv} this is not the driver being too old. No "
                f"other EvaluateFeature call shows as hooked either, so this "
                f"session may have had nothing to run the model on - the "
                f"add-on's panel in the game says for certain.")

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
        # Frames came, and then the feed said it stopped - "the 64-bit host
        # went away" on a 32-bit game, and the game carries on without the
        # pass. Octowow (#156) was called "Working." on exactly that log
        # whenever the helper's own log was not in the folder to name the
        # fault.
        last_frame = max((m.end() for m in re.finditer(
            r"frame \d+ (?:delivered|evaluated)", text or "")), default=-1)
        stop = None
        for m in re.finditer(r"stopped: ([^\n]+)", text or ""):
            if m.start() > last_frame:
                stop = m
        if old_compiler:
            rep.add(BAD, "The game's own d3dcompiler_47.dll is too old for "
                         "the neural pass.", _COMPILER_FIX)
            rep.verdict = ("Frames flow, but neural rendering is silently doing "
                           "nothing - old d3dcompiler_47.dll in the game folder.")
        elif stop is not None:
            why = stop.group(1).split(" -- ")[0].strip().rstrip(".")
            rep.add(BAD, f"The feed stopped after frame {delivered[-1]}: {why}.",
                    "The game kept running without the pass from that point. "
                    + ("On a 32-bit game the pass runs in a 64-bit helper; "
                       "host64\\dlss5-feed-host.log, when it is there, says why "
                       "it ended. If that is a crash inside NVIDIA's runtime "
                       "on driver 616.64 or newer, 616.56 is the test."
                       if "host" in why else
                       "dlss5-feed.log has the lines just before it."))
            rep.verdict = "It started, then the feed stopped - see why below."
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
        if rep.route == "upstream":
            rep.add(INFO, "If the picture only gets darker, switch the route "
                          "to native.",
                    "neural-upstream normalises the frame against the game's "
                    "exposure buffer, and some games do not expose one; the "
                    "native route runs after the game's own tone mapping.")
        rep.verdict = (f"Add-ons loaded. Confirm in the {panel} - this "
                       f"route does not log frames.")
        if rep.route == "bridge" and man.get("native_dlss") is False:
            # #127: an unstamped settings file is replaced by the bridge with
            # its defaults, substitute off - and a game with no DLSS of its
            # own then gives the bridge nothing to work on.
            try:
                from . import feedcfg as _fc
                cfgp = Path(install_dir) / _fc.BRIDGE_NAME
                first = cfgp.read_text(encoding="utf8",
                                       errors="replace").split("\n", 1)[0].strip()
                vals = _fc.read(cfgp)
                on = (int(_fc.number(vals.get("synth", 0))) != 0
                      or int(_fc.number(vals.get("synth_after", 0))) > 0)
            except (OSError, ValueError, OverflowError):
                first, on = _fc.BRIDGE_STAMP, True
            if first != _fc.BRIDGE_STAMP and not on:
                rep.add(BAD, "The bridge replaced the settings this install "
                             "wrote.",
                        "This game has no DLSS of its own, so the bridge needs "
                        "its substitute contract, which the install switches "
                        f"on in {_fc.BRIDGE_NAME}. That file does not start "
                        f"with '{_fc.BRIDGE_STAMP}', so the bridge replaced it "
                        "with its defaults, where the substitute is off. "
                        "Install the bridge route again.")
                rep.verdict = ("The bridge's substitute is off - install the "
                               "bridge route again.")
    else:
        # A log that stops at the hooks it installed is not "did not get far
        # enough" in some vague way: everything the feed can say about the
        # picture starts from the effect runtime ReShade hands it, and that
        # log says it never got one (Web of Shadows, #164: eight lines, and
        # the answer named none of them). Which of the two ends is at fault is
        # not in this log, so ask for what would tell us instead of guessing.
        #
        # The absence of ONE wording is not that evidence, though - the word
        # "effect runtime" is one build's. Ask for the absence of every line
        # that can only be written once a runtime exists: a technique state, a
        # build, a frame. With any of those present this is a different answer
        # and the findings above have already given it.
        if _attached(text) \
                and not _FEED_GOT_RUNTIME.search(text or "") \
                and not _FEED_GOT_RUNTIME.search(htext or ""):
            rep.add(WARN, "The add-on loaded, and ReShade never handed it an "
                          "effect runtime.",
                    "Everything the feed does starts from that runtime, and "
                    "its log stops at the hooks it installed. Open the "
                    "ReShade overlay in the game and check that 'DLSS 5 Feed' "
                    "is ticked in the effect list - and if it is, send "
                    "dlss5-feed.log and ReShade.log whole: this pair of logs "
                    "cannot say which side stopped.")
            rep.verdict = ("The add-on loaded but ReShade never gave it an "
                           "effect runtime - check 'DLSS 5 Feed' is ticked in "
                           "the overlay.")
        else:
            rep.verdict = "Inconclusive - the feed did not get far enough to tell."

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
                "(Home) and switch it on in the " + UPSTREAM_PANEL + ".")
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
            "Open the ReShade overlay (Home), the " + UPSTREAM_PANEL +
            ", and check it is switched on; then play a few seconds and "
            "check again.")
    rep.verdict = "Loaded and set up; no neural frame yet."
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
        rep.never_ran = True
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
        rep.never_ran = True
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

# "EvaluateFeature": the hook lines are DEBUG, and the driver rule reads
# them - a report without them cannot be replayed to the same answer.
_RESHADE_KEEP = ("WARN", "ERROR", "Registered add-on", "CreateSwapChain",
                 "Direct3DCreate9", "Exiting", "EvaluateFeature")
# What a report's ReShade.log excerpt gives up last when it is over budget:
# the lines the diagnosis itself reads, then which add-ons loaded.
_RESHADE_FIRM = ("EvaluateFeature",)
_RESHADE_ALSO = ("Registered add-on",)
_HOOK_ADDRESSES = re.compile(r" with 0x[0-9A-Fa-f]+ => 0x[0-9A-Fa-f]+")


def _reshade_excerpt(text: str, n: int = 25, budget: int = 1500) -> list[str]:
    """The ReShade.log lines a report carries, fitted to its budget.

    The excerpt used to lose its oldest lines first, and the hook lines are
    written at the start of a session - so the "hooked" line went and the
    "Failed to find" beside it stayed, which replays to the opposite answer.
    Over budget, the lines nothing reads go first.
    """
    kept = [ln for ln in text.splitlines() if any(k in ln for k in _RESHADE_KEEP)]
    if not kept and text.strip():
        # A log with none of those lines is still a log: ReShade started and
        # stopped before any add-on registered. The report printed "(none)"
        # for it (#155), which reads as "ReShade never loaded" - the opposite
        # - and left nothing to replay.
        return [_HOOK_ADDRESSES.sub("", ln.rstrip())[:200]
                for ln in text.splitlines() if ln.strip()][-min(n, 12):]
    # A 250 KB tail can hold thousands of these; only the newest of each
    # kind can end up in the excerpt, so the rest are not looked at twice.
    firm = set(sorted(i for i, ln in enumerate(kept)
                      if any(k in ln for k in _RESHADE_FIRM))[-8:])
    firm |= set(sorted(i for i, ln in enumerate(kept)
                       if any(k in ln for k in _RESHADE_ALSO))[-8:])
    rest = [i for i in range(len(kept)) if i not in firm][-n:]
    lines = [_HOOK_ADDRESSES.sub("", kept[i].rstrip())[:200]
             for i in sorted(firm | set(rest))]
    for spare in (lambda ln: not any(k in ln for k in _RESHADE_FIRM + _RESHADE_ALSO),
                  lambda ln: not any(k in ln for k in _RESHADE_FIRM),
                  lambda ln: True):
        while len(lines) > n or len("\n".join(lines)) > budget:
            i = next((i for i, ln in enumerate(lines) if spare(ln)), None)
            if i is None:
                break
            del lines[i]
    return lines


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


def _their_provider(install_dir: Path, prov: str, man: dict) -> str:
    """Where the person's own copy of the provider shader is, or "".

    The installer leaves a pack that is already there alone (a second
    technique of the same name is a red error in ReShade's overlay), so the
    place the install WOULD have written to is empty by design.

    Only when it really did not write it. A file OUR install wrote and
    something has since removed is the quarantine case #13 and #84 exist for,
    and "your own copy is used" would hide it - so the manifest's own file
    list has the last word, and is what the installer itself asks.
    """
    want = ("reshade-shaders/shaders/" + prov).lower()
    ours = [f for f in (man.get("files") or []) if isinstance(f, str)]
    if any(f.replace("\\", "/").lower() == want for f in ours):
        return ""                       # we wrote it; it is gone, say so
    try:
        from . import installer as _inst
        hit = _inst.foreign_lumenite(Path(install_dir), ours, marker=prov)
    except Exception:
        return ""
    if not hit:
        return ""
    try:
        return str(Path(hit).relative_to(install_dir)).replace("\\", "/")
    except ValueError:
        # Can only come out of rglob under install_dir, so this is
        # unreachable - and a full path must never reach a published report.
        return ""


# DLSS5_MV_PROVIDER -> the shader file the feeder needs for it.
PROVIDER_FX = {2: "vort_Motion.fx", 3: "lumenite_Kernel.fx",
               4: "lumenite_QuantMotion.fx"}


# A swapped ray-reconstruction runtime is invisible in a bug report
# otherwise, and it is exactly the kind of thing that explains one.
RR_RUNTIME = "nvngx_dlssd.dll"
# installer.BACKUP_SUFFIX, spelled here so this module does not import
# the installer for one string (it already avoids that everywhere).
_BACKUP_SUFFIX = ".dlss5-autopilot-backup"


def _presence(install_dir: Path, man: dict, route: str) -> list[str]:
    """One line per file that decides whether anything can load at all."""
    names: list[str] = []
    extra: list[str] = []
    proxy = man.get("proxy")
    if man.get("vr"):
        try:
            from . import openxr
            reg = openxr.existing_registration()
        except Exception:
            reg = None
        extra.append("- ReShade OpenXR layer (VR): "
                     + ("registered" if reg else "NOT REGISTERED - install again"))
    if proxy == VULKAN_LAYER:
        # Not a file, so it cannot be looked for in the folder. It used to be
        # reported as "MISSING" here, which sent people hunting through their
        # antivirus quarantine for a file that never existed.
        any_layer, mine = _layer_state(man)
        bits = 32 if man.get("bitness") == 32 else 64
        extra.append(f"- ReShade {bits}-bit Vulkan layer: "
                     + ("registered" if mine else
                        "NOT REGISTERED"
                        + (" (another ReShade layer is, for the other "
                           "architecture)" if any_layer else "")))
    elif proxy:
        names.append(proxy)
    names += _addons(man)
    if route == "standalone":
        names.append("nvngx.dll")
    if route == "remix":
        # Nothing of ours sits beside the executable on this route; the files
        # that decide whether it can work are all inside .trex.
        from . import remix as _remix
        trex = _remix.find_runtime(install_dir)
        out = [f"- .trex: {'found at ' + str(trex) if trex else 'MISSING'}"]
        if trex is not None:
            for n in (_remix.RUNTIME_DLL, _remix.DLSSNR, _remix.REMIX_NVNGX):
                state = "present" if (trex / n).is_file() else "MISSING"
                out.append(f"- {trex.name}/{n}: {state}")
            out.append(f"- runtime flavour: "
                       f"{_remix.runtime_flavour(trex) or 'no DLSS 5 pass'}")
        rx = man.get("remix") or {}
        if rx.get("key"):
            conf = Path(rx.get("conf") or "")
            if not conf.is_absolute():
                conf = install_dir / conf
            out.append(f"- {rx['key']}: "
                       f"{'set' if _remix.option_set(conf, rx['key']) else 'NOT SET'}")
        return out
    # A ray-reconstruction runtime this install swapped, wherever the game
    # keeps it: invisible in a report otherwise, and exactly the kind of
    # thing that explains one.
    rr = (man.get("components") or {}).get("dlssd")
    if rr:
        where = next((f for f in (man.get("files") or [])
                      if isinstance(f, str)
                      and f.replace("\\", "/").lower().endswith(RR_RUNTIME)
                      and not f.endswith(_BACKUP_SUFFIX)), RR_RUNTIME)
        there = (install_dir / where).is_file()
        extra.append(f"- {where}: {'present' if there else 'MISSING'} "
                     f"(swapped to {rr}; the game's own is backed up)")
    if route == "optiscaler":
        # Three builds can be installed here and their packages differ; a
        # report that does not say which one is unanswerable.
        extra.append("- optiscaler build: "
                     + (str(man.get("opti_build") or "") or "Dagherbou"))
    else:
        names.append("ReShade.ini")
    names += _dxvk_files(man)
    # On the 32-bit feeder route the DLSS runtimes live in host64/ beside the
    # helper, not next to the game - looking for them in the folder reported
    # "nvngx_dlssnr.dll: MISSING" on installs that were perfectly fine.
    if man.get("bitness") == 32 and route == "feeder":
        names.append("host64/nvngx_dlssnr.dll")
    else:
        names.append("nvngx_dlssnr.dll")
    prov = None
    if route == "feeder":
        # The feed is a shader technique plus a motion-vector provider; when
        # either file is gone the add-ons load and nothing happens (issue
        # #13, Dying Light: "DLSS5_Feed.fx never loaded" with no way to see
        # from the report whether the file was there).
        names.append("reshade-shaders/Shaders/DLSS5_Feed.fx")
        prov = PROVIDER_FX.get(man.get("provider"))
        if prov:
            names.append("reshade-shaders/Shaders/" + prov)
    out = []
    for n in dict.fromkeys(names):
        state = "present" if (install_dir / n).is_file() else "MISSING"
        out.append(f"- {n}: {state}")
        if state == "MISSING" and route == "feeder" and prov \
                and n.endswith(prov):
            # The install does not write this one when the person already has
            # the pack: their copy is used, wherever they keep it under
            # reshade-shaders. Reporting the place we would have written to as
            # MISSING sent Web of Shadows (#164) looking for a file the
            # install had deliberately not put there.
            mine = _their_provider(install_dir, prov, man)
            if mine:
                # Whether ReShade loads that copy depends on its own
                # EffectSearchPaths, which nothing here reads - so say where
                # the file is, not that it is the one in use.
                out[-1] = (f"- {n}: not written by this install - your own "
                           f"copy is at {mine}")
    out += extra
    # The game's own compiler beside the exe is the cause of the silent
    # "frames flow, nothing happens" case; worth a line whenever it is there.
    if (install_dir / "d3dcompiler_47.dll").is_file():
        out.append("- d3dcompiler_47.dll: present (the game's own)")
    elif any(install_dir.glob("d3dcompiler_47.dll.*")):
        out.append("- d3dcompiler_47.dll: renamed aside")
    return out


# What the library scan writes about every game on the machine. In a report
# about one game these lines were the whole excerpt - "Minecraft for
# Windows ... is not readable yet" in a report about Enshrouded (#130), RPCS3
# search budgets in one about Remember Me (#134).
_SCAN_NOISE = re.compile(
    r"scan \S+: \d+ found|is not readable yet|an earlier install is recorded "
    r"in|the store names|could not read |no executable under|inspected .+ in "
    r"[\d.]+s|checked .+ in [\d.]+s|scan: .+ is the same executable|stopped "
    r"looking for")


def _tool_log_lines(tail: str, game, install_dir, n: int = 15) -> list[str]:
    """The tool's own log for a report: this run, and not the other games."""
    text = tail or ""
    cut = text.rfind("=" * 70)
    if cut >= 0:
        text = text[cut:]
    marks = [m.lower() for m in (getattr(game, "name", "") or "",
                                 str(install_dir or "")) if m]

    def keep(ln: str) -> bool:
        if not _SCAN_NOISE.search(ln):
            return True
        low = ln.lower()
        return any(m in low for m in marks)
    return _last_lines(text, n, keep)


def issue_body(version: str, gpu_name: str, sm, driver: str, game, route: str,
               last_diag, autopilot_tail: str, autopilot_log_path,
               install_dir, last_error: str = "",
               answers: dict | None = None, crash=None) -> str:
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
    # Asked in the tool now (reportui). A template nobody filled in is how
    # four of the nine reports on the first day of 1.7.3 arrived with the
    # "yes / no / it closed itself" line untouched and nothing else said.
    if answers:
        said = str(answers.get("happened") or "").strip()
        asked = (f"**Did the game start?** {answers.get('started') or '-'}\n\n"
                 f"**What happened**\n{said}\n\n")
    else:
        asked = ("**Did the game start?** yes / no / it closed itself\n\n"
                 "**What happened**\n\n\n"
                 "**What I expected**\n\n\n")
    head = (
        asked
        + "---\n"
        f"- version: {version}\n"
        f"- gpu: {gpu_name} (sm_{sm}), driver {driver}\n"
        f"- game: {getattr(game, 'name', None) or '-'}\n"
        f"- exe: {exe}\n"
        f"- arch/api: {getattr(game, 'bit_label', None) or '-'} / "
        f"{getattr(game, 'api', None) or '-'}"
        + (f" ({game.api_why})" if getattr(game, 'api_why', None) else "") + "\n"
        f"- route: {route or '-'}\n"
        # What Windows recorded, when there is one: the faulting module is
        # the most useful line a "the game closed itself" report can carry,
        # and nobody was attaching it because nobody knew to look.
        + (f"- windows event: {crash.exe} faulted in {crash.module} "
           f"{crash.code} at {crash.when} UTC\n"
           if crash is not None and getattr(crash, "module", "") else "")
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
    # The last session only, as analyse() reads it: ReShade.log is never
    # truncated, and the excerpt's priorities pulled hook lines and add-on
    # builds out of older sessions over the last one's own errors.
    parts.append(_block("ReShade.log",
                        _reshade_excerpt(_last_session(reshade)), 1500))
    parts.append(_block("dlss5-feed.log", _last_lines(feed, 20), 1400))
    if route == "optiscaler":
        parts.append(_block("OptiScaler.log", _last_lines(opti, 20), 900))
    if route == "standalone":
        parts.append(_block("standalone-dlssnr.log",
                            _last_lines(_tail(STANDALONE_LOG, 100_000), 20), 900))
    if route == "remix" and d is not None:
        # Only the lines that say anything about the neural pass: the Remix
        # log is enormous and the rest of it is path-tracing chatter.
        from . import remix as _remix
        rtx_log = _tail(_remix.log_path(d), 300_000)
        nr_lines = _last_lines(
            rtx_log, 20, lambda ln: "DLSS-NR" in ln or "dlssnr" in ln.lower()
            or "Neural" in ln)
        # A log with no such line is itself the answer (the runtime never
        # tried the pass) - say so, with its last lines, rather than "(none)",
        # which reads as no log at all (#155 had the same shape).
        if not nr_lines and rtx_log.strip():
            nr_lines = (["(no DLSS-NR line in this log - its last lines:)"]
                        + _last_lines(rtx_log, 8))
        parts.append(_block("remix-dxvk.log", nr_lines, 1200))
    if last_error:
        parts.append(f"\n**Last error**\n```\n{last_error[-900:]}\n```\n")
    parts.append(_block(
        f"autopilot.log (`{autopilot_log_path}`)",
        _tool_log_lines(autopilot_tail or "", game, install_dir), 900))
    body = "".join(parts)
    return body[:6000]
