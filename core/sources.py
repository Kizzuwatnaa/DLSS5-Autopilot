"""Where every component is downloaded from - all in one place, auditable.

This tool never contacts a private server. It stays within these hosts:
    reshade.me
    raw.githubusercontent.com   (crosire/reshade-shaders, NVIDIA/DLSS)
    api.github.com / github.com (DLSS5-Feeder, rhi-repo, DXVK,
                                 DLSS5-Reshade-AIO, dxvk-remix-plus-dlssnr,
                                 REFramework-nightly)
    codeload.github.com         (LumeniteFX, vort_Shaders)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import http.client
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "dlss5-autopilot/1.3.1 (+local install helper)"}

RESHADE_HOME = "https://reshade.me"
RESHADE_SETUP_RE = re.compile(r"/downloads/ReShade_Setup_([\d.]+)_Addon\.exe")

RESHADE_HEADERS_BASE = "https://raw.githubusercontent.com/crosire/reshade-shaders/slim/Shaders/"
RESHADE_HEADERS = ("ReShade.fxh", "ReShadeUI.fxh", "DrawText.fxh")

FEEDER_REPO = "jlrouzies-fr/DLSS5-Feeder"
FEEDER_API = "https://api.github.com/repos/jlrouzies-fr/DLSS5-Feeder/releases/latest"
# The feeder's author ships test builds as pre-releases; /latest never lists
# them. Newer add-on builds (renodx-dlss5 4.6+) are only supported by those.
FEEDER_LIST_API = "https://api.github.com/repos/jlrouzies-fr/DLSS5-Feeder/releases?per_page=15"
LUMENITE_ZIP = "https://codeload.github.com/umar-afzaal/LumeniteFX/zip/refs/heads/mainline"
RHI_REPO = "RankFTW/rhi-repo"
RHI_API = "https://api.github.com/repos/RankFTW/rhi-repo/releases?per_page=100"
BRIDGE_API = "https://api.github.com/repos/NIGos/dlss5-bridge/releases/latest"
UPSTREAM_API = "https://api.github.com/repos/matiasLombo/neural-upstream/releases/latest"
# The file name is load-bearing: the NGX snippet only creates the feature
# for a caller whose module path contains "nvngx.dll".
UPSTREAM_ASSET = "nvngx.dll.addon64"
# GitHub's plain redirect to the newest asset. Not an API call, so it still
# works when the 60-an-hour allowance is spent and nothing is cached yet.
UPSTREAM_LATEST = ("https://github.com/matiasLombo/neural-upstream/releases/"
                   "latest/download/" + UPSTREAM_ASSET)
STANDALONE_API = ("https://api.github.com/repos/kibblerz/DLSS5-Reshade-AIO/"
                  "releases/latest")
# kibblerz's standalone add-on ships three loose assets. The plain nvngx.dll
# is its caller-identity bridge: without it beside the add-on nothing
# initialises ("required private runtime dependency missing" in its log).
STANDALONE_ASSETS = ("standalone-dlssnr.addon64", "nvngx.dll", "DLSS5_AIO_Feed.fx")
# From 2.1.0 the release is two laid-out archives instead of loose files;
# the 64-bit one holds the same three files (plus a second shader) under
# the paths a game folder uses. resolve_standalone() hands the archive back
# under this key and the installer extracts by file name.
STANDALONE_ZIP = "__zip64__"
STANDALONE_ZIP_EXTRA = ("StandaloneBoundary.fx",)
STANDALONE_REPO = "kibblerz/DLSS5-Reshade-AIO"
# v2.1.0 - v2.2.0: "DLSS5-ReShade-AIO-v2.2.0-64-bit.zip". Only used when the
# API cannot be reached and the asset list has to be guessed from the tag.
STANDALONE_ZIP_NAME = "DLSS5-ReShade-AIO-{tag}-64-bit.zip"
STANDALONE_LATEST = ("https://github.com/kibblerz/DLSS5-Reshade-AIO/releases/"
                     "latest/download/")
# Vortigern's VORT shaders (MIT). vort_Motion.fx is the optical-flow provider
# the standalone add-on schedules for real motion vectors; without it the
# add-on runs on zero-motion guides. The whole-repo zip, as with LumeniteFX:
# the .fx needs a dozen .fxh includes and a texture beside it, and codeload
# is not rate limited.
VORT_ZIP = "https://codeload.github.com/vortigern11/vort_Shaders/zip/refs/heads/main"
VORT_ZIP_NAME = "vort_Shaders-main.zip"

# lunks/dxvk-remix-plus-dlssnr: an RTX Remix runtime with the DLSS-NR stage
# built in, as a drop-in for any Remix game's .trex folder. NVIDIA's own
# runtime has no neural pass at all, so a game whose mod ships the stock
# runtime needs this before the route can do anything.
REMIX_RUNTIME_API = ("https://api.github.com/repos/lunks/"
                     "dxvk-remix-plus-dlssnr/releases/latest")
# d3d9.dll is the runtime itself; remix_nvngx.dll is the caller-identity
# bridge, and its name is load-bearing (the snippet checks the caller's
# module path for "nvngx.dll").
REMIX_RUNTIME_ASSETS = ("d3d9.dll", "remix_nvngx.dll")
REMIX_RUNTIME_LATEST = ("https://github.com/lunks/dxvk-remix-plus-dlssnr/"
                        "releases/latest/download/")
# 7-Zip's one-file console build, fetched only when neither 7-Zip nor
# Windows' tar.exe can open a .7z (#87, #93: tar.exe is built without LZMA
# on some Windows builds). Pinned by version and hash: it is unsigned, and
# it runs. 7-zip.org's own link always serves the newest build, whose hash
# would stop matching at the next release, so only the versioned one is used.
SEVEN_ZR = (("https://github.com/ip7z/7zip/releases/download/26.03/7zr.exe",),
            "ad4c82fadcbdf93c03b4fc440f300509c7d60c5c2f4d183e35d9d70d6957037d")

# None = take the newest build from the mirror. On the feeder route the pick
# is narrowed by renodx_for_feeder(): the feeder's stable release only works
# with 4.55, its pre-releases with 4.6/4.7.
RENODX_DEFAULT = None

# The last renodx-dlss5 build the feeder's STABLE release (0.7.0) accepts.
# Its README pins it: "newer builds now overlap this project and conflict".
# Support for 4.6 arrived in 0.8.0-beta.3 and for 4.7 in 0.9.0-beta.1.
FEEDER_RENODX_PIN = "4.55"

# The last renodx-dlss5 build that works on OpenGL. 4.70's fenced workset
# pool never recycles under GL - "NR workset pool exhausted; preserving game
# output" after four frames - on every OpenGL game perseval-BLR verified
# (OpenMW, ioquake3, Serious Sam, Jedi Academy, Riddick, DOOM 3 BFG).
OPENGL_RENODX_PIN = "4.60"

# NVIDIA driver 616.64 (32.0.16.1664) and 616.86 route NGX feature 18 into
# nvngx_dlssnr.dll itself, and with renodx-dlss5 4.6/4.7 every evaluate
# then faults inside D3D12Core.dll - the game renders on, no neural frame
# ever arrives. Measured by the feeder's author on an RTX 5090 (DLSS5-Feeder
# #54, 0.14.0-beta.1 notes): 4.55 passes 300/300 on the same driver, 4.7
# passes 0/300. dlss5-bridge 1.4.9 closes that route in memory; the
# renodx-dlss5 add-on cannot, so on these drivers it is pinned to 4.55.
DRIVER_FAULT_MIN = "616.64"
DRIVER_FAULT_RENODX_PIN = "4.55"

# The first DLSS5-Feeder that reaches Direct3D 10 (a private D3D11 relay
# device inside the game; nothing extra to install). Older builds refuse
# D3D10 games outright.
FEEDER_DX10_MIN = "v0.13.1-beta.1"

# The first DLSS5-Feeder that handles an HDR swapchain correctly. An HDR10
# swapchain is R10G10B10A2_UNORM carrying PQ BT.2020, which is neither of
# the two things the neural pass assumed, so on anything older the bright
# parts of an HDR picture come out blown or flat (the project's own 0.15.1
# release notes: "the neural pass was wrecking HDR highlights").
FEEDER_HDR_MIN = "v0.15.1"


class RateLimited(RuntimeError):
    """GitHub's anonymous API allows 60 requests an hour per IP."""


# 5xx and a request timeout: the server having a bad minute rather than an
# answer. Retried, and then explained - never shown as a traceback (#103).
RETRY_CODES = (500, 502, 503, 504, 408)


class Unavailable(RuntimeError):
    """A publisher's server is down or overloaded right now (5xx).

    Not our bug and not the person's: reshade.me has answered 500 (#59,
    #61) and a mirror has answered 503 "Backend.max_conn reached" (#103).
    Retried a few times before it reaches anyone, and then explained -
    never as a traceback.
    """


def latest_tag(repo: str) -> str | None:
    """The newest release's tag, without spending an API request.

    `github.com/<repo>/releases/latest` is a redirect to the tag's own page,
    and a redirect is not the API: it still answers when the 60 anonymous
    API requests an hour are gone, which is exactly when the API-less
    fallbacks below are running. Returns None when even that fails.
    """
    url = f"https://github.com/{repo}/releases/latest"
    try:
        from . import net           # net imports this module
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=30,
                                    context=net.ssl_context()) as r:
            final = r.geturl()
    except Exception:
        return None
    tag = final.rstrip("/").rsplit("/tag/", 1)
    return tag[1] if len(tag) == 2 and tag[1] else None


def _url_exists(url: str) -> bool:
    """Does this download answer? One HEAD, no body, never raises."""
    try:
        from . import net
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=30,
                                    context=net.ssl_context()) as r:
            return 200 <= getattr(r, "status", 200) < 400
    except Exception:
        return False


# api.github.com is the one host that fails on its own. It is rate limited
# per address (60 anonymous calls an hour, spent by a household or a campus
# long before this tool asks), and it is the name a filter or an inspecting
# antivirus tends to catch: #175 arrived as a certificate issued to some
# other name for api.github.com while github.com itself answered fine. The
# two facts the API is asked for - which releases exist, and what each
# one's assets are called - are on github.com's own pages, so those pages
# are the step between "the API did not answer" and "this cannot install".
RELEASES_PAGE = "https://github.com/{repo}/releases?page={page}"
EXPANDED_ASSETS = "https://github.com/{repo}/releases/expanded_assets/{tag}"
# Only the pages needed to fill the families below are read; each is about
# 400 KB, so this walks as few as it can and stops as soon as it has them.
RELEASE_PAGES_MAX = 4


def _page(url: str, timeout: int = 30) -> str:
    """One HTML page, or "" - a fallback never raises on its way to failing."""
    try:
        from . import net
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=net.ssl_context()) as r:
            return r.read().decode("utf8", "replace")
    except Exception:
        return ""


_PRE_WORDS = ("beta", "alpha", "-rc", "preview", "-pre")


def release_tags_html(repo: str, pages: int = 1) -> list[tuple[str, bool]]:
    """[(tag, is_prerelease)] newest first, from github.com, with no API call.

    The pre-release flag is read off the tag's own name rather than the
    page's badge: the badge's markup sits before some titles and after
    others, so matching it by position mislabelled half of the feeder's
    betas when this was written. Every project here numbers its test builds
    in the tag, which is the fact this needs.
    """
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for page in range(1, max(1, pages) + 1):
        html = _page(RELEASES_PAGE.format(repo=repo, page=page))
        if not html:
            break
        found = 0
        for tag in re.findall(r'/releases/tag/([^"?#]+)"', html):
            tag = urllib.parse.unquote(tag)
            if tag in seen:
                continue
            seen.add(tag)
            found += 1
            low = tag.lower()
            out.append((tag, any(w in low for w in _PRE_WORDS)))
        if not found:
            break
    return out


def release_assets_html(repo: str, tag: str) -> dict[str, str]:
    """{filename: download url} for one release, with no API call.

    github.com renders a release's asset list as its own fragment, which is
    what the release page itself fetches when the list is expanded.
    """
    html = _page(EXPANDED_ASSETS.format(repo=repo, tag=tag))
    out: dict[str, str] = {}
    for href in re.findall(r'"(/[^"]+/releases/download/[^"]+)"', html):
        name = urllib.parse.unquote(href.rsplit("/", 1)[-1])
        out.setdefault(name, "https://github.com" + href)
    return out


def _get(url: str, timeout: int = 60, attempts: int = 3) -> bytes:
    """One small read (a release listing, reshade.me's page).

    A timeout, a dropped connection or a 5xx is retried with a pause; any
    other HTTP error is the answer and is raised as it is.
    """
    req = urllib.request.Request(url, headers=UA)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            from . import net           # net imports this module
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=net.ssl_context()) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and "api.github.com" in url:
                raise RateLimited(
                    "GitHub is rate limiting this connection (60 anonymous API "
                    "requests per hour). Wait an hour and try again, or use a VPN / "
                    "different network. Downloads already in the cache still work."
                ) from e
            if e.code in RETRY_CODES:
                last = e
                if attempt < attempts - 1:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                host = url.split("/")[2] if "/" in url else url
                raise Unavailable(
                    f"{host} is not answering right now (HTTP {e.code}). "
                    f"That is the server this list is published on, not your "
                    f"connection and not this tool - it was asked "
                    f"{attempts} times. Wait a few minutes and try again; "
                    f"anything already downloaded is cached and will not be "
                    f"fetched twice.") from e
            raise
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException) as e:
            # HTTPException: the body dropped after the headers (IncompleteRead)
            last = e
            if attempt == attempts - 1:
                from . import net
                if net.untrusted(url.split("/")[2], e):
                    raise net.untrusted(url.split("/")[2], e) from e
                raise
            time.sleep(2.0 * (attempt + 1))
    raise last if last else RuntimeError(url)


# GitHub allows 60 anonymous API calls an hour per address. That is easy to
# exhaust, and being unable to install anything because of it is unacceptable -
# so every API answer is kept on disk and reused when the live call fails.
_API_CACHE = Path(os.environ.get("LOCALAPPDATA", Path.home())) \
    / "dlss5-autopilot" / "api-cache"
_API_FRESH_SECONDS = 6 * 3600

# Set by _json when it had to fall back to a stale copy, so the installer can
# tell the user why the version list might be out of date.
last_fallback: str | None = None


def _cache_path(url: str) -> Path:
    return _API_CACHE / (hashlib.sha256(url.encode("utf8")).hexdigest()[:32] + ".json")


def cached_json(url: str):
    """Whatever is in the cache for this URL, of any age, or None.

    For the preview, which promises not to make a single request: it may
    look at what an earlier install fetched, and must simply know less when
    nothing has been fetched yet.
    """
    try:
        return json.loads(_cache_path(url).read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return None


def _json(url: str):
    """Fetch JSON, backed by an on-disk cache.

    Fresh cache is used without a request at all. If the request fails - rate
    limit, no connection - a stale cache of any age is used rather than
    failing the install outright.
    """
    global last_fallback
    p = _cache_path(url)
    try:
        age = time.time() - p.stat().st_mtime
        if age < _API_FRESH_SECONDS:
            return json.loads(p.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        pass

    try:
        raw = _get(url).decode("utf8")
        data = json.loads(raw)
        try:
            _API_CACHE.mkdir(parents=True, exist_ok=True)
            p.write_text(raw, encoding="utf8")
        except OSError:
            pass
        return data
    except Exception as original:
        try:
            data = json.loads(p.read_text(encoding="utf8"))
            age_h = int((time.time() - p.stat().st_mtime) / 3600)
        except (OSError, json.JSONDecodeError):
            # Nothing cached to fall back to: report why the LIVE call failed,
            # not the missing cache file - that would be a misleading error.
            raise original from None
        last_fallback = (f"GitHub could not be reached (rate limit or no "
                         f"connection); using the version list cached "
                         f"{age_h}h ago.")
        return data


RESHADE_TAGS_API = "https://api.github.com/repos/crosire/reshade/tags?per_page=5"


def _reshade_url(version: str) -> str:
    return f"{RESHADE_HOME}/downloads/ReShade_Setup_{version}_Addon.exe"


def resolve_reshade() -> tuple[str, str]:
    """(version, url) of the latest ReShade add-on installer.

    reshade.me first. The site answers 500 to about every other request on
    some days, with the full page as the body, so a 5xx with the link in it
    still counts and a bare 5xx is retried. Then the release tag on GitHub
    (crosire/reshade publishes tags, not release assets - the exe only lives
    on reshade.me, but its URL follows the version). Then the newest setup
    already in the cache, so an install on a machine that has done one
    before does not depend on the site at all.
    """
    errors: list[str] = []
    for attempt in range(3):
        try:
            html = _get(RESHADE_HOME).decode("utf8", "replace")
        except urllib.error.HTTPError as e:
            try:
                html = e.read().decode("utf8", "replace")
            except Exception:
                html = ""
            errors.append(f"reshade.me: HTTP {e.code}")
        except Exception as e:
            errors.append(f"reshade.me: {e}")
            html = ""
        m = RESHADE_SETUP_RE.search(html)
        if m:
            return m.group(1), RESHADE_HOME + m.group(0)
        time.sleep(0.5 * (attempt + 1))
    try:
        tags = _json(RESHADE_TAGS_API)
        for t in (tags if isinstance(tags, list) else []):
            name = str(t.get("name", ""))
            if re.fullmatch(r"v\d+(\.\d+)+", name):
                return name[1:], _reshade_url(name[1:])
        errors.append("GitHub: no version tag on crosire/reshade")
    except Exception as e:
        errors.append(f"GitHub tags: {e}")
    try:
        from . import net
        cached = sorted(net.cache_dir().glob("ReShade_Setup_*_Addon.exe"),
                        key=lambda p: [int(x) for x in p.name.split("_")[2].split(".")])
        if cached:
            ver = cached[-1].name.split("_")[2]
            return ver, _reshade_url(ver)
    except Exception as e:
        errors.append(f"cache: {e}")
    raise RuntimeError("Could not find the ReShade add-on installer: "
                       + "; ".join(errors))


def feeder_releases() -> list[tuple[str, bool]]:
    """Every feeder release GitHub lists, newest first: (tag, is_prerelease).

    The version dropdown is built from this, so a build that broke a game can
    be swapped for the one before it without leaving the tool.
    """
    try:
        rels = _json(FEEDER_LIST_API)
    except Exception:
        # The dropdown is not worth failing an install for: github.com's own
        # release page lists the same tags without the API (#175).
        return release_tags_html(FEEDER_REPO, pages=2)[:15]
    if not isinstance(rels, list):
        return []
    return [(r.get("tag_name", "?"), bool(r.get("prerelease")))
            for r in rels if not r.get("draft") and r.get("tag_name")]


def resolve_feeder(prerelease: bool = False, tag: str = "") -> tuple[str, dict[str, str]]:
    """DLSS5-Feeder release: (tag, {filename: download_url}).

    `tag` pins one exact release. Otherwise `prerelease=True` takes the newest
    build of any kind, which is where the feeder's support for the newer
    DLSS 5 add-on generations lives, and the default is the newest stable
    release, exactly as GitHub's /latest reports it.
    """
    global last_fallback
    try:
        if tag:
            rels = _json(FEEDER_LIST_API)
            rel = next((r for r in (rels if isinstance(rels, list) else [])
                        if r.get("tag_name") == tag), None)
            if rel is None:
                raise _NoSuchTag(tag)
        elif prerelease:
            rels = _json(FEEDER_LIST_API)
            rels = [r for r in rels if not r.get("draft")] if isinstance(rels, list) else []
            rel = rels[0] if rels else _json(FEEDER_API)
        else:
            rel = _json(FEEDER_API)
    except _NoSuchTag:
        raise RuntimeError(f"DLSS5-Feeder release {tag} is not on GitHub's "
                           f"release list (the newest 15 are checked).") from None
    except Exception:
        # The API is the only host that fails on its own - rate limited, or
        # answered by a filter (#175). github.com lists the same releases and
        # the same assets, so the route stays installable without it.
        want, assets = _feeder_html(prerelease, tag)
        if not assets:
            raise
        last_fallback = ("GitHub's API could not be reached; the feeder "
                         "release was read from github.com's release pages "
                         "instead.")
        return want, assets
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    return rel.get("tag_name", "?"), assets


class _NoSuchTag(RuntimeError):
    """A pinned tag the API answered about and does not have.

    Its own type so the fallback below does not treat "this release does not
    exist" as "the API is unreachable" and go looking for it on github.com.
    """


def _feeder_html(prerelease: bool, tag: str) -> tuple[str, dict[str, str]]:
    """The same answer as resolve_feeder, off github.com's pages."""
    tags = release_tags_html(FEEDER_REPO, pages=2)
    if tag:
        want = tag if any(t == tag for t, _ in tags) else ""
    elif prerelease:
        want = tags[0][0] if tags else ""
    else:
        want = latest_tag(FEEDER_REPO) or next(
            (t for t, pre in tags if not pre), "")
    if not want:
        return "", {}
    return want, release_assets_html(FEEDER_REPO, want)


def feeder_key(tag: str) -> tuple:
    """'v0.9.0-beta.1' -> (0, 9, 0, 1, 1): sortable, betas below the release.

    A plain release of the same number sorts above any of its betas, which is
    how the project numbers them.
    """
    nums = [int(n) for n in re.findall(r"\d+", tag or "")]
    base = tuple(nums[:3]) + (0,) * (3 - len(nums[:3]))
    if "beta" in (tag or "").lower():
        return base + (0, nums[3] if len(nums) > 3 else 0)
    return base + (1, 0)


def renodx_for_feeder(feeder_tag: str) -> str | None:
    """Which renodx-dlss5 build a given feeder release accepts.

    None means "the newest is fine". Anything below 0.8.0-beta.3 is pinned to
    4.55 - that is the mismatch behind CreateFeature 0xC0000005 crashes on
    otherwise correct feeder installs.
    """
    if feeder_key(feeder_tag) < feeder_key("v0.8.0-beta.3"):
        return FEEDER_RENODX_PIN
    return None


def resolve_bridge() -> tuple[str, str]:
    """Latest dlss5-bridge release: (tag, addon download url)."""
    rel = _json(BRIDGE_API)
    for a in rel.get("assets", []):
        if a["name"].lower().endswith(".addon64"):
            return rel.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("The dlss5-bridge release has no .addon64 asset.")


def resolve_upstream() -> tuple[str, str]:
    """Latest neural-upstream release: (tag, addon download url).

    Falls back to the "latest" redirect when the API is out of reach and
    nothing is cached: the route must not be unavailable just because this
    machine has spent its anonymous allowance on the other components.
    """
    try:
        rel = _json(UPSTREAM_API)
    except Exception:
        return "latest", UPSTREAM_LATEST
    for a in rel.get("assets", []):
        if a["name"].lower() == UPSTREAM_ASSET:
            return rel.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError(f"The neural-upstream release has no {UPSTREAM_ASSET} asset.")


def resolve_standalone() -> tuple[str, dict[str, str]]:
    """Latest DLSS5-Reshade-AIO release: (tag, {filename: url}).

    The three release assets plus the VORT shader zip. Same fallback as
    neural-upstream: with the API out of reach and nothing cached, GitHub's
    "latest" download redirect still resolves each asset by name.
    """
    urls = {VORT_ZIP_NAME: VORT_ZIP}
    try:
        rel = _json(STANDALONE_API)
    except Exception:
        # From 2.2.0 the loose files are gone from the release: the three
        # names below now answer 404, so the old fallback failed the install
        # for exactly the people it was written for - the rate-limited ones.
        # The archive's name carries the tag, and the tag comes out of a
        # redirect rather than the API.
        tag = latest_tag(STANDALONE_REPO)
        if tag:
            guess = (f"https://github.com/{STANDALONE_REPO}/releases/download/"
                     f"{tag}/{STANDALONE_ZIP_NAME.format(tag=tag)}")
            # The archive's name is a guess - the publisher has renamed
            # things before, which is the whole reason this branch exists.
            # Ask before committing the install to it; a 404 here still has
            # the loose files below to fall back on.
            if _url_exists(guess):
                urls[STANDALONE_ZIP] = guess
                return tag, urls
        # Older releases still carry the loose files; nothing else is left.
        for name in STANDALONE_ASSETS:
            urls[name] = STANDALONE_LATEST + name
        return "latest", urls
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    missing = [n for n in STANDALONE_ASSETS if n not in assets]
    if missing:
        zip64 = next((u for n, u in assets.items()
                      if n.lower().endswith("-64-bit.zip")), None)
        if zip64:
            urls[STANDALONE_ZIP] = zip64
            return rel.get("tag_name", "?"), urls
        raise RuntimeError("The DLSS5-Reshade-AIO release is missing "
                           f"{', '.join(missing)} and has no 64-bit archive.")
    for name in STANDALONE_ASSETS:
        urls[name] = assets[name]
    return rel.get("tag_name", "?"), urls


def resolve_remix_runtime() -> tuple[str, dict[str, str]]:
    """Latest dxvk-remix-plus-dlssnr release: (tag, {filename: url}).

    Same fallback as neural-upstream and the standalone add-on: with the API
    rate limited and nothing cached, GitHub's "latest" download redirect
    still resolves each asset by name, so the route stays available.
    """
    try:
        rel = _json(REMIX_RUNTIME_API)
    except Exception:
        return "latest", {n: REMIX_RUNTIME_LATEST + n
                          for n in REMIX_RUNTIME_ASSETS}
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    missing = [n for n in REMIX_RUNTIME_ASSETS if n not in assets]
    if missing:
        raise RuntimeError("The dxvk-remix-plus-dlssnr release is missing "
                           f"{', '.join(missing)}.")
    return rel.get("tag_name", "?"), {n: assets[n] for n in REMIX_RUNTIME_ASSETS}


def _ver_key(tag: str, prefix: str) -> tuple:
    """'dlss-310.8.0' -> (310, 8, 0), a sortable key."""
    raw = tag[len(prefix):].lstrip("-")
    nums = re.findall(r"\d+", raw)
    return tuple(int(n) for n in nums) if nums else (0,)


_CATALOG_CACHE: dict[str, list[dict]] | None = None

# Longest prefix first: "dlssnr-310.8.0" and "dlssg-310.9.1" both start with
# "dlss", so the bare "dlss-" family has to be the last thing tried.
RHI_FAMILIES = (("renodx-dlss5", "renodx"),
                ("renodx-dlss-SF", "renodx_sf"),
                ("dlssnr", "dlssnr"),
                ("dlssg", "dlssg"),
                ("dlss-", "dlss"))
# How many builds per family the API-free walk collects. The dropdowns show
# more when the API answers; this is enough to install with and to step back
# one build when the newest breaks a game.
RHI_HTML_PER_FAMILY = 3
# Builds the installer pins by name. A shortened list that happens to leave
# one of these out is worse than no list at all: pick() falls back to the
# newest, so "pinned to 4.55" would quietly install 5.2.1 - the build that
# faults on every evaluate on the very drivers the pin exists for. They are
# kept whatever the cap says.
RHI_HTML_REQUIRED = {"renodx": (FEEDER_RENODX_PIN, OPENGL_RENODX_PIN,
                                DRIVER_FAULT_RENODX_PIN)}


def _rhi_html_catalog() -> dict[str, list[dict]]:
    """rhi_catalog's answer built from github.com's pages, or {}.

    One page listing per 10 releases and one asset fragment per release kept,
    so it walks as little as it can: it stops as soon as every family has
    something and never reads more than RELEASE_PAGES_MAX pages.
    """
    fams: dict[str, list[dict]] = {}
    for page in range(1, RELEASE_PAGES_MAX + 1):
        tags = release_tags_html(RHI_REPO, pages=1) if page == 1 else None
        if tags is None:
            html = _page(RELEASES_PAGE.format(repo=RHI_REPO, page=page))
            tags = [(urllib.parse.unquote(t), False)
                    for t in dict.fromkeys(
                        re.findall(r'/releases/tag/([^"?#]+)"', html))]
        if not tags:
            break
        for tag, _pre in tags:
            fam = next((f for pre, f in RHI_FAMILIES if tag.startswith(pre)),
                       None)
            if fam is None:
                continue
            prefix = next(pre for pre, f in RHI_FAMILIES if f == fam)
            label = tag[len(prefix):].lstrip("-") or tag
            if (len(fams.get(fam, [])) >= RHI_HTML_PER_FAMILY
                    and label not in RHI_HTML_REQUIRED.get(fam, ())):
                continue
            assets = release_assets_html(RHI_REPO, tag)
            url = next((u for n, u in assets.items()
                        if n.lower().endswith(".zip")), None)
            if not url:
                continue
            fams.setdefault(fam, []).append({
                "tag": tag,
                "label": label,
                "url": url,
                "size": 0,
                "key": _ver_key(tag, prefix.rstrip("-")),
            })
        have = {e["label"] for e in fams.get("renodx", [])}
        if (all(fams.get(f) for _p, f in RHI_FAMILIES)
                and all(v in have for v in RHI_HTML_REQUIRED["renodx"])):
            break
    for entries in fams.values():
        entries.sort(key=lambda d: d["key"], reverse=True)
    return fams


def rhi_catalog(force: bool = False) -> dict[str, list[dict]]:
    """Group rhi-repo releases by component family (newest first).

    Cached for the lifetime of the process: installing several games in one
    session should not burn through GitHub's anonymous API allowance.
    """
    global _CATALOG_CACHE, last_fallback
    if _CATALOG_CACHE is not None and not force:
        return _CATALOG_CACHE
    try:
        rels = _json(RHI_API)
    except Exception:
        # Every route needs a build from here, so this is the one list whose
        # loss takes the whole tool down with it. github.com's release pages
        # carry the same tags and the same assets (#175).
        fams = _rhi_html_catalog()
        if not fams:
            raise
        last_fallback = ("GitHub's API could not be reached; the build list "
                         "was read from github.com's release pages instead. "
                         "It is shorter than usual - every build the tool "
                         "pins is in it.")
        for fam, entries in nvidia_dlss().items():
            fams[fam] = entries + fams.get(fam, [])
        _CATALOG_CACHE = fams
        return fams
    fams: dict[str, list[dict]] = {}
    for r in rels:
        tag = r.get("tag_name", "")
        for prefix, fam in RHI_FAMILIES:
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
    # NVIDIA publishes the super-resolution, ray-reconstruction and frame
    # generation runtimes itself. The tool was taking two of the three from
    # a community mirror while the publisher shipped them itself, current
    # and under a licence - so the publisher's builds go on the front of
    # each list, and the mirror's stay behind them.
    for fam, entries in nvidia_dlss().items():
        fams[fam] = entries + fams.get(fam, [])
    _CATALOG_CACHE = fams
    return fams


# NVIDIA's own SDK. The DLLs live in the repository tree rather than in a
# release asset, so they are read at the tag - which is what makes the
# version in the label true. Neural rendering is NOT among them: the SDK
# carries super resolution, ray reconstruction and frame generation only.
NVIDIA_DLSS_REPO = "NVIDIA/DLSS"
NVIDIA_DLSS_DIR = "lib/Windows_x86_64/rel"
NVIDIA_DLSS_FILES = (("dlss", "nvngx_dlss.dll"),
                     ("dlssd", "nvngx_dlssd.dll"),
                     ("dlssg", "nvngx_dlssg.dll"))
_NVIDIA_CACHE: dict[str, list[dict]] | None = None


def nvidia_dlss() -> dict[str, list[dict]]:
    """NVIDIA's published runtimes as catalog entries, or {} when unreachable.

    The tag comes from the releases redirect, so this costs no API request -
    it works on the same connection that is already rate limited.
    """
    global _NVIDIA_CACHE
    if _NVIDIA_CACHE:
        return _NVIDIA_CACHE
    tag = latest_tag(NVIDIA_DLSS_REPO)
    if not tag:
        # Not cached: a redirect that failed once (a blip, a proxy waking
        # up) must not take the publisher's builds out of the list for the
        # rest of the session.
        return {}
    out: dict[str, list[dict]] = {}
    # The tag is the SDK's, and one SDK ships all three runtimes; NVIDIA
    # versions them separately, so the label says which SDK rather than
    # claiming to be each file's own version.
    label = f"{tag.lstrip('vV')} (NVIDIA SDK)"
    for fam, name in NVIDIA_DLSS_FILES:
        out[fam] = [{
            "tag": tag,
            "label": label,
            "url": (f"https://raw.githubusercontent.com/{NVIDIA_DLSS_REPO}/"
                    f"{tag}/{NVIDIA_DLSS_DIR}/{name}"),
            "size": 0,
            # Not an archive: the file IS the download.
            "raw": name,
            "key": _ver_key(tag, ""),
        }]
    _NVIDIA_CACHE = out
    return out


def pick(entries: list[dict], want: str | None) -> dict:
    """Pick the entry whose label/tag matches `want`, else the newest."""
    if want:
        for e in entries:
            if e["label"] == want or e["tag"] == want:
                return e
    return entries[0]
