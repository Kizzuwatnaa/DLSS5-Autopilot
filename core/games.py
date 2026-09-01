r"""Finding installed games: Steam, Epic, GOG, emulators, manual folders.

Nothing is executed; library files are read and executable headers inspected.
Scanning is entirely local - no network access.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import emulators, log, pe

# If a game folder contains one of these, we have already installed there.
# Any of these next to the executable means we (or an older release of this
# tool) have installed here. The bridge and native routes leave no feeder
# add-on, so the manifest name is part of the check.
MARKER_FILES = ("dlss5-feed.addon64", "dlss5-feed.addon32",
                "dlss5-bridge.addon64", "dlss5-autopilot.json",
                "dlss5kur-kurulum.json", "dlss5-installer.json")


@dataclass
class Game:
    name: str
    folder: Path                 # the game's root folder
    exe: Path | None = None      # chosen executable
    bitness: int | None = None   # 32 / 64
    api: str = "?"
    api_why: str = ""
    source: str = "Manual"       # Steam / Epic / GOG / Emulator / Manual
    candidates: list[Path] = field(default_factory=list)
    error: str = ""
    emu: object | None = None    # emulators.Profile, when applicable
    install_root: Path | None = None   # folder an earlier install wrote to

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
        try:
            for acf in apps.glob("appmanifest_*.acf"):
                t = acf.read_text(encoding="utf8", errors="replace")
                nm = re.search(r'"name"\s*"([^"]+)"', t)
                d = re.search(r'"installdir"\s*"([^"]+)"', t)
                if nm and d:
                    names[d.group(1).lower()] = nm.group(1)
        except OSError:
            pass
        try:
            folders = [p for p in common.iterdir() if p.is_dir()]
        except OSError:
            continue
        for f in folders:
            rp = f.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            out.append(Game(name=names.get(f.name.lower(), f.name), folder=f, source="Steam"))
    return out


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
        if not root.is_dir():
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


def scan_xbox() -> list[Game]:
    r"""Xbox / Game Pass.

    Only ModifiableWindowsApps is readable and writable; the protected
    WindowsApps copy cannot be modified at all, so listing it would offer
    installs that can never work.
    """
    out: list[Game] = []
    roots = []
    for drive in "CDEFGH":
        roots.append(Path(f"{drive}:/Program Files/ModifiableWindowsApps"))
        roots.append(Path(f"{drive}:/XboxGames"))
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for f in root.iterdir():
                if not f.is_dir():
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
            if r.is_dir():
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
        if base.is_dir():
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
        if not base.is_dir():
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
    """A store's launch executable is often a stub that starts the real one.

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


def enrich(g: Game) -> Game:
    """Pick the executable and detect its architecture / graphics API."""
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
        _prefer_real_exe(g)
        adopt_previous_install(g)
        g.bitness = pe.exe_bitness(g.exe)
        g.api, g.api_why = pe.detect_api(g.exe)
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
    games: list[Game] = []
    for label, fn in (("Steam", scan_steam), ("Epic", scan_epic),
                      ("GOG", scan_gog), ("EA", scan_ea),
                      ("Ubisoft", scan_ubisoft), ("Battle.net", scan_battlenet),
                      ("Rockstar", scan_rockstar), ("Amazon", scan_amazon),
                      ("itch", scan_itch), ("Heroic", scan_heroic),
                      ("Xbox", scan_xbox), ("Folders", scan_folders),
                      ("Emulator", scan_emulators)):
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
    games = [g for g in games
             if not (g.source == "Folder" and g.folder.name.lower() in junk)]

    total = len(games)
    for i, g in enumerate(games, 1):
        if progress and (i % 5 == 0 or i == total):
            progress(f"Inspecting games... {i}/{total}")
        enrich(g)
    games.sort(key=lambda g: g.name.lower())
    return games


def manual(path: Path) -> Game:
    """Build a Game from a user-selected folder or executable."""
    path = Path(path)
    if path.is_file():
        g = Game(name=path.parent.name, folder=path.parent, exe=path, source="Manual")
    else:
        g = Game(name=path.name, folder=path, source="Manual")
    return enrich(g)
