r"""Finding installed games: Steam, Epic, GOG, emulators, manual folders.

Nothing is executed; library files are read and executable headers inspected.
Scanning is entirely local - no network access.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
import pathlib
from pathlib import Path

from . import emulators, log, pe

# If a game folder contains one of these, we have already installed there.
# Any of these next to the executable means we (or an older release of this
# tool) have installed here. The bridge and native routes leave no feeder
# add-on, so the manifest name is part of the check.
MARKER_FILES = ("dlss5-feed.addon64", "dlss5-feed.addon32",
                "dlss5-bridge.addon64", "dlss5-autopilot.json",
                "dlss5kur-kurulum.json", "dlss5-installer.json")


def _isdir(p) -> bool:
    """Path.is_dir() that survives a drive letter Windows will not talk about.

    A card reader with no card, a BitLocker volume that is locked, or a
    drive that went away raise OSError 87 ("wrong parameter") from stat()
    instead of returning False, and one such letter used to take the whole
    Xbox / folder / emulator scan down with it (issue #2).
    """
    try:
        return pathlib.Path(p).is_dir()
    except OSError:
        return False

# What to tell someone whose Xbox / Game Pass folder cannot be written to.
# One sentence, shared with the installer so both places say the same thing.
XBOX_HINT = ("Windows does not let anything write into this Xbox / Game Pass "
             "folder. Only games whose publisher allows modding have 'Enable "
             "mods' (or 'Manage > Files > Browse') in the Xbox app; if yours "
             "has it, turn it on and press rescan. If it does not - Microsoft "
             "Flight Simulator, most Store titles - no tool can change this "
             "folder, and the Steam version of the game is the one that can "
             "be set up.")
# A protected executable is a different thing from a locked folder (#157,
# Forza Horizon 6): the exe cannot be opened, the folder beside it can be
# written. What is lost is what the exe would have told us.
XBOX_EXE_HINT = ("Windows protects this Xbox game's executable, so the tool "
                 "cannot read it. Choose 64-bit or 32-bit in the game's "
                 "details (almost every Game Pass game is 64-bit) and check "
                 "the graphics API there - that guess comes from the files "
                 "beside the game, not from the game itself.")
# ...and on the install page, where the choice has already been made.
XBOX_EXE_CHOSEN = ("Windows protects this executable: the architecture is "
                   "the one chosen on the games page, and the graphics API is "
                   "chosen there or guessed from the files beside the game - "
                   "neither is read from the game.")

# Folder names Windows keeps under its own ownership for store games. Exact
# segment match on purpose: ModifiableWindowsApps is the one that IS meant
# to be touched and must not be caught by a substring test.
_LOCKED_STORE_DIRS = ("xboxgames", "windowsapps")


def is_locked_store_path(path: Path) -> bool:
    r"""Is this path inside C:\XboxGames or a WindowsApps folder?

    Says nothing about read or write access; callers must probe separately.
    """
    try:
        parts = Path(path).parts
    except TypeError:
        return False
    return any(p.lower() in _LOCKED_STORE_DIRS for p in parts)


def _xbox_locked(g: "Game") -> bool:
    """Warn about EXE protection without inferring directory write access."""
    if g.exe is None or not is_locked_store_path(g.exe):
        return False
    try:
        with open(g.exe, "rb") as f:
            f.read(1)
    except PermissionError:
        g.exe_warning = XBOX_EXE_HINT
        log.write(f"{g.name}: {g.exe} is protected - {XBOX_EXE_HINT}")
        return True
    except OSError:
        return False
    return False


@dataclass
class Game:
    name: str
    folder: Path                 # the game's root folder
    exe: Path | None = None      # chosen executable
    bitness: int | None = None   # 32 / 64
    api: str = "?"
    api_why: str = ""
    api_detected: str = ""          # what the executable said, before any override
    source: str = "Manual"       # Steam / Epic / GOG / Emulator / Manual
    candidates: list[Path] = field(default_factory=list)
    error: str = ""
    emu: object | None = None    # emulators.Profile, when applicable
    install_root: Path | None = None   # folder an earlier install wrote to
    kind: str = "game"             # "game" or "video" (a player, no depth)
    exe_warning: str = ""          # protected Xbox EXE; not a write-access verdict

    @property
    def install_dir(self) -> Path:
        r"""Where files go: next to the executable.

        In many games the exe is not in the root (e.g. Kingdom Come 2 ->
        Bin\Win64MasterMasterSteamPGO\KingdomCome.exe). The ReShade proxy must
        sit beside the executable or it is never loaded.

        Once an install has happened, the folder it wrote to wins - see
        `adopt_previous_install`.
        """
        if self.install_root is not None:
            return self.install_root
        return self.exe.parent if self.exe else self.folder

    @property
    def installed(self) -> bool:
        """Has this tool (or an older release of it) set this folder up?

        The install record is the real answer. An add-on file alone is not:
        a Downloads folder full of components someone fetched by hand used
        to show as "installed" - so a loose add-on only counts when a loader
        (ReShade's proxy, a Vulkan layer install, or OptiScaler) sits beside
        it.
        """
        d = self.install_dir
        if any((d / m).is_file() for m in MARKER_FILES if m.endswith(".json")):
            return True
        if not any((d / m).is_file() for m in MARKER_FILES):
            return False
        loaders = ("dxgi.dll", "d3d11.dll", "d3d12.dll", "d3d9.dll", "d3d10.dll",
                   "opengl32.dll", "winmm.dll", "version.dll", "dbghelp.dll",
                   "ReShade.ini", "OptiScaler.ini")
        return any((d / n).is_file() for n in loaders)

    @property
    def bit_label(self) -> str:
        return f"{self.bitness}-bit" if self.bitness else "?"


# ---------------------------------------------------------------- Steam

def _steam_root() -> Path | None:
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for val in ("SteamPath", "InstallPath"):
                        try:
                            p = Path(winreg.QueryValueEx(k, val)[0])
                            if p.is_dir():
                                return p
                        except OSError:
                            pass
            except OSError:
                continue
    except Exception:
        pass
    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if Path(guess).is_dir():
            return Path(guess)
    return None


def _steam_libraries(root: Path) -> list[Path]:
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf8", errors="replace")
    except OSError:
        return libs
    for m in re.finditer(r'"path"\s*"([^"]+)"', text):
        p = Path(m.group(1).replace("\\\\", "\\"))
        if p.is_dir() and p not in libs:
            libs.append(p)
    return libs


def scan_steam() -> list[Game]:
    root = _steam_root()
    if not root:
        return []
    out: list[Game] = []
    seen: set[Path] = set()
    for lib in _steam_libraries(root):
        apps = lib / "steamapps"
        common = apps / "common"
        if not common.is_dir():
            continue
        # appmanifest files carry the real display name
        names: dict[str, str] = {}
        # Steam deletes a game's appmanifest when it is uninstalled and
        # leaves the folder behind - saves, shader caches, config, sometimes
        # nothing at all. On this machine that is 29 of 32 folders in one
        # library, and every one of them used to be listed as a game the
        # tool could install into. A manifest is the only cheap proof that
        # the game is actually there.
        installed: set[str] = set()
        try:
            manifests = list(apps.glob("appmanifest_*.acf"))
        except OSError:
            manifests = []
        for acf in manifests:
            # One unreadable manifest used to abort the whole loop, and now
            # that the set decides what is shown, a file Steam happened to be
            # rewriting would have hidden every game after it.
            try:
                t = acf.read_text(encoding="utf8", errors="replace")
            except OSError:
                continue
            nm = re.search(r'"name"\s*"([^"]+)"', t)
            d = re.search(r'"installdir"\s*"([^"]+)"', t)
            # StateFlags carries 4 when the game is fully installed. One
            # that is queued or still downloading has a manifest and a
            # folder - Battlefield 6
            # sat in the list as an empty folder with StateFlags 1042 - and
            # there is nothing to install into until it is finished.
            st = re.search(r'"StateFlags"\s*"(\d+)"', t)
            done = bool(int(st.group(1)) & 4) if st else True
            if d and done:
                installed.add(d.group(1).lower())
            if nm and d:
                names[d.group(1).lower()] = nm.group(1)
        try:
            folders = [p for p in common.iterdir() if p.is_dir()]
        except OSError:
            continue
        for f in folders:
            rp = f.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            # No manifest: believe the folder only if it still has an
            # executable of its own at the top level. That keeps a game
            # whose manifest is missing for some other reason, and drops the
            # leftovers, without walking into anything.
            # ...and never hide a folder this tool has installed into, even
            # when the game itself is gone: that is the one place someone
            # needs the entry, to press uninstall and get the leftovers out.
            if (f.name.lower() not in installed and not _has_exe(f)
                    and not _marked(f)):
                continue
            out.append(Game(name=names.get(f.name.lower(), f.name), folder=f, source="Steam"))
    return out


def _has_exe(folder: Path, limit: int = 400) -> bool:
    """Is there an .exe directly in this folder? One listing, capped."""
    try:
        with os.scandir(folder) as it:
            for i, e in enumerate(it):
                if i >= limit:
                    return True     # a folder this full is not a leftover
                if e.name.lower().endswith(".exe") and e.is_file():
                    return True
    except OSError:
        return True                 # unreadable: leave it in the list
    return False


# ---------------------------------------------------------------- Epic

def scan_epic() -> list[Game]:
    man = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / \
        "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    if not man.is_dir():
        return []
    out: list[Game] = []
    for item in man.glob("*.item"):
        try:
            d = json.loads(item.read_text(encoding="utf8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        # Epic also registers engine plugins, content packs and Unreal
        # Engine itself. A plugin's InstallLocation can be the entire engine
        # tree (Quixel Bridge), which is both a false game and a huge scan.
        # Older manifests without these flags still get inspected.
        if d.get("bIsApplication") is False or d.get("bIsExecutable") is False:
            continue
        loc = d.get("InstallLocation")
        if not loc or not Path(loc).is_dir():
            continue
        g = Game(name=d.get("DisplayName") or Path(loc).name,
                 folder=Path(loc), source="Epic")
        launch = d.get("LaunchExecutable")
        if launch:
            cand = Path(loc) / launch
            if cand.is_file():
                g.exe = cand
        out.append(g)
    return out


# ---------------------------------------------------------------- GOG

def scan_gog() -> list[Game]:
    out: list[Game] = []
    try:
        import winreg
    except ImportError:
        return out
    for key in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as root:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(root, sub) as k:
                            path = Path(winreg.QueryValueEx(k, "path")[0])
                            name = winreg.QueryValueEx(k, "gameName")[0]
                            if path.is_dir():
                                out.append(Game(name=name, folder=path, source="GOG"))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


# ------------------------------------------------- EA / Ubisoft / Battle.net

def _reg_walk(hive, key: str, value: str, name_value: str = "") -> list[Game]:
    """Every subkey of `key` that names an install folder in `value`."""
    out: list[Game] = []
    try:
        import winreg
    except ImportError:
        return out
    try:
        with winreg.OpenKey(hive, key) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub) as k:
                        p = Path(winreg.QueryValueEx(k, value)[0])
                        if not p.is_dir():
                            continue
                        name = p.name
                        if name_value:
                            try:
                                name = winreg.QueryValueEx(k, name_value)[0] or name
                            except OSError:
                                pass
                        out.append(Game(name=name, folder=p))
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return out


def scan_ea() -> list[Game]:
    """EA app / Origin. Battlefield and Dead Space live here, not on Steam."""
    out: list[Game] = []
    import sys as _sys
    hives = ()
    try:
        import winreg
        hives = ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Electronic Arts"),
                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Electronic Arts"))
    except ImportError:
        return out
    for hive, key in hives:
        for g in _reg_walk(hive, key, "Install Dir"):
            g.source = "EA"
            out.append(g)
    # The EA app also keeps a plain games folder.
    for guess in (r"C:\Program Files\EA Games", r"C:\Program Files (x86)\EA Games"):
        root = Path(guess)
        if not _isdir(root):
            continue
        try:
            for f in root.iterdir():
                if f.is_dir():
                    out.append(Game(name=f.name, folder=f, source="EA"))
        except OSError:
            pass
    del _sys
    return out


def scan_ubisoft() -> list[Game]:
    """Ubisoft Connect. Far Cry, Assassin's Creed, Avatar."""
    out: list[Game] = []
    try:
        import winreg
    except ImportError:
        return out
    for g in _reg_walk(winreg.HKEY_LOCAL_MACHINE,
                       r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs",
                       "InstallDir"):
        g.source = "Ubisoft"
        out.append(g)
    return out


def scan_battlenet() -> list[Game]:
    """Battle.net titles, from their uninstall entries."""
    out: list[Game] = []
    try:
        import winreg
    except ImportError:
        return out
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as root:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    if "battle.net" not in sub.lower():
                        continue
                    try:
                        with winreg.OpenKey(root, sub) as k:
                            loc = Path(winreg.QueryValueEx(k, "InstallLocation")[0])
                            nm = winreg.QueryValueEx(k, "DisplayName")[0]
                            if loc.is_dir():
                                out.append(Game(name=nm, folder=loc,
                                                source="Battle.net"))
                    except (OSError, ValueError):
                        continue
        except OSError:
            continue
    return out


# Folders the Xbox app keeps beside the games. GameSave holds cloud saves in
# empty container directories and Minecraft Launcher is a launcher with a
# large runtime tree: both are unscannable, neither is a game (issue #8).
XBOX_NOT_GAMES = {"gamesave", "minecraft launcher"}


def scan_xbox() -> list[Game]:
    r"""Xbox / Game Pass.

    Discover the usual library folders. Executable read access is checked
    by enrich(); directory write access is checked by installer.preflight().
    """
    out: list[Game] = []
    roots = []
    for drive in "CDEFGH":
        roots.append(Path(f"{drive}:/Program Files/ModifiableWindowsApps"))
        roots.append(Path(f"{drive}:/XboxGames"))
    for root in roots:
        if not _isdir(root):
            continue
        try:
            for f in root.iterdir():
                if not f.is_dir():
                    continue
                if f.name.lower() in XBOX_NOT_GAMES:
                    continue
                # XboxGames puts the real files one level down, in Content.
                inner = f / "Content"
                out.append(Game(name=f.name,
                                folder=inner if inner.is_dir() else f,
                                source="Xbox"))
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- emulators

# ---------------------------------------------- Rockstar / Amazon / itch / Heroic

def scan_rockstar() -> list[Game]:
    """Rockstar Games Launcher: GTA V, Red Dead Redemption 2 bought there."""
    out: list[Game] = []
    try:
        import winreg
    except ImportError:
        return out
    for key in (r"SOFTWARE\WOW6432Node\Rockstar Games", r"SOFTWARE\Rockstar Games"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as root:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    if sub.lower() in ("launcher", "rockstar games launcher"):
                        continue
                    try:
                        with winreg.OpenKey(root, sub) as k:
                            p = Path(winreg.QueryValueEx(k, "InstallFolder")[0])
                            if p.is_dir():
                                out.append(Game(name=sub, folder=p, source="Rockstar"))
                    except (OSError, ValueError):
                        continue
        except OSError:
            continue
    return out


def scan_amazon() -> list[Game]:
    """Amazon Games keeps every title under one library folder."""
    out: list[Game] = []
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Amazon Games" / "Library"
    # The launcher's SQLite db would be nicer, but the folder is enough and
    # needs no parser: each game is a folder with its exe inside.
    roots = [base]
    for drive in "CDEFGH":
        roots.append(Path(f"{drive}:/Amazon Games/Library"))
    for r in roots:
        try:
            if _isdir(r):
                out += [Game(name=f.name, folder=f, source="Amazon")
                        for f in r.iterdir() if f.is_dir()]
        except OSError:
            continue
    return out


def scan_itch() -> list[Game]:
    r"""itch.io app: %APPDATA%\itch\apps\<game>."""
    out: list[Game] = []
    base = Path(os.environ.get("APPDATA", "")) / "itch" / "apps"
    try:
        if _isdir(base):
            out += [Game(name=f.name, folder=f, source="itch")
                    for f in base.iterdir() if f.is_dir()]
    except OSError:
        pass
    return out


def scan_heroic() -> list[Game]:
    """Heroic (Epic/GOG/Amazon through one launcher): reads its own records."""
    out: list[Game] = []
    cfg = Path(os.environ.get("APPDATA", "")) / "heroic"
    for rel in ("legendaryConfig/legendary/installed.json",
                "gog_store/installed.json", "nile_config/nile/installed.json"):
        p = cfg / rel
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = data.values() if isinstance(data, dict) else data
        for it in items:
            if not isinstance(it, dict):
                continue
            loc = it.get("install_path") or it.get("path")
            name = it.get("title") or it.get("app_name") or ""
            if loc and Path(loc).is_dir():
                out.append(Game(name=name or Path(loc).name, folder=Path(loc),
                                source="Heroic"))
    return out


def is_removable(root: Path) -> bool:
    """A USB stick or external drive? Those hold backups and transfers, not
    the copy the person plays - an install landed on one (issue #18)."""
    try:
        import ctypes
        DRIVE_REMOVABLE = 2
        return ctypes.windll.kernel32.GetDriveTypeW(str(root)) == DRIVE_REMOVABLE
    except Exception:
        return False


# Folder names that say nothing about the game: "bin" was the name a report
# came in under (issue #17, World War Z lives in <game>\bin).
# Only names that are unmistakably a binaries folder: "Game", "Content" or
# "Engine" can be a real game's own folder, and walking past them would
# have named a Steam title "common".
GENERIC_DIRS = {"bin", "bin64", "binaries", "win64", "win32", "x64", "x86",
                "retail", "shipping", "release", "system", "exe", "executable"}


def display_name(folder: Path) -> str:
    """The nearest folder name up the path that is not a generic one."""
    for p in (folder, *folder.parents):
        if p.name and p.name.lower() not in GENERIC_DIRS:
            return p.name
    return folder.name


def scan_folders() -> list[Game]:
    r"""Plain game folders people keep outside any launcher: D:\Games\X.

    "It does not list my game" was almost always one of these. Only folders
    with a game-looking name are scanned, one level deep, and only when a
    drive actually has such a folder - so this stays cheap.
    """
    out: list[Game] = []
    names = ("Games", "Game", "Oyunlar", "Juegos", "Spiele", "Jeux", "Giochi",
             "My Games", "PC Games", "Installed Games")
    for drive in "CDEFGHIJ":
        base = Path(f"{drive}:/")
        if not _isdir(base) or is_removable(base):
            continue
        for n in names:
            d = base / n
            try:
                if not d.is_dir():
                    continue
                for f in d.iterdir():
                    if f.is_dir() and not f.name.startswith(("." , "$")):
                        out.append(Game(name=f.name, folder=f, source="Folder"))
            except OSError:
                continue
    return out


def scan_emulators(progress=None) -> list[Game]:
    out: list[Game] = []
    for prof, exe in emulators.scan(progress):
        g = Game(name=f"{prof.name} ({prof.system})", folder=exe.parent,
                 exe=exe, source="Emulator")
        g.emu = prof
        out.append(g)
    return out


# ---------------------------------------------------------------- shared

def _marked(d: Path) -> bool:
    """Has an install of ours - or an older release's - left its record here?"""
    try:
        return any((d / m).is_file() for m in MARKER_FILES)
    except OSError:
        return False


def _recorded_exe(d: Path) -> str | None:
    """The executable name the manifest in this folder was written for."""
    for name in ("dlss5-autopilot.json", "dlss5kur-kurulum.json",
                 "dlss5-installer.json"):
        f = d / name
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            exe = data.get("exe")
            if isinstance(exe, str) and exe.strip():
                return exe.strip()
    return None


def adopt_previous_install(g: Game) -> None:
    r"""Point the game at the folder an earlier install actually wrote to.

    The executable is chosen fresh on every scan and `pe.find_game_exes` only
    ranks the candidates - so a game installed against `Bin\Win64\Game.exe`
    can be listed against a different executable the next time round.
    `install_dir` then pointed at a folder holding nothing of ours: the
    uninstall button stayed greyed out and the files stayed on disk. Reported
    as "uninstall does not work".

    So look for our record in every candidate executable's folder. When one is
    found, adopt that folder - and the executable the manifest names, so the
    architecture and api shown describe the game that was actually patched.
    """
    here = g.exe.parent if g.exe else None
    dirs: list[Path] = []
    for d in ([here] if here else []) + [g.folder] + [c.parent for c in g.candidates]:
        if d is not None and d not in dirs:
            dirs.append(d)
    for d in dirs:
        if not _marked(d):
            continue
        g.install_root = d
        if here is not None and d != here:
            log.write(f"{g.name}: an earlier install is recorded in {d}, "
                      f"not next to {g.exe.name}")
        chosen: Path | None = None
        want = _recorded_exe(d)
        if want and (d / want).is_file():
            chosen = d / want
        elif here != d:
            chosen = next((c for c in g.candidates if c.parent == d), None)
        if chosen is not None:
            g.exe = chosen
        return


def _prefer_real_exe(g: Game) -> None:
    r"""A store's launch executable is often a stub that starts the real one.

    Epic's manifest names GWT.exe in the root of Ghostwire Tokyo; the game is
    Snowfall\Binaries\Win64\GWT.exe. Files placed beside the stub are never
    loaded, and the install "does nothing". When the ranked candidates put an
    executable under a Binaries folder first and the store's pick is not in
    one, the ranking wins.
    """
    if not g.exe or not g.candidates:
        return
    top = g.candidates[0]
    if top == g.exe:
        return
    in_bin = lambda p: any(part.lower() == "binaries" for part in p.parts)
    if in_bin(top) and not in_bin(g.exe):
        log.write(f"{g.name}: the store names {g.exe.name} but the game runs "
                  f"from {top.relative_to(g.folder)} - using that")
        g.exe = top


APIS = ("DX9", "DX10", "DX11", "DX12", "Vulkan", "OpenGL")


def api_override(folder: Path) -> str:
    """The graphics API the person chose for this folder, or ""."""
    try:
        from . import prefs
        return str((prefs.get("api_override") or {}).get(str(folder).lower(), "") or "")
    except Exception:
        return ""


def set_api_override(folder: Path, api: str | None) -> None:
    """Remember (or forget, with None/"") the API chosen for this folder."""
    from . import prefs
    d = dict(prefs.get("api_override") or {})
    key = str(folder).lower()
    if api and api in APIS:
        d[key] = api
    else:
        d.pop(key, None)
    prefs.set_("api_override", d)


def bitness_override(folder: Path) -> int | None:
    """Architecture chosen for a protected Xbox executable, or None."""
    from . import prefs
    try:
        value = (prefs.get("bitness_override") or {}).get(str(folder).lower())
        return value if type(value) is int and value in (32, 64) else None
    except (AttributeError, TypeError):
        return None


def set_bitness_override(folder: Path, bitness: int | None) -> None:
    """Remember an explicit architecture; None restores automatic detection."""
    if bitness is not None and (type(bitness) is not int or bitness not in (32, 64)):
        raise ValueError("Architecture must be 32 or 64.")
    from . import prefs
    d = dict(prefs.get("bitness_override") or {})
    key = str(folder).lower()
    if bitness is None:
        d.pop(key, None)
    else:
        d[key] = bitness
    prefs.set_("bitness_override", d)


def enrich(g: Game, chosen: bool = False) -> Game:
    """Pick the executable and detect its architecture / graphics API.

    `chosen` means the person picked this executable in the list: it is not
    replaced by the store's stub or by an earlier install's record, and the
    files go beside it (issue #56: Conan Exiles installed beside the launcher
    after the Shipping exe was chosen).
    """
    if chosen:
        g.install_root = None
    g.error = g.exe_warning = ""
    g.bitness = None
    g.api, g.api_why, g.api_detected = "?", "", ""
    try:
        if g.exe is None or not g.exe.is_file():
            cands = pe.find_game_exes(g.folder)
            if not cands:
                g.error = "no executable found"
                adopt_previous_install(g)
                return g
            g.candidates = cands
            g.exe = cands[0]
        elif not g.candidates:
            g.candidates = pe.find_game_exes(g.folder) or [g.exe]
        if not chosen:
            _prefer_real_exe(g)
            adopt_previous_install(g)
        if _xbox_locked(g):
            g.bitness = bitness_override(g.folder)
        else:
            g.bitness = pe.exe_bitness(g.exe)
        g.api, g.api_why = pe.detect_api(g.exe)
        g.api_detected = g.api
        forced = api_override(g.folder)
        if forced:
            # The import table can lie: R.U.S.E. links D3D11 and renders
            # with D3D9 (#24). A choice made on the install page wins.
            g.api, g.api_why = forced, f"set by hand (detected {g.api_detected})"
        if g.emu is None:
            prof = emulators.profile_for(g.exe)
            if prof:
                g.emu = prof
                if g.source == "Manual":
                    g.name = f"{prof.name} ({prof.system})"
    except pe.PEError as e:
        g.error = str(e)
        log.write(f"could not read {g.name}: {e}", "warn")
    except Exception as e:
        g.error = f"unreadable: {e}"
        log.write(f"could not read {g.name} ({g.folder}): {e}", "warn")
    if g.exe is None:
        # Reported as "sometimes it does not see my games". A folder with no
        # executable we recognise is dropped from the list entirely, and until
        # now without saying which one, so it looked random.
        log.write(f"no executable under {g.folder} - {g.name} will not be "
                  f"listed", "warn")
    return g


def scan_all(progress=None) -> list[Game]:
    """Scan every source, resolve executables, sort by name."""
    games = list_games(progress)
    total = len(games)
    for i, g in enumerate(games, 1):
        if progress:
            progress(f"Inspecting games... {i}/{total}: {g.name}")
        started = time.monotonic()
        enrich(g)
        if time.monotonic() - started >= 1:
            log.write(f"inspected {g.name} in {time.monotonic() - started:.1f}s")
    games = same_exe_once(games)
    games.sort(key=lambda g: g.name.lower())
    return games


def quick_scan(known: list, progress=None) -> tuple[list, list]:
    """The library as it stands, reading only what is new: (games, fresh).

    #144: six drives, one of them an old USB disk, and every new game meant
    walking all of them again. The stores' own lists are cheap to read - it
    is the per-game inspection and the emulator search across drives that
    take the time. So the stores are asked what they have, a game already
    in `known` is kept as it was read, and only folders not seen before are
    inspected. `fresh` are those, for the caller to check. Emulators are not
    searched for again; "full rescan" does that.
    """
    have: dict = {}
    for g in known:
        try:
            have[g.folder.resolve()] = g
        except OSError:
            continue
    fresh = []
    listed = list_games(progress, emulators=False)
    seen = set()
    for g in listed:
        try:
            seen.add(g.folder.resolve())
        except OSError:
            continue

    def still_here(g) -> bool:
        """Kept unless a store listed it before and lists it no more.

        A game removed in Steam often leaves its folder behind, and a quick
        rescan that only ever added would keep it forever. What no store
        lists in the first place stays: a folder chosen by hand, an emulator
        (not searched for here), and anything this tool set up, so it can
        still be uninstalled.
        """
        if not g.folder.exists():
            return False
        if g.source in ("Manual", "Emulator") or getattr(g, "kind", "") == "video":
            return True
        try:
            if g.folder.resolve() in seen:
                return True
        except OSError:
            return True
        try:
            return bool(g.installed)
        except Exception:
            return True

    out = [g for g in known if still_here(g)]
    for g in listed:
        try:
            key = g.folder.resolve()
        except OSError:
            continue
        if key in have:
            continue
        if progress:
            progress(f"Inspecting a new game: {g.name}")
        enrich(g)
        have[key] = g
        out.append(g)
        fresh.append(g)
    out = same_exe_once(out)
    out.sort(key=lambda g: g.name.lower())
    return out, [g for g in fresh if g in out]


def list_games(progress=None, emulators: bool = True) -> list[Game]:
    """What every store says is installed, before anything is inspected."""
    games: list[Game] = []
    sources_ = [("Steam", scan_steam), ("Epic", scan_epic),
                ("GOG", scan_gog), ("EA", scan_ea),
                ("Ubisoft", scan_ubisoft), ("Battle.net", scan_battlenet),
                ("Rockstar", scan_rockstar), ("Amazon", scan_amazon),
                ("itch", scan_itch), ("Heroic", scan_heroic),
                ("Xbox", scan_xbox), ("Folders", scan_folders)]
    if emulators:
        sources_.append(("Emulator", scan_emulators))
    for label, fn in sources_:
        if progress:
            progress(f"Scanning {label}...")
        try:
            got = fn(progress) if label == "Emulator" else fn()
            games += got
            log.write(f"scan {label}: {len(got)} found")
        except Exception as e:
            # One store failing must not stop the others - but it must not be
            # silent either. "It finds no games" was impossible to act on
            # while every failure here was swallowed.
            log.exception(f"scanning {label} failed", e)
            if progress:
                progress(f"{label} could not be read: {type(e).__name__}")

    # The same folder may be reported by two stores; the store wins over a
    # plain folder scan, which is why Folders comes after them.
    uniq: dict[Path, Game] = {}
    for g in games:
        try:
            uniq.setdefault(g.folder.resolve(), g)
        except OSError:
            continue
    games = list(uniq.values())
    # A Games folder often holds the launcher libraries themselves
    # (D:\Games\SteamLibrary), which are not games.
    junk = ("steamlibrary", "steamapps", "epic games", "gog galaxy", "ea games",
            "ubisoft", "xboxgames", "amazon games", "battle.net", "common",
            "riot games", "rockstar games")
    return [g for g in games
            if not (g.source == "Folder" and g.folder.name.lower() in junk)]


def same_exe_once(games: list) -> list:
    """One entry per game executable, the first store's.

    Two stores can report one install under different folders - Steam gives
    the library folder, the Rockstar launcher the GTAIV subfolder inside it -
    and the folder check above passes both. The executable is only known
    after enrich(), so this runs after it. Emulators are left alone: several
    games of one system can share an emulator's executable.
    """
    def weight(g) -> tuple:
        # The entry that carries something wins over the first one: an
        # install recorded against it, or a graphics API set by hand - both
        # are keyed by that entry's folder and would be lost with it.
        try:
            forced = bool(api_override(g.folder) or bitness_override(g.folder))
        except Exception:
            forced = False
        return (bool(getattr(g, "installed", False)), forced)

    at: dict = {}
    out = []
    for g in games:
        exe = getattr(g, "exe", None)
        if exe is None or getattr(g, "emu", None) is not None \
                or getattr(g, "kind", "game") != "game":
            out.append(g)
            continue
        try:
            key = str(Path(exe).resolve()).lower()
        except OSError:
            key = str(exe).lower()
        if key in at:
            i = at[key]
            if weight(g) > weight(out[i]):
                g, out[i] = out[i], g
            log.write(f"scan: {g.name} ({g.source}) is the same executable as "
                      f"{out[i].name} ({out[i].source}) - listed once")
            continue
        at[key] = len(out)
        out.append(g)
    return out


def manual(path: Path) -> Game:
    """Build a Game from a user-selected folder or executable."""
    path = Path(path)
    if path.is_file():
        g = Game(name=display_name(path.parent), folder=path.parent, exe=path,
                 source="Manual")
    else:
        g = Game(name=display_name(path), folder=path, source="Manual")
    return enrich(g)
