r"""The text of a bug report - what the person actually posts.

Rendering, not diagnosis: nothing here decides anything, and
everything here has to be true of the folder it describes.

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
from .helper import _helper_excerpt
from .layer import _dxvk_files


__all__ = [
    "_block", "_dlss_record_root", "_last_lines", "_presence",
    "_reshade_excerpt", "_their_provider", "_tool_log_lines", "issue_body"
]

def _reshade_excerpt(text: str, n: int = 25, budget: int = 1500) -> list[str]:
    """The ReShade.log lines a report carries, fitted to its budget.

    The excerpt used to lose its oldest lines first, and the hook lines are
    written at the start of a session - so the "hooked" line went and the
    "Failed to find" beside it stayed, which replays to the opposite answer.
    Over budget, the lines nothing reads go first.
    """
    every = text.splitlines()
    nrpre = _nrpre_picks(every)
    kept = [ln for i, ln in enumerate(every) if i in nrpre or any(k in ln for k in _RESHADE_KEEP)]
    held = {_HOOK_ADDRESSES.sub("", every[i].rstrip())[:200] for i in nrpre}
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
    firm |= {i for i, ln in enumerate(kept) if "[NRPRE] " in ln}
    firm |= set(sorted(i for i, ln in enumerate(kept)
                       if any(k in ln for k in _RESHADE_ALSO))[-8:])
    rest = [i for i in range(len(kept)) if i not in firm][-n:]
    lines = [_HOOK_ADDRESSES.sub("", kept[i].rstrip())[:200]
             for i in sorted(firm | set(rest))]
    for spare in (lambda ln: ln not in held and not any(k in ln for k in _RESHADE_FIRM + _RESHADE_ALSO),
                  lambda ln: ln not in held and not any(k in ln for k in _RESHADE_FIRM),
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
        from .. import installer as _inst
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


_RECORD_LEVELS = 4


def _dlss_record_root(install_dir: Path, game_root=None) -> Path | None:
    """The folder holding the dlss page's record for this install, or None.

    The page writes it in the game's root, which an Unreal game keeps three
    levels above the exe (ReadyOrNot/Binaries/Win64): the game's own folder
    when the caller knows it, else the first folder up from the exe's that
    holds one, a bounded climb that stops at the drive root.
    """
    from .. import dlssupdate as _du
    if game_root:
        try:
            if (Path(game_root) / _du.RECORD).is_file():
                return Path(game_root)
        except OSError:
            pass
    d = Path(install_dir)
    for _ in range(_RECORD_LEVELS + 1):
        try:
            if (d / _du.RECORD).is_file():
                return d
        except OSError:
            pass
        if d.parent == d:
            break
        d = d.parent
    return None


def _presence(install_dir: Path, man: dict, route: str, game_root=None) -> list[str]:
    """One line per file that decides whether anything can load at all.

    `game_root` is the game's own folder when the caller knows it (the dlss
    page's record lives there)."""
    names: list[str] = []
    extra: list[str] = []
    # Whether there is an install record at all has to be IN the report.
    # Without it the list simply had fewer lines in it, and a folder this
    # tool had never installed into looked like an install with files
    # missing - to a reader and to the replay both (#43, #194).
    if _manifest_file(install_dir) is None:
        extra.append("- install record: MISSING (no install recorded in "
                     "this folder)")
    proxy = man.get("proxy")
    if man.get("vr"):
        try:
            from .. import openxr
            reg = openxr.existing_registration()
        except Exception:
            reg = None
        extra.append("- ReShade OpenXR layer (VR): "
                     + ("registered" if reg else "NOT REGISTERED - install again"))
    if proxy == VULKAN_LAYER:
        # Not a file, so it cannot be looked for in the folder. It used to be
        # reported as "MISSING" here, which sent people hunting through their
        # antivirus quarantine for a file that never existed.
        any_layer, mine = model._layer_state(man)
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
        from .. import remix as _remix
        trex = _remix.find_runtime(install_dir)
        out = [f"- .trex: {'found at ' + str(trex) if trex else 'MISSING'}"]
        if trex is not None:
            for n in (_remix.RUNTIME_DLL, _remix.DLSSNR, _remix.REMIX_NVNGX):
                state = "present" if (trex / n).is_file() else "MISSING"
                out.append(f"- {trex.name}/{n}: {state}")
            out.append(f"- runtime flavour: "
                       f"{_remix.runtime_flavour(trex) or 'no DLSS 5 pass'}")
            # Whether the runtime in there is the mod's own or one this
            # install swapped in: the first thing to suspect when a game
            # stops starting, and nothing in the report said it (#218).
            out.append("- Remix runtime: "
                       + ("swapped by this install (the mod's own is backed "
                          "up)" if (man.get("components") or {}).get("remix_runtime")
                          else "the mod's own, left alone"))
        rx = man.get("remix") or {}
        if rx.get("key"):
            conf = Path(rx.get("conf") or "")
            if not conf.is_absolute():
                conf = install_dir / conf
            out.append(f"- {rx['key']}: "
                       f"{'set' if _remix.option_set(conf, rx['key']) else 'NOT SET'}")
        # The lines that are not about files - "install record: MISSING"
        # above all - belong in a Remix report too. This branch returned
        # before them, which is the class (#43, #194) that line exists for.
        return out + extra
    # A ray-reconstruction runtime this install swapped, wherever the game
    # keeps it: invisible in a report otherwise, and exactly the kind of
    # thing that explains one.
    # What the add-on has written about itself, so the report answers
    # "is it even switched on" without anybody opening an overlay.
    try:
        from .. import reshade_ini as _ini2
        st = _ini2.addon_state(install_dir)
        if st and route in ("native", "bridge", "feeder"):
            extra.append(f"- DLSS 5 add-on switch: "
                         + (f"{_ini2.ADDON_SWITCH}={st['switch']}"
                            if st.get("switch") is not None else
                            f"no [{_ini2.ADDON_SECTION}] line in ReShade.ini"))
    except Exception:
        pass
    rr = (man.get("components") or {}).get("dlssd")
    if rr:
        where = next((f for f in (man.get("files") or [])
                      if isinstance(f, str)
                      and f.replace("\\", "/").lower().endswith(RR_RUNTIME)
                      and not f.endswith(_BACKUP_SUFFIX)), RR_RUNTIME)
        there = (install_dir / where).is_file()
        extra.append(f"- {where}: {'present' if there else 'MISSING'} "
                     f"(swapped to {rr}; the game's own is backed up)")
    # The game's own nvngx_dlss.dll replaced where it keeps it (#225): a
    # swap is the first suspect when a game starts crashing.
    _dl = (man.get("components") or {}).get("dlss")
    # Every one of them, not the first the walk reached: a reinstall can
    # swap the copy beside the executable AND a nested one, and which of
    # the two a report showed was decided by os.walk order (gate 1.9.1).
    _nbs = [f for f in (man.get("files") or [])
            if isinstance(f, str)
            # beside the exe (no slash) or nested (#225, gate 1.9.1)
            and f.replace("\\", "/").lower().split("/")[-1]
            == "nvngx_dlss.dll" + _BACKUP_SUFFIX]
    for _nb in _nbs if _dl else []:
        _w = _nb[:-len(_BACKUP_SUFFIX)]
        _p = Path(_w) if Path(_w).is_absolute() else install_dir / _w
        extra.append(f"- {_w}: {'present' if _p.is_file() else 'MISSING'} "
                     f"(swapped to {_dl}; the game's own is backed up)")
    # The dlss page swaps runtimes outside any install record: a crash after
    # one names that build, or the report points at everything but the file
    # that changed.
    try:
        from types import SimpleNamespace as _NS
        from .. import dlssupdate as _du
        _gf = _dlss_record_root(Path(install_dir), game_root)
        for _e in _du.load_record(_NS(folder=Path(_gf), install_dir=Path(install_dir))) if _gf else []:
            _p = Path(_gf) / _e["path"]
            extra.append(f"- {_e['path']}: {'present' if _p.is_file() else 'MISSING'} "
                         f"(updated on the dlss page to {_e['label'] or _e['written'] or '?'}; "
                         f"the game's own {_e['original'] or ''} kept beside it)".replace("own  kept", "own kept"))
    except Exception:
        pass
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
    elif route == "standalone":
        # The same two files under different names, and the same failure:
        # "the add-on did not get VORT and DLSS5_AIO_Feed.fx" is a verdict
        # this route gives (#212, and every "inconclusive" like it), and the
        # report it is printed on did not say whether either file was there.
        from .. import installer as _inst
        names.append("reshade-shaders/Shaders/" + _inst.STANDALONE_FX)
        prov = _inst.VORT_FX
        names.append("reshade-shaders/Shaders/" + prov)
    out = []
    for n in dict.fromkeys(names):
        state = "present" if (install_dir / n).is_file() else "MISSING"
        out.append(f"- {n}: {state}")
        if state == "MISSING" and route in ("feeder", "standalone") and prov \
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


def _has_dial(game, route: str) -> bool:
    """Does this route, in this game, have a work area the add-on reads?

    The same rule the slider uses: OptiScaler always, the feeder on the
    64-bit D3D11 path only. Every feeder install writes work_resolution
    into its cfg, so without this a DX12 or 32-bit feeder report would
    carry a number its own add-on ignores - and the window says as much
    three lines away.
    """
    if route == "optiscaler":
        return True
    if route != "feeder":
        return False
    return (getattr(game, "bitness", None) == 64
            and str(getattr(game, "api", "")).upper() == "DX11")


def _work_area(install_dir, route: str, game=None) -> str:
    """"- work area: 75%", when the add-on's own config says so."""
    try:
        from .. import autotune
        got = (autotune.ran_at_exact(install_dir, route)
               if install_dir and _has_dial(game, route) else None)
        return f"- work area: {got}%\n" if got is not None else ""
    except Exception:
        return ""


def issue_body(version: str, gpu_name: str, sm, driver: str, game, route: str,
               last_diag, autopilot_tail: str, autopilot_log_path,
               install_dir, last_error: str = "", session_error: str = "",
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
        # The work area the add-on itself was told to use. Every number the
        # tool prints about what the session cost is worked out from this,
        # and a report about one of those numbers used to arrive without
        # it - so neither the reply nor the replay could reproduce a line.
        + _work_area(install_dir, route, game)
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
        files = "\n**Files in the folder**\n" + "\n".join(
            _presence(d, man, route, getattr(game, "folder", None))) + "\n"
        reshade = _tail(d / RESHADE_LOG, 250_000)
        feed = _tail(d / FEED_LOG, 100_000)
        if route == "optiscaler":
            p = _opti_log(d)
            opti = _tail(p, 100_000) if p else ""

    parts = [head, files, _loaded_block(d)]
    # The last session only, as analyse() reads it: ReShade.log is never
    # truncated, and the excerpt's priorities pulled hook lines and add-on
    # builds out of older sessions over the last one's own errors.
    parts.append(_block("ReShade.log",
                        _reshade_excerpt(_last_session(reshade)), 1500))
    parts.append(_block("dlss5-feed.log", _last_lines(feed, 20), 1400))
    # A 32-bit game's DLSS runs in the helper, and #252's report carried
    # every log except the one that named the fault.
    if route == "feeder" and d is not None and (d / HOST_LOG).is_file():
        parts.append(_block("dlss5-feed-host.log",
                            _helper_excerpt(_tail(d / HOST_LOG, 150_000)), 900))
    if route == "optiscaler":
        parts.append(_block("OptiScaler.log", _last_lines(opti, 20), 900))
    if route == "standalone":
        parts.append(_block("standalone-dlssnr.log",
                            _last_lines(_tail(model.STANDALONE_LOG, 100_000), 20), 900))
    if route == "remix" and d is not None:
        # Only the lines that say anything about the neural pass: the Remix
        # log is enormous and the rest of it is path-tracing chatter.
        from .. import remix as _remix
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
    elif session_error:
        # Something went wrong in this session, but not while installing
        # into this folder - worth having in the report, under a heading
        # that says so. A replay reads the block above this one, so a
        # traceback from the update check cannot be read as an install's.
        parts.append(f"\n**Last error in this session (not from an install "
                     f"into this folder)**\n```\n{session_error[-900:]}\n```\n")
    parts.append(_block(
        f"autopilot.log (`{autopilot_log_path}`)",
        _tool_log_lines(autopilot_tail or "", game, install_dir), 900))
    body = "".join(parts)
    return body[:6000]
