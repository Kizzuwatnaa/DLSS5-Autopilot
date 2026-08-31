"""dlss5-feed.cfg yazma.

Eklenti bu dosyayi kendisi olusturuyor ama biz onceden yazarsak ilk acilista
dogru ayarlarla baslar. Anahtarlar dlss5-feed.addon64 ikilisinden ve
DLSS5-Feeder belgelerinden dogrulandi.

ONEMLI - "DLSS Performance modu" hakkinda:
    Feeder yolu her zaman DLAA'dir ve baska turlu olamaz. Sebep mimari:
    DLSS5-Feeder oyunun DUSUK cozunurluklu render'ini gormez, ReShade
    zincirinin sonundaki BITMIS tam cozunurluklu kareyi gorur. Upscale
    edilecek dusuk cozunurluklu bir kaynak yoktur, dolayisiyla Quality /
    Balanced / Performance modlari bu yolda anlamsizdir; log da bu yuzden
    hep "DLAA" yazar.

    Performans icin dogru dugme work_resolution'dir (asagida): neural
    islemenin uygulandigi alani %50-100 arasinda kucultur.
"""
from __future__ import annotations

from pathlib import Path

NAME = "dlss5-feed.cfg"

# DLSS preset ipucu. DLSS5-Feeder sorun giderme tablosu: alevlerin/saydam
# nesnelerin etrafinda bozulma varsa 5 veya 6 (eski CNN) dene.
PRESETS = {
    0:  "Varsayilan (eklenti karar versin)",
    5:  "Preset E - eski CNN (alev/saydam bozulmasina iyi gelir)",
    6:  "Preset F - eski CNN",
    10: "Preset J - transformer",
    11: "Preset K - transformer (en yeni)",
}

HDR = {-1: "Otomatik", 0: "SDR (zorla)", 1: "HDR (zorla)"}
DEPTH = {-1: "ReShade'i izle", 0: "Ters degil (zorla)", 1: "Ters (zorla)"}
MODE = {2: "Tam DLSS (normal)", 1: "Sadece tasima testi", 0: "Kapali"}


def defaults() -> dict:
    return {
        "enabled": 1,
        "mode": 2,
        "hdr": -1,
        "depth_inverted": -1,
        "flags": -1,
        "reset_every": 0,
        "warmup_rebuild": 180,
        "rebuild": 0,
        "log_frames": 3,
        "create_delay": 60,
        "preset": 0,
        "work_resolution": 100,
        "mv_scale_x": 1.0,
        "mv_scale_y": 1.0,
    }


def read(path: Path) -> dict:
    out: dict = {}
    try:
        for line in path.read_text(encoding="utf8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write(dir_: Path, settings: dict | None = None, host_window: bool | None = None) -> Path:
    """dlss5-feed.cfg olustur/guncelle; dokunmadigimiz anahtarlari korur."""
    p = dir_ / NAME
    cur = defaults()
    cur.update({k: v for k, v in read(p).items()})      # kullanicinin mevcut degerleri
    if settings:
        cur.update(settings)
    if host_window is not None:
        cur["host_window"] = 1 if host_window else 0

    lines = []
    for k, v in cur.items():
        if isinstance(v, float) or k.startswith("mv_scale"):
            lines.append(f"{k}={float(v):.3f}")
        else:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf8")
    return p


def describe(settings: dict) -> list[str]:
    """Kullaniciya gosterilecek ozet satirlari."""
    out = []
    wr = int(settings.get("work_resolution", 100))
    if wr != 100:
        out.append(f"work_resolution={wr}% (neural isleme alani kucultuldu - "
                   f"daha yuksek fps, biraz daha az detay)")
    pr = int(settings.get("preset", 0))
    if pr:
        out.append(f"preset={pr} ({PRESETS.get(pr, '?')})")
    hd = int(settings.get("hdr", -1))
    if hd != -1:
        out.append(f"hdr={hd} ({HDR.get(hd)})")
    di = int(settings.get("depth_inverted", -1))
    if di != -1:
        out.append(f"depth_inverted={di} ({DEPTH.get(di)})")
    for ax in ("x", "y"):
        v = float(settings.get(f"mv_scale_{ax}", 1.0))
        if abs(v - 1.0) > 1e-6:
            out.append(f"mv_scale_{ax}={v:.3f}")
    return out
