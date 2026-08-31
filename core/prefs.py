"""Kalici tercihler + yerel renodx dosyasi bulma.

Discord'dan indirilen renodx surumleri aynada olmayabiliyor. Kullanicinin bir
kez sectigi dosyayi hatirlayip BUTUN oyunlarda varsayilan yapiyoruz.

Arama sirasi:
    1. daha once secilmis/kaydedilmis dosya
    2. uygulamanin yanindaki  renodx\\  klasoru      <- tasinabilir kurulum
    3. Indirilenler / Masaustu (bir alt klasor derinligine kadar)
    4. hicbiri yoksa -> rhi-repo aynasindan indir
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dlss5kur" / "ayarlar.json"

# Uygulamanin yanindaki klasor (PyInstaller onefile'da exe'nin bulundugu yer)
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save(data: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    except OSError:
        pass


def get(key: str, default=None):
    return load().get(key, default)


def set_(key: str, value) -> None:
    d = load()
    d[key] = value
    save(d)


def is_renodx(path: Path) -> bool:
    """Dosya gercekten RenoDX DLSS 5 eklentisi mi?

    DLSS5-Feeder'in kendi dlss5-feed.addon64'u de ayni uzantiyi tasidigi icin
    ada guvenmiyoruz; ikilinin icindeki imza stringine bakiyoruz.
    """
    try:
        if not path.is_file() or path.stat().st_size < 200_000:
            return False
        data = path.read_bytes()
        if data[:2] != b"MZ":
            return False
        return b"RenoDX" in data and b"DLSS" in data
    except OSError:
        return False


def _candidates() -> list[Path]:
    hits: list[Path] = []

    # 2) uygulamanin yanindaki renodx klasoru (ve exe'nin kendi klasoru)
    for d in (app_dir() / "renodx", app_dir()):
        if d.is_dir():
            try:
                hits += [f for f in d.glob("*.addon64") if f.is_file()]
            except OSError:
                pass

    # 3) Indirilenler / Masaustu - bir alt klasor derinligine kadar.
    #    (Kullanicinin dosyasi cogu zaman "Masaustu\bir klasor\..." icinde olur.)
    home = Path.home()
    roots = [home / "Downloads", home / "Desktop",
             home / "İndirilenler", home / "Masaüstü",
             Path(os.environ.get("USERPROFILE", home)) / "Downloads"]
    for d in roots:
        if not d.is_dir():
            continue
        try:
            hits += [f for f in d.glob("*.addon64") if f.is_file()]
            for sub in d.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    try:
                        hits += [f for f in sub.glob("*.addon64") if f.is_file()]
                    except OSError:
                        continue
        except OSError:
            continue

    uniq: dict[Path, Path] = {}
    for f in hits:
        try:
            uniq.setdefault(f.resolve(), f)
        except OSError:
            continue
    # Adi ne olursa olsun ICERIGI dogrula: DLSS5-Feeder'in kendi addon'u da
    # .addon64 uzantili, onu renodx sanip kurarsak kurulum sessizce bozulur.
    good = [f for f in uniq.values() if is_renodx(f)]
    good.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return good


def find_renodx() -> tuple[Path | None, list[Path]]:
    """(secilecek_dosya, tum_adaylar). Kaydedilmis tercih varsa o one gecer."""
    cands = _candidates()
    saved = get("renodx_local")
    if saved:
        p = Path(saved)
        if p.is_file():
            others = [c for c in cands if c.resolve() != p.resolve()]
            return p, [p] + others
    return (cands[0] if cands else None), cands


def remember_renodx(path: Path | None) -> None:
    set_("renodx_local", str(path) if path else None)
