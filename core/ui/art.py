"""A game's pictures: chosen by hand, Steam's cache, Xbox files, or looked up online.

Steam keeps every owned game's library art in appcache/librarycache/<appid>/
(sometimes one hashed folder deeper): the 600x900 cover, the 1920x620 hero,
a blurred hero and the transparent logo. The appid comes from the library's
appmanifest, matched on the install folder the scan already knows. Games
from other stores get theirs through core.covers (Epic's catalog, Xbox's
own files, Steam's store by name), and fall back to the executable's own
icon, and to a plain tile when there is none.

Three calls, by thread:
  find(game)    worker only: walks folders, then queues an online lookup on
                the art thread when nothing local has a cover
  peek(game)    the Tk thread: what `find` last answered, from memory
  listen(fn)    fn(folder) is called from the art thread when new pictures
                land; the window turns it into an ("art", folder) message
"""
from __future__ import annotations

import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .. import covers
from .. import games as _games
from .. import log
from . import imaging

_lock = threading.Lock()
_appids: dict[str, str] | None = None
_found: dict[str, "Art"] = {}
_jobs: "queue.Queue" = queue.Queue()
_asked: set[str] = set()       # asked online this run - never twice (no retry loop)
_running = False
_listeners: list = []


@dataclass
class Art:
    cover: Path | None = None
    hero: Path | None = None
    blur: Path | None = None
    logo: Path | None = None
    exe: Path | None = None
    accent: str | None = None
    chosen: bool = False        # a picture was chosen by hand (menus offer the reset)


def _index() -> dict[str, str]:
    """installdir (lower) -> appid, over every Steam library on the machine."""
    global _appids
    with _lock:
        if _appids is not None:
            return _appids
        out: dict[str, str] = {}
        root = _games._steam_root()
        if root:
            for lib in _games._steam_libraries(root):
                try:
                    manifests = list((lib / "steamapps").glob("appmanifest_*.acf"))
                except OSError:
                    continue
                for acf in manifests:
                    try:
                        t = acf.read_text(encoding="utf8", errors="replace")
                    except OSError:
                        continue
                    a = re.search(r'"appid"\s*"(\d+)"', t)
                    d = re.search(r'"installdir"\s*"([^"]+)"', t)
                    if a and d:
                        out[d.group(1).lower()] = a.group(1)
        _appids = out
        return out


def _cache_dir() -> Path | None:
    root = _games._steam_root()
    d = root / "appcache" / "librarycache" if root else None
    return d if d and d.is_dir() else None


def _pick(folder: Path, *names: str) -> Path | None:
    for n in names:
        p = folder / n
        if p.is_file():
            return p
    try:
        for sub in folder.iterdir():          # hashed folders, one level down
            if sub.is_dir():
                for n in names:
                    p = sub / n
                    if p.is_file():
                        return p
    except OSError:
        pass
    return None


def appid(game) -> str | None:
    if str(getattr(game, "source", "")).lower() != "steam":
        return None
    try:
        return _index().get(Path(game.folder).name.lower())
    except Exception:
        return None


def find(game) -> Art:
    """Where this game's pictures are. Cached; call off the Tk thread.

    Per picture, the first that has it: chosen by hand, Steam's own cache,
    the Xbox folder's files, downloaded earlier. With no cover from any of
    them the online lookup is queued, and `listen` hears when it lands.
    """
    key = str(getattr(game, "folder", ""))
    hit = _found.get(key)
    if hit is not None:
        return hit
    art = Art(exe=getattr(game, "exe", None))
    aid = appid(game)
    base = _cache_dir()
    steam: dict = {}
    if aid and base and (base / aid).is_dir():
        d = base / aid
        steam = {"cover": _pick(d, "library_600x900.jpg", "library_capsule.jpg"),
                 "hero": _pick(d, "library_hero.jpg"),
                 "blur": _pick(d, "library_hero_blur.jpg"),
                 "logo": _pick(d, "logo.png")}
    mine, got = {}, {}
    try:
        mine, got = covers.user(game), covers.got(game)
    except Exception:
        pass
    xbox: dict = {}
    if not (mine.get("cover") or steam.get("cover") or got.get("cover")):
        try:
            xbox = covers.xbox(game.folder)
        except Exception:
            xbox = {}
    for kind in ("cover", "hero", "logo"):
        for src in (mine, steam, xbox, got):
            if src.get(kind):
                setattr(art, kind, src[kind])
                break
    art.chosen = bool(mine)
    # Steam's blurred copy belongs to Steam's hero, not to one chosen by hand
    if art.hero is not None and art.hero == steam.get("hero"):
        art.blur = steam.get("blur")
    if art.cover:
        art.accent = imaging.accent(imaging.picture(art.cover, 24, 36))
    elif getattr(game, "kind", "game") == "game":
        try:
            if covers.pending(game):
                _want(game, aid)
        except Exception:
            pass
    _found[key] = art
    return art


def peek(game) -> Art | None:
    """What `find` last answered for this game, or None. Memory only: the
    Tk thread asks this on every draw, so it never touches the disk."""
    return _found.get(str(getattr(game, "folder", "")))


def listen(fn) -> None:
    """fn(folder: str) runs on the art thread when a game's pictures change.
    One window per process, so the last one to listen is the one told."""
    with _lock:
        _listeners[:] = [fn]


def _tell(game) -> None:
    folder = str(getattr(game, "folder", ""))
    _found.pop(folder, None)
    for fn in list(_listeners):
        try:
            fn(folder)
        except Exception:
            pass


def _want(game, aid) -> None:
    global _running
    k = covers.key(game.folder)
    with _lock:
        if k in _asked:
            return
        _asked.add(k)
        _jobs.put((game, aid))
        if _running:
            return
        _running = True
    threading.Thread(target=_work, daemon=True, name="art").start()


def _work() -> None:
    """One lookup at a time, so a library of fifty games is fifty polite
    requests in a row, not fifty at once."""
    global _running
    while True:
        with _lock:
            try:
                game, aid = _jobs.get_nowait()
            except queue.Empty:
                _running = False
                return
        try:
            if covers.lookup(game, appid=aid):
                _tell(game)
        except Exception as e:
            log.write(f"art: {getattr(game, 'name', '?')}: {type(e).__name__}: {e}")


def choose(game, kind: str, path) -> str:
    """A picture chosen by hand; '' when it is in place, else what to say.
    Worker only: it reads and copies the file."""
    said = covers.set_user(game, kind, path,
                           readable=lambda p: imaging.picture(p, 4, 4) is not None)
    if not said:
        _tell(game)
    return said


def reset(game) -> None:
    """Back to the automatic pictures. Worker only."""
    covers.reset_user(game)
    _tell(game)


def retry() -> None:
    """The online setting was switched on: games asked about this run may ask again."""
    with _lock:
        _asked.clear()
    _found.clear()


def forget() -> None:
    """Drop every cached lookup - after a rescan, Steam may have new art."""
    global _appids
    with _lock:
        _appids = None
    _found.clear()
