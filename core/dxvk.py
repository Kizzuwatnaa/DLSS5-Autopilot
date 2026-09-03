r"""DXVK - run a D3D10/D3D11 game on Vulkan so ReShade can stay out of it.

Some games watch their own process and quit, without a word, the moment a
ReShade proxy DLL hooks Direct3D. Metal Gear Solid V is the known case: with
ReShade as dxgi.dll or d3d11.dll the game creates its D3D11 device, never
gets as far as a swap chain, and exits cleanly about a second later. No crash,
no dialog, no log line. It does not matter which add-ons are loaded - a bare
ReShade does it too.

DXVK sidesteps that. It replaces dxgi.dll and d3d11.dll with a translation
layer that renders through Vulkan, and ReShade then reaches the game as a
Vulkan layer - a registry entry the Vulkan loader reads, nothing hooked inside
the game. The chain becomes:

    game (D3D11) -> dxgi.dll + d3d11.dll (DXVK, translates to Vulkan)
                 -> ReShade Vulkan layer -> add-ons, exactly as on a Vulkan game

The feeder's Vulkan transport then runs DLSS on a private D3D12 device and
shares the frame across the API boundary. Verified on MGS V (RTX 4060 Ti,
DXVK 3.1, DLSS5-Feeder 0.7.0): the DLAA feature builds and frames flow.

DXVK is MIT-licensed; releases are at github.com/doitsujin/dxvk.
"""
from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from . import net

API = "https://api.github.com/repos/doitsujin/dxvk/releases/latest"

# What DXVK drops beside the game, per API. DXVK translates D3D9 as well,
# and since 1.6.0 that is the ONLY DirectX 9 translation here - dgVoodoo2
# was dropped. Verified on Bayonetta (32-bit DX9): DLSS 5 built at
# 1920x1080 and delivered frames through the Vulkan transport.
FILES_BY_API = {
    "DX11": ("dxgi.dll", "d3d11.dll"),
    "DX9": ("d3d9.dll",),
}
FILES = FILES_BY_API["DX11"]
ALL_FILES = tuple(sorted({n for fs in FILES_BY_API.values() for n in fs}))

# Executables known to close themselves when ReShade hooks D3D11. MGS V's two
# executables share one build; Ground Zeroes is the same engine and the same
# reports (reshade.me forum, "MGS V TPP & GZ ReShade don't work").
NEEDS_DXVK = {
    "mgsvtpp.exe": "Metal Gear Solid V: The Phantom Pain",
    "mgsvmgo.exe": "Metal Gear Online (MGS V)",
    "mgsgzs.exe": "Metal Gear Solid V: Ground Zeroes",
}

APIS = tuple(FILES_BY_API)


def files_for(api: str) -> tuple[str, ...]:
    return FILES_BY_API.get(api, ())

BACKUP_SUFFIX = ".dlss5-autopilot-backup"


def wanted(exe: Path | None) -> str | None:
    """The game's name when it is one that needs DXVK, else None."""
    if exe is None:
        return None
    return NEEDS_DXVK.get(exe.name.lower())


def logs_for(exe: Path | None) -> tuple[str, ...]:
    """The log files DXVK writes beside the game while it runs."""
    if exe is None:
        return ()
    stem = exe.stem
    return (f"{stem}_dxgi.log", f"{stem}_d3d11.log", f"{stem}_d3d9.log")


def resolve() -> tuple[str, str]:
    """(version, download_url) of the latest DXVK release archive."""
    rel = net.json_get(API)
    for a in rel.get("assets", []):
        n = a["name"].lower()
        if n.startswith("dxvk-") and n.endswith(".tar.gz") and "native" not in n:
            return rel.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("Could not find a DXVK release archive.")


def is_dxvk(path: Path) -> bool:
    """A DXVK DLL carries its own name in its strings and is well over 1 MB."""
    try:
        if not path.is_file() or path.stat().st_size < (1 << 20):
            return False
        data = path.read_bytes()
        return b"DXVK" in data and b"ReShade" not in data
    except OSError:
        return False


def _extract(tgz: Path, member_suffix: str, dest: Path) -> None:
    with tarfile.open(tgz, "r:gz") as t:
        hit = next((m for m in t.getmembers()
                    if m.isfile() and m.name.lower().endswith(member_suffix.lower())),
                   None)
        if hit is None:
            raise RuntimeError(f"{tgz.name} does not contain {member_suffix}.")
        src = t.extractfile(hit)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)


def is_dxvk(path: Path) -> bool:
    """Is this file DXVK? Its DLLs carry the literal string "DXVK".

    Needed because "back up whatever is already there" must not fire on a
    DXVK we put there ourselves on an earlier install. It did: the backup
    then looked like the game's own file, uninstall dutifully restored it,
    and the game was left rendering through DXVK forever - with nothing on
    disk saying so. Seen on Bayonetta after a reinstall.
    """
    try:
        if not path.is_file() or path.stat().st_size < (1 << 20):
            return False
        return b"DXVK" in path.read_bytes()
    except OSError:
        return False


def install(exe_dir: Path, x64: bool, log=None, api: str = "DX11") -> tuple[str, list[str]]:
    """Put DXVK beside the game. Returns (version, files written).

    Anything already sitting under one of DXVK's names - an ENB, a game's own
    wrapper - is kept as a backup so uninstall puts it back. An earlier DXVK
    of ours is not: backing that up would hand it to uninstall as "the game's
    own file" and leave the game on DXVK after removal.
    """
    log = log or (lambda *_: None)
    ver, url = resolve()
    log(f"      DXVK {ver}")
    tgz = net.download(url, f"dxvk-{ver}.tar.gz")
    arch = "x64" if x64 else "x32"
    written: list[str] = []
    names = files_for(api) or FILES
    for name in names:
        dest = exe_dir / name
        bak = dest.with_name(name + BACKUP_SUFFIX)
        if dest.is_file() and not bak.exists() and not is_dxvk(dest):
            try:
                shutil.copy2(dest, bak)
                written.append(bak.name)
                log(f"      kept your existing {name} as {bak.name}")
            except OSError:
                log(f"      WARNING: could not back up the existing {name}")
        _extract(tgz, f"{arch}/{name}", dest)
        written.append(name)
    log(f"      {', '.join(names)} ({arch} build) - the game now renders "
        f"through Vulkan; ReShade loads as a Vulkan layer, nothing hooks the game")
    return ver, written
