r"""What the folder, the logs and the running game say.

Readers, not rules: each answers one question about the disk
or the process and says nothing about what it means.

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
from .layer import (_addon_in, _dxvk_files, _layer_clash,  # noqa: F401
                    _layer_detail, _through_layer)


__all__ = [
    "_addon_switch", "_addons", "_anything_of_ours", "_area",
    "_attached", "_biggest", "_crash_verdict", "_dlss_mod",
    "_dxvk_gone", "_engine_says_not_dxgi", "_family", "_fault_chain",
    "_feed_shaders", "_fresh", "_game_ran", "_in",
    "_install_crash", "_installed_at", "_last_feed_session", "_last_session",
    "_NET_VERDICTS", "_exc_line", "_net_cause", "_net_stop",
    "_launcher_installed", "_layer_gone", "_live_evidence", "_loaded_block",
    "_manifest", "_manifest_file", "_missing_addons",
    "_missing_core", "_near", "_nrpre_picks", "_opti_log", "_opti_sighting",
    "_remembered_evidence",
    "_reshade_died_early",
    "_route", "_route_from_files", "_same_launch", "_shader_failures",
    "_stale_install", "_standalone_named", "_tail", "_upstream_named",
    "_user_data_names", "windows_crash",
    "TAIL_RESHADE", "TAIL_FEED", "TAIL_HOST", "TAIL_OPTI", "TAIL_STANDALONE",
    "TAIL_REMIX", "_strip_ngx",
]

# How much of each log is read - by the analyser AND by the report body. The
# report carried the last 100K of OptiScaler.log while the analyser read
# 400K; once Streamline flooded the log after a device loss (#580), the
# forwarder and dispatch lines fell out of the report and the replay said
# "never asked for". One number per log, used by both.
TAIL_RESHADE = 250_000
TAIL_FEED = 400_000
TAIL_HOST = 150_000
TAIL_OPTI = 400_000
TAIL_STANDALONE = 200_000
TAIL_REMIX = 300_000

# The block feeder 1.18.0-beta copies from NGX's own log into the feed log
# after a failure, between "===== NGX's own log of the failure above ..."
# and "end of NGX's own log". Those are NGX's lines, not the feed's, and
# they carry words ("-> 0x00000001", "feature", "frame") the rules read as
# ours. Without an end line the block stops at the feed's next own line
# (one carrying "[feed", after a clock or not), never at the end of the
# text: a session that went on after the copy must keep its frames.
_NGX_BLOCK = re.compile(r"^[^\n]*===== NGX's own log[^\n]*\n"
                        r"(?:(?![^\n]*\[feed)[^\n]*\n)*?"
                        r"(?:[^\n]*(?i:end of NGX's own log)[^\n]*(?:\n|$)"
                        r"|(?=[^\n]*\[feed)|(?![^\n]*\[feed)[^\n]*\Z)",
                        re.M)


def _strip_ngx(text: str) -> str:
    return _NGX_BLOCK.sub("", text or "")

def _upstream_named(name: str) -> bool:
    low = name.strip().lower()
    return (low == UPSTREAM_ADDON_NAME.lower() or "pre-upscale" in low
            or "upstream" in low)


def _standalone_named(name: str) -> bool:
    low = name.strip().lower()
    return "standalone dlss-nr" in low or "standalone-dlssnr" in low


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


def _tail(path: Path, limit: int = TAIL_FEED) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read().decode("utf8", "replace")
    except OSError:
        return ""


def _last_session(text: str) -> str:
    """The last ReShade session in a log that may hold several."""
    at = text.rfind(_RESHADE_SESSION)
    # A tail can cut into the middle of a session, and then there is no
    # marker to find - the whole tail is the best evidence there is.
    return text[at:] if at > 0 else text


def _last_feed_session(text: str) -> str:
    last = None
    for last in _FEED_SESSION.finditer(text):
        pass
    return text[last.start():] if last is not None and last.start() > 0 else text


def _attached(text: str) -> bool:
    """Did the feed add-on say it was loaded? Its own session marker, so a
    build named something new is still recognised - and "detached" is not it."""
    return bool(_FEED_SESSION.search(text or ""))


def _reshade_died_early(rtext: str) -> bool:
    """Did ReShade's last session end before it did anything at all?

    Only when the session is whole - it begins where ReShade began - and
    holds none of the lines that say it got somewhere.
    """
    low = (rtext or "").lower()
    if _RESHADE_STARTED not in low:
        return False
    return not any(k in low for k in _RESHADE_GOT_GOING)


# neural-upstream writes its whole run into ReShade.log under [NRPRE], and
# _analyse_upstream reads it from there - none of those lines carry WARN or
# ERROR, so the excerpt dropped every one and a posted report replayed as a
# route that said nothing. The newest line of each kind the diagnosis reads.
# Here, not in routes.py: the report's excerpt (body.py) picks with it, and
# body sits below routes.
_NRPRE_KINDS = ("settings loaded", "hook ", "CreateFeature", "2b:", "HB #", "DIAG",
                "eval feat")


def _nrpre_picks(lines: list[str]) -> set[int]:
    picks: set[int] = set()
    newest: dict[str, list[int]] = {}
    last = None
    for i, ln in enumerate(lines):
        at = ln.find("[NRPRE] ")
        if at < 0:
            continue
        last = i
        body = ln[at + 8:]
        for kind in _NRPRE_KINDS:
            if body.startswith(kind):
                newest.setdefault(kind, []).append(i)
                break
    for kind, idx in newest.items():
        # every hook line counts (the verdict says how many entry points)
        picks.update(idx[-4:] if kind == "hook " else idx[-1:])
    if last is not None:
        picks.add(last)
    return picks


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


def _manifest_file(install_dir: Path) -> Path | None:
    """The install record in this folder, whichever name it was written under."""
    for name in (MANIFEST,) + LEGACY_MANIFESTS:
        p = install_dir / name
        if p.is_file():
            return p
    return None


def _manifest(install_dir: Path) -> dict:
    p = _manifest_file(install_dir)
    if p is None:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _live_evidence(install_dir: Path, man: dict, rep: Report) -> bool:
    """Ask the running game itself, instead of guessing from an absent log.

    This is the one branch where the tool used to offer three guesses at
    once - the game was never started, or it launches a different
    executable, or it ignores that DLL name - and 34 of the first 84 reports
    got that list. All three are questions Windows will answer outright
    while the game is up: which executable is running, and which DLLs are
    mapped into it. Somebody who alt-tabs out to press "did it work?" has
    the game running, and this reads it.

    True when it settled the question and wrote a verdict.
    """
    # What the process would have to hold for this to say anything. With no
    # install record there is nothing in it, and every sighting then comes
    # back with ours, missing and elsewhere all empty - which used to read
    # as "it has loaded none of the files here" about a list of none.
    want = [f for f in (man.get("files") or [])
            if isinstance(f, str)
            and f.lower().endswith((".dll", ".addon64", ".addon32"))]
    try:
        from .. import watch
        seen = watch.inspect(install_dir, want, exe=str(man.get("exe") or ""))
    except Exception:
        return False
    if not seen:
        # The game has closed. Whatever the watcher wrote down while it WAS
        # running is the same evidence, dated - most people play first and
        # press the button afterwards, so this is the common path, not the
        # fallback. A sighting older than the install describes an install
        # that is not the one being read.
        try:
            return _remembered_evidence(install_dir, man, rep,
                                        watch.last_sighting(
                                            install_dir,
                                            _installed_at(install_dir)))
        except Exception:
            return False
    s = seen[0]
    app = "app" if man.get("kind") == "video" else "game"
    running = Path(s.proc.path).name

    # Whatever else is true, the game IS running: "not started since the
    # install" is off the table from here on.
    if not s.loaded.known:
        rep.add(WARN, f"{running} is running now, and will not say what it "
                      f"has loaded.",
                f"{s.loaded.refused.capitalize()}. So the {app} has been "
                f"started - what could not be checked is whether the files "
                f"here are in it. Anti-cheat and ReShade add-ons do not "
                f"coexist; if this game has anti-cheat, that is the answer.")
        rep.verdict = (f"{running} is running and nothing here has written a "
                       f"log - the process is protected, so nothing of ours "
                       f"can be read in it.")
        rep.never_ran = False
        return True

    recorded = str(man.get("exe") or "")
    if recorded and running.lower() != Path(recorded).name.lower():
        rep.add(BAD, f"The {app} is running from {running}, not "
                     f"{Path(recorded).name}.",
                f"{s.proc.path} is the process that is up. The install went "
                f"beside {Path(recorded).name}, and a process only loads what "
                f"is beside the executable it started from. Point the tool at "
                f"{running} and install again.")
        rep.verdict = (f"The {app} runs from {running}, and the install went "
                       f"beside {Path(recorded).name} - install again there.")
        rep.never_ran = False
        return True

    if s.elsewhere:
        names = ", ".join(sorted({Path(p).name for p in s.elsewhere}))
        rep.add(BAD, f"The {app} loaded {names} from somewhere else, not from "
                     f"this folder.",
                "Its own copy is in the process: "
                + "; ".join(s.elsewhere[:3])
                + ". A DLL already loaded under that name is never loaded a "
                  "second time, so the one written here is ignored - another "
                  "mod, an overlay, or the folder the game was started from "
                  "got there first. Remove the other one, or install under a "
                  "different name in the 'reshade loads as' dropdown.")
        rep.verdict = (f"The {app} is loading {names} from another folder - "
                       f"ours is never reached.")
        rep.never_ran = False
        return True

    if s.ours and _through_layer(man) and not _addon_in(s.ours):
        # DXVK (or the game itself) is in, and ReShade reaches this game as
        # a Vulkan layer: there is no proxy DLL to be "reached" (#238).
        if _layer_clash(man) is not None:
            rep.add(BAD, "The 32-bit ReShade layer carries the layer name "
                         "the 64-bit one uses, so the Vulkan loader throws "
                         "it away.",
                    "Loaded right now: " + ", ".join(sorted(
                        {Path(p).name for p in s.ours}))
                    + ". Both of ReShade's layer manifests are called "
                      "VK_LAYER_reshade, and an implicit layer name may "
                      "only appear once: the loader keeps the 64-bit one, "
                      "then refuses it because a 32-bit game cannot load "
                      "it. Install again with this version - it gives the "
                      "32-bit layer its own name.")
            rep.verdict = ("The 32-bit Vulkan layer is being discarded as a "
                           "duplicate name - install again to rewrite it.")
            rep.never_ran = False
            return True
        rep.add(WARN, f"Our files are loaded in {running}, and ReShade's "
                      f"Vulkan layer has written no log.",
                _layer_detail(s.ours, s.missing, man, "right now"))
        rep.verdict = (f"Loaded into {running}, and no log was written - "
                       f"ReShade's Vulkan layer is not reaching the game.")
        rep.never_ran = False
        return True

    if s.ours:
        rep.add(WARN, f"Our files are loaded in {running}, and nothing has "
                      f"written a log.",
                "Loaded right now: " + "; ".join(sorted(
                    {Path(p).name for p in s.ours}))
                + ". So the proxy and the executable are both right, and "
                  "what failed is further in - ReShade writes its log the "
                  "moment it initialises, so it has been loaded and has not "
                  "got that far. Close the game and press this again; if "
                  "there is still no log, say so in an issue with this "
                  "report.")
        rep.verdict = (f"Loaded into {running}, and no log was written - the "
                       f"proxy is reached and ReShade is not initialising.")
        rep.never_ran = False
        return True

    if not want:
        # Nothing was recorded for this folder, so "none of ours is in it"
        # is a statement about an empty list. The game running is still a
        # fact worth having, and it is the only one there is here.
        rep.add(WARN, f"{running} is running, and nothing is recorded here "
                      f"to look for in it.",
                "What is read out of a running game is the list of files an "
                "install wrote; this folder's record names none, so this can "
                "say the game is up and nothing more. Close the game, "
                "install again, and the next run answers it.")
        rep.verdict = (f"{running} is running - close it and install again, "
                       f"so there is a record to read it against.")
        rep.never_ran = False
        return True

    if _wrong_api_verdict(install_dir, man, rep, running, app):
        return True
    rep.add(BAD, f"{running} is running and has loaded none of the files "
                 f"here.",
            f"Windows lists every DLL in a process, and not one of this "
            f"install's is in {running}. The {app} has been started, so it is "
            f"not that - it does not load this folder's "
            f"{man.get('proxy') or 'proxy DLL'} at all. Try another name in "
            f"the 'reshade loads as' dropdown in the game's settings.")
    rep.verdict = (f"{running} is running and has loaded nothing from this "
                   f"folder - try another proxy name.")
    rep.never_ran = False
    return True


def _same_dir(a, b) -> bool:
    """One folder, whatever the spelling: a junction or a symlinked library
    folder is the same place (watch._under resolves both sides too)."""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return os.path.normcase(os.path.normpath(str(a))) == \
            os.path.normcase(os.path.normpath(str(b)))


def _opti_sighting(install_dir: Path, man: dict, rep: Report,
                   logging_on: bool) -> bool:
    """The OptiScaler route with no log: ask what the game had loaded.

    The watcher was only asked on the ReShade chain, so a d3d12.dll proxy
    the game never loaded was answered "the log is off, install again"
    (#577). Only the proxy decides here: OptiScaler also writes libxess,
    libxell and the FidelityFX DLLs, and a game that ships its own copies
    loads those from its own folder whatever we do.
    True when it settled the question and wrote a verdict.
    """
    proxy = str(man.get("proxy") or "")
    if not proxy:
        return False
    want = [f for f in (man.get("files") or [])
            if isinstance(f, str) and f.lower().endswith(".dll")]
    # The watcher only looks for what the record names; a proxy missing from
    # it would read as "never loaded" on every run.
    if not any(os.path.basename(f).lower() == proxy.lower() for f in want):
        return False
    try:
        from .. import watch
        live = watch.inspect(install_dir, want, exe=str(man.get("exe") or ""))
        # Only the game's own process answers: a crash reporter or service
        # still up after the game closed is not it, and the remembered
        # sighting of the game is the better evidence (gate 2.0.8 pass 3).
        rec_name = Path(str(man.get("exe") or "")).name.lower()
        live = [x for x in live or [] if x.proc.name.lower() == rec_name]
        if live:
            s = live[0]
            if not s.loaded.known:
                return False
            seen = {"name": s.proc.name, "exe": s.proc.path, "now": True,
                    "ours": [os.path.basename(p) for p in s.ours],
                    "elsewhere": list(s.elsewhere)}
        else:
            seen = watch.settle(watch.last_sighting(
                install_dir, _installed_at(install_dir)), man.get("files"))
    except Exception:
        return False
    if not seen or seen.get("refused"):
        return False
    # A clock, never "just now": the verdict is shared and read later.
    when = "at " + datetime.fromtimestamp(
        time.time() if seen.get("now") else seen.get("at", 0)).strftime("%d %b %H:%M")
    running = str(seen.get("name") or "the game")
    recorded = Path(str(man.get("exe") or "")).name
    low = proxy.lower()
    loaded = any(os.path.basename(n).lower() == low for n in seen.get("ours") or [])
    ran_in = os.path.dirname(str(seen.get("exe") or ""))
    here = not os.path.isabs(ran_in) or _same_dir(ran_in, install_dir)
    # The proxy in the process answers "was it reached" before any guess
    # from names and folders (gate 2.0.8 pass 2).
    if recorded and running.lower() != recorded.lower() and not loaded:
        if here:
            # Another program in the same folder - a launcher still up when
            # the watcher looked, or the second exe of a shared install. It
            # is not the game, and it says nothing about ours.
            return False
        rep.add(BAD, f"The game ran from {running} ({when}), not {recorded}.",
                f"{seen.get('exe') or running} is what started. The install "
                f"went beside {recorded}, and a process only loads what is "
                f"beside the executable it started from. Set 'target exe' to "
                f"{running} in the game's settings and install again.")
        rep.verdict = (f"The game runs from {running}, and the install went "
                       f"beside {recorded} - install again there.")
        rep.never_ran = False
        return True
    # The same exe name in another folder (a game root and its bin\x64, gate
    # 2.0.8): OptiScaler's proxy has to sit beside the executable that ran.
    if not here and not loaded:
        rep.add(BAD, f"{running} ran from {ran_in} ({when}), and this install "
                     f"is in {install_dir}.",
                f"A process only loads a proxy from the folder its executable "
                f"started from, so OptiScaler's {proxy} here is never "
                f"reached. Set 'target exe' to {seen.get('exe')} in the "
                f"game's settings and install again.")
        rep.verdict = (f"{running} runs from another folder, and the install "
                       f"went beside a copy that does not start - install "
                       f"again there.")
        rep.never_ran = False
        return True
    if loaded:
        if logging_on:
            rep.add(WARN, f"OptiScaler ({proxy}) was loaded in {running} "
                          f"{when}, and wrote no log.",
                    "The proxy and the executable are both right, and "
                    "OptiScaler.ini asks for a log. Close the game and press "
                    "'did it work?' again; if there is still no log, say so "
                    "in an issue with this report.")
            rep.verdict = (f"OptiScaler was loaded into {running} {when}, and "
                           f"no log was written.")
            rep.never_ran = False
            return True
        return False        # loaded, log off: the log-off answer is right
    # A launcher recorded as the game starts the real one as another process:
    # not loading the proxy is what it does, and the proxy name is not the
    # fault. The launcher answer is the diagnosis's own (#191).
    if _launcher_installed(install_dir, recorded):
        return False
    # The watcher looks a few seconds after the start, and a game can load a
    # proxy later than that: said as what was seen, not as a rule of the game.
    other = [p for p in seen.get("elsewhere") or []
             if os.path.basename(p).lower() == low]
    rep.add(BAD, f"When the tool looked ({when}), {running} had not loaded "
                 f"this folder's {proxy}.",
            (f"It had {proxy} from somewhere else: {other[0]}. A DLL already "
             f"loaded under that name is never loaded a second time. "
             if other else
             f"Windows lists every DLL in a process, and {proxy} was not one "
             f"of them. No OptiScaler log was written either. ")
            + "Set 'loads as' to another name in the game's settings"
            + ("" if low == "dxgi.dll" else
               " (dxgi.dll is the first one auto picks)")
            + " and install again.")
    rep.verdict = (f"{running} ran, and OptiScaler's {proxy} was not loaded "
                   f"in it - try another 'loads as' name.")
    rep.never_ran = False
    return True


def _loaded_block(install_dir: Path | None) -> str:
    """What the game had loaded when it last ran, for the report.

    A report that says "every file is present and there is no log" leaves
    the reader with the same three guesses the person got. This line says
    which executable actually ran and whether our files were in it - the
    thing nobody could see, and the reason the biggest class of report was
    unanswerable.
    """
    if install_dir is None:
        return ""
    try:
        from .. import watch
        man = _manifest(install_dir)
        seen = watch.settle(
            watch.last_sighting(install_dir, _installed_at(install_dir)),
            man.get("files"))
        proxy = str(man.get("proxy") or "")
        also = ()
        if man.get("dxvk"):
            from .. import dxvk as _dx
            also = _dx.ALL_FILES
        need = [n for n in seen.get("missing") or []
                if watch.essential(n, proxy, also)]
    except Exception:
        return ""
    if not seen:
        return ""
    when = datetime.fromtimestamp(seen.get("at", 0)).strftime("%d %b %H:%M")
    out = [f"\n**What ran, and what it loaded** (seen at {when})",
           f"- process: {seen.get('name') or '?'}"]
    # Where it ran from, relative to the install folder (no user paths in a
    # report): the replay needs it for "ran from another folder" (gate 2.0.8).
    ran = str(seen.get("exe") or "")
    if ran:
        # Only inside the game's own tree: a path that climbs to the drive
        # or the user profile would carry a user name into a public issue.
        try:
            rel = os.path.relpath(ran, str(install_dir))
            base = os.path.commonpath([os.path.abspath(ran),
                                       os.path.abspath(str(install_dir))])
            deep = len(Path(base).parts) >= 3        # e.g. C:\ Games Foo
            inside = rel.split(os.sep)[0] != ".."
            out.append("- ran from: " + (rel if inside or deep else
                                         "outside the game folder"))
        except ValueError:          # another drive
            out.append("- ran from: outside the game folder")
    if seen.get("refused"):
        out.append(f"- DLL list: refused - {seen['refused']}")
    else:
        out.append(f"- DLLs in the process: {seen.get('modules', 0)}")
        out.append("- ours, loaded: "
                   + (", ".join(seen.get("ours") or []) or "none"))
        if seen.get("elsewhere"):
            out.append("- same name, loaded from elsewhere: "
                       + ", ".join(seen["elsewhere"]))
        if need:
            out.append("- ours, not loaded: " + ", ".join(need))
        rest = [n for n in seen.get("missing") or [] if n not in need]
        if rest:
            # Printed apart, because both #225 and #238 read the one list
            # as the fault. Which of the two lists a name belongs in is
            # decided against the install's file list; with no file list
            # there is nothing to decide it against, and calling everything
            # "loaded only if the game asks" would be a claim the record
            # cannot support (gate 1.9.1).
            out.append(("- also written, loaded only if the game asks: "
                        if man.get("files") else "- not loaded: ")
                       + ", ".join(rest))
    return "\n".join(out) + "\n"


def _remembered_evidence(install_dir: Path, man: dict, rep: Report,
                         seen: dict) -> bool:
    """The same answers, from what the watcher saw while the game was up.

    Dated, and said as the past tense it is: "when the game last ran" rather
    than "is running". A record from before this install is refused by
    watch.last_sighting, so anything that arrives here describes this one.
    """
    try:
        from .. import watch
        seen = watch.settle(seen, man.get("files"))
    except Exception:
        pass
    if not seen:
        return False
    when = datetime.fromtimestamp(seen.get("at", 0)).strftime("%d %b %H:%M")
    running = str(seen.get("name") or "the game")
    app = "app" if man.get("kind") == "video" else "game"
    recorded = Path(str(man.get("exe") or "")).name

    if seen.get("refused"):
        rep.add(WARN, f"{running} ran at {when}, and would not say what it "
                      f"had loaded.",
                f"{str(seen['refused']).capitalize()}. So the {app} HAS been "
                f"started since the install - what could not be read is "
                f"whether the files here were in it. Anti-cheat and ReShade "
                f"add-ons do not coexist; if this game has anti-cheat, that "
                f"is the answer.")
        rep.verdict = (f"{running} ran at {when} and wrote no log - it is a "
                       f"protected process, so nothing of ours could be read "
                       f"in it.")
        rep.never_ran = False
        return True

    if recorded and running.lower() != recorded.lower():
        rep.add(BAD, f"At {when} the {app} ran from {running}, not "
                     f"{recorded}.",
                f"{seen.get('exe') or running} is what started. The install "
                f"went beside {recorded}, and a process only loads what is "
                f"beside the executable it started from. Point the tool at "
                f"{running} and install again.")
        rep.verdict = (f"The {app} runs from {running}, and the install went "
                       f"beside {recorded} - install again there.")
        rep.never_ran = False
        return True

    if seen.get("elsewhere"):
        names = ", ".join(sorted({Path(p).name for p in seen["elsewhere"]}))
        rep.add(BAD, f"At {when} the {app} had {names} loaded from somewhere "
                     f"else, not from this folder.",
                "It had: " + "; ".join(list(seen["elsewhere"])[:3])
                + ". A DLL already loaded under that name is never loaded a "
                  "second time, so the one written here is ignored. Remove "
                  "the other one, or install under a different name in the "
                  "'reshade loads as' dropdown.")
        rep.verdict = (f"The {app} loads {names} from another folder - ours "
                       f"is never reached.")
        rep.never_ran = False
        return True

    if seen.get("ours") and _through_layer(man) \
            and not _addon_in(seen["ours"]):
        if _layer_clash(man) is not None:
            rep.add(BAD, "The 32-bit ReShade layer carries the layer name "
                         "the 64-bit one uses, so the Vulkan loader throws "
                         "it away.",
                    "Loaded then: " + ", ".join(seen["ours"])
                    + ". Both of ReShade's layer manifests are called "
                      "VK_LAYER_reshade, and an implicit layer name may "
                      "only appear once: the loader keeps the 64-bit one, "
                      "then refuses it because a 32-bit game cannot load "
                      "it. Install again with this version - it gives the "
                      "32-bit layer its own name.")
            rep.verdict = ("The 32-bit Vulkan layer is being discarded as a "
                           "duplicate name - install again to rewrite it.")
            rep.never_ran = False
            return True
        rep.add(WARN, f"At {when} our files WERE loaded in {running}, and "
                      f"ReShade's Vulkan layer wrote no log.",
                _layer_detail(seen["ours"], seen.get("missing"), man, "then"))
        rep.verdict = (f"Loaded into {running} at {when}, and no log was "
                       f"written - ReShade's Vulkan layer is not reaching the "
                       f"game.")
        rep.never_ran = False
        return True

    if seen.get("ours"):
        rep.add(WARN, f"At {when} our files WERE loaded in {running}, and "
                      f"nothing wrote a log.",
                "Loaded then: " + ", ".join(seen["ours"])
                + ". So the proxy and the executable are both right and what "
                  "failed is further in - ReShade writes its log the moment "
                  "it initialises. Say so in an issue with this report.")
        rep.verdict = (f"Loaded into {running} at {when}, and no log was "
                       f"written - the proxy is reached and ReShade is not "
                       f"initialising.")
        rep.never_ran = False
        return True

    if _wrong_api_verdict(install_dir, man, rep, running,
                          "app" if man.get("kind") == "video" else "game"):
        return True
    rep.add(BAD, f"{running} ran at {when} with none of this folder's files "
                 f"loaded.",
            f"Windows lists every DLL in a process and not one of this "
            f"install's was in it, so the {app} HAS been started - it does "
            f"not load this folder's {man.get('proxy') or 'proxy DLL'} at "
            f"all. Try another name in the 'reshade loads as' dropdown in "
            f"the game's settings.")
    rep.verdict = (f"{running} ran at {when} and loaded nothing from this "
                   f"folder - try another proxy name.")
    rep.never_ran = False
    return True


# Proxy names a game only loads if it draws through DXGI (D3D10/11/12).
_DXGI_FAMILY = ("dxgi.dll", "d3d11.dll", "d3d12.dll", "d3d10.dll")


def _engine_says_not_dxgi(install_dir: Path, man: dict) -> tuple[str, str] | None:
    """(api, why) when the engine beside the exe draws with something a
    DXGI-family proxy never sees, and the install put one there anyway.

    #403: POSTAL 2 is Unreal Engine 2, its renderer is D3DDrv/D3D9Drv, and
    the detection of the time found no graphics import and assumed DXGI. A
    process that loads none of our files is then not "the wrong proxy
    name" - no DXGI name would ever load. Asked of the same engine rule the
    scan uses now (pe._engine_default), so this and a fresh scan agree.
    """
    proxy = str(man.get("proxy") or "").lower()
    exe = str(man.get("exe") or "")
    if proxy not in _DXGI_FAMILY or not exe:
        return None
    try:
        from .. import pe
        hit = pe._engine_default(install_dir / exe)
    except Exception:
        return None
    if not hit or hit[0] in ("DX10", "DX11", "DX12"):
        return None
    return hit


def _wrong_api_verdict(install_dir: Path, man: dict, rep: Report,
                       running: str, app: str) -> bool:
    """Say the install was made for the wrong renderer, when it was."""
    hit = _engine_says_not_dxgi(install_dir, man)
    if hit is None:
        return False
    api, why = hit
    rep.add(BAD, f"This install went in for a DXGI game, and {running} "
                 f"draws with {api if api != 'Unknown' else 'a renderer nothing here reaches'}.",
            f"The engine beside the exe says so: {why}. A "
            f"{man.get('proxy')} proxy is only ever loaded by a Direct3D "
            f"10/11/12 game, so no name in 'reshade loads as' would have "
            f"worked. "
            + ("The scan reads this engine: run 'full rescan' in the scan "
               "menu so the game is read again, then install again - it is "
               "set up for that renderer." if api != "Unknown" else
               "No route reaches this renderer. If the game's ini offers an "
               "OpenGL or Direct3D 9 RenderDevice, switch to it and run "
               "'full rescan' in the scan menu."))
    rep.verdict = (f"{running} ran and draws with {api}, not DXGI - 'full "
                   f"rescan', then install again."
                   if api != "Unknown" else
                   f"{running} ran on a renderer this tool cannot reach - "
                   f"see below.")
    rep.never_ran = False
    return True


def _launcher_installed(install_dir: Path, exe: str) -> bool:
    """Did this install go in front of a launcher rather than the game?

    The manifest keeps the executable's NAME, which is all this needs: a
    launcher starts the game as a separate process, so nothing beside it is
    ever loaded, and the folder looks exactly like a game that ignores its
    proxy. Read before either of those guesses is offered (#191).
    """
    if not exe:
        return False
    try:
        from .. import pe
        return pe.launcher_like(Path(exe))
    except Exception:
        return False


def _route_from_files(install_dir: Path) -> str:
    """Which route installed here, read off the folder.

    Only for a folder whose record is gone: the route decides which reader
    below runs, and the wrong one answers about files that route never
    writes. Ordered by how exclusive the marker is.
    """
    def there(*names: str) -> bool:
        return any((install_dir / n).is_file() for n in names)

    # The installer's own names, so a renamed add-on is renamed here too.
    from .. import installer as _i
    if there("OptiScaler.ini", "OptiScaler.log"):
        return "optiscaler"
    if any(install_dir.glob("*.trex/NvRemixBridge.exe")) \
            or (install_dir / ".trex").is_dir():
        return "remix"
    if there(_i.BRIDGE_ADDON):
        return "bridge"
    # Before the feeder and standalone: nvngx.dll.addon64 is only ever the
    # upstream add-on, and without this line an upstream folder read as
    # the feeder's and was answered about add-ons it never had.
    if there(_i.UPSTREAM_ADDON):
        return "upstream"
    if there(_i.FEEDER_ADDON64, _i.FEEDER_ADDON32, "dlss5-feed.log"):
        return "feeder"
    if there(_i.STANDALONE_ADDON, _i.STANDALONE_BRIDGE):
        return "standalone"
    # Last: the feeder and the bridge put renodx-dlss5.addon64 down too. The
    # RenoDX route's own build has a different name; the native route is
    # the one left with the plain add-on and nothing else.
    if there(_i.RENODX_SF):
        return "renodx"
    if there(_i.RENODX):
        return "native"
    return ""


def _anything_of_ours(install_dir: Path) -> str:
    """The first file in the folder that only this tool would have put there."""
    for n in _OUR_MARKS:
        try:
            if (install_dir / n).is_file():
                return n
        except OSError:
            pass
    # Nothing this tool writes on the Remix route sits beside the executable:
    # the runtime files go inside .trex and the option into rtx.conf. Without
    # this, a Remix install whose record is gone was told "nothing is
    # installed in this folder" and _route_from_files' own .trex branch could
    # never run, because it is gated on this answer.
    try:
        from .. import remix as _remix
        trex = _remix.find_runtime(install_dir)
        if trex is not None and (trex / _remix.DLSSNR).is_file():
            # A Remix mod ships a runtime, not a neural one: nvngx_dlssnr.dll
            # inside .trex is what an install of ours puts there. The option
            # in rtx.conf would be better evidence still, but that file is
            # the mod's own and other programs rewrite it (the foreign
            # writers rule), and the installer may have written it a folder
            # up - so the file is what this answers on.
            return f"{trex.name}/{_remix.DLSSNR}"
    except Exception:
        pass
    return ""


def _exc_line(text: str) -> str:
    """The exception line of a traceback: the frames are indented and the
    message is not, so it is the last unindented line."""
    for ln in reversed((text or "").strip().splitlines()):
        ln = ln.strip()
        if ln and not ln.startswith(("File ", "Traceback", "During handling",
                                     "The above exception")):
            return ln
    return ""


def _install_crash(last_error: str) -> tuple[str, str]:
    """(why, the exception line) from the traceback the install left behind.

    ("", "") when the last error did not come out of the install path - the
    update check and the GUI fail in their own ways and neither of them
    explains an empty folder.
    """
    text = last_error or ""
    # The frames, not the text: "installer.py" could be in a message, and
    # the report keeps only the last 900 characters, so the installer's own
    # frame is often cut off above net.py's (#197). Any frame of a module
    # the install reaches counts; one only in the window, the scan or the
    # library does not (#148's library.py).
    # A frame inside the window's package (core/ui) is the window's, whatever
    # its file is called: a stem shared with an install module must not make
    # a traceback of window frames read as an install crash.
    frames = [f for d, f in re.findall(r'(?:[\\/](\w+)[\\/])?(\w+)\.py", line \d+, in ', text)
              if d.lower() not in ("ui", "gui")]
    if not any(f in _install_modules() for f in frames):
        return "", ""
    line = _exc_line(text)
    low = line.lower()
    for key, why in _CRASH_CAUSES:
        if key in low:
            return why, line[:200]
    return "it stopped with an error", line[:200]


def _crash_verdict(rep: "Report", last_error: str) -> bool:
    """Say that the install crashed, when that is what emptied this folder.

    Only when nothing of ours arrived at all. A folder with our files in it
    and one missing has its own rules below - antivirus quarantine among
    them - and they are better answers than this one.
    """
    why, line = _install_crash(last_error)
    if not why:
        return False
    kind, what, todo = _net_cause(last_error)
    # Not "before it wrote anything": since 2.0.6 a failed fresh install is
    # also taken back out (installer._roll_back).
    rep.add(BAD, "The install stopped with an error, and nothing of it is "
                 "in this folder.",
            f"Nothing is installed here: {what or why}. It said: "
            f"{line} - so there is nothing here for the game to load, and "
            f"nothing to clean up. " + (todo or "Install again."))
    rep.verdict = _NET_VERDICTS.get(
        kind, "The install crashed before it finished - install again.")
    rep.ran = False
    return True


# By net.net_kind(); each starts with the stage-1 fragment "The install
# stopped", and none is "install again" alone (#434, #438).
_NET_VERDICTS = {
    "blocked": ("The install stopped: Windows blocked the download - allow "
                "this tool through the firewall, then install again."),
    "cut": ("The install stopped: the download kept being cut off - try "
            "another network or turn off the VPN/proxy, then install again."),
    "dns": ("The install stopped: this PC could not look up the download "
            "server - check the connection, then install again."),
    "refused": ("The install stopped: the download server could not be "
                "reached - try another network, then install again."),
    "timeout": ("The install stopped: the download server did not answer in "
                "time - try again later or from another network."),
}


def _net_cause(last_error: str) -> tuple[str, str, str]:
    """(net kind, what happened, what to do) for an install traceback that
    ends in a connection fault with a name, else ("", "", ""). A 2.0.3
    "[WinError 10013]" reads as 2.0.6's Unreachable line does; only the
    newer one names the host."""
    from .. import net as _net
    line = _exc_line(last_error) if _install_crash(last_error)[0] else ""
    kind = _net.net_kind(line) if line else ""
    m = re.search(r"Unreachable: ([\w.-]+):", line)
    return (kind, *_net.net_explain(kind, m.group(1) if m else "")) \
        if kind else ("", "", "")


def _net_stop(rep: "Report", last_error: str) -> bool:
    """An unfinished install whose own traceback (installer.last_failure,
    this folder's only) names the connection fault: "install again" cannot
    help a socket Windows refuses (#434)."""
    kind, what, todo = _net_cause(last_error)
    if kind:
        rep.add(BAD, "The install did not finish: the download could not "
                     "get through.", f"{what[:1].upper() + what[1:]}. {todo}")
        rep.verdict = _NET_VERDICTS[kind]
    return bool(kind)


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


def _dlss_mod():
    """core.dlss, imported where it is used, like this file's other siblings:
    dlss imports installer, and installer imports this module."""
    from .. import dlss
    return dlss


def _feed_shaders(install_dir: Path | None,
                  man: dict | None) -> tuple[list[str], list[str]]:
    """(recorded, gone) for the standalone route's two shaders.

    Recorded-and-gone, never "not on disk": a report from before these files
    were listed says nothing about them, and absence of evidence has cost
    this project three wrong verdicts already. Which is also why the
    reassuring half needs `recorded` - "both are in the folder" may not be
    said about files nothing ever wrote down.
    """
    if install_dir is None or not man:
        return [], []
    from .. import installer as _inst
    want = {_inst.STANDALONE_FX.lower(), _inst.VORT_FX.lower()}
    recorded, gone = [], []
    for f in man.get("files") or []:
        if not isinstance(f, str):
            continue
        base = f.replace("\\", "/").rsplit("/", 1)[-1]
        if base.lower() not in want:
            continue
        recorded.append(base)
        if not (install_dir / f).is_file():
            gone.append(base)
    return recorded, gone


def _stale_install(rep: "Report", man: dict) -> None:
    """Note when another build of this tool set the folder up.

    Every rule under this one reads files that build wrote, and half the
    reports that arrive on an old version are answered with a fix that
    shipped months ago (#215 came in on 1.5.0). Said as a finding and never
    as a verdict: the install may work perfectly, and what is wrong with it
    is decided by the rules below, not by its age.
    """
    from .. import update as _update
    was = str(man.get("tool") or "")
    if not was or was == _update.VERSION:
        return
    if _update._parse(was) > _update._parse(_update.VERSION):
        # A rollback, or a shared machine. Telling that person their install
        # is old and to press INSTALL would be two false statements.
        rep.add(INFO, f"This folder was set up by version {was}, which is "
                      f"newer than the {_update.VERSION} running now.",
                "What is read below was written by that build.")
        return
    rep.add(INFO, f"This folder was set up by version {was}; "
                  f"you are running {_update.VERSION}.",
            "Install again so it gets this build's files and fixes - "
            "your settings and backups are kept. Everything below is read "
            "from what that older install wrote.")


def _addon_switch(install_dir: Path, rep: "Report") -> str:
    """Say what the DLSS 5 add-on's own switch is set to.

    Returns "on", "off", "default" or "" - and the caller writes the
    verdict, because a verdict set here would be overwritten by the one
    written after the call.

    The add-on keeps it in ReShade.ini beside the game, so this is a fact on
    the person's own disk - and every route that "leaves no frame log" was
    answered by sending them to read it in an overlay.
    """
    try:
        from .. import reshade_ini as _ini
        state = _ini.addon_state(Path(install_dir))
    except Exception:
        return ""
    if not state:
        return ""
    switch = state.get("switch")
    if switch is None:
        if state.get("overlay_seen"):
            rep.add(INFO, "The add-on has written no settings of its own yet.",
                    f"ReShade.ini has no [{_ini.ADDON_SECTION}] section, so "
                    f"nobody has switched the neural pass either way in this "
                    f"game - the overlay's DLSS 5 tab says where it stands.")
            return "default"
        return ""
    value = str(switch).strip().lower()
    if not value:
        return ""                   # the key is there with nothing in it
    on = value not in ("0", "false", "off", "no")
    if on:
        rep.add(OK, "The add-on's own switch is on.",
                f"ReShade.ini beside the game has "
                f"[{_ini.ADDON_SECTION}] {_ini.ADDON_SWITCH}={switch}, so "
                f"this is not a case of it never having been turned on.")
        return "on"
    rep.add(BAD, "The neural pass is switched OFF in the add-on itself.",
            f"ReShade.ini beside the game has "
            f"[{_ini.ADDON_SECTION}] {_ini.ADDON_SWITCH}={switch}. Open the "
            f"ReShade overlay in the game and tick it back on in the DLSS 5 "
            f"tab. Installing again will not do it for you: a switch you "
            f"turned off is left alone, because turning it back on behind "
            f"you would be worse.")
    return "off"


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


def _opti_log(install_dir: Path) -> Path | None:
    """OptiScaler writes next to itself by default, or under Logs/."""
    cands = [install_dir / OPTI_LOG]
    try:
        cands += sorted((install_dir / "Logs").glob("*.log"),
                        key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return next((c for c in cands if c.is_file()), None)


def _layer_gone(man: dict) -> tuple[str, str, str] | None:
    """(title, detail, verdict) when the Vulkan layer cannot reach this game."""
    any_layer, mine = model._layer_state(man)
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


def _dxvk_gone(install_dir: Path, man: dict) -> list[str]:
    """DXVK files the install recorded that are no longer in the folder."""
    return [n for n in _dxvk_files(man) if not (install_dir / n).is_file()]


def _user_data_names(install_dir: Path, exe: str) -> list[str]:
    """What this game's per-user folder is plausibly called."""
    names: list[str] = []
    stem = Path(exe or "").stem
    for cut in ("-Win64-Shipping", "-WinGDK-Shipping", "-Win32-Shipping",
                "-Shipping"):
        if stem.lower().endswith(cut.lower()):
            stem = stem[: -len(cut)]
    if stem:
        names.append(stem)
        for tail in ("Client", "Game", "_x64", "64"):
            if stem.lower().endswith(tail.lower()) and len(stem) > len(tail):
                names.append(stem[: -len(tail)])
    # Up through the container folders only. One more step and this is the
    # launcher's name, and %LOCALAPPDATA%\Steam is not this game's data.
    p = install_dir
    for _ in range(_RAN_PARENTS + 1):
        if p.name:
            names.append(p.name)
        if p.name.lower() not in _RAN_CONTAINERS or p.parent == p:
            break
        p = p.parent
    out: list[str] = []
    low: set[str] = set()
    for n in names:
        n = n.strip()
        if n and n.lower() not in low and n.lower() not in _RAN_NOT_A_NAME:
            low.add(n.lower())
            out.append(n)
    return out[:6]


def _game_ran(install_dir: Path, exe: str, since: float,
              ours: set[str]) -> tuple[str, float]:
    """(what the game wrote after the install, when), or ("", 0).

    Evidence, not proof: a Steam update writes into a game folder too. It
    is reported as exactly what it is - something in the game's own files
    changed after the install.
    """
    if not since:
        return "", 0.0
    since += _RAN_MARGIN
    deadline = time.monotonic() + _RAN_SECONDS
    seen = 0
    best, best_t = "", 0.0
    places: list[tuple[Path, Path]] = []
    # The per-user folders first: they are small, they are named after this
    # game, and they are where most engines actually write.
    names = _user_data_names(install_dir, exe)
    for base in model._user_data_roots():
        for n in names:
            d = base / n
            for rel in _RAN_LOOK:
                places.append((d, d / rel if rel else d))
    # Then the folder holding the executable, and only the NAMED subfolders
    # of its parents. A parent itself is somebody else's ground - a game
    # under steamapps/common shares it with every other game installed, and
    # a file in there says nothing about this one.
    root = install_dir
    for depth in range(_RAN_PARENTS + 1):
        for rel in _RAN_LOOK:
            if rel:
                places.append((root, root / rel))
            elif depth == 0:
                places.append((root, root))
        # Unreal keeps Saved/ two levels above Binaries/Win64, which is the
        # only reason this climbs at all - so it climbs only while it is
        # standing in one of those container folders. At the game's own root
        # it stops: the next level up is steamapps/common, and the one above
        # that is Steam itself, whose logs and config it was reading.
        if root.parent == root or root.name.lower() not in _RAN_CONTAINERS:
            break
        root = root.parent
    for shown_from, d in places:
        if seen > _RAN_ENTRIES or time.monotonic() > deadline:
            break
        try:
            # islice, not list()[:n]: a folder with 100k entries would be
            # materialised in full before the slice bounded anything.
            with os.scandir(d) as it:
                entries = list(itertools.islice(it, _RAN_PER_DIR))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > _RAN_ENTRIES or time.monotonic() > deadline:
                break
            low = e.name.lower()
            if low in _RAN_SKIP or low in ours \
                    or low.endswith(_RAN_SKIP_SUFFIX) \
                    or any(part in low for part in _RAN_SKIP_PART) \
                    or low.endswith(_RAN_NOT_EVIDENCE):
                continue
            try:
                if not e.is_file():
                    continue
                t = e.stat().st_mtime
            except OSError:
                continue
            if t > since and t > best_t:
                try:
                    best = str(Path(e.path).relative_to(shown_from))
                except ValueError:
                    best = e.name
                best_t = t
    return best, best_t


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


def windows_crash(install_dir: Path, exe_name: str):
    """(crash, (title, detail)) Windows recorded for this game since the
    install, or (None, None). Worker only: it asks PowerShell (about 1 s).

    The game page reads this after every session and its "did it work?"
    counts a crash as not working; a caller that reads only analyse() can
    say "Working" about a session Windows saw fault. Whether the crash
    belongs to THIS session is the caller's (the game page's
    crash_is_this_session): it needs the log times the window keeps.
    """
    try:
        from .. import wincrash
        where = Path(install_dir)
        man = _manifest(where) or {}
        written = tuple(str(f) for f in (man.get("files") or []) if isinstance(f, str))
        c = wincrash.last_crash(exe_name or "", since=_installed_at(where) or 0.0)
        said = wincrash.describe(c, str(man.get("proxy") or ""), written)
    except Exception:
        return None, None
    return (c, said) if said else (None, None)
