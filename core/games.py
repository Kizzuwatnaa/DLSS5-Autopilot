"""Kurulu oyunlari bulur: Steam, Epic, GOG + elle secilen klasorler.

Hicbir sey calistirilmaz; sadece kutuphane dosyalari okunur ve exe basliklarina
bakilir. Tarama tamamen yereldir, internete cikilmaz.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import emulators, pe

# Bir oyun klasorunde bunlar varsa kurulum zaten yapilmis demektir.
MARKER_FILES = ("dlss5-feed.addon64", "dlss5-feed.addon32")


@dataclass
class Game:
    name: str
    folder: Path                 # oyunun kok klasoru
    exe: Path | None = None      # secilen calistirilabilir
    bitness: int | None = None   # 32 / 64
    api: str = "?"
    api_why: str = ""
    source: str = "Elle"         # Steam / Epic / GOG / Elle
    candidates: list[Path] = field(default_factory=list)
    error: str = ""
    emu: object | None = None      # emulators.Profile (varsa)

    @property
    def install_dir(self) -> Path:
        """Dosyalarin gidecegi yer: exenin yani.

        Cok sayida oyunda exe kok klasorde degildir (ornek: Kingdom Come 2 ->
        Bin\\Win64MasterMasterSteamPGO\\KingdomCome.exe). ReShade proxy'si
        exenin yaninda olmak zorunda, yoksa hic yuklenmez.
        """
        return self.exe.parent if self.exe else self.folder

    @property
    def installed(self) -> bool:
        return any((self.install_dir / m).is_file() for m in MARKER_FILES)

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
    # "path"    "D:\\SteamLibrary"
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
        # appmanifest dosyalari gercek oyun adini verir
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


# ---------------------------------------------------------------- emulatorler

def scan_emulators(progress=None) -> list[Game]:
    out: list[Game] = []
    for prof, exe in emulators.scan(progress):
        g = Game(name=f"{prof.name} ({prof.system})", folder=exe.parent,
                 exe=exe, source="Emülatör")
        g.emu = prof
        out.append(g)
    return out


# ---------------------------------------------------------------- ortak

def enrich(g: Game) -> Game:
    """Oyunun exesini sec ve mimarisini/API'sini tespit et."""
    try:
        if g.exe is None or not g.exe.is_file():
            cands = pe.find_game_exes(g.folder)
            if not cands:
                g.error = "exe bulunamadi"
                return g
            g.candidates = cands
            g.exe = cands[0]
        elif not g.candidates:
            g.candidates = pe.find_game_exes(g.folder) or [g.exe]
        g.bitness = pe.exe_bitness(g.exe)
        g.api, g.api_why = pe.detect_api(g.exe)
        if g.emu is None:
            prof = emulators.profile_for(g.exe)
            if prof:
                g.emu = prof
                if g.source == "Elle":
                    g.name = f"{prof.name} ({prof.system})"
    except pe.PEError as e:
        g.error = str(e)
    except Exception as e:
        g.error = f"okunamadi: {e}"
    return g


def scan_all(progress=None) -> list[Game]:
    """Tum kaynaklari tara, exeleri coz, ada gore sirala."""
    games: list[Game] = []
    for label, fn in (("Steam", scan_steam), ("Epic", scan_epic),
                      ("GOG", scan_gog), ("Emülatör", scan_emulators)):
        if progress:
            progress(f"{label} taranıyor...")
        try:
            games += fn(progress) if label == "Emülatör" else fn()
        except Exception:
            pass          # bir magaza okunamazsa digerleri devam etsin

    # Ayni klasoru iki magaza da bildirmis olabilir
    uniq: dict[Path, Game] = {}
    for g in games:
        try:
            uniq.setdefault(g.folder.resolve(), g)
        except OSError:
            continue
    games = list(uniq.values())

    total = len(games)
    for i, g in enumerate(games, 1):
        if progress and (i % 5 == 0 or i == total):
            progress(f"Oyunlar inceleniyor... {i}/{total}")
        enrich(g)
    games.sort(key=lambda g: g.name.lower())
    return games


def manual(path: Path) -> Game:
    """Kullanicinin elle sectigi klasor/exe icin Game olustur."""
    path = Path(path)
    if path.is_file():
        g = Game(name=path.parent.name, folder=path.parent, exe=path, source="Elle")
    else:
        g = Game(name=path.name, folder=path, source="Elle")
    return enrich(g)
