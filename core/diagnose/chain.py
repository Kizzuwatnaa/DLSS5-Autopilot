r"""analyse(): the order the evidence is read in.

The order IS the diagnosis - a rule put in front of the others
changes what all of them see, which is why every change here is
replayed against all 96 saved reports.

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
from .process import *  # noqa: F401,F403
from .helper import *  # noqa: F401,F403
from .routes import *  # noqa: F401,F403
from .body import *  # noqa: F401,F403


__all__ = [
    "_explain_no_log", "analyse"
]

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
        from .. import dxvk as _dxvk
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
                from .. import vulkan as _vk
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

    # Did the game itself run? Its own files answer that, and the answer
    # decides which of these is the headline (#182 and 33 others).
    _ours_files = {str(f).replace("\\", "/").rsplit("/", 1)[-1].lower()
                   for f in (man.get("files") or []) if isinstance(f, str)}
    _ran_what, _ran_when = _game_ran(install_dir, str(man.get("exe") or ""),
                                     _installed_at(install_dir) or 0.0,
                                     _ours_files)
    if _ran_what:
        _clock = datetime.fromtimestamp(_ran_when).strftime("%d %b %H:%M")
        rep.add(BAD, f"Something in the {app}'s own files changed after "
                     f"the install.",
                f"{_ran_what}, {_clock} - so it looks as though it has been "
                f"run since, though a store update writes into a game folder "
                f"too. ReShade writes ReShade.log the moment it loads and "
                f"there is none, and everything is still in place: if it did "
                f"run, it is the loading that failed rather than the "
                f"install.")
    elif rep.no_record:
        # Without the record there is no install time, so "has not been
        # started SINCE the install" is a sentence this cannot support: a
        # log from a run before the install and no log at all read the
        # same. Say only what is true - there is no log.
        rep.add(WARN, f"Nothing has loaded ReShade in this folder.",
                "ReShade writes ReShade.log the moment it loads, and there is "
                "none here. With the install record gone there is no install "
                "time to measure against either, so this cannot tell an "
                "install that was never run from one whose files were "
                "removed. Install again, play once, and press this again.")
    else:
        rep.add(WARN, f"The {app} has not been started since the install.",
                "ReShade writes ReShade.log the moment it loads, and there is "
                "none in the folder. Nothing the game itself writes has "
                "changed since the install either. All the files are still in "
                "place.")
    # Before any of the guesses: if the game is up right now, none of this
    # has to be guessed at all.
    if _live_evidence(install_dir, man, rep):
        # The process was seen, so every guess written above is no longer
        # true, and #238's report printed one right above the sighting. The
        # same prune the window applies to a fault record, from the same
        # list - one fragment against the title alone left the rest of them
        # on the screen (gate 1.9.1).
        rep.findings = model.drop_never_ran(rep.findings)
        return rep

    # Then: an executable that is a launcher explains all of them, and it is
    # the one cause in this branch that can be read off the install itself
    # rather than guessed at (#191).
    if _launcher_installed(install_dir, str(man.get("exe") or "")):
        real = None
        try:
            from .. import pe as _pe
            real = _pe.real_exe_for(install_dir / Path(str(man["exe"])).name)
        except Exception:
            real = None
        rep.add(BAD, f"{exe} is a launcher, not the {app} itself.",
                f"A launcher starts the {app} as a separate program and then "
                f"hands over, so nothing put beside it is ever loaded by the "
                f"{app} - which is why every file here is in place and no log "
                f"was written. "
                + (f"{real.name} in this folder looks like the executable "
                   f"that draws: pick it in the game's details and install "
                   f"again."
                   if real is not None else
                   f"Point the tool at the executable the {app} itself runs "
                   f"from - usually under a Binaries or Bin folder - and "
                   f"install there."))
        rep.verdict = (f"The install went beside a launcher, not the {app} - "
                       f"install again beside the executable that draws.")
        rep.never_ran = True
        return rep
    rep.add(INFO,
            f"The likeliest reason: it launches something other than {exe}."
            if _ran_what else
            f"If you DID start it, it launches something other than {exe}.",
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
                "DirectX 11 or DirectX 12 in the 'graphics api' dropdown in the "
                "game's settings and install again.")
    elif proxy:
        alt = "d3d11.dll" if proxy.lower() == "dxgi.dll" else "dxgi.dll"
        # The same mapping the crash override uses (ctl_game.crash_overrides):
        # optiscaler calls it 'loads as'; the feeder carries its
        # motion-vector provider on that row and the ReShade name on one of
        # its own; Remix installs no ReShade, so there is nothing to name.
        # Naming a control that is not on the screen is #148's shape.
        # Only the ReShade routes reach this function - analyse() hands
        # optiscaler and remix to their own readers (:1389-1393) - so those
        # two branches never fire today. Kept in step with the window all
        # the same: a second copy that drifts is the bug itself.
        _drop = ("'loads as'" if rep.route == "optiscaler"
                 else "" if rep.route == "remix"
                 else "'reshade loads as'")
        rep.add(INFO, f"Or the {app} ignores {proxy}.",
                f"Some load the graphics DLLs in a way that skips {proxy}. "
                + (f"Set {_drop} to {alt} in the game's settings and install "
                   f"again." if _drop else
                   f"This route does not offer the proxy name on the page; "
                   f"if the {app} starts with nothing else in the folder, "
                   f"say so in an issue with this report."))
    rep.verdict = (f"It looks as though it ran and nothing this install "
                   f"wrote was loaded - most likely the proxy name or the "
                   f"executable."
                   if _ran_what else
                   f"No install record here and no ReShade.log - install "
                   f"again, then run the {app} once."
                   if rep.no_record else
                   f"Not started since the install - run the {app} once, then "
                   f"check again.")
    # Said in a way the caller can act on: Windows' own fault record for this
    # executable is proof the game DID start, and it outranks "there is no
    # log" (#171 - GTA5.exe faulted eleven minutes before the report was
    # written, and the answer told the person to run the game once).
    rep.never_ran = True
    return rep


def analyse(install_dir: Path, last_error: str = "") -> Report:
    """Read whatever logs apply to this install and explain the outcome.

    `last_error` is the tool's own last traceback (`log.last_error()`). It
    is only ever consulted for a folder nothing arrived in: an install that
    crashed is indistinguishable from one that never happened by looking at
    the folder, and the traceback is the only thing that tells them apart.
    """
    # A second DLSS hook in the session is said in the verdict, whichever
    # of the dozen returns below produced it (#250) - so it is applied here,
    # once, rather than beside each of them.
    foreign: list[str] = []
    return _name_foreign_hooks(_analyse(install_dir, last_error, foreign),
                               foreign)


def _analyse(install_dir: Path, last_error: str, foreign: list) -> Report:
    rep = Report()
    since = _installed_at(install_dir)
    man = _manifest(install_dir)
    rep.route = man.get("path") or ""

    # No install record in this folder. Everything below reads the logs as
    # "since the install" and, finding none, tells the person the game has
    # not been started since an install that never happened here - eight
    # reports got that answer (#43, #194 among them), and it blames them for
    # the tool's own mistake. The button is not gated on having installed:
    # a folder picked by hand, a game moved or reinstalled by its store, a
    # second copy, or the record removed by a cleanup all arrive here.
    if _manifest_file(install_dir) is None:
        ours = _anything_of_ours(install_dir)
        if not ours:
            # An install that crashed says so itself. Without this, the one
            # person who knows least about why their folder is empty is the
            # one being asked to work it out.
            if _crash_verdict(rep, last_error):
                return rep
            rep.add(BAD, "Nothing of this tool is in this folder.",
                    "There is no install record here and none of the files an "
                    "install writes. Either nothing has been installed for "
                    "this game yet, or it went to a different folder - the "
                    "one the executable that actually runs sits in. Pick the "
                    "game and press install, then play once and press did it "
                    "work? again.")
            rep.verdict = "Nothing is installed in this folder - install first."
            rep.ran = False
            rep.never_ran = True
            return rep
        rep.no_record = True
        # The record is also where the route is written, and the readers
        # below are chosen by it: without one, an OptiScaler folder was read
        # by the feeder's reader and answered about add-ons that route never
        # installs. The files say which route it was.
        rep.route = man["path"] = _route_from_files(install_dir)
        rep.add(WARN, "The install record is gone from this folder.",
                f"{ours} is here, so an install was made, but "
                f"{MANIFEST} - which records what was written, for which "
                f"game and by which route - is not. Something removed it: a "
                f"cleanup tool, the game's own file verification, or a "
                f"partial uninstall. What is read below is only what the logs "
                f"say; install again to restore the record.")

    # The install itself did not finish. The installer records that, and the
    # folder then holds whatever arrived before it stopped - which is why a
    # download cut short in the middle (a reset connection, issue #74) came
    # back here as a game that would not work, with the parts that never
    # downloaded reported as "MISSING" as if antivirus had eaten them.
    # Nothing below this can mean anything until the install is finished.
    if man.get("complete") is False:
        from .. import net as _net
        if _net.DISK_FULL_NOTE in (man.get("notes") or []):
            rep.add(BAD, "The install stopped because the drive was full.",
                    "Whatever had not been written yet is missing, which is "
                    "why files are listed as gone. Free up a few hundred MB "
                    "on the game's drive and on the one %LOCALAPPDATA% is on, "
                    "then install again.")
            rep.verdict = "The drive was full - free up space and install again."
            return rep
        stopped = next((n[len(_net.STOP_NOTE):] for n in (man.get("notes") or [])
                        if isinstance(n, str) and n.startswith(_net.STOP_NOTE)), "")
        if stopped:
            rep.add(BAD, "The install was stopped before it finished.",
                    f"It said: {stopped.rstrip('.')}. Whatever came before "
                    f"that step is in "
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
                "stopped - the log says why, and it is "
                "usually a download that was cut off. "
                + (f"Written and now gone: {', '.join(missing[:4])}"
                   f"{', ...' if len(missing) > 4 else ''}. " if missing else "")
                + "Install again (whatever downloaded is kept, so it "
                  "carries on), or 'uninstall' first if you would rather "
                  "start from a clean folder.")
        rep.verdict = "The install never finished - install again."
        return rep

    # The record says it finished, and the folder is empty anyway. That
    # happens when a re-install crashed over a record an earlier one wrote:
    # every rule below reads this as files that went missing after a good
    # install, and names antivirus for something the install never wrote
    # (#213 - "not started since the install", to somebody whose install
    # had just died on a DNS lookup).
    if not _anything_of_ours(install_dir) and _crash_verdict(rep, last_error):
        return rep

    # Which build of this tool set the folder up. A finding, never a
    # verdict: the install may well still work, but every rule below reads
    # files an older build wrote, and "install again" is the whole fix.
    # Nothing is said when the record does not carry it - the key is new,
    # and a record without it is silence, not an old build.
    _stale_install(rep, man)

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
    # A file with nothing in it is not a log. ReShade creates ReShade.log
    # when it attaches and a game that dies on the next breath leaves it
    # empty - which used to read as "ReShade ran and loaded no add-ons",
    # printed above a report block saying "(none)" (#182). Whitespace is
    # the same thing: #155 covered a log whose lines nothing reads, not a
    # log with no lines.
    if not rtext.strip():
        rtext = ""

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
        from .. import installer as _inst
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
        # "Beside ours" only when ours is in the session: with it missing,
        # "did not load" below is the answer and this would bury it.
        if ours or any("Feed" in n for n in loaded):
            foreign[:] = _foreign_hooks(loaded, rep.route)
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
            files = [h for h in hooks if h.lower().endswith((".addon64", ".addon32"))]
            if foreign:
                rep.add(BAD, "Another DLSS hook was loaded beside ours: "
                             + ", ".join(foreign),
                        "ReShade loads every add-on in the game folder, and "
                        "this one is another DLSS tool's: it works on the same "
                        "frame and the same NGX calls as this route's add-on, "
                        "and two at once end in nothing happening, flicker or "
                        "a crash. Move it out of the folder (or uninstall the "
                        "tool that put it there) and test with ours alone."
                        + (f" In this folder: {', '.join(files[:3])}."
                           if files else ""))
            plain = [n for n in others if n not in foreign]
            if plain:
                rep.add(WARN, "Other ReShade add-ons are loaded: " + ", ".join(plain),
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
                    "in the game's settings and install again - the game then "
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
                stext = _tail(model.STANDALONE_LOG, 150_000)
            except OSError:
                stext = ""
            attached = _STANDALONE_SESSION in stext
            if attached and since and not _fresh(model.STANDALONE_LOG, since):
                attached = False
        if "Registered add-on" not in rtext and not wrote and attached:
            rep.add(OK, "The add-on loaded - it wrote its own log in the "
                        "session this report reads.",
                    "ReShade's own log no longer holds the registration line "
                    "(a long log is read from its tail), but nothing except "
                    "ReShade loads this add-on.")
        if "Registered add-on" not in rtext and not wrote and not attached:
            if _reshade_died_early(rtext):
                rep.add(BAD, "ReShade attached and the session ended before "
                             "anything else happened.",
                        "Its log holds nothing but the start-up lines - no "
                        "add-on, no runtime, no swap chain - so the game was "
                        "gone a moment later. That is the game closing during "
                        "start-up rather than anything about the add-ons. "
                        "Uninstall (the game's own files go back), check it "
                        "starts on its own, then install again"
                        + ("." if rep.route in ("feeder", "remix", "optiscaler")
                           else " and try another name in the 'reshade loads "
                                "as' dropdown in the game's settings."))
            else:
                rep.add(BAD, "ReShade loaded no add-ons.",
                        "Add-on support requires the ReShade build WITH "
                        "add-ons, and AddonPath must point at the game "
                        "folder.")
            if (man.get("proxy") or "").lower() == "opengl32.dll":
                rep.add(INFO, "Or this log is from another program's ReShade.",
                        "This install went in as opengl32.dll, which only "
                        "loads when the game draws with OpenGL; Unity and "
                        "other engines name OpenGL among their backends yet "
                        "draw with Direct3D on Windows. Pick DirectX 11 or "
                        "DirectX 12 in the 'graphics api' dropdown in the "
                        "game's settings and install again.")
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
        return _analyse_standalone(rep, since, bool(rtext), install_dir, man)

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
                    "'dlss5 add-on' build in the game's settings, and if the "
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
                    + named + pacer_note + " Two things to try in the "
                    "game's settings: another 'feeder build' from the list (the "
                    "stable release is the long-tested 32-bit path), and a "
                    "lower work resolution. Then report it to the "
                    "DLSS5-Feeder project with this log" + where + ".")
            rep.verdict = ("The feed crashed after starting - a feeder bug; "
                           "try another feeder build.")
        return rep
    # The helper's own fault outranks everything the game's side says after
    # it: #252's helper logged "feature ready" and the picture was frozen,
    # and one delivered frame in the feed log would have read "Working.".
    # Only its log from the same launch speaks.
    if htext and _same_launch(feed, host) and (not since or _fresh(host, since)) \
            and _helper_verdict(rep, htext):
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
            from .. import gpu as _gpu
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
        # Half of what that overlay would show is in ReShade.ini, beside
        # the game: the add-on writes its own switch there. Asking somebody
        # to go and read it was 14 of the first 96 reports.
        # Only where that add-on is the one installed: the renodx route
        # ships ShortFuse's ([RENODX-DLSS]) and upstream ships
        # matiasLombo's, and a key left by an earlier install would have
        # been read as theirs.
        switch = (_addon_switch(install_dir, rep)
                  if rep.route in ("native", "bridge") else "")
        rep.add(INFO, "This route leaves no frame log of its own.",
                (f"Open the ReShade overlay and check the {panel}: it shows "
                 f"the live state, frame by frame.") if switch else
                (f"Open the ReShade overlay and check the {panel}: it shows "
                 f"the live state and whether it is switched on."))
        if rep.route == "upstream":
            rep.add(INFO, "If the picture only gets darker, switch the route "
                          "to native.",
                    "neural-upstream normalises the frame against the game's "
                    "exposure buffer, and some games do not expose one; the "
                    "native route runs after the game's own tone mapping.")
        rep.verdict = (
            "The add-ons are loaded and the neural pass is switched off in "
            "the add-on itself - turn it on in the overlay."
            if switch == "off" else
            f"Add-ons loaded and the switch is on. This route logs no "
            f"frames, so the {panel} is the only live picture."
            if switch == "on" else
            f"Add-ons loaded. Confirm in the {panel} - this "
            f"route does not log frames.")
        # Half of that confirmation is a fact we already have.
        _loaded_note(install_dir, man, rep)
        if rep.route == "bridge" and man.get("native_dlss") is False:
            # #127: an unstamped settings file is replaced by the bridge with
            # its defaults, substitute off - and a game with no DLSS of its
            # own then gives the bridge nothing to work on.
            try:
                from .. import feedcfg as _fc
                cfgp = Path(install_dir) / _fc.BRIDGE_NAME
                first = cfgp.read_text(encoding="utf8",
                                       errors="replace").split("\n", 1)[0].strip()
                vals = _fc.read(cfgp)
                # synth is the switch; synth_after is only its delay
                on = int(_fc.number(vals.get("synth", 0))) != 0
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
        # Frames shipped to the 64-bit helper prove a runtime existed, so
        # that answer is read first (#252).
        if _fed_the_helper(rep, text):
            pass
        elif _attached(text) \
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
        elif _reshade_died_early(rtext):
            rep.verdict = ("It started and closed during start-up - ReShade "
                           "attached and nothing else got to run.")
        else:
            rep.verdict = "Inconclusive - the feed did not get far enough to tell."

    return rep
