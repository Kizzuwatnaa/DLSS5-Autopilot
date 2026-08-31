"""NVIDIA ekran karti tespiti ve nvngx_dlssnr.dll uyumluluk denetimi.

NEDEN GEREKLI
-------------
Sizdirilan DLSS 5 neural rendering kutuphanesi icindeki CUDA kodu belirli GPU
mimarileri icin derlenmis. Kartinla uyusmayan bir surum secersen DLSS hic
calismaz. rhi-repo'daki surumleri tarayarak olctugumuz gercek durum:

    310.8.0         -> yalnizca RTX 50
    310.8.0-RTX40   -> RTX 40 + 50
    310.8.SF        -> RTX 20 + 30 + 40 + 50
    310.8.SF-v2     -> RTX 20 + 30 + 40 + 50

Bu tabloyu sabit yazmiyoruz: indirilen dosyanin icindeki fatbin kayitlarini
okuyup hangi mimarilere kod icerdigini dogrudan tespit ediyoruz. Boylece yeni
bir surum ciktiginda da dogru calisir.
"""
from __future__ import annotations

import collections
import re
import struct
from pathlib import Path

# CUDA "compute capability" -> insan tarafindan okunur kart ailesi
SM_NAMES = {
    75:  "RTX 20 / GTX 16 (Turing)",
    80:  "A100 (Ampere DC)",
    86:  "RTX 30 (Ampere)",
    87:  "Orin",
    89:  "RTX 40 (Ada Lovelace)",
    90:  "H100 (Hopper)",
    100: "Blackwell (veri merkezi)",
    120: "RTX 50 (Blackwell)",
    121: "Blackwell",
}
KNOWN_SM = set(SM_NAMES) | {50, 52, 53, 60, 61, 62, 70, 72, 101}

_FATBIN_MAGIC = struct.pack("<I", 0xBA55ED50)


# ------------------------------------------------------------------ kart tespiti

def _adapters() -> list[str]:
    """Kayit defterindeki ekran karti adlari (harici bagimlilik yok)."""
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
    """Kart adindan CUDA mimari numarasi cikar."""
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
            return 61          # Pascal - DLSS 5 zaten calismaz
    return None


def detect() -> tuple[str | None, int | None]:
    """(kart_adi, sm) - NVIDIA karti yoksa (None, None)."""
    best: tuple[str | None, int | None] = (None, None)
    for name in _adapters():
        sm = sm_for_name(name)
        if sm is not None:
            # En yeni mimariyi tercih et (dizustunde iGPU + dGPU birlikte olur)
            if best[1] is None or sm > best[1]:
                best = (name, sm)
    return best


def label(sm: int | None) -> str:
    return SM_NAMES.get(sm, "bilinmiyor") if sm is not None else "bilinmiyor"


# ------------------------------------------------------- dosya mimari taramasi

def dll_architectures(path: Path) -> set[int]:
    """DLL icindeki CUDA fatbin kayitlarindan desteklenen sm surumlerini cikarir.

    Cubin'ler sikistirilmis oldugu icin ELF basliklarini degil, fatbin kayit
    basliklarindaki sm alanini okuyoruz.
    """
    try:
        d = path.read_bytes()
    except OSError:
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
    return set(found)


def check(path: Path, sm: int | None) -> tuple[bool | None, str]:
    """(uyumlu_mu, aciklama). Bilinemiyorsa (None, ...)."""
    archs = dll_architectures(path)
    if not archs:
        return None, "dosyadaki mimariler okunamadi"
    listed = ", ".join(SM_NAMES.get(a, f"sm_{a}") for a in sorted(archs))
    if sm is None:
        return None, f"kart tespit edilemedi; dosya destegi: {listed}"
    if sm in archs:
        return True, f"kartinla uyumlu (dosya destegi: {listed})"
    return False, (f"BU DOSYA KARTINLA CALISMAZ. Icinde {label(sm)} icin kod yok. "
                   f"Dosya destegi: {listed}")
