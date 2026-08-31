"""NVIDIA GPU detection and nvngx_dlssnr.dll compatibility checking.

WHY THIS EXISTS
---------------
The CUDA code inside the leaked DLSS 5 neural rendering library is compiled
for specific GPU architectures. Pick a build that does not match your card and
DLSS simply never starts. Measured by parsing the rhi-repo releases:

    310.8.0         -> RTX 50 only
    310.8.0-RTX40   -> RTX 40 + 50
    310.8.SF        -> RTX 20 + 30 + 40 + 50
    310.8.SF-v2     -> RTX 20 + 30 + 40 + 50

This table is NOT hard-coded: we read the fatbin records inside the downloaded
file and detect which architectures it actually carries code for, so new
releases work correctly too.
"""
from __future__ import annotations

import collections
import re
import struct
from pathlib import Path

# CUDA compute capability -> human-readable card family
SM_NAMES = {
    75:  "RTX 20 / GTX 16 (Turing)",
    80:  "A100 (Ampere DC)",
    86:  "RTX 30 (Ampere)",
    87:  "Orin",
    89:  "RTX 40 (Ada Lovelace)",
    90:  "H100 (Hopper)",
    100: "Blackwell (data centre)",
    120: "RTX 50 (Blackwell)",
    121: "Blackwell",
}
KNOWN_SM = set(SM_NAMES) | {50, 52, 53, 60, 61, 62, 70, 72, 101}

_FATBIN_MAGIC = struct.pack("<I", 0xBA55ED50)


# ------------------------------------------------------------ card detection

def _adapters() -> list[str]:
    """Display adapter names from the registry (no third-party dependency)."""
    names: list[str] = []
    try:
        import winreg
        key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        names.append(str(winreg.QueryValueEx(k, "DriverDesc")[0]))
                except OSError:
                    continue
    except Exception:
        pass
    return names


def sm_for_name(name: str) -> int | None:
    """Derive the CUDA architecture number from a card name."""
    n = name.upper()
    if "NVIDIA" not in n and "GEFORCE" not in n and "RTX" not in n and "QUADRO" not in n:
        return None
    m = re.search(r"(?:RTX|GTX)\s*(\d{3,4})", n)
    if m:
        num = int(m.group(1))
        if 5000 <= num <= 5999:
            return 120
        if 4000 <= num <= 4999:
            return 89
        if 3000 <= num <= 3999:
            return 86
        if 2000 <= num <= 2999 or 1600 <= num <= 1699:
            return 75
        if 1000 <= num <= 1099:
            return 61          # Pascal - DLSS 5 will not run anyway
    return None


def detect() -> tuple[str | None, int | None]:
    """(card_name, sm) - (None, None) when there is no NVIDIA card."""
    best: tuple[str | None, int | None] = (None, None)
    for name in _adapters():
        sm = sm_for_name(name)
        if sm is not None:
            # Prefer the newest architecture (laptops list iGPU + dGPU)
            if best[1] is None or sm > best[1]:
                best = (name, sm)
    return best


def label(sm: int | None) -> str:
    return SM_NAMES.get(sm, "unknown") if sm is not None else "unknown"


# ------------------------------------------------- file architecture scanning

def dll_architectures(path: Path) -> set[int]:
    """Supported sm versions, from the CUDA fatbin records inside the DLL.

    The cubins are compressed, so we read the sm field in the fatbin entry
    headers rather than ELF headers.
    """
    try:
        # Memory-map instead of loading 165 MB at once; the OS only pages in
        # what we actually touch.
        import mmap
        fh = open(path, "rb")
    except OSError:
        return set()
    try:
        d = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError):
        try:
            d = fh.read()
        except OSError:
            fh.close()
            return set()
    found: collections.Counter[int] = collections.Counter()
    off = 0
    while True:
        i = d.find(_FATBIN_MAGIC, off)
        if i < 0:
            break
        off = i + 4
        try:
            hsize = struct.unpack_from("<H", d, i + 6)[0]
            fatsize = struct.unpack_from("<Q", d, i + 8)[0]
            if hsize < 16 or not (0 < fatsize <= len(d)):
                continue
            p, end = i + hsize, i + hsize + fatsize
            while p < end - 32:
                ehdr = struct.unpack_from("<I", d, p + 4)[0]
                payload = struct.unpack_from("<Q", d, p + 8)[0]
                if ehdr < 24 or ehdr > 4096 or not (0 < payload <= len(d)):
                    break
                for so in (24, 28, 20):
                    if p + so + 4 > len(d):
                        continue
                    sm = struct.unpack_from("<I", d, p + so)[0]
                    if sm in KNOWN_SM:
                        found[sm] += 1
                        break
                p += ehdr + payload
        except Exception:
            continue
    try:
        if hasattr(d, "close"):
            d.close()
        fh.close()
    except Exception:
        pass
    return set(found)


def check(path: Path, sm: int | None) -> tuple[bool | None, str]:
    """(compatible, explanation). (None, ...) when it cannot be determined."""
    archs = dll_architectures(path)
    if not archs:
        return None, "could not read architectures from the file"
    listed = ", ".join(SM_NAMES.get(a, f"sm_{a}") for a in sorted(archs))
    if sm is None:
        return None, f"card not detected; file supports: {listed}"
    if sm in archs:
        return True, f"compatible with your card (file supports: {listed})"
    return False, (f"THIS BUILD WILL NOT RUN ON YOUR CARD. It contains no code for "
                   f"{label(sm)}. File supports: {listed}")
