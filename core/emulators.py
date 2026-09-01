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
from dataclasses import dataclass
from pathlib import Path


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
        if d and Path(d).is_dir():
            roots.append(Path(d))
    home = Path.home()
    roots += [home / "Desktop", home / "Downloads", home / "Documents"]
    # Common folders at drive roots
    for drive in "CDEFGH":
        base = Path(f"{drive}:/")
        if not base.is_dir():
            continue
        for name in ("Emulators", "Emulator", "Games", "Emu", "RetroArch",
                     "PS2", "PS1", "Emulation", "Roms", "Apps", "Programs",
                     "Oyunlar", "Tools", "Portable",
                     "EmuDeck", "Emulators_Portable", "LaunchBox"):
            d = base / name
            if d.is_dir():
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
            if r.is_dir() and r.resolve() not in seen:
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
