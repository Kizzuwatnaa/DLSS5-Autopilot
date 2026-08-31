"""DirectX 9 destegi - dgVoodoo2 ile DX9 -> D3D11 cevirisi.

DLSS5-Feeder DX9'u dogrudan besleyemiyor. Cozum zinciri:

    oyun (DX9) -> D3D9.dll (dgVoodoo2, D3D11'e cevirir)
               -> dxgi.dll (ReShade)
               -> dlss5-feed.addon32  ->  host64\\ (64-bit DLSS)

Yani dgVoodoo2 kurulduktan sonrasi 32-bit yolla birebir ayni.

dgVoodoo2 acik kaynak degil ama serbestce dagitilan bir arac; surumleri
github.com/dege-diosg/dgVoodoo2 uzerinden yayimlaniyor.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import net

API = "https://api.github.com/repos/dege-diosg/dgVoodoo2/releases"

D3D9 = "D3D9.dll"
CONF = "dgVoodoo.conf"
CPL = "dgVoodooCpl.exe"


def resolve() -> tuple[str, str]:
    """(surum, indirme_url) - en guncel normal paket (dbg/dev64 haric)."""
    rels = net.json_get(API + "?per_page=10")
    for r in rels:
        for a in r.get("assets", []):
            n = a["name"].lower()
            if n.startswith("dgvoodoo2_") and n.endswith(".zip") \
                    and "dbg" not in n and "dev" not in n:
                return r.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("dgVoodoo2 paketi bulunamadi.")


def tune_conf(text: str, vram_mb: int = 1024) -> str:
    """DLSS5-Feeder'in istedigi ayarlari conf'a yazar.

    Belgelerdeki gereksinimler:
      [DirectX] DisableAndPassThru=false, VRAM=1024, VideoCard=internal3D
      [General] OutputAPI=d3d11_fl11_0
      dgVoodooWatermark kapali (once acip test etmek isteyen elle acabilir)
    """
    wanted = {
        "OutputAPI": "d3d11_fl11_0",
        "DisableAndPassThru": "false",
        "VRAM": str(vram_mb),
        "VideoCard": "internal3D",
        "dgVoodooWatermark": "false",
    }
    out = []
    seen = set()
    for line in text.splitlines():
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$", line)
        if m and m.group(2) in wanted:
            key = m.group(2)
            out.append(f"{m.group(1)}{key} = {wanted[key]}")
            seen.add(key)
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def install(exe_dir: Path, log=None) -> list[str]:
    """dgVoodoo2'yi oyunun yanina kurar. Yazilan dosyalarin listesini dondurur."""
    log = log or (lambda *_: None)
    ver, url = resolve()
    log(f"      dgVoodoo2 {ver}")
    z = net.download(url, f"dgVoodoo2_{ver}.zip")

    written: list[str] = []
    # 32-bit oyun icin MS/x86/D3D9.dll gerekir
    net.extract_one(z, "MS/x86/D3D9.dll", exe_dir / D3D9)
    written.append(D3D9)
    log(f"      {D3D9} (MS/x86 - 32-bit oyun icin)")

    # dgVoodoo.conf - varsa kullanicininkini koru, yoksa paketinkini al
    conf_path = exe_dir / CONF
    if conf_path.is_file():
        text = conf_path.read_text(encoding="utf8", errors="replace")
        log("      mevcut dgVoodoo.conf bulundu, sadece gerekli anahtarlar guncellendi")
    else:
        tmp = exe_dir / (CONF + ".tmp")
        net.extract_one(z, CONF, tmp)
        text = tmp.read_text(encoding="utf8", errors="replace")
        tmp.unlink(missing_ok=True)
    conf_path.write_text(tune_conf(text), encoding="utf8")
    written.append(CONF)
    log("      dgVoodoo.conf -> OutputAPI=d3d11_fl11_0, VRAM=1024, VideoCard=internal3D")

    # Kontrol paneli - kullanici elle ayar yapmak isterse
    try:
        net.extract_one(z, CPL, exe_dir / CPL)
        written.append(CPL)
        log(f"      {CPL} (ayarlari elle degistirmek istersen)")
    except Exception:
        pass
    return written
