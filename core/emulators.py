"""Emulator support.

Emulators do not appear in Steam/Epic/GOG libraries, so they are searched for
separately.

HOW IT WORKS
------------
An emulator is just another D3D11/D3D12/Vulkan/OpenGL application; once
ReShade is loaded, DLSS5-Feeder behaves exactly as it does in a normal game.
Direct3D 11/12 is the proven backend. Vulkan works through ReShade's layer
registration (64-bit emulators) and OpenGL through the feeder's interop; both
are newer and less proven, and the tool says so per game.

DEPTH BUFFER CAVEAT
-------------------
The feeder needs a depth buffer. Emulators often expose several and ReShade
may latch onto the wrong one. If the image looks broken, pick the correct
buffer manually from ReShade's DX11/DX12 tab.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
import pathlib
from pathlib import Path
from typing import Callable


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


@dataclass
class Profile:
    key: str
    name: str
    system: str
    exes: tuple[str, ...]
    # Can this emulator present through D3D11/D3D12?
    d3d: bool
    renderer_hint: str
    note: str = ""


PROFILES: tuple[Profile, ...] = (
    Profile("duckstation", "DuckStation", "PlayStation 1",
            ("duckstation-qt-x64.exe", "duckstation-nogui-x64.exe", "duckstation.exe"),
            True,
            "Settings -> Graphics -> Renderer = Direct3D 11 (or 12)",
            "Raise the internal resolution scale to 2x-4x so DLAA has detail to work with."),
    Profile("pcsx2", "PCSX2", "PlayStation 2",
            ("pcsx2-qt.exe", "pcsx2x64.exe", "pcsx2x64-avx2.exe", "pcsx2.exe"),
            True,
            "Settings -> Graphics -> Renderer = Direct3D 11 (or 12)",
            "An upscale multiplier of 2x or more is recommended."),
    Profile("dolphin", "Dolphin", "GameCube / Wii",
            ("Dolphin.exe", "DolphinQt.exe"),
            True,
            "Graphics -> Backend = Direct3D 11 (or 12)"),
    Profile("ppsspp", "PPSSPP", "PSP",
            ("PPSSPPWindows64.exe", "PPSSPPWindows.exe"),
            True,
            "Settings -> Graphics -> Backend = Direct3D 11"),
    Profile("xenia", "Xenia", "Xbox 360",
            ("xenia.exe", "xenia_canary.exe"),
            True,
            "D3D12 is the default backend"),
    Profile("cemu", "Cemu", "Wii U",
            ("Cemu.exe",),
            False,
            "Cemu offers Vulkan/OpenGL - set Vulkan (beta path)",
            "Vulkan goes through ReShade's layer registration; OpenGL "
            "installs as opengl32.dll and is the least reliable."),
    Profile("rpcs3", "RPCS3", "PlayStation 3",
            ("rpcs3.exe",),
            False,
            "RPCS3 offers Vulkan/OpenGL - set Vulkan (beta path)"),
    Profile("ryujinx", "Ryujinx", "Switch",
            ("Ryujinx.exe", "Ryujinx.Ava.exe", "Ryujinx.Headless.SDL2.exe"),
            False,
            "Vulkan/OpenGL only - set Vulkan (beta path)"),
    Profile("yuzu", "yuzu / suyu / Eden / Citron", "Switch",
            ("yuzu.exe", "suyu.exe", "eden.exe", "citron.exe", "sudachi.exe"),
            False,
            "Vulkan/OpenGL only - set Vulkan (beta path)"),
    Profile("shadps4", "shadPS4", "PlayStation 4",
            ("shadPS4.exe", "shadps4.exe"),
            False,
            "Vulkan only (beta path)"),
    Profile("azahar", "Azahar / Citra / Lime3DS", "3DS",
            ("azahar.exe", "citra.exe", "citra-qt.exe", "lime3ds.exe"),
            False,
            "Vulkan/OpenGL - set Vulkan (beta path)"),
    Profile("melonds", "melonDS", "DS",
            ("melonDS.exe",),
            False,
            "OpenGL only - the least reliable path"),
    Profile("flycast", "Flycast", "Dreamcast",
            ("flycast.exe",),
            True,
            "Set the renderer to DirectX 11"),
    Profile("xemu", "xemu", "Xbox",
            ("xemu.exe",),
            False,
            "Vulkan/OpenGL - set Vulkan (beta path)"),
    Profile("vita3k", "Vita3K", "PS Vita",
            ("Vita3K.exe",),
            False,
            "Vulkan/OpenGL - set Vulkan (beta path)"),
    Profile("retroarch", "RetroArch", "multi-system",
            ("retroarch.exe",),
            True,
            "Set the video driver to d3d11 or d3d12"),
    Profile("mgba", "mGBA", "Game Boy Advance",
            ("mGBA.exe",),
            False,
            "OpenGL only - the least reliable path"),
    Profile("snes9x", "Snes9x", "SNES",
            ("snes9x-x64.exe", "snes9x.exe"),
            True,
            "Set the output method to Direct3D"),
    Profile("play", "Play!", "PlayStation 2",
            ("Play.exe",),
            False,
            "Vulkan/OpenGL - set Vulkan (beta path)"),
)

_BY_EXE = {e.lower(): p for p in PROFILES for e in p.exes}


def profile_for(exe: Path) -> Profile | None:
    return _BY_EXE.get(exe.name.lower())


def _search_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ
    for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        d = env.get(v)
        if d and _isdir(d):
            roots.append(Path(d))
    home = Path.home()
    roots += [home / "Desktop", home / "Downloads", home / "Documents"]
    # Common folders at drive roots
    for drive in "CDEFGH":
        base = Path(f"{drive}:/")
        if not _isdir(base):
            continue
        for name in ("Emulators", "Emulator", "Games", "Emu", "RetroArch",
                     "PS2", "PS1", "Emulation", "Roms", "Apps", "Programs",
                     "Oyunlar", "Tools", "Portable",
                     "EmuDeck", "Emulators_Portable", "LaunchBox"):
            d = base / name
            if _isdir(d):
                roots.append(d)
        # People also unpack an emulator straight to D:\PCSX2. Searching the
        # drive root itself catches that; the walk below is depth-limited, so
        # this stays bounded rather than crawling the whole disk.
        if drive != "C":
            roots.append(base)

    # Several emulators ship on Steam (RetroArch, DuckStation, Dolphin), and
    # a Steam library is rarely in any of the folders above.
    try:
        from . import games as _games
        steam = _games._steam_root()
        if steam:
            for lib in _games._steam_libraries(steam):
                common = lib / "steamapps" / "common"
                if common.is_dir():
                    roots.append(common)
    except Exception:
        pass

    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        try:
            if _isdir(r) and r.resolve() not in seen:
                seen.add(r.resolve())
                out.append(r)
        except OSError:
            continue
    return out


def _registry_locations() -> list[Path]:
    """Emulator install paths from the Add/Remove Programs entries."""
    out: list[Path] = []
    try:
        import winreg
    except ImportError:
        return out
    names = {p.name.lower() for p in PROFILES}
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                      (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
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
                            dn = str(winreg.QueryValueEx(k, "DisplayName")[0]).lower()
                            if not any(n in dn for n in names):
                                continue
                            loc = winreg.QueryValueEx(k, "InstallLocation")[0]
                            if loc and Path(loc).is_dir():
                                out.append(Path(loc))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def scan(progress=None) -> list[tuple[Profile, Path]]:
    """(profile, exe) pairs for known emulator executables."""
    found: dict[Path, tuple[Profile, Path]] = {}

    def consider(f: Path) -> None:
        p = _BY_EXE.get(f.name.lower())
        if p:
            try:
                found.setdefault(f.resolve(), (p, f))
            except OSError:
                pass

    roots = _registry_locations() + _search_roots()
    for r in roots:
        if progress:
            progress(f"Looking for emulators: {r}")
        try:
            # Search the root and two levels below it
            for f in r.glob("*.exe"):
                consider(f)
            for sub in r.iterdir():
                if not sub.is_dir() or sub.name.startswith("."):
                    continue
                try:
                    for f in sub.glob("*.exe"):
                        consider(f)
                    for sub2 in sub.iterdir():
                        if sub2.is_dir() and not sub2.name.startswith("."):
                            try:
                                for f in sub2.glob("*.exe"):
                                    consider(f)
                            except OSError:
                                continue
                except OSError:
                    continue
        except OSError:
            continue
    return list(found.values())


# ------------------------------------------------------------------ backends
#
# Every renderer_hint above tells the person to open a settings dialog and
# pick Direct3D. Below the tool does that edit itself, straight in the
# emulator's config file, so the install is one click like it is for games.
#
# The key names, value spellings and config locations were checked against
# each emulator's source (settings.cpp / Pcsx2Config.cpp / MainSettings.cpp /
# Config.cpp / xenia_main.cc / retroarch.cfg) rather than guessed: a wrong
# spelling would silently be ignored, or worse, break the emulator's start.
#
# Files are edited as text, one line, never re-serialised through a parser:
# INI/TOML/cfg writers reorder keys and drop comments, and the person's other
# settings must come back byte for byte on uninstall.

BACKUP_SUFFIX = ".dlss5-autopilot-backup"

# Marker for a hand-written value we do not understand
_UNKNOWN = "unknown"
_BOM = "\ufeff"


@dataclass(frozen=True)
class _Backend:
    """How one emulator stores its render backend."""
    fmt: str                     # "ini" (section + key), "kv" (key = "value", no sections)
    section: str                 # "" for kv
    key: str
    target: str                  # the raw value we write
    target_name: str             # human name of that value
    names: dict[str, str]        # raw value -> human name, for status
    dxgi: tuple[str, ...]        # raw values that already present through DXGI
    locate: Callable[[Path], list[Path]]   # candidate config files, best first
    quoted: bool = False         # value written as "..." (TOML / RetroArch cfg)


def _documents() -> Path:
    # USERPROFILE\Documents is where every emulator here puts its user folder
    # when it is not portable; Path.home() follows the same variable.
    return Path(os.environ.get("USERPROFILE") or str(Path.home())) / "Documents"


def _env_dir(var: str) -> Path | None:
    v = os.environ.get(var)
    return Path(v) if v else None


def _loc_duckstation(exe: Path) -> list[Path]:
    # core.cpp: portable.txt or settings.ini next to the exe -> the exe folder;
    # else Documents\DuckStation if it exists; else LocalAppData\DuckStation.
    d = exe.parent
    out = []
    if (d / "portable.txt").is_file() or (d / "settings.ini").is_file():
        out.append(d / "settings.ini")
    out.append(_documents() / "DuckStation" / "settings.ini")
    la = _env_dir("LOCALAPPDATA")
    if la:
        out.append(la / "DuckStation" / "settings.ini")
    return out


def _loc_pcsx2(exe: Path) -> list[Path]:
    # Pcsx2Config.cpp: portable.ini or portable.txt next to the exe -> the
    # exe folder; else Documents\PCSX2. Settings live in "inis".
    d = exe.parent
    out = []
    if (d / "portable.ini").is_file() or (d / "portable.txt").is_file():
        out.append(d / "inis" / "PCSX2.ini")
    out.append(_documents() / "PCSX2" / "inis" / "PCSX2.ini")
    return out


def _loc_dolphin(exe: Path) -> list[Path]:
    # UICommon.cpp: portable.txt -> <exe>\User; else Documents\Dolphin
    # Emulator, then AppData\Roaming\Dolphin Emulator. Config\Dolphin.ini.
    d = exe.parent
    out = []
    if (d / "portable.txt").is_file():
        out.append(d / "User" / "Config" / "Dolphin.ini")
    out.append(_documents() / "Dolphin Emulator" / "Config" / "Dolphin.ini")
    ad = _env_dir("APPDATA")
    if ad:
        out.append(ad / "Dolphin Emulator" / "Config" / "Dolphin.ini")
    return out


def _loc_ppsspp(exe: Path) -> list[Path]:
    # Windows/main.cpp: installed.txt next to the exe means the memstick is
    # the path written in it, or Documents\PPSSPP when empty; otherwise the
    # memstick folder sits next to the exe. ppsspp.ini is in PSP\SYSTEM.
    d = exe.parent
    roots: list[Path] = []
    inst = d / "installed.txt"
    if inst.is_file():
        try:
            text = inst.read_text(encoding="utf8", errors="replace").strip().lstrip(_BOM)
        except OSError:
            text = ""
        if text and Path(text).is_dir():
            roots.append(Path(text))
        roots.append(_documents() / "PPSSPP")
        roots.append(d / "memstick")
    else:
        roots.append(d / "memstick")
        roots.append(_documents() / "PPSSPP")
    return [r / "PSP" / "SYSTEM" / "ppsspp.ini" for r in roots]


def _loc_xenia(exe: Path) -> list[Path]:
    # xenia_main.cc: portable.txt next to the exe -> the exe folder, else
    # Documents\Xenia. Canary names its file xenia-canary.config.toml.
    names = ("xenia-canary.config.toml", "xenia.config.toml")
    if "canary" not in exe.name.lower():
        names = names[::-1]
    d = exe.parent
    dirs = []
    if (d / "portable.txt").is_file():
        dirs.append(d)
    dirs.append(_documents() / "Xenia")
    return [dd / n for dd in dirs for n in names]


def _loc_retroarch(exe: Path) -> list[Path]:
    # retroarch.cfg next to the exe (the Windows zip), else %APPDATA%\RetroArch.
    out = [exe.parent / "retroarch.cfg"]
    ad = _env_dir("APPDATA")
    if ad:
        out.append(ad / "RetroArch" / "retroarch.cfg")
    return out


def _loc_rpcs3(exe: Path) -> list[Path]:
    # RPCS3 on Windows keeps config.yml next to the exe.
    return [exe.parent / "config.yml"]


def _loc_cemu(exe: Path) -> list[Path]:
    out = [exe.parent / "settings.xml"]
    ad = _env_dir("APPDATA")
    if ad:
        out.append(ad / "Cemu" / "settings.xml")
    return out


_PCSX2_NAMES = {"-1": "Automatic", "3": "D3D11", "15": "D3D12", "12": "OpenGL",
                "14": "Vulkan", "13": "Software", "11": "Null"}
_PPSSPP_NAMES = {"0": "OpenGL", "2": "D3D11", "3": "Vulkan"}

_BACKENDS: dict[str, _Backend] = {
    # settings.cpp: [GPU] Renderer = Automatic|D3D11|D3D12|Vulkan|OpenGL|Software
    "duckstation": _Backend("ini", "GPU", "Renderer", "D3D12", "D3D12",
                            {"D3D11": "D3D11", "D3D12": "D3D12", "Vulkan": "Vulkan",
                             "OpenGL": "OpenGL", "Automatic": "Automatic", "Software": "Software"},
                            ("D3D11", "D3D12"), _loc_duckstation),
    # Config.h GSRendererType: DX11 = 3, DX12 = 15, OGL = 12, VK = 14, Auto = -1
    "pcsx2": _Backend("ini", "EmuCore/GS", "Renderer", "15", "D3D12", _PCSX2_NAMES,
                      ("3", "15"), _loc_pcsx2),
    # MainSettings.cpp: [Core] GFXBackend; CONFIG_NAME "D3D" (11) / "D3D12"
    "dolphin": _Backend("ini", "Core", "GFXBackend", "D3D12", "D3D12",
                        {"D3D": "D3D11", "D3D12": "D3D12", "Vulkan": "Vulkan",
                         "OGL": "OpenGL", "Software Renderer": "Software", "Null": "Null"},
                        ("D3D", "D3D12"), _loc_dolphin),
    # Config.cpp: [Graphics] GraphicsBackend = "<n> (<NAME>)"; DIRECT3D11 = 2
    "ppsspp": _Backend("ini", "Graphics", "GraphicsBackend", "2 (DIRECT3D11)", "D3D11",
                       _PPSSPP_NAMES, ("2", "2 (DIRECT3D11)", "DIRECT3D11"), _loc_ppsspp),
    # xenia_main.cc: DEFINE_string(gpu, "any", ..., "GPU") -> [GPU] gpu = "d3d12"
    "xenia": _Backend("ini", "GPU", "gpu", "d3d12", "D3D12",
                      {"d3d12": "D3D12", "vulkan": "Vulkan", "any": "Automatic", "null": "Null"},
                      ("d3d12",), _loc_xenia, quoted=True),
    # retroarch.cfg: video_driver = "d3d11". d3d11 rather than d3d12: the
    # d3d12 driver still lacks features some cores rely on.
    "retroarch": _Backend("kv", "", "video_driver", "d3d11", "D3D11",
                          {"d3d11": "D3D11", "d3d12": "D3D12", "gl": "OpenGL", "glcore": "OpenGL",
                           "vulkan": "Vulkan", "d3d9": "D3D9", "sdl2": "SDL", "gdi": "GDI"},
                          ("d3d11", "d3d12"), _loc_retroarch, quoted=True),
}

# Emulators whose backend we only read: they have no DXGI path at all.
# rpcs3: system_config_types.h video_renderer = null|opengl|vulkan.
# cemu: CemuConfig.h GraphicAPI kOpenGL = 0, kVulkan = 1, stored as <Graphic><api>.
_READ_ONLY: dict[str, tuple[str, Callable[[Path], list[Path]], dict[str, str]]] = {
    "rpcs3": (r"^\s*Renderer:\s*(\S+)", _loc_rpcs3,
              {"vulkan": "Vulkan", "opengl": "OpenGL", "null": "Null"}),
    "cemu": (r"<api>\s*(\d+)\s*</api>", _loc_cemu, {"0": "OpenGL", "1": "Vulkan", "2": "Metal"}),
}


def _first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _read(path: Path) -> str:
    # surrogateescape keeps any non-UTF-8 byte alive so the write-back is
    # byte-exact everywhere but the one line we change.
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def _is_header(line: str, section: str) -> bool:
    s = line.lstrip(_BOM).strip()
    return s.startswith("[") and s.endswith("]") and s[1:-1].strip().lower() == section.lower()


def _key_re(key: str) -> re.Pattern[str]:
    # value group stops before a trailing comment or the line ending
    return re.compile(r"^(\s*" + re.escape(key) + r"\s*=\s*)"
                      r"(\"[^\"\r\n]*\"|[^\r\n#;]*?)(\s*(?:[#;][^\r\n]*)?)(\r?\n?)$",
                      re.IGNORECASE)


def _find_line(lines: list[str], spec: _Backend) -> int | None:
    """Index of the line holding the key, inside its section for INI files."""
    rx = _key_re(spec.key)
    inside = spec.fmt == "kv"
    for i, line in enumerate(lines):
        stripped = line.lstrip(_BOM).strip()
        if spec.fmt == "ini" and stripped.startswith("["):
            inside = _is_header(line, spec.section)
            continue
        if inside and rx.match(line):
            return i
    return None


def _raw_value(lines: list[str], spec: _Backend) -> str | None:
    i = _find_line(lines, spec)
    if i is None:
        return None
    m = _key_re(spec.key).match(lines[i])
    v = m.group(2).strip() if m else ""
    return v.strip('"')


def _set_value(lines: list[str], spec: _Backend, value: str) -> list[str]:
    """Return new lines with the key set; the rest untouched."""
    nl = "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"
    written = f'"{value}"' if spec.quoted else value
    i = _find_line(lines, spec)
    out = list(lines)
    if i is not None:
        m = _key_re(spec.key).match(out[i])
        assert m
        out[i] = m.group(1) + written + m.group(3) + m.group(4)
        return out
    new_line = f"{spec.key} = {written}{nl}"
    if spec.fmt == "kv":
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += nl
        out.append(new_line)
        return out
    for j, line in enumerate(out):
        if _is_header(line, spec.section):
            if not out[j].endswith(("\n", "\r")):
                out[j] += nl
            out.insert(j + 1, new_line)
            return out
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += nl
    out += [f"[{spec.section}]{nl}", new_line]
    return out


def _human(spec: _Backend, raw: str | None) -> str:
    if raw is None:
        return _UNKNOWN
    if spec.key == "GraphicsBackend":
        # PPSSPP writes "3 (VULKAN)"; the number is what it reads back.
        raw = raw.split(" ", 1)[0]
    return spec.names.get(raw, spec.names.get(raw.lower(), raw or _UNKNOWN))


def backend_status(profile: Profile, exe: Path) -> tuple[str, Path | None]:
    """Current render backend name (or "unknown") and the config file it came from."""
    spec = _BACKENDS.get(profile.key)
    if spec:
        cfg = _first_existing(spec.locate(exe))
        if not cfg:
            return _UNKNOWN, None
        try:
            lines = _read(cfg).splitlines(keepends=True)
        except OSError:
            return _UNKNOWN, cfg
        return _human(spec, _raw_value(lines, spec)), cfg
    ro = _READ_ONLY.get(profile.key)
    if ro:
        rx, locate, names = ro
        cfg = _first_existing(locate(exe))
        if not cfg:
            return _UNKNOWN, None
        try:
            m = re.search(rx, _read(cfg), re.MULTILINE | re.IGNORECASE)
        except OSError:
            return _UNKNOWN, cfg
        if not m:
            return _UNKNOWN, cfg
        v = m.group(1)
        return names.get(v.lower(), v), cfg
    return _UNKNOWN, None


def set_backend(profile: Profile, exe: Path) -> list[str]:
    """Switch the emulator to its best DXGI backend and say what changed.

    Safe to call again: a second run finds the DXGI value and does nothing.
    The first real edit backs the config file up next to itself so
    restore_backend() can put it back byte for byte.
    """
    spec = _BACKENDS.get(profile.key)
    if not spec:
        if not profile.d3d:
            return [f"{profile.name}: no DXGI backend, leave Vulkan/OpenGL "
                    f"({profile.renderer_hint})"]
        return [f"{profile.name}: set the backend by hand: {profile.renderer_hint}"]
    cfg = _first_existing(spec.locate(exe))
    if not cfg:
        return [f"{profile.name}: config file not found (run the emulator once first). "
                f"Set the backend by hand: {profile.renderer_hint}"]
    try:
        text = _read(cfg)
    except OSError as e:
        return [f"{profile.name}: could not read {cfg} ({e}). "
                f"Set the backend by hand: {profile.renderer_hint}"]
    lines = text.splitlines(keepends=True)
    raw = _raw_value(lines, spec)
    if raw is not None and (raw in spec.dxgi or raw.lower() in spec.dxgi):
        return [f"{profile.name}: backend already {_human(spec, raw)} in {cfg}"]
    backup = cfg.with_name(cfg.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(cfg, backup)
    _write(cfg, "".join(_set_value(lines, spec, spec.target)))
    old = _human(spec, raw) if raw is not None else "(not set)"
    where = f"[{spec.section}] {spec.key}" if spec.section else spec.key
    return [f"{profile.name}: {cfg}: {where}: {old} -> {spec.target_name}",
            f"backup: {backup}"]


def restore_backend(profile: Profile, exe: Path) -> list[str]:
    """Undo set_backend(): the backed-up config comes back, the backup goes."""
    spec = _BACKENDS.get(profile.key)
    if not spec:
        return []
    notes: list[str] = []
    for cfg in spec.locate(exe):
        backup = cfg.with_name(cfg.name + BACKUP_SUFFIX)
        try:
            if not backup.is_file():
                continue
            shutil.copy2(backup, cfg)
            backup.unlink()
            notes.append(f"{profile.name}: restored {cfg} from its backup")
        except OSError as e:
            notes.append(f"{profile.name}: could not restore {cfg} ({e})")
    if not notes:
        notes.append(f"{profile.name}: no backend backup to restore")
    return notes
