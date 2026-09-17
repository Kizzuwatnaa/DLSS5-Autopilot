r"""Cover art for games outside Steam: Epic's catalog, Xbox's own files, Steam's store.

Steam's library cache only has pictures for games Steam installed, so a game
from Epic, Ubisoft, EA or a plain folder was an icon on an empty tile. This
module finds the rest, in this order:

  - pictures the person chose themselves (always win);
  - an Xbox / Game Pass folder's own logo and splash files (local);
  - Epic: the launcher's catalog cache names the images for the game its
    manifest installed in this folder; they are downloaded from Epic's CDN;
  - everything else: Steam's store search by the game's name, accepted only
    on a close name match, then the images from Steam's CDN for that appid.

Anything that goes online is gated by the `online_art` preference, runs on
a worker (core.ui.art queues it), and degrades to "nothing found" on any
failure. A game with no match is remembered for a week, a failed connection
for a day, so no game searches on every launch.

Everything lands in %LOCALAPPDATA%\dlss5-autopilot\art\<key>\, one folder
per game folder, so two games never share an image.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import log, net, prefs, sources

ROOT = prefs.FILE.parent / "art"
KINDS = ("cover", "hero", "logo")
MAX_BYTES = 8 * 1024 * 1024
MISS_TTL = 7 * 24 * 3600          # no match: ask again in a week
FAIL_TTL = 24 * 3600              # no connection: ask again tomorrow
TIMEOUT = 15
PREF = "online_art"

EPIC_DATA = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US"
_FASTLY = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/"
_CLOUDFLARE = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/"

# Epic's keyImages type -> what it is here. DieselGameBoxTall is the
# 1200x1600 library cover, DieselGameBox the 2560x1440 key art.
EPIC_TYPES = {"cover": ("DieselGameBoxTall", "OfferImageTall", "DieselStoreFrontTall"),
              "hero": ("DieselGameBox", "DieselGameBoxWide", "OfferImageWide"),
              "logo": ("DieselGameBoxLogo",)}


def online() -> bool:
    """Only after the person said yes. The privacy policy under which the
    exe is signed promises nothing leaves the PC unless asked for, and a
    store search carries the game's name - so there is no default of on."""
    return prefs.get(PREF) is True


def undecided() -> bool:
    """Never asked (or a settings file from before the question existed)."""
    return prefs.get(PREF) not in (True, False)


# ---------------------------------------------------------------- the cache
def _norm_path(folder) -> str:
    return os.path.normcase(str(folder).replace("/", "\\")).rstrip("\\")


def key(folder) -> str:
    """One folder name per game folder; case and slash direction do not matter."""
    return hashlib.sha1(_norm_path(folder).encode("utf8")).hexdigest()[:20]


def dir_of(game) -> Path:
    return ROOT / key(game.folder)


def image_ext(data: bytes) -> str | None:
    """The suffix the bytes really are, or None: a captive portal's HTML page
    answered with 200 must never be kept as a cover."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"BM" and len(data) > 26:
        return ".bmp"
    return None


def _files(d: Path, prefix: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        stem, ext = os.path.splitext(n)
        if ext.lower() in (".jpg", ".png", ".bmp") and stem.startswith(prefix):
            kind = stem[len(prefix):]
            if kind in KINDS:
                out[kind] = d / n
    return out


def user(game) -> dict[str, Path]:
    """Pictures the person chose for this game."""
    return _files(dir_of(game), "user-")


def got(game) -> dict[str, Path]:
    """Pictures downloaded for this game."""
    return _files(dir_of(game), "got-")


def _meta(game) -> dict:
    try:
        d = json.loads((dir_of(game) / "meta.json").read_text(encoding="utf8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(game, data: dict) -> None:
    d = dir_of(game)
    try:
        d.mkdir(parents=True, exist_ok=True)
        data = dict(data, name=game.name, folder=str(game.folder))
        (d / "meta.json").write_text(json.dumps(data, ensure_ascii=True), encoding="utf8")
    except OSError:
        pass


def _store(game, prefix: str, kind: str, data: bytes) -> Path | None:
    ext = image_ext(data)
    if ext is None or not data or len(data) > MAX_BYTES:
        return None
    if prefix == "got-" and not online():
        # a download that finished after the switch went off is not kept
        return None
    d = dir_of(game)
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{prefix}{kind}.part"
        tmp.write_bytes(data)
        for old in _files(d, prefix).values():
            if old.stem == prefix + kind:
                old.unlink(missing_ok=True)
        dest = d / f"{prefix}{kind}{ext}"
        tmp.replace(dest)
        return dest
    except OSError:
        return None


def pending(game, now: float | None = None) -> bool:
    """Should an online lookup run for this game? Never when the setting is
    off, when a cover is already here, or while a recorded miss is fresh -
    unless the game has been renamed since, which is a different question."""
    if not online():
        return False
    if "cover" in user(game) or "cover" in got(game):
        return False
    m = _meta(game)
    miss = m.get("miss")
    if isinstance(miss, (int, float)) and m.get("name") == game.name:
        ttl = m.get("ttl") if isinstance(m.get("ttl"), (int, float)) else MISS_TTL
        if (now if now is not None else time.time()) - miss < ttl:
            return False
    return True


# ---------------------------------------------------------------- choosing by hand
def set_user(game, kind: str, src, readable=None) -> str:
    """Copy a chosen picture into the cache. '' on success, else what to say.

    The file is copied, never referenced: the original is in Downloads or on
    a stick and moves. `readable(path)` is the decoder's own verdict (GDI+),
    so a file with a JPEG header that Windows cannot draw is refused too.
    """
    if kind not in KINDS:
        return f"unknown picture kind: {kind}"
    try:
        size = os.path.getsize(src)
    except OSError:
        return "That file could not be read."
    if size > MAX_BYTES:
        return f"That picture is larger than {MAX_BYTES // (1024 * 1024)} MB."
    try:
        with open(src, "rb") as f:
            data = f.read(MAX_BYTES + 1)
    except OSError:
        return "That file could not be read."
    if image_ext(data) is None:
        return "That is not a JPEG, PNG or BMP picture."
    dest = _store(game, "user-", kind, data)
    if dest is None:
        return "The picture could not be saved in the tool's cache."
    if readable is not None and not readable(dest):
        dest.unlink(missing_ok=True)
        return "Windows could not read that picture."
    return ""


def reset_user(game) -> None:
    for p in user(game).values():
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- Xbox, local only
def _xbox_file(folder: Path, rel: str) -> Path | None:
    """A manifest's image path, or its largest scale-qualified variant
    (StoreLogo.png is shipped as StoreLogo.scale-100.png and friends)."""
    rel = rel.replace("/", "\\").lstrip("\\")
    if not rel or ".." in rel.split("\\"):
        return None
    p = folder / rel
    if p.is_file():
        return p
    try:
        stem, ext = os.path.splitext(p.name)
        best, size = None, -1
        for n in os.listdir(p.parent):
            s, e = os.path.splitext(n)
            if e.lower() == ext.lower() and s.lower().startswith(stem.lower() + "."):
                z = (p.parent / n).stat().st_size
                if z > size:
                    best, size = p.parent / n, z
        return best
    except OSError:
        return None


def xbox(folder) -> dict[str, Path]:
    """Cover and hero from MicrosoftGame.config / AppxManifest.xml, if any."""
    folder = Path(folder)
    out: dict[str, Path] = {}
    for name in ("MicrosoftGame.config", "appxmanifest.xml"):
        f = folder / name
        try:
            if not f.is_file() or f.stat().st_size > 2 * 1024 * 1024:
                continue
            text = f.read_text(encoding="utf8", errors="replace")
        except OSError:
            continue
        attrs = dict((k.lower(), v) for k, v in re.findall(r'([A-Za-z0-9]+)\s*=\s*"([^"]*)"', text))
        logo = re.search(r"<Logo>\s*([^<]+?)\s*</Logo>", text)
        cover = [attrs.get("square480x480logo"), attrs.get("square310x310logo"),
                 attrs.get("square150x150logo"), attrs.get("storelogo"), logo.group(1) if logo else None]
        hero = [attrs.get("splashscreenimage"), attrs.get("image")]
        for kind, names in (("cover", cover), ("hero", hero)):
            for rel in names:
                p = _xbox_file(folder, rel) if rel else None
                if p and kind not in out:
                    out[kind] = p
        if out:
            break
    return out


# ---------------------------------------------------------------- Epic
_catalog: tuple | None = None       # (mtime, size, {id: item})


def parse_catcache(raw: bytes) -> dict[str, dict]:
    """catcache.bin is base64 of a JSON list of catalog items; id -> item."""
    try:
        items = json.loads(base64.b64decode(raw.strip()))
    except (ValueError, TypeError):
        return {}
    if not isinstance(items, list):
        return {}
    return {it["id"]: it for it in items if isinstance(it, dict) and isinstance(it.get("id"), str)}


def _catalog_items() -> dict[str, dict]:
    global _catalog
    f = EPIC_DATA / "Catalog" / "catcache.bin"
    try:
        st = f.stat()
        if st.st_size > 64 * 1024 * 1024:
            return {}
        if _catalog and _catalog[:2] == (st.st_mtime, st.st_size):
            return _catalog[2]
        items = parse_catcache(f.read_bytes())
    except OSError:
        return {}
    _catalog = (st.st_mtime, st.st_size, items)
    return items


def epic_manifest(folder, manifests: Path | None = None) -> dict | None:
    """The Epic manifest that installed this folder."""
    want = _norm_path(folder)
    man = manifests or (EPIC_DATA / "Manifests")
    try:
        items = list(man.glob("*.item"))
    except OSError:
        return None
    for it in items:
        try:
            d = json.loads(it.read_text(encoding="utf8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("InstallLocation") and _norm_path(d["InstallLocation"]) == want:
            return d
    return None


def epic_urls(manifest: dict, catalog: dict[str, dict]) -> dict[str, str]:
    """kind -> image URL for the manifest's catalog item (main game second)."""
    out: dict[str, str] = {}
    for id_key, ns_key in (("CatalogItemId", "CatalogNamespace"),
                           ("MainGameCatalogItemId", "MainGameCatalogNamespace")):
        item = catalog.get(manifest.get(id_key) or "")
        if not item:
            continue
        ns = manifest.get(ns_key)
        if ns and item.get("namespace") and item["namespace"] != ns:
            continue
        images = [k for k in item.get("keyImages") or [] if isinstance(k, dict)]
        for kind, types in EPIC_TYPES.items():
            if kind in out:
                continue
            for t in types:
                hit = next((k.get("url") for k in images if k.get("type") == t
                            and str(k.get("url", "")).startswith("https://")), None)
                if hit:
                    out[kind] = hit
                    break
    return out


# ---------------------------------------------------------------- Steam store, by name
_SYMBOLS = dict.fromkeys(map(ord, "\u2122\u00ae\u00a9\u2120"), None)
_APOSTROPHES = dict.fromkeys(map(ord, "'`\u2018\u2019\u02bc\u00b4"), None)
_EDITION = re.compile(
    r"(?:\s+the)?\s+(?:(?:complete|definitive|enhanced|deluxe|digital deluxe|standard|ultimate|"
    r"gold|premium|special|anniversary|collectors|legendary|game of the year|goty)\s+edition"
    r"|directors cut|goty)$")
# Words that make a store result something other than the game itself.
_ADDON = re.compile(
    r"\b(?:dlc|soundtrack|ost|pack|bundle|upgrade|demo|season pass|expansion|skin|skins|costume|"
    r"artbook|art book|supply drop|episode|chapter|trial|benchmark|playtest|dedicated server|"
    r"sdk|wallpapers?|prelude|mask|cosmetic)\b")


def norm(name: str) -> str:
    """Lower-case words: no trademark signs, accents, apostrophes or punctuation."""
    s = str(name).translate(_SYMBOLS).translate(_APOSTROPHES)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return " ".join(s.split())


def strip_edition(n: str) -> str:
    """'grand theft auto iv the complete edition' -> 'grand theft auto iv'."""
    return _EDITION.sub("", n).strip()


def match(name: str, items) -> int | None:
    """The store result that IS this game, or None. A wrong cover is worse
    than none, so only three shapes are accepted:
      1. the same name, once symbols and punctuation are gone;
      2. the same name once an edition suffix is dropped from either side;
      3. the name plus a ':' subtitle ('DEATH STRANDING 2: ON THE BEACH'), for
         a name of two words or more, when exactly one such result exists
         and its subtitle is not a DLC, a soundtrack or a pack.
    A sequel ('The Division 2' for 'The Division') matches none of them."""
    g = norm(name)
    gs = strip_edition(g)
    if not gs:
        return None
    apps = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("type", "app") != "app":
            continue
        try:
            aid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        raw = str(it.get("name") or "")
        if aid > 0 and raw:
            apps.append((aid, raw))
    for aid, raw in apps:
        if norm(raw) == g:
            return aid
    for aid, raw in apps:
        n = norm(raw)
        if strip_edition(n) == gs and not _ADDON.search(n):
            return aid
    subs = []
    if len(gs.split()) >= 2:
        for aid, raw in apps:
            if ":" not in raw:
                continue
            head, tail = raw.split(":", 1)
            if strip_edition(norm(head)) == gs and norm(tail) and not _ADDON.search(norm(tail)):
                subs.append(aid)
    return subs[0] if len(subs) == 1 else None


_ASSET_NAME = re.compile(r"^(?:[0-9a-f]{40}/)?[A-Za-z0-9_.-]+\.(?:jpg|png)$")


def steam_urls(appid, assets: dict | None = None) -> dict[str, list[str]]:
    """Where Steam's CDN keeps an app's library pictures, best first.

    Apps published since 2024 keep them under a hash folder
    (.../3280350/9b52.../library_capsule_2x.jpg) and the plain path is a 404
    - DEATH STRANDING 2 was - so the store's own asset list comes first when
    there is one, and the plain paths older apps use after it."""
    a = {"appid": int(appid)}
    out = {"cover": [], "hero": [], "logo": []}
    fmt = str((assets or {}).get("asset_url_format") or "")
    if fmt.startswith(f"steam/apps/{a['appid']}/${{FILENAME}}"):
        for kind, names in (("cover", ("library_capsule_2x", "library_capsule")),
                            ("hero", ("library_hero",)), ("logo", ("library_logo",))):
            for n in names:
                v = str(assets.get(n) or "")
                if _ASSET_NAME.match(v):
                    out[kind].append("https://shared.fastly.steamstatic.com/store_item_assets/"
                                     + fmt.replace("${FILENAME}", v))
    out["cover"] += [_FASTLY.format(**a) + "library_600x900_2x.jpg",
                     _FASTLY.format(**a) + "library_600x900.jpg",
                     _CLOUDFLARE.format(**a) + "library_600x900.jpg"]
    out["hero"] += [_FASTLY.format(**a) + "library_hero.jpg",
                    _CLOUDFLARE.format(**a) + "library_hero.jpg"]
    out["logo"] += [_FASTLY.format(**a) + "logo.png",
                    _CLOUDFLARE.format(**a) + "logo.png"]
    return out


STEAM_ITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json={q}"


def steam_assets(appid) -> dict | None:
    """The store's asset list for an app, or None. Not having it is fine:
    the plain CDN paths are tried anyway."""
    q = json.dumps({"ids": [{"appid": int(appid)}],
                    "context": {"language": "english", "country_code": "US"},
                    "data_request": {"include_assets": True}}, separators=(",", ":"))
    try:
        body = _get(STEAM_ITEMS.format(q=urllib.parse.quote(q)), 2 * 1024 * 1024)
        items = json.loads(body.decode("utf8"))["response"]["store_items"] if body else []
        assets = items[0].get("assets") if items and isinstance(items[0], dict) else None
        return assets if isinstance(assets, dict) else None
    except (_Offline, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None


# ---------------------------------------------------------------- the network
class _Offline(Exception):
    """The request did not get an answer (as opposed to a 404)."""


class _SwitchedOff(Exception):
    """The online setting was switched off while a lookup was running."""


def _get(url: str, limit: int = MAX_BYTES) -> bytes | None:
    """The body, None for a clean 'not there' or an oversized answer;
    _Offline when the connection itself failed. One try, a short timeout."""
    # Asked before every request, not once per lookup: a lookup takes several
    # (search, asset list, three pictures), and "off" means off from the
    # moment it is switched, not after the one already running is done.
    if not online():
        raise _SwitchedOff(url)
    req = urllib.request.Request(url, headers=sources.UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=net.ssl_context()) as r:
            n = int(r.headers.get("Content-Length") or 0)
            if n > limit:
                return None
            data = r.read(limit + 1)
            return None if len(data) > limit else data
    except urllib.error.HTTPError as e:
        if e.code in net.RETRY_CODES or e.code in (403, 429):
            raise _Offline(str(e)) from e
        return None
    except Exception as e:
        raise _Offline(str(e)) from e


def _image(game, kind: str, urls) -> bool:
    for u in urls:
        data = _get(u)
        if data and image_ext(data) and _store(game, "got-", kind, data):
            return True
    return False


def lookup(game, appid=None) -> bool:
    """Go online for this game's pictures. True when anything new was saved.

    Never raises: every failure is a recorded miss the UI does not hear about.
    Callers run this on a worker; `pending` decides whether to call at all,
    and it is checked again here so a setting switched off in between holds.
    """
    if not pending(game):
        return False
    have = set(got(game))
    before = set(have)
    source = ""
    try:
        if str(getattr(game, "source", "")).lower() == "epic":
            m = epic_manifest(game.folder)
            urls = epic_urls(m, _catalog_items()) if m else {}
            for kind, u in urls.items():
                if kind not in have and _image(game, kind, [u]):
                    have.add(kind)
            if "cover" in have:
                source = "epic"
        if "cover" not in have:
            aid = appid
            if aid is None:
                term = urllib.parse.quote(" ".join(str(game.name).translate(_SYMBOLS).split()))
                body = _get(STEAM_SEARCH.format(term=term), 2 * 1024 * 1024) if term else None
                try:
                    items = json.loads(body.decode("utf8")).get("items") if body else []
                except (ValueError, AttributeError):
                    items = []
                aid = match(game.name, items)
            if aid:
                for kind, urls in steam_urls(aid, steam_assets(aid)).items():
                    if kind not in have and _image(game, kind, urls):
                        have.add(kind)
                if "cover" in have:
                    source = f"steam {aid}"
    except _SwitchedOff:
        # not a miss: nothing is recorded, so switching it back on asks again
        log.write(f"art: {game.name}: online lookup switched off part way; stopped")
        return have != before
    except _Offline as e:
        log.write(f"art: {game.name}: no answer ({str(e)[:120]}); asking again tomorrow")
        _write_meta(game, {"miss": time.time(), "ttl": FAIL_TTL})
        return have != before
    except Exception as e:                          # never into the UI
        log.write(f"art: {game.name}: lookup failed ({type(e).__name__}: {str(e)[:120]})")
        _write_meta(game, {"miss": time.time(), "ttl": FAIL_TTL})
        return False
    if not online():
        # switched off after the last request: no record written either
        return have != before
    if source:
        log.write(f"art: {game.name}: pictures from {source}")
        _write_meta(game, {"source": source, "when": time.time()})
    else:
        log.write(f"art: {game.name}: no match online; asking again in a week")
        _write_meta(game, {"miss": time.time(), "ttl": MISS_TTL})
    return have != before
