"""Emulator destegi.

Emulatorler Steam/Epic/GOG kutuphanelerinde gorunmez, o yuzden ayrica araniyor.

CALISMA MANTIGI
---------------
Emulator de sonucta bir D3D11/D3D12 uygulamasi; ReShade dxgi.dll olarak
yuklendiginde DLSS5-Feeder normal bir oyunda oldugu gibi calisir. Sart:
emulatorun render arka ucu Direct3D 11 ya da 12 olmali. Vulkan/OpenGL
secilirse dxgi.dll hic devreye girmez.

DERINLIK TAMPONU UYARISI
------------------------
Feeder'in derinlik tamponuna ihtiyaci var. Emulatorlerde ReShade cogu zaman
birden fazla derinlik tamponu gorur ve yanlisini secebilir. Calismazsa
ReShade panelindeki DX11/DX12 sekmesinden dogru tamponu elle secmek gerekir.
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
    # Bu emulator D3D11/D3D12 sunabiliyor mu?
    d3d: bool
    renderer_hint: str
    note: str = ""


PROFILES: tuple[Profile, ...] = (
    Profile("duckstation", "DuckStation", "PlayStation 1",
            ("duckstation-qt-x64.exe", "duckstation-nogui-x64.exe", "duckstation.exe"),
            True,
            "Ayarlar -> Grafikler -> Renderer = Direct3D 11 (veya 12)",
            "İç çözünürlüğü (resolution scale) 2x-4x yap ki DLAA'nın işleyecek "
            "detayı olsun."),
    Profile("pcsx2", "PCSX2", "PlayStation 2",
            ("pcsx2-qt.exe", "pcsx2x64.exe", "pcsx2x64-avx2.exe", "pcsx2.exe"),
            True,
            "Settings -> Graphics -> Renderer = Direct3D 11 (veya 12)",
            "Upscale multiplier 2x+ önerilir."),
    Profile("dolphin", "Dolphin", "GameCube / Wii",
            ("Dolphin.exe", "DolphinQt.exe"),
            True,
            "Graphics -> Backend = Direct3D 11 (veya 12)"),
    Profile("ppsspp", "PPSSPP", "PSP",
            ("PPSSPPWindows64.exe", "PPSSPPWindows.exe"),
            True,
            "Ayarlar -> Grafikler -> Backend = Direct3D 11"),
    Profile("xenia", "Xenia", "Xbox 360",
            ("xenia.exe", "xenia_canary.exe"),
            True,
            "D3D12 varsayılan arka uç"),
    Profile("cemu", "Cemu", "Wii U",
            ("Cemu.exe",),
            False,
            "Cemu yalnızca Vulkan/OpenGL sunar",
            "OpenGL seçilirse ReShade opengl32.dll olarak kurulur; "
            "Vulkan'da otomatik kurulum yok."),
    Profile("rpcs3", "RPCS3", "PlayStation 3",
            ("rpcs3.exe",),
            False,
            "RPCS3 yalnızca Vulkan/OpenGL sunar",
            "D3D arka ucu olmadığı için bu araçla otomatik kurulum yapılamıyor."),
    Profile("ryujinx", "Ryujinx", "Switch",
            ("Ryujinx.exe", "Ryujinx.Ava.exe"),
            False,
            "Yalnızca Vulkan/OpenGL"),
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
    roots += [home / "Desktop", home / "Downloads", home / "Masaüstü",
              home / "İndirilenler"]
    # Surucu koklerindeki yaygin klasorler
    for drive in "CDEFG":
        base = Path(f"{drive}:/")
        if not base.is_dir():
            continue
        for name in ("Emulators", "Emulator", "Emülatör", "Games", "Oyunlar",
                     "Emu", "RetroArch", "PS2", "PS1"):
            d = base / name
            if d.is_dir():
                roots.append(d)
    return [r for r in roots if r.is_dir()]


def _registry_locations() -> list[Path]:
    """Kaldir/Degistir kayitlarindan emulator kurulum yollari."""
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
    """(profil, exe) ciftleri. Bilinen emulator exelerini arar."""
    found: dict[Path, tuple[Profile, Path]] = {}
    wanted = set(_BY_EXE)

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
            progress(f"Emülatör aranıyor: {r}")
        try:
            # kokte ve iki alt seviyede ara (emulatorler genelde sig bir klasorde)
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
