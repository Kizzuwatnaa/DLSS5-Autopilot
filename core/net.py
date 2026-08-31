r"""Downloading, caching and zip extraction.

nvngx_dlssnr.dll alone is 165 MB. Without a cache every game would pull
~150 MB again, so downloads are kept under %LOCALAPPDATA%\dlss5-autopilot\cache
and later installs finish instantly.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import sources

CACHE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dlss5-autopilot" / "cache"


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


def download(url: str, name: str, progress=None, force: bool = False,
             attempts: int = 3) -> Path:
    """Download to the cache and return the path. progress(done, total).

    Retries on failure and resumes from a partial file with an HTTP Range
    request - dropping 150 MB and starting over because a connection blipped
    is miserable on a slow line.
    """
    dest = cache_dir() / name
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        if progress:
            progress(dest.stat().st_size, dest.stat().st_size)
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None

    for attempt in range(attempts):
        have = tmp.stat().st_size if tmp.is_file() else 0
        headers = dict(sources.UA)
        if have and attempt:                     # only resume on a retry
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resuming = r.status == 206
                if not resuming:
                    have = 0
                total = int(r.headers.get("Content-Length") or 0) + have
                done = have
                with open(tmp, "ab" if resuming else "wb") as f:
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            # If the server declared a size, catch truncated downloads here.
            if total and tmp.stat().st_size != total:
                raise RuntimeError(
                    f"{name}: incomplete download "
                    f"({tmp.stat().st_size}/{total} bytes).")
            tmp.replace(dest)
            return dest
        except urllib.error.HTTPError:
            tmp.unlink(missing_ok=True)          # 4xx/5xx: resuming won't help
            raise
        except Exception as e:                   # network hiccup - retry
            last = e
            if attempt == attempts - 1:
                tmp.unlink(missing_ok=True)
                raise
    raise last if last else RuntimeError(f"{name}: download failed")


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
    """Extract the first member whose name ends with member_suffix."""
    with zipfile.ZipFile(zpath) as z:
        hit = next((n for n in z.namelist()
                    if not n.endswith("/") and n.lower().endswith(member_suffix.lower())), None)
        if hit is None:
            raise RuntimeError(f"{zpath.name} does not contain {member_suffix}.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with z.open(hit) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)


def extract_tree(zpath: Path, inner_dir: str, dest_dir: str, out_root: Path,
                 only_ext: tuple[str, ...] | None = None) -> list[Path]:
    """Flatten files under inner_dir into out_root/dest_dir."""
    written: list[Path] = []
    key = inner_dir.strip("/").lower()
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            if n.endswith("/"):
                continue
            parts = n.split("/")
            # 'LumeniteFX-mainline/Shaders/x.fx' -> drop the archive root
            rel = "/".join(parts[1:]) if len(parts) > 1 else n
            rl = rel.lower()
            if not rl.startswith(key + "/"):
                continue
            tail = rel[len(key) + 1:]
            if "/" in tail:            # only files at this level
                continue
            if only_ext and not tail.lower().endswith(only_ext):
                continue
            target = out_root / dest_dir / tail
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            written.append(target)
    return written


def fetch_text(url: str) -> bytes:
    req = urllib.request.Request(url, headers=sources.UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        # Same anonymous API allowance as sources._get; keep the message
        # identical so the user sees one clear explanation either way.
        if e.code in (403, 429) and "api.github.com" in url:
            raise sources.RateLimited(
                "GitHub is rate limiting this connection (60 anonymous API "
                "requests per hour). Wait an hour and try again, or use a VPN / "
                "different network. Downloads already in the cache still work."
            ) from e
        raise


def json_get(url: str):
    """Read JSON from a URL."""
    return json.loads(fetch_text(url).decode("utf8"))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
