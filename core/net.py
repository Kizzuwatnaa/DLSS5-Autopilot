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
import ssl
import time
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


# errno 28 is what Python says; ERROR_DISK_FULL (112) and
# ERROR_HANDLE_DISK_FULL (39) are what Windows says underneath it.
_FULL_ERRNO = 28
_FULL_WINERROR = (112, 39)
# The note an install that stopped on a full drive leaves in its record,
# for the diagnosis to read.
STOP_NOTE = "install stopped: "
DISK_FULL_NOTE = STOP_NOTE + "the drive ran out of space"


def is_disk_full(e: BaseException | None) -> bool:
    """Did this fail because a drive ran out of space - here or underneath?

    The OSError is often wrapped (a zip that could not be written comes up
    as something else), so the chain it was raised from is followed too.
    """
    seen = 0
    while e is not None and seen < 8:
        if isinstance(e, OSError) and (
                e.errno == _FULL_ERRNO
                or getattr(e, "winerror", None) in _FULL_WINERROR):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


def _drive_of(p) -> str:
    try:
        return Path(p).drive or Path(os.path.abspath(p)).drive
    except (OSError, ValueError, TypeError):
        return ""


def _free_on(drive: str) -> str:
    try:
        return human(shutil.disk_usage(drive + "\\").free) + " free"
    except OSError:
        return "free space unknown"


def disk_full_message(e: BaseException, game_dir=None, cache_dir=None) -> str:
    """What to tell someone whose drive is full: which drive, how much is free.

    #148's drive was full, and the report carried a Python traceback where
    "free up space" belonged. The drive named as full is the one the failed
    write was on; the game's drive and the cache's are listed for what they
    are, not as full as well.
    """
    full = ""
    cur: BaseException | None = e
    while cur is not None and not full:
        name = getattr(cur, "filename", None)
        if name:
            full = _drive_of(str(name))
        cur = cur.__cause__ or cur.__context__
    head = (f"Out of disk space on {full} ({_free_on(full)})." if full
            else "A drive ran out of space.")
    g, c = _drive_of(game_dir) if game_dir else "", _drive_of(cache_dir) if cache_dir else ""
    where = []
    if g:
        where.append(f"beside the game ({g}, {_free_on(g)})")
    if c:
        where.append(f"in the tool's download cache (%LOCALAPPDATA%\\dlss5-autopilot, "
                     f"{c}, {_free_on(c)})")
    need = " and ".join(where) if where else "beside the game and in the tool's cache"
    return (f"{head} The install needs a few hundred MB {need} - free some "
            f"up, then install again.")


_SSL: ssl.SSLContext | None = None


def untrusted(name: str, e: Exception) -> RuntimeError | None:
    """The error to raise when TLS verification failed, else None.

    urlopen reports a failed verification as a URLError whose reason is
    the SSLError, so the text is checked rather than the type.
    """
    if "CERTIFICATE_VERIFY_FAILED" not in str(e):
        return None
    return RuntimeError(
        f"{name}: Windows does not trust GitHub's certificate ({e}). Open "
        f"https://github.com once in Edge (Windows fetches a missing root "
        f"certificate the first time a Microsoft program needs it), then "
        f"try again. An antivirus that inspects HTTPS causes this too - "
        f"exclude this tool or turn that off.")


def ssl_context() -> ssl.SSLContext:
    """Windows' root store plus the certifi bundle shipped in the exe.

    Python reads the Windows certificate store once, as it is. A freshly
    installed Windows has only a handful of roots in it and fetches the rest
    on demand through CryptoAPI - which Python's OpenSSL never asks for - so
    the first download on a new machine died with 'unable to get local
    issuer certificate' (issue #54, Windows 11 25H2, two reporters). The
    bundle covers that; the system store stays, so a corporate or antivirus
    root that inspects HTTPS is still trusted.
    """
    global _SSL
    if _SSL is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass
        _SSL = ctx
    return _SSL


# A server that is overloaded for a second is the commonest failure of
# all, and it used to end the install with a traceback (#103).
RETRY_CODES = (500, 502, 503, 504, 408)
RETRIES = 3
RETRY_WAIT = 2.0


# What a file saved under these suffixes starts with. Deliberately a small
# set where the answer is certain: every other suffix passes unchecked.
_MAGIC = {
    ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
    ".7z": (b"7z\xbc\xaf\x27\x1c",),
    ".exe": (b"MZ",),
    ".dll": (b"MZ",),
    ".addon64": (b"MZ",),
    ".addon32": (b"MZ",),
}


class WrongContent(RuntimeError):
    """The server answered, but not with the file that was asked for."""


def _looks_right(path: Path, suffix: str) -> bool:
    """Does the file start the way a `suffix` file must?

    A captive portal, a DNS filter or an antivirus answers 200 with an HTML
    page, and a cut connection can leave a zip without its directory. Either
    one used to be cached and served again on every retry, so the install
    failed with "File is not a zip file" however often it was run (#140).
    """
    magic = _MAGIC.get(suffix.lower())
    if magic is None:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    if not head.startswith(magic):
        return False
    if suffix.lower() == ".zip":
        return zipfile.is_zipfile(path)
    return True


def _head(path: Path, n: int = 24) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def download(url: str, name: str, progress=None, force: bool = False,
             attempts: int = 4) -> Path:
    """Download to the cache and return the path. progress(done, total).

    Retries on failure and resumes from a partial file with an HTTP Range
    request - dropping 150 MB and starting over because a connection blipped
    is miserable on a slow line.
    """
    dest = cache_dir() / name
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        if _looks_right(dest, dest.suffix):
            if progress:
                progress(dest.stat().st_size, dest.stat().st_size)
            return dest
        # A bad file in the cache is fetched again, not served forever (#140).
        try:
            dest.unlink()
        except OSError:
            pass

    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None

    for attempt in range(attempts):
        have = tmp.stat().st_size if tmp.is_file() else 0
        headers = dict(sources.UA)
        if have and attempt:                     # only resume on a retry
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120, context=ssl_context()) as r:
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
            if not _looks_right(tmp, dest.suffix):
                head = _head(tmp)
                tmp.unlink(missing_ok=True)
                host = url.split("/")[2] if "//" in url else url
                raise WrongContent(
                    f"{name}: {host} answered with something that is not a "
                    f"{dest.suffix} file (it starts with {head!r}). That is "
                    f"usually a proxy, DNS filter or antivirus page standing "
                    f"in for the download. Nothing was written; try again "
                    f"or from another network.")
            tmp.replace(dest)
            return dest
        except WrongContent:
            raise                                # the same page comes back
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)          # 4xx/5xx: resuming won't help
            # ...but a 5xx is the server having a bad second, not a wrong
            # URL. The big files come through here, and an overloaded host
            # used to end the install with a traceback (#103).
            if e.code in RETRY_CODES and attempt < attempts - 1:
                last = e
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            if e.code in RETRY_CODES:
                host = url.split("/")[2] if "/" in url else url
                raise sources.Unavailable(
                    f"{host} is not answering right now (HTTP {e.code}). "
                    f"That is the server this file is published on, not your "
                    f"connection and not this tool - it was asked "
                    f"{attempts} times. Wait a few minutes and install "
                    f"again: anything already downloaded is cached and will "
                    f"not be fetched twice, and whatever this attempt did "
                    f"write is recorded, so 'uninstall' takes it back "
                    f"out.") from e
            raise
        except ssl.SSLError as e:
            # "decryption failed or bad record mac" is not a hiccup: it is
            # almost always an antivirus or a VPN sitting in the middle of the
            # TLS connection. Retrying instantly three times just reproduces
            # it, so back off - and if it survives that, say what it means
            # instead of showing a raw ssl.c traceback (issue #7).
            last = e
            if attempt == attempts - 1:
                tmp.unlink(missing_ok=True)
                if untrusted(name, e):
                    raise untrusted(name, e) from e
                raise RuntimeError(
                    f"{name}: the secure connection kept breaking ({e}). "
                    f"Something is sitting between this PC and GitHub - an "
                    f"antivirus with HTTPS/SSL scanning, a VPN or a proxy. "
                    f"Turn that off (or exclude this tool) and try again; the "
                    f"download resumes where it stopped.") from e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:                   # network hiccup - retry
            if is_disk_full(e):
                # Asking again fills the same drive again; the partial file
                # goes, so the space it took comes back (#148).
                tmp.unlink(missing_ok=True)
                raise
            last = e
            if attempt == attempts - 1:
                tmp.unlink(missing_ok=True)
                if untrusted(name, e):
                    raise untrusted(name, e) from e
                raise
            time.sleep(1.0 * (attempt + 1))
    raise last if last else RuntimeError(f"{name}: download failed")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


class OutsideError(ValueError):
    """A path from a file or an archive pointed outside where it may write."""


def inside(root: Path, rel: str, absolute_ok: bool = False) -> Path:
    """Resolve `rel` under `root`, refusing anything that leaves it.

    Every path this tool writes to comes out of something somebody else
    wrote: an archive's member names, or the install manifest sitting in the
    game folder. `..` in either of those walks out of the folder, and a
    string prefix test is not enough to catch it - "C:/Games/game-other"
    starts with "C:/Games/game" (#79). Compare whole path parts instead.

    An archive member is never absolute, so `absolute_ok` is off by default.
    The install manifest is the exception: this tool writes an absolute
    entry itself when a backup or a Remix runtime lands outside the install
    folder, and refusing those meant an uninstall silently left them behind.
    Even then the path has to resolve under `root` - what is refused is a
    path that ESCAPES, not one that is written differently.
    """
    root = Path(root).resolve()
    p = Path(rel.replace("\\", "/"))
    if (p.is_absolute() or p.drive or rel.startswith(("/", "\\"))) \
            and not absolute_ok:
        raise OutsideError(f"{rel} is an absolute path - refused.")
    target = (p if p.is_absolute() else root / p).resolve()
    if target != root and root not in target.parents:
        raise OutsideError(f"{rel} points outside {root} - refused.")
    return target


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
                 only_ext: tuple[str, ...] | None = None,
                 only_names: tuple[str, ...] | None = None) -> list[Path]:
    """Flatten files under inner_dir into out_root/dest_dir.

    `only_names` keeps just those file names (case-insensitive) - used to
    take one shader out of a pack instead of the whole pack."""
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
            if only_names and tail.lower() not in tuple(n.lower() for n in only_names):
                continue
            try:
                target = inside(Path(out_root) / dest_dir, tail)
            except OutsideError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            written.append(target)
    return written


def fetch_text(url: str, _try: int = 0) -> bytes:
    req = urllib.request.Request(url, headers=sources.UA)
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as r:
            return r.read()
    except urllib.error.URLError as e:
        if (isinstance(e, urllib.error.HTTPError)
                and e.code in RETRY_CODES and _try < RETRIES):
            time.sleep(RETRY_WAIT * (_try + 1))
            return fetch_text(url, _try + 1)
        if not isinstance(e, urllib.error.HTTPError):
            if untrusted(url.split("/")[2], e):
                raise untrusted(url.split("/")[2], e) from e
            raise
        # (HTTPError is a URLError; the checks below apply to it only)
        # Same anonymous API allowance as sources._get; keep the message
        # identical so the user sees one clear explanation either way.
        if e.code in (403, 429) and "api.github.com" in url:
            raise sources.RateLimited(
                "GitHub is rate limiting this connection (60 anonymous API "
                "requests per hour). Wait an hour and try again, or use a VPN / "
                "different network. Downloads already in the cache still work."
            ) from e
        if e.code in RETRY_CODES:
            host = url.split("/")[2] if "/" in url else url
            raise sources.Unavailable(
                f"{host} is not answering right now (HTTP {e.code}). That is "
                f"the server this file is published on, not your connection "
                f"and not this tool - it was asked {RETRIES + 1} times. "
                f"Wait a few minutes and install again: anything already "
                f"downloaded is cached and will not be fetched twice, and "
                f"whatever this attempt did write is recorded, so "
                f"'uninstall' takes it back out.") from e
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
