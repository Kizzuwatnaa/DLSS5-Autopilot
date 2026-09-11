r"""The discovered game library, kept between launches.

Finding the library is the slowest thing this tool does: every store's
folders are walked, every executable is read for its architecture and its
renderer, and the compatibility pass walks the game folder again looking for
DLSS runtimes. On a large library across several drives that is minutes, and
it happened again on every launch even when nothing had changed - which is
most launches, because people open the tool several times while trying one
game (issues #8, #18, #32, and josema0890's #67, which came with a working
proof of concept).

So the result is written to

    %LOCALAPPDATA%\dlss5-autopilot\library.json

and read back on the next launch. Two things decide whether it may be used:

* the cache is written by one version of this tool and read by the same one.
  What the tool believes about a game - its renderer above all - is exactly
  what changes between releases: 1.7.1 called Unity games OpenGL and 1.7.2
  did not (issues #46, #47, #48). A cache that survived the update would
  keep handing back the answer the update was written to fix.
* each game carries a stamp of what it looked like. A game whose folder or
  executable has changed since - Steam updated it, the person installed
  something into it by hand - is inspected again on the spot; the rest come
  back as they were.

Nothing here is load-bearing: a missing, unreadable or stale file simply
means the normal full scan runs, exactly as before.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import prefs

SCHEMA = 2
# Beside settings.json in %LOCALAPPDATA%, not beside the executable: the
# exe is run from Downloads, from a USB stick, from a folder Defender
# has taken an interest in - none of them a place to keep state.
FILE = prefs.FILE.parent / "library.json"


def _stamp(g) -> list:
    """What this game looked like when it was inspected.

    The executable's size and time catch a game the store has updated; the
    folder's own time catches a file dropped in beside it (a mod, another
    add-on) - which is what the compatibility columns are read from.
    """
    out: list = []
    for p in (g.exe, g.folder, g.install_root):
        try:
            st = os.stat(p) if p else None
            out.append([int(st.st_mtime), st.st_size] if st else None)
        except OSError:
            out.append(None)
    return out


def _to_json(g) -> dict:
    return {
        "name": g.name,
        "folder": str(g.folder),
        "exe": str(g.exe) if g.exe else None,
        "bitness": g.bitness,
        "api": g.api,
        "api_why": g.api_why,
        "api_detected": g.api_detected,
        "source": g.source,
        "candidates": [str(c) for c in (g.candidates or [])],
        "error": g.error,
        "exe_warning": g.exe_warning,
        "install_root": str(g.install_root) if g.install_root else None,
        "kind": g.kind,
        "stamp": _stamp(g),
    }


def _from_json(d: dict):
    from . import emulators, games
    exe = Path(d["exe"]) if d.get("exe") else None
    g = games.Game(
        name=d["name"],
        folder=Path(d["folder"]),
        exe=exe,
        bitness=d.get("bitness"),
        api=d.get("api") or "?",
        api_why=d.get("api_why") or "",
        api_detected=d.get("api_detected") or "",
        source=d.get("source") or "Manual",
        candidates=[Path(c) for c in d.get("candidates") or []],
        error=d.get("error") or "",
        exe_warning=d.get("exe_warning") or "",
        install_root=Path(d["install_root"]) if d.get("install_root") else None,
        kind=d.get("kind") or "game",
    )
    if g.exe_warning:
        g.bitness = games.bitness_override(g.folder)
        g.api = g.api_detected or "?"
    # A graphics API set by hand on the install page lives in the settings,
    # not in the library, and it has to win here exactly as it wins in
    # games.enrich(): a cached game that came back with its DETECTED renderer
    # would be installed for the wrong one, which is the fault that dropdown
    # exists to fix (#24, #66, #70).
    try:
        forced = games.api_override(g.folder)
    except Exception:
        forced = ""
    if forced:
        g.api = forced
        g.api_why = f"set by hand (detected {g.api_detected or '?'})"
    # An emulator profile is a live object with its own config paths; it is
    # not written out, it is found again from the executable.
    if exe is not None:
        try:
            g.emu = emulators.profile_for(exe)
        except Exception:
            g.emu = None
    return g


def _key(g) -> str:
    return f"{g.folder}\x00{g.exe}"


def save(all_games: list, rows: dict, version: str, sm) -> None:
    """Write the library and its compatibility rows. Never raises."""
    try:
        data = {
            "schema": SCHEMA,
            "app_version": version,
            "sm": sm,
            "games": [_to_json(g) for g in all_games],
            # The row keys are (folder, exe) tuples; JSON has string keys.
            "rows": {f"{f}\x00{e}": list(r) if r is not False else False
                     for (f, e), r in rows.items()},
        }
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf8")
            os.replace(tmp, FILE)
        finally:
            # A write that failed half way (a full drive, #148) left its
            # .tmp behind on every save, taking more of the space it lacked.
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    except Exception as e:
        from . import log, net
        if net.is_disk_full(e):
            log.write("the library cache was not saved: the drive is full",
                      "warn")
        else:
            log.exception("saving the library cache")


def load(version: str, sm) -> tuple[list, dict, list] | None:
    """The library as it was last seen: (games, rows, changed).

    `changed` holds the games whose folder or executable is not what it was.
    Their cached row is left out AND they are handed back for a fresh read -
    a game that switched renderer in an update would otherwise be installed
    for the old one, which is the very mistake the cache is version-gated to
    avoid. Reading them costs a folder walk each, so the caller does it off
    the Tk thread.
    """
    try:
        data = json.loads(FILE.read_text(encoding="utf8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    # The compatibility columns are read against the card that is in the
    # machine, so a different one has to be looked at again.
    if data.get("app_version") != version or data.get("sm") != sm:
        return None
    try:
        out, rows, changed = [], {}, []
        raw = data.get("rows") or {}
        for d in data.get("games") or []:
            g = _from_json(d)
            if not g.folder.exists():
                continue                      # the game (or its drive) is gone
            out.append(g)
            # Protection can change without an EXE timestamp change; also
            # recheck manual metadata rather than reuse a compatibility row.
            if g.exe_warning or _stamp(g) != [list(x) if x else None for x in (d.get("stamp") or [])]:
                changed.append(g)             # changed on disk: read it again
                continue
            r = raw.get(_key(g))
            if r is not None:
                rows[(str(g.folder), str(g.exe))] = False if r is False else tuple(r)
        # One entry per executable here too: a folder picked by hand is
        # saved beside the store's entry for the same game.
        from .games import same_exe_once
        out = same_exe_once(out)
        changed = [g for g in changed if any(g is k for k in out)]
        return (out, rows, changed) if out else None
    except Exception:
        from . import log
        log.exception("reading the library cache")
        return None


def forget() -> None:
    """Drop the cache - the next launch scans from scratch."""
    try:
        FILE.unlink()
    except OSError:
        pass
