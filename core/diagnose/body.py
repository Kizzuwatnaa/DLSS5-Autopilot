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
from .live import _bridge_excerpt, _bridge_text


__all__ = [
    "CLOSED_ITSELF", "NEVER_STARTED", "_block", "_dlss_record_root",
    "_last_lines", "_presence", "answered", "event_line", "said_started",
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


def _keyed_lines(text: str, kind, n: int = 20, budget: int = 900,
                 width: int = 200) -> list[str]:
    """The last `n` lines, plus the newest line of every kind `kind(line)`
    names, fitted to `budget` by dropping the oldest unkeyed lines first.

    A tail alone loses the line a verdict was decided on: #458's standalone
    log was "Working." on the machine (10 frames through the pipeline) and
    its report carried only the teardown after them, and #289's OptiScaler
    tail dropped the dispatch lines - both replay to another answer."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    newest: dict[str, int] = {}
    for i, ln in enumerate(lines):
        k = kind(ln)
        if k:
            newest[k] = i
    firm = set(newest.values())
    idx = sorted(firm | set(range(max(0, len(lines) - n), len(lines))))
    out = [(i, lines[i][:width]) for i in idx if lines[i].strip() or i in firm]
    while len(out) > 1 and len("\n".join(t for _, t in out)) > budget:
        j = next((j for j, (i, _) in enumerate(out) if i not in firm), 0)
        del out[j]
    return [t for _, t in out]


def _standalone_kind(ln: str) -> str:
    """The standalone add-on lines _analyse_standalone decides on."""
    for k in ("pipeline FAILED at", "contract ready:", "on-present frame "):
        if k in ln:
            return k
    return ""


def _opti_kind(ln: str) -> str:
    """The OptiScaler lines _analyse_optiscaler decides on: the forwarder, the
    settings echo, a failure, and the model's own work (a duration or a count
    of finished pictures, #168 #311)."""
    if "forwarder loaded" in ln:
        return "forwarder"
    if not ("DLSS-NR" in ln or "dlssnr" in ln.lower()):
        return ""
    if re.search(r"DlssNr\.\w+:", ln):
        return "setting"
    low = ln.lower()
    if any(k in low for k in ("create failed", "unavailable", "did not run",
                              "not found beside", "would not load",
                              "disabling for this session", "refused")):
        return "failed"
    if "running at" in ln or re.search(r"\d\s*ms\b", ln) \
            or re.search(r"finished picture:\s*[1-9]", ln):
        return "work"
    return "nr"


_FEED_KINDS = (("attached", "attached."), ("provider", "DLSS5_MV_PROVIDER="),
               ("addon", "DLSS 5 add-on:"), ("technique", "DLSS5_Feed.fx technique"),
               ("notloaded", "is not loaded"), ("mv", "MV probe"),
               ("depth", "Depth probe"), ("spawned", "host spawned"),
               ("connected", "host connected"), ("building", "building: "),
               ("fault", " stack, by module (innermost first):"),
               # feeder 1.18.0-beta: NGX's own result and the helper mode
               ("ngxresult", "FeatureInitResult"),
               ("inprocess", "would not set DLSS up inside this game's process"),
               ("standsdown", "stands down"))


def _feed_kind(ln: str) -> str:
    """The dlss5-feed.log lines the feeder chain decides on (chain.py)."""
    # the session start, in every build's form ("attached." / "attached;")
    if _FEED_SESSION.search(ln):
        return "attached"
    for k, word in _FEED_KINDS:
        if word in ln:
            return k
    if re.search(r"frame \d+ (?:delivered|evaluated)", ln):
        return "frame"
    return ""


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
    # The person's own frame generation files (#370) and the Windows graphics
    # setting (#427): both are things a reply asks the person to confirm,
    # so the report says what the record holds and what is on disk.
    try:
        own = man.get("own_fg") or {}
        if isinstance(own, dict) and own.get("recipe"):
            fl = [str(f) for f in (own.get("files") or [])]
            extra.append(f"- frame generation files: {own['recipe']} ("
                         + ", ".join(f"{f} {'present' if (install_dir / f).is_file() else 'MISSING'}"
                                     for f in fl[:4]) + ")")
        from .. import gpupref as _gp
        for r in (man.get("gpu_pref") or [])[:3]:
            if isinstance(r, dict) and r.get("exe"):
                now = _gp.chosen(r["exe"])
                extra.append(f"- Windows graphics setting: {Path(str(r['exe'])).name}: "
                             + {None: "not set", "0": "let Windows decide",
                                "1": "power saving", "2": "High performance"}
                             .get(now, str(now)))
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
        # ...and the release it took, which is the difference between an
        # install that never wrote a proxy and one something emptied (#364).
        _c = man.get("components")
        _otag = str((_c.get("optiscaler") if isinstance(_c, dict) else "") or "")
        extra.append("- optiscaler build: "
                     + (str(man.get("opti_build") or "") or "Dagherbou")
                     + (f" ({_otag})" if _otag else ""))
    else:
        names.append("ReShade.ini")
    names += _dxvk_files(man)
    # DXVK's own log beside the game is the proof it ran - the one thing
    # "DXVK ran and ReShade did not" rests on - and the report never said
    # whether there was one, so no replay could reproduce that verdict and
    # #400 came back as "not started since the install".
    if man.get("dxvk") and man.get("exe"):
        try:
            from .. import dxvk as _dxvk
            since = _installed_at(install_dir)
            for n in _dxvk.logs_for(Path(str(man.get("exe")))):
                if (install_dir / n).is_file():
                    extra.append(f"- {n}: present ("
                                 + ("written since the install"
                                    if _fresh(install_dir / n, since) else
                                    "from before the install") + ")")
        except Exception:
            pass
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


# What the person answers in the report dialog (reportui.STARTED), in the
# words the report prints them. The old template's "yes / no / it closed
# itself", left untouched, is no answer at all.
CLOSED_ITSELF = "closed itself"
NEVER_STARTED = "never started"

# Verdicts that send the person to look at something in a running game -
# the overlay, a panel, a checkbox - or that call the session fine. Said to
# somebody whose game closed itself, or never started, every one of them is
# wrong: there is no running game to look at (#412, and the top class of
# the backlog - "we ask the person to look in the overlay", 21 of 121).
_LOOK_IN_GAME = ("confirm in the", "check '", "the switch is on",
                 "open the overlay", "the only live picture", "Working.",
                 "Add-ons loaded", "Inconclusive", "Loaded and set up",
                 # "switch it on in the game" needs a game that stays up too:
                 # #460 crashed on start and was told to press Insert and
                 # tick a box, #461 crashed on turning the upscaler on and
                 # was told to turn the upscaler on (2.0.5).
                 "not switched on", "the game's own upscaler has to be on",
                 "made no D3D12 DLSS call", "never called DLSS",
                 "no neural frame followed",
                 # the bridge's frames, and the panel to compare them in
                 "whether the neural pass drew them",
                 "delivered no frame")


_OVERLAY_ASK = re.compile(r"\b(open|press|check)\b[^.]{0,60}\b(overlay|panel|tab)\b",
                          re.I)


def said_started(text: str) -> str:
    """CLOSED_ITSELF, NEVER_STARTED or "" from a report's own answer line."""
    low = str(text or "").lower()
    if not low or "yes / no" in low:
        return ""
    if "never started" in low:
        return NEVER_STARTED
    if "closed itself" in low:
        return CLOSED_ITSELF
    return ""


def event_line(crash) -> str:
    """"X.exe faulted in Y.dll 0xC0000005" from a wincrash.Crash, or ""."""
    if crash is None or not getattr(crash, "module", ""):
        return ""
    return f"{crash.exe} faulted in {crash.module} {crash.code}".strip()


def answered(rep: Report, started: str, presence=(), kind: str = "game",
             event: str = "") -> Report:
    """The verdict, corrected by what the person saw.

    `started` is their answer to "did the game start?" and `presence` the
    report's own "Files in the folder" lines. A game that closed itself or
    never started cannot be checked in an overlay, and a "Working." over it
    is the report being wrong, not the person: that verdict is replaced by
    what to take out first, from what the folder says was changed. Every
    verdict that names something the logs show is kept.

    `kind` is the install's ("game" or "video"). A video player is set up
    from the video page, not a game's, and has no routes to swap between,
    so its verdict is left as it is rather than given steps that name
    controls it does not have.
    """
    said = said_started(started)
    if not said or not rep.verdict or kind == "video" \
            or not any(n in rep.verdict for n in _LOOK_IN_GAME):
        return rep
    lines = [str(x).strip().lstrip("- ") for x in (presence or ())]
    swaps = [x.split(":", 1)[0] for x in lines if "updated on the dlss page" in x]
    ours_swapped = [x.split(":", 1)[0] for x in lines
                    if "swapped to" in x and "backed up" in x]
    ran = rep.verdict.startswith("Working")
    steps = []
    if swaps:
        steps.append(f"undo what the dlss page changed first - "
                     f"{', '.join(swaps[:3])} - with 'restore original' on "
                     f"that page, and start the game once: a runtime swap is "
                     f"the first suspect when a game stops starting")
    if ours_swapped:
        steps.append(f"this install also swapped {', '.join(ours_swapped[:3])} "
                     f"(uninstall puts the game's own back)")
    steps.append("press uninstall on the game's page and start the game "
                 "once with nothing of ours in it. If it closes the same "
                 "way, it is not this install. If it runs, install again "
                 "with another route - where the window shows 'what other "
                 "people found', it says which worked for this game")
    what = ("closed itself" if said == CLOSED_ITSELF else "never started")
    # Windows' own record of the fault, when the report carries one (#460:
    # "nothing here recorded why" sat above the line that recorded it).
    event = (event or "").strip()
    seen = (f"Windows recorded it: {event}. " if event else "")
    detail = (("The logs say neural rendering ran, and you say the game "
               f"{what} - so the session did not end well, whatever the "
               "logs got as far as. " + seen if ran else
               seen + ("The add-on logs could not show why. " if event else
                       "The logs could not show why, and the check they would "
                       "have asked for needs a running game. "))
              + "In this order: " + "; then ".join(steps) + ".")
    # The findings that said the same thing as the verdict - go and look in
    # the overlay - go with it, or the report prints the correction above
    # the advice it corrects (#171's shape). Evidence stays.
    rep.findings = [f for f in rep.findings
                    if not (f.level == INFO and _OVERLAY_ASK.search(
                        f"{f.title} {f.detail}"))]
    rep.findings.insert(0, Finding(BAD, f"You said the game {what}.", detail))
    # Whole strings, so core/verdicts.py can stage them by their own words.
    if ran and said == CLOSED_ITSELF:
        rep.verdict = ("Neural rendering ran, then the game closed itself - "
                       "see below for what to take out first.")
    elif ran:
        # The logs hold a session that drew, and this start never came up:
        # they describe an earlier launch than the one being reported.
        rep.verdict = ("The logs show an earlier session that ran; this time "
                       "the game never started - see below for what to take "
                       "out first.")
    elif event:
        rep.verdict = ("Windows recorded the game faulting - see below for "
                       "what to take out first.")
    elif said == CLOSED_ITSELF:
        rep.verdict = ("The game closed itself and nothing here recorded why "
                       "- see below for what to take out first.")
    else:
        rep.verdict = ("The game never started with this install in - see "
                       "below for what to take out first.")
    return rep


def _start_lines(game) -> str:
    """The report's lines on how Windows starts the game, or "".

    Printed only when there is something to say, and read back by the
    replay (the header keys "starts as administrator" and "this tool").
    """
    from .. import wincrash as _wc
    out = ""
    try:
        exe = getattr(game, "exe", None)
        flags = _wc.start_flags(exe) if exe else ""
        if flags:
            out += f"- starts as administrator: {flags}\n" \
                if _wc.runs_as_admin(flags) else f"- compatibility flags: {flags}\n"
        if _wc.elevated():
            out += "- this tool: running as administrator\n"
    except Exception:
        pass
    return out


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
    d = Path(install_dir) if install_dir else None
    presence: list[str] = []
    if d is not None and d.is_dir():
        try:
            presence = _presence(d, _manifest(d), route,
                                 getattr(game, "folder", None))
        except Exception:
            presence = []
    diag = ""
    # What the person answered decides whether a "look in the overlay"
    # verdict can stand: with a game that closed itself or never started
    # there is nothing to look at (#412). A copy, so the window's own
    # diagnosis is not rewritten behind its back.
    if last_diag is not None and answers:
        try:
            import copy
            last_diag = answered(copy.deepcopy(last_diag),
                                 str(answers.get("started") or ""), presence,
                                 str(getattr(game, "kind", "") or "game"),
                                 event_line(crash))
        except Exception:
            pass
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
        # How Windows starts the game: an elevated game gets no per-user
        # Vulkan layer, which is the whole DXVK route (#400, #348, #238).
        + _start_lines(game)
        + diag[:1200])

    files = ""
    reshade = feed = opti = ""
    if d is not None and d.is_dir():
        files = "\n**Files in the folder**\n" + "\n".join(presence) + "\n"
        reshade = _tail(d / RESHADE_LOG, TAIL_RESHADE)
        feed = _tail(d / FEED_LOG, TAIL_FEED)
        if route == "optiscaler":
            p = _opti_log(d)
            # the last run, as _analyse_optiscaler reads it
            from .routes import _last_run
            opti = _last_run(_tail(p, TAIL_OPTI)) if p else ""

    parts = [head, files, _loaded_block(d)]
    # The last session only, as analyse() reads it: ReShade.log is never
    # truncated, and the excerpt's priorities pulled hook lines and add-on
    # builds out of older sessions over the last one's own errors.
    parts.append(_block("ReShade.log",
                        _reshade_excerpt(_last_session(reshade)), 1500))
    # The bridge's own log: the build, its frames and why it stopped. The
    # replay ties it to the ReShade block above by the two logs' clocks.
    if route == "bridge" and d is not None and (d / BRIDGE_LOG).is_file():
        parts.append(_block(BRIDGE_LOG,
                            _bridge_excerpt(_bridge_text(d / BRIDGE_LOG)), 900))
    # The last session, as chain.py reads it, and its deciding lines kept.
    parts.append(_block("dlss5-feed.log",
                        _keyed_lines(_strip_ngx(_last_feed_session(feed)), _feed_kind, 20, 1400), 1400))
    # A 32-bit game's DLSS runs in the helper, and #252's report carried
    # every log except the one that named the fault.
    if route == "feeder" and d is not None and (d / HOST_LOG).is_file():
        parts.append(_block("dlss5-feed-host.log",
                            _helper_excerpt(_tail(d / HOST_LOG, TAIL_HOST)), 900))
    if route == "optiscaler":
        parts.append(_block("OptiScaler.log", _keyed_lines(opti, _opti_kind), 900))
    if route == "standalone":
        parts.append(_block("standalone-dlssnr.log",
                            _keyed_lines(_tail(model.STANDALONE_LOG, TAIL_STANDALONE),
                                         _standalone_kind), 900))
    if route == "remix" and d is not None:
        # Only the lines that say anything about the neural pass: the Remix
        # log is enormous and the rest of it is path-tracing chatter.
        from .. import remix as _remix
        rtx_log = _tail(_remix.log_path(d), TAIL_REMIX)
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
