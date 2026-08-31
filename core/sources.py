"""Bilesenlerin nereden indirildigi - hepsi tek yerde, denetlenebilsin diye.

Bu araç HICBIR ozel sunucuya baglanmaz. Asagidaki alan adlari disina cikmaz:
    reshade.me
    raw.githubusercontent.com   (crosire/reshade-shaders)
    api.github.com / github.com (DLSS5-Feeder, rhi-repo)
    codeload.github.com         (LumeniteFX)
"""
from __future__ import annotations

import json
import re
import urllib.request

UA = {"User-Agent": "dlss5kur/1.0 (+local install helper)"}

RESHADE_HOME = "https://reshade.me"
RESHADE_SETUP_RE = re.compile(r"/downloads/ReShade_Setup_([\d.]+)_Addon\.exe")

RESHADE_HEADERS_BASE = "https://raw.githubusercontent.com/crosire/reshade-shaders/slim/Shaders/"
RESHADE_HEADERS = ("ReShade.fxh", "ReShadeUI.fxh", "DrawText.fxh")

FEEDER_API = "https://api.github.com/repos/jlrouzies-fr/DLSS5-Feeder/releases/latest"
LUMENITE_ZIP = "https://codeload.github.com/umar-afzaal/LumeniteFX/zip/refs/heads/mainline"
RHI_API = "https://api.github.com/repos/RankFTW/rhi-repo/releases?per_page=100"

# DLSS5-Feeder README'si renodx-dlss5 icin acikca v4.55 diyor; varsayilan bu.
# Aynadan indirilecekse en guncel yapiyi al. (README 4.55 diyor ama o yapi
# yeni nvngx_dlssnr surumleriyle CreateFeature sirasinda cokuyor.)
RENODX_DEFAULT = None


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _json(url: str):
    return json.loads(_get(url).decode("utf8"))


def resolve_reshade() -> tuple[str, str]:
    """reshade.me ana sayfasindan en guncel Addon kurulumunun (surum, url) bilgisi."""
    html = _get(RESHADE_HOME).decode("utf8", "replace")
    m = RESHADE_SETUP_RE.search(html)
    if not m:
        raise RuntimeError("reshade.me uzerinde Addon kurulum baglantisi bulunamadi.")
    return m.group(1), RESHADE_HOME + m.group(0)


def resolve_feeder() -> tuple[str, dict[str, str]]:
    """DLSS5-Feeder son surumu: (etiket, {dosya_adi: indirme_url})."""
    rel = _json(FEEDER_API)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    return rel.get("tag_name", "?"), assets


def _ver_key(tag: str, prefix: str) -> tuple:
    """'dlss-310.8.0' -> (310,8,0) seklinde siralanabilir anahtar."""
    raw = tag[len(prefix):].lstrip("-")
    nums = re.findall(r"\d+", raw)
    return tuple(int(n) for n in nums) if nums else (0,)


def rhi_catalog() -> dict[str, list[dict]]:
    """rhi-repo surumlerini bilesen ailesine gore gruplar (yeni -> eski)."""
    rels = _json(RHI_API)
    fams: dict[str, list[dict]] = {}
    for r in rels:
        tag = r.get("tag_name", "")
        for prefix, fam in (("renodx-dlss5", "renodx"),
                            ("dlssnr", "dlssnr"),
                            ("dlss-", "dlss")):
            if not tag.startswith(prefix):
                continue
            for a in r.get("assets", []):
                if not a["name"].endswith(".zip"):
                    continue
                fams.setdefault(fam, []).append({
                    "tag": tag,
                    "label": tag[len(prefix):].lstrip("-") or tag,
                    "url": a["browser_download_url"],
                    "size": a.get("size", 0),
                    "key": _ver_key(tag, prefix.rstrip("-")),
                })
            break
    for fam in fams.values():
        fam.sort(key=lambda d: d["key"], reverse=True)
    return fams


def pick(entries: list[dict], want: str | None) -> dict:
    """Etiketi/etiket parcasi 'want' olan girdiyi sec; yoksa en guncelini dondur."""
    if want:
        for e in entries:
            if e["label"] == want or e["tag"] == want:
                return e
    return entries[0]
