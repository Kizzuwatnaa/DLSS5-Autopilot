r"""Indirme + onbellek + zip cikarma.

nvngx_dlssnr.dll tek basina 165 MB. Onbellek olmadan her oyunda ~150 MB
yeniden inerdi; bu yuzden indirilenler %LOCALAPPDATA%\dlss5kur\cache altinda
saklanir ve sonraki kurulumlar aninda tamamlanir.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

from . import sources

CACHE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dlss5kur" / "cache"


def cache_dir() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE


def cache_size() -> int:
    if not CACHE.is_dir():
        return 0
    return sum(p.stat().st_size for p in CACHE.rglob("*") if p.is_file())


def clear_cache() -> None:
    if CACHE.is_dir():
        shutil.rmtree(CACHE, ignore_errors=True)


def download(url: str, name: str, progress=None, force: bool = False) -> Path:
    """URL'yi onbellege indirir ve yolunu dondurur. progress(indirilen, toplam)."""
    dest = cache_dir() / name
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        if progress:
            progress(dest.stat().st_size, dest.stat().st_size)
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=sources.UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    # Sunucu boyut bildirdiyse eksik indirmeyi burada yakala.
    if total and tmp.stat().st_size != total:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{name}: indirme eksik ({tmp.stat().st_size}/{total} bayt).")
    tmp.replace(dest)
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def zip_members(zpath: Path) -> list[str]:
    with zipfile.ZipFile(zpath) as z:
        return z.namelist()


def extract_one(zpath: Path, member_suffix: str, dest: Path) -> None:
    """Adi member_suffix ile biten ilk uyeyi dest dosyasina cikarir."""
    with zipfile.ZipFile(zpath) as z:
        hit = next((n for n in z.namelist()
                    if not n.endswith("/") and n.lower().endswith(member_suffix.lower())), None)
        if hit is None:
            raise RuntimeError(f"{zpath.name} icinde {member_suffix} yok.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with z.open(hit) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)


def extract_tree(zpath: Path, inner_dir: str, dest_dir: str, out_root: Path,
                 only_ext: tuple[str, ...] | None = None) -> list[Path]:
    """inner_dir altindaki dosyalari out_root/dest_dir icine duz olarak cikarir."""
    written: list[Path] = []
    key = inner_dir.strip("/").lower()
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            if n.endswith("/"):
                continue
            parts = n.split("/")
            # 'LumeniteFX-mainline/Shaders/x.fx' -> kok klasoru atla
            rel = "/".join(parts[1:]) if len(parts) > 1 else n
            rl = rel.lower()
            if not rl.startswith(key + "/"):
                continue
            tail = rel[len(key) + 1:]
            if "/" in tail:            # sadece bu seviyedeki dosyalar
                continue
            if only_ext and not tail.lower().endswith(only_ext):
                continue
            target = out_root / dest_dir / tail
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            written.append(target)
    return written


def json_get(url: str):
    """URL'den JSON oku."""
    import json
    return json.loads(fetch_text(url).decode("utf8"))


def fetch_text(url: str) -> bytes:
    req = urllib.request.Request(url, headers=sources.UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
