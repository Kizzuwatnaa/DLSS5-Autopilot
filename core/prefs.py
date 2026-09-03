r"""Persistent preferences + locating a local renodx add-on.

renodx builds shared on Discord are not on the public mirror. Once the user
points at a file, remember it and use it as the default for every game.

Search order:
    1. previously chosen / remembered file
    2. the  renodx\  folder next to the executable    <- portable install
    3. Downloads / Desktop (up to one subfolder deep)
    4. none found -> download from the rhi-repo mirror
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dlss5-autopilot" / "settings.json"


def app_dir() -> Path:
    """Folder next to the executable (PyInstaller onefile aware)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save(data: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    except OSError:
        pass


def get(key: str, default=None):
    return load().get(key, default)


def set_(key: str, value) -> None:
    d = load()
    d[key] = value
    save(d)


def is_renodx(path: Path) -> bool:
    """Is this actually a RenoDX DLSS 5 add-on (either family)?

    DLSS5-Feeder's own dlss5-feed.addon64 shares the extension, so we do not
    trust the name - we look for the signature string inside the binary.
    Installing the wrong file would break the install silently.
    """
    try:
        if not path.is_file() or path.stat().st_size < 200_000:
            return False
        data = path.read_bytes()
        if data[:2] != b"MZ":
            return False
        return b"RenoDX" in data and b"DLSS" in data
    except OSError:
        return False


def is_renodx_sf(path: Path) -> bool:
    """ShortFuse's renodx-dlss build, as opposed to Krish's renodx-dlss5.

    The two are not interchangeable: the SF build hooks D3D9/D3D11/D3D12
    itself and carries its own module name in the PE, so that is what is
    checked - not the file name, which people rename freely.
    """
    try:
        if not is_renodx(path):
            return False
        data = path.read_bytes()
        return (b"renodx-dlss.addon64" in data
                or "renodx-dlss.addon64".encode("utf-16-le") in data)
    except OSError:
        return False


def _candidates() -> list[Path]:
    hits: list[Path] = []

    # 2) the renodx folder next to the app (and the app folder itself)
    for d in (app_dir() / "renodx", app_dir()):
        if d.is_dir():
            try:
                hits += [f for f in d.glob("*.addon64") if f.is_file()]
            except OSError:
                pass

    # 3) Downloads / Desktop, one subfolder deep - people usually leave the
    #    file inside a folder rather than loose on the Desktop.
    home = Path.home()
    roots = [home / "Downloads", home / "Desktop",
             Path(os.environ.get("USERPROFILE", home)) / "Downloads"]
    for d in roots:
        if not d.is_dir():
            continue
        try:
            hits += [f for f in d.glob("*.addon64") if f.is_file()]
            for sub in d.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    try:
                        hits += [f for f in sub.glob("*.addon64") if f.is_file()]
                    except OSError:
                        continue
        except OSError:
            continue

    uniq: dict[Path, Path] = {}
    for f in hits:
        try:
            uniq.setdefault(f.resolve(), f)
        except OSError:
            continue
    good = [f for f in uniq.values() if is_renodx(f)]
    good.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return good


def find_renodx(sf: bool = False) -> tuple[Path | None, list[Path]]:
    """(file_to_use, all_candidates) of the requested family.

    A remembered choice wins, but only if it is of the right family - a
    remembered renodx-dlss5 must not be handed to the renodx-dlss route.
    """
    cands = [c for c in _candidates() if is_renodx_sf(c) == sf]
    saved = get("renodx_local")
    if saved:
        p = Path(saved)
        if p.is_file() and is_renodx_sf(p) == sf:
            others = [c for c in cands if c.resolve() != p.resolve()]
            return p, [p] + others
    return (cands[0] if cands else None), cands


def remember_renodx(path: Path | None) -> None:
    set_("renodx_local", str(path) if path else None)


def remember_consumer_dir(consumer: str, path: Path | None) -> None:
    """Where the person keeps a Discord-only neural add-on's files."""
    set_(f"consumer_dir_{consumer}", str(path) if path else None)


def consumer_dir(consumer: str):
    return get(f"consumer_dir_{consumer}")


# --- Vulkan installs ----------------------------------------------------
# The Vulkan layer is registered once for the whole user, not per game, so it
# must only be removed when the LAST Vulkan game that needs it is uninstalled.

def installs() -> list[str]:
    """Every folder this tool has set up (and not removed since)."""
    return [str(x) for x in (get("installs") or [])]


def add_install(install_dir) -> None:
    lst = installs()
    if str(install_dir) not in lst:
        lst.append(str(install_dir))
        set_("installs", lst)


def drop_install(install_dir) -> None:
    lst = [x for x in installs() if x != str(install_dir)]
    set_("installs", lst)


def vulkan_games() -> list[str]:
    v = get("vulkan_games", [])
    return v if isinstance(v, list) else []


def add_vulkan_game(install_dir) -> None:
    d = str(install_dir)
    games_ = vulkan_games()
    if d not in games_:
        games_.append(d)
        set_("vulkan_games", games_)


def drop_vulkan_game(install_dir) -> list[str]:
    """Forget this game and return the ones still registered."""
    d = str(install_dir)
    games_ = [g for g in vulkan_games() if g != d]
    set_("vulkan_games", games_)
    return games_
