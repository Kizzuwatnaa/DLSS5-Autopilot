r"""Multi-frame generation on RTX 40: dashdogy's RTX40MFG-Unlock.

NVIDIA sells DLSS Multi Frame Generation (3x/4x, up to 6x) as an RTX 50
feature; RTX 40 gets plain 2x frame generation. dashdogy's unlock hooks the
Streamline and NGX calls inside the game before the frame-generation
feature is created, validates the adapter and the wrapper, and asks for the
higher multiplier with the Ada temporal correction applied. Nothing on disk
is changed - NVIDIA's own files are left alone, the change lives in memory.

It needs three things, all of which this module handles:

  - the game already has working DLSS Frame Generation (Streamline). The
    unlock adds multipliers to a feature the game has; it does not add
    frame generation to a game without it.
  - Ultimate ASI Loader (ThirteenAG) under a proxy name the game imports
    early, so RTX40MFG.asi runs before the first frame-generation pipeline
    exists. ReShade holds dxgi.dll, so the loader goes in as dinput8.dll
    or version.dll - whichever the executable imports. An executable that
    imports none of the loader's names cannot take it, and the tool says
    so instead of guessing.
  - a ReShade with add-on support, for the DLSS MFG tab where the
    multiplier is chosen. Every ReShade route here has that.

Research software, in its author's words: higher multipliers and Vulkan may
give artifacts, a frozen picture or a crash. Off by default, RTX 40 only,
and only offered when the game itself ships a DLSS Frame Generation runtime.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from . import net, pe

# The release LIST, not /releases/latest: v1.3.2 turned the three files into
# a single RTXMFG.dll in RTXMFG-v1.3.2.zip, and "latest" plus an any-zip
# fallback picked that zip and failed on its first missing file (#141).
API = "https://api.github.com/repos/dashdogy/RTX40MFG-Unlock/releases?per_page=20"
# The asset name of the releases this module knows how to place
# (Universal-RTX-40-MFG-Unlock-v1.2.1.zip, the last of them so far).
ASSET_PREFIX = "universal-rtx-40-mfg-unlock"
LOADER_API = "https://api.github.com/repos/ThirteenAG/Ultimate-ASI-Loader/releases/latest"
LOADER_ASSET = "Ultimate-ASI-Loader_x64.zip"
# What the release zip carries and where it goes: beside the executable.
FILES = ("RTX40MFGCore.dll", "RTX40MFG.asi", "RTX40MFG-UI.addon64")
UI_ADDON = "RTX40MFG-UI.addon64"
# The loader's own file inside its zip, and the names it can be loaded as.
# dinput8.dll first (the author's example); version.dll for games that do
# not import DirectInput, and for RE Engine games where dinput8.dll is
# already REFramework.
LOADER_DLL = "dinput8.dll"
LOADER_NAMES = ("dinput8.dll", "version.dll", "winmm.dll")
# The values the release's global.ini asks for, merged into the loader's
# own <proxy>.ini rather than overwriting it.
GLOBAL_SETS = {
    "LoadPlugins": "1",
    "LoadFromScriptsOnly": "1",
    "LoadExtraPlugins": "RTX40MFG.asi",
    "DontLoadFromDllMain": "0",
    "ForceEntryPointHook": "0",
}
BACKUP_SUFFIX = ".dlss5-autopilot-backup"
MANIFEST = "dlss5-autopilot.json"
ADA = 89                      # sm_89, the RTX 40 architecture

# Files a game ships when it has DLSS Frame Generation: the NGX runtime and
# Streamline's frame-generation plugin. Either one is enough evidence.
DLSSG_FILES = ("nvngx_dlssg.dll", "sl.dlss_g.dll")


def _ours(install_dir: Path) -> set[str]:
    """File names our own manifest in this folder says an install wrote.

    The standalone route puts an nvngx_dlssg.dll beside a game that never
    had frame generation; counting that as the game's own would offer an
    unlock with nothing to unlock.
    """
    try:
        man = json.loads((install_dir / MANIFEST).read_text(encoding="utf8"))
        return {str(f).replace("\\", "/").rsplit("/", 1)[-1].lower()
                for f in man.get("files") or [] if isinstance(f, str)}
    except (OSError, ValueError, AttributeError):
        return set()


def has_dlssg(game_dir: Path, install_dir: Path | None = None) -> str:
    """The frame-generation file the GAME ships, relative to game_dir, or "".

    Searched the way the DLSS files are (dlss.find_dlss_files): Unreal keeps
    them under Engine/Plugins/Runtime/Nvidia/DLSS/..., nine levels down from
    the game folder and nowhere near the executable.
    """
    from . import dlss
    ours = _ours(install_dir) if install_dir is not None else set()
    try:
        hits = dlss.find_dlss_files(Path(game_dir), names=DLSSG_FILES)
    except Exception:
        hits = []
    for rel in hits:
        if rel.replace("\\", "/").rsplit("/", 1)[-1].lower() not in ours:
            return rel
    return ""


def applies(sm: int | None, api: str, install_dir: Path,
            game_dir: Path | None = None) -> tuple[bool, str]:
    """(offer it?, why not) for this card, API and game."""
    if sm != ADA:
        return False, "RTX 40 only - RTX 50 has multi-frame generation natively"
    if api not in ("DX12", "Vulkan"):
        return False, "D3D12 and Vulkan games only"
    if not has_dlssg(game_dir or install_dir, install_dir):
        return False, "the game has no DLSS Frame Generation to unlock"
    return True, ""


# Where a loader that is already there keeps its .asi plugins. Cyber Engine
# Tweaks is the one this came from (#137): its version.dll IS an ASI loader,
# it loads plugins out of this folder, and the tool used to put Ultimate ASI
# Loader in its place - which takes CET, and every mod that depends on it,
# out of the game.
PLUGINS_DIR = "plugins"
# What goes into that folder rather than beside the executable: the backend
# and the .asi the loader picks up. The ReShade add-on stays beside the exe,
# where ReShade looks for it.
PLUGIN_FILES = ("RTX40MFGCore.dll", "RTX40MFG.asi")


def free_for_loader(exe_dir: Path, name: str,
                    ours: set[str] = frozenset()) -> bool:
    """May the loader be written under this name here?

    Yes when nothing has that name, when an earlier install of ours wrote
    it, or when what is there is Ultimate ASI Loader itself - its ini is
    merged rather than replaced, so its own plugins keep loading. Anything
    else under that name belongs to another mod and is left alone.
    """
    p = exe_dir / name
    if not p.is_file() or name.lower() in {o.lower() for o in ours}:
        return True
    return is_loader(p)


def loader_name(exe: Path | None, taken: set[str] = frozenset(),
                exe_dir: Path | None = None,
                ours: set[str] = frozenset()) -> str | None:
    """The proxy name the loader goes in as: one the executable really
    imports and nothing else in the folder already uses - or None when the
    import table names none of them (a guessed name would never load).

    With `exe_dir`, a name another mod's DLL already occupies is skipped
    too. Without it the old answer stands, so every caller that does not
    pass a folder is unchanged.
    """
    imports: set[str] = set()
    if exe is not None:
        try:
            imports = {i.lower() for i in pe.pe_imports(exe)}
        except Exception:
            imports = set()
    for n in LOADER_NAMES:
        if n not in imports or n in taken:
            continue
        if exe_dir is not None and not free_for_loader(exe_dir, n, ours):
            continue
        return n
    return None


def existing_plugins(exe_dir: Path) -> Path | None:
    """A loader that is already here, with its plugins folder, or None.

    Only when both halves are there: a plugins folder AND a DLL under one
    of the names a loader is loaded as. A plugins folder on its own is some
    other program's.
    """
    plug = exe_dir / PLUGINS_DIR
    if not plug.is_dir():
        return None
    if any((exe_dir / n).is_file() for n in LOADER_NAMES):
        return plug
    return None


class ShapeChanged(RuntimeError):
    """The release no longer carries the files this module places.

    Raised instead of guessing: the unlock is an opt-in extra, and the
    installer turns this into a warning rather than failing the install.
    """


def resolve() -> tuple[str, str]:
    """(tag, download url) of the newest release with the universal zip.

    Newest first through the list, skipping releases of another shape - a
    newer layout is not taken on trust (#141); the installer places FILES
    and nothing else.
    """
    rels = net.json_get(API)
    if not isinstance(rels, list):
        rels = [rels] if isinstance(rels, dict) else []
    rels = [r for r in rels if isinstance(r, dict)
            and not r.get("draft") and not r.get("prerelease")]
    rels.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    for r in rels:
        for a in r.get("assets") or []:
            n = str(a.get("name", "")).lower()
            if (n.startswith(ASSET_PREFIX) and n.endswith(".zip")
                    and a.get("browser_download_url")):
                return r.get("tag_name", "?"), a["browser_download_url"]
    newest = rels[0].get("tag_name", "?") if rels else "none"
    raise ShapeChanged(
        f"no RTX40MFG-Unlock release carries the Universal-RTX-40-MFG-Unlock "
        f"zip this tool knows how to place (newest: {newest}) - the project "
        f"changed its layout; the rest of the install is unaffected")


def resolve_loader() -> tuple[str, str]:
    """(tag, download url) of Ultimate ASI Loader's 64-bit zip."""
    r = net.json_get(LOADER_API)
    tag = r.get("tag_name", "?")
    for a in r.get("assets", []):
        if a.get("name", "") == LOADER_ASSET:
            return tag, a["browser_download_url"]
    raise RuntimeError(f"Could not find {LOADER_ASSET} in Ultimate ASI Loader's release.")


def is_loader(path: Path) -> bool:
    """Is this file Ultimate ASI Loader? It carries its own name."""
    try:
        if not path.is_file() or path.stat().st_size < (1 << 18):
            return False
        return b"Ultimate ASI Loader" in path.read_bytes()
    except OSError:
        return False


def _merge_ini(path: Path) -> None:
    """Put GLOBAL_SETS into [GlobalSets] of the loader's ini, keeping the rest."""
    from .optiscaler import _ini_set
    text = path.read_text(encoding="utf8", errors="replace") if path.is_file() else ""
    path.write_text(_ini_set(text, "GlobalSets", GLOBAL_SETS), encoding="utf8")


class NoLoaderName(RuntimeError):
    """The executable imports none of the names the loader can take."""


def _check_zip(zpath: Path, names, what: str) -> None:
    """Raise ShapeChanged unless the zip holds every one of `names`.

    Matched the way net.extract_one finds them: a file member whose name
    ends with the wanted one, case-insensitive.
    """
    try:
        members = [m.lower() for m in net.zip_members(zpath)
                   if not m.endswith("/")]
    except (OSError, zipfile.BadZipFile) as e:
        raise ShapeChanged(f"{what}: {zpath.name} is not a readable zip "
                           f"({e}) - nothing was placed") from e
    missing = [n for n in names
               if not any(m.endswith(n.lower()) for m in members)]
    if missing:
        raise ShapeChanged(
            f"{what} does not carry {', '.join(missing)} - the release "
            f"changed its layout; nothing was placed")


def install(exe_dir: Path, exe: Path | None, log=None,
            taken: set[str] = frozenset(),
            preinstalled=()) -> tuple[str, list[str]]:
    """Put the unlock and its loader beside the game. Returns (tag, files).

    A file already under the loader's name is backed up once so uninstall
    puts it back - unless an earlier install of OURS wrote it (it is in
    `preinstalled`, the previous manifest's file list). A loader the person
    installed by hand for other .asi mods is theirs and is backed up like
    any other file, whatever it contains.
    """
    log = log or (lambda *_: None)
    ours_now = {str(p).replace("\\", "/").rsplit("/", 1)[-1].lower()
                for p in preinstalled}
    name = loader_name(exe, taken, exe_dir, ours_now)
    plug = None
    if name is None:
        # Every name is either not imported or occupied by another mod. If
        # one of those mods is itself a loader, its plugins folder is the
        # way in - no second loader, no name taken off anyone (#137).
        plug = existing_plugins(exe_dir)
        if plug is None:
            taken_here = [n for n in LOADER_NAMES
                          if (exe_dir / n).is_file()
                          and not free_for_loader(exe_dir, n, ours_now)]
            why = (f"{', '.join(taken_here)} beside the executable belongs to "
                   f"the game or another mod, and this does not overwrite it"
                   if taken_here else
                   f"{exe.name if exe else 'the executable'} imports none of "
                   f"{', '.join(LOADER_NAMES)}")
            raise NoLoaderName(
                f"{why}, so Ultimate ASI Loader has no name it would be "
                f"loaded under - the unlock cannot be placed here. Everything "
                f"else in the install is unaffected.")
    tag, url = resolve()
    log(f"      RTX40MFG-Unlock {tag}")
    z = net.download(url, f"RTX40MFG-Unlock-{tag}.zip")
    if plug is not None:
        _check_zip(z, FILES, f"RTX40MFG-Unlock {tag}")
        written = []
        for n in PLUGIN_FILES:
            net.extract_one(z, n, plug / n)
            written.append(f"{PLUGINS_DIR}/{n}")
        net.extract_one(z, UI_ADDON, exe_dir / UI_ADDON)
        written.append(UI_ADDON)
        log(f"      a loader is already here: {', '.join(PLUGIN_FILES)} into "
            f"{PLUGINS_DIR}/, {UI_ADDON} beside the game - its own loader "
            f"picks the .asi up, and nothing of anyone else's is replaced")
        log("      in game: ReShade overlay -> DLSS MFG tab -> Follow game, a "
            "fixed multiplier, or Dynamic")
        return tag, written
    ltag, lurl = resolve_loader()
    lz = net.download(lurl, f"Ultimate-ASI-Loader-{ltag}_x64.zip")
    # Both archives are downloaded and checked before the first file lands:
    # a zip missing one file used to fail half way, after the others were
    # already beside the game and before any of them was recorded, so an
    # uninstall could not take them back out (#141).
    _check_zip(z, FILES, f"RTX40MFG-Unlock {tag}")
    _check_zip(lz, (LOADER_DLL,), f"Ultimate ASI Loader {ltag}")

    ours = {str(p).replace("\\", "/").lower() for p in preinstalled}
    written: list[str] = []
    for n in FILES:
        net.extract_one(z, n, exe_dir / n)
        written.append(n)
    log(f"      {', '.join(FILES)}")

    dest = exe_dir / name
    bak = dest.with_name(name + BACKUP_SUFFIX)
    if dest.is_file() and not bak.exists() and name.lower() not in ours:
        try:
            shutil.copy2(dest, bak)
            written.append(bak.name)
            log(f"      kept your existing {name} as {bak.name}")
        except OSError:
            log(f"      WARNING: could not back up the existing {name}")
    net.extract_one(lz, LOADER_DLL, dest)
    written.append(name)
    ini = dest.with_suffix(".ini")
    existed = ini.is_file() and ini.name.lower() not in ours
    _merge_ini(ini)
    if not existed:
        written.append(ini.name)
    log(f"      Ultimate ASI Loader {ltag} as {name} + {ini.name} "
        f"([GlobalSets] LoadExtraPlugins=RTX40MFG.asi)")
    log("      in game: ReShade overlay -> DLSS MFG tab -> Follow game, a "
        "fixed multiplier, or Dynamic")
    return tag, written


def remove_leftovers(exe_dir: Path, preinstalled, log=None) -> list[str]:
    """Take out an earlier install's unlock when this one does not want it.

    A reinstall on the same route skips the full uninstall, so without this
    the three files and the loader stayed - hooking Streamline while the new
    manifest said the option was off. Returns the relative names removed
    (the caller drops them from `preinstalled`).
    """
    log = log or (lambda *_: None)
    ours = {str(p).replace("\\", "/") for p in preinstalled}
    lowers = {p.lower() for p in ours}
    if not any(f.lower() in lowers for f in
               [*FILES] + [f"{PLUGINS_DIR}/{x}" for x in PLUGIN_FILES]):
        return []
    gone: list[str] = []
    for n in [*FILES] + [f"{PLUGINS_DIR}/{x}" for x in PLUGIN_FILES]:
        p = exe_dir / n
        if n.lower() in lowers and p.is_file():
            try:
                p.unlink()
                gone.append(n)
            except OSError:
                log(f"      WARNING: could not remove {n}")
    for n in LOADER_NAMES:
        if n.lower() not in lowers:
            continue
        p = exe_dir / n
        bak = p.with_name(n + BACKUP_SUFFIX)
        try:
            if p.is_file():
                p.unlink()
                gone.append(n)
            if bak.is_file():
                bak.replace(p)
                gone.append(bak.name)
                log(f"      put your own {n} back")
            ini = p.with_suffix(".ini")
            if ini.name.lower() in lowers and ini.is_file():
                ini.unlink()
                gone.append(ini.name)
        except OSError:
            log(f"      WARNING: could not remove {n}")
    if gone:
        log(f"      multi-frame generation off: removed {', '.join(gone)}")
    return gone
