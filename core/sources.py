"""Where every component is downloaded from - all in one place, auditable.

This tool never contacts a private server. It stays within these hosts:
    reshade.me
    raw.githubusercontent.com   (crosire/reshade-shaders)
    api.github.com / github.com (DLSS5-Feeder, rhi-repo, dgVoodoo2)
    codeload.github.com         (LumeniteFX)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = {"User-Agent": "dlss5-autopilot/1.3 (+local install helper)"}

RESHADE_HOME = "https://reshade.me"
RESHADE_SETUP_RE = re.compile(r"/downloads/ReShade_Setup_([\d.]+)_Addon\.exe")

RESHADE_HEADERS_BASE = "https://raw.githubusercontent.com/crosire/reshade-shaders/slim/Shaders/"
RESHADE_HEADERS = ("ReShade.fxh", "ReShadeUI.fxh", "DrawText.fxh")

FEEDER_API = "https://api.github.com/repos/jlrouzies-fr/DLSS5-Feeder/releases/latest"
# The feeder's author ships test builds as pre-releases; /latest never lists
# them. Newer add-on builds (renodx-dlss5 4.6+) are only supported by those.
FEEDER_LIST_API = "https://api.github.com/repos/jlrouzies-fr/DLSS5-Feeder/releases?per_page=15"
LUMENITE_ZIP = "https://codeload.github.com/umar-afzaal/LumeniteFX/zip/refs/heads/mainline"
RHI_API = "https://api.github.com/repos/RankFTW/rhi-repo/releases?per_page=100"
BRIDGE_API = "https://api.github.com/repos/NIGos/dlss5-bridge/releases/latest"

# None = take the newest build from the mirror. On the feeder route the pick
# is narrowed by renodx_for_feeder(): the feeder's stable release only works
# with 4.55, its pre-releases with 4.6/4.7.
RENODX_DEFAULT = None

# The last renodx-dlss5 build the feeder's STABLE release (0.7.0) accepts.
# Its README pins it: "newer builds now overlap this project and conflict".
# Support for 4.6 arrived in 0.8.0-beta.3 and for 4.7 in 0.9.0-beta.1.
FEEDER_RENODX_PIN = "4.55"


class RateLimited(RuntimeError):
    """GitHub's anonymous API allows 60 requests an hour per IP."""


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 429) and "api.github.com" in url:
            raise RateLimited(
                "GitHub is rate limiting this connection (60 anonymous API "
                "requests per hour). Wait an hour and try again, or use a VPN / "
                "different network. Downloads already in the cache still work."
            ) from e
        raise


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


def resolve_reshade() -> tuple[str, str]:
    """(version, url) of the latest ReShade add-on installer, from reshade.me."""
    html = _get(RESHADE_HOME).decode("utf8", "replace")
    m = RESHADE_SETUP_RE.search(html)
    if not m:
        raise RuntimeError("Could not find the ReShade add-on installer link on reshade.me.")
    return m.group(1), RESHADE_HOME + m.group(0)


def resolve_feeder(prerelease: bool = False) -> tuple[str, dict[str, str]]:
    """DLSS5-Feeder release: (tag, {filename: download_url}).

    `prerelease=True` takes the newest build of any kind, which is where the
    feeder's support for the newer DLSS 5 add-on generations lives. Otherwise
    the newest stable release, exactly as GitHub's /latest reports it.
    """
    if prerelease:
        rels = _json(FEEDER_LIST_API)
        rels = [r for r in rels if not r.get("draft")] if isinstance(rels, list) else []
        rel = rels[0] if rels else _json(FEEDER_API)
    else:
        rel = _json(FEEDER_API)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    return rel.get("tag_name", "?"), assets


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


def _ver_key(tag: str, prefix: str) -> tuple:
    """'dlss-310.8.0' -> (310, 8, 0), a sortable key."""
    raw = tag[len(prefix):].lstrip("-")
    nums = re.findall(r"\d+", raw)
    return tuple(int(n) for n in nums) if nums else (0,)


_CATALOG_CACHE: dict[str, list[dict]] | None = None


def rhi_catalog(force: bool = False) -> dict[str, list[dict]]:
    """Group rhi-repo releases by component family (newest first).

    Cached for the lifetime of the process: installing several games in one
    session should not burn through GitHub's anonymous API allowance.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None and not force:
        return _CATALOG_CACHE
    rels = _json(RHI_API)
    fams: dict[str, list[dict]] = {}
    for r in rels:
        tag = r.get("tag_name", "")
        for prefix, fam in (("renodx-dlss5", "renodx"),
                            ("renodx-dlss-SF", "renodx_sf"),
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
    _CATALOG_CACHE = fams
    return fams


def pick(entries: list[dict], want: str | None) -> dict:
    """Pick the entry whose label/tag matches `want`, else the newest."""
    if want:
        for e in entries:
            if e["label"] == want or e["tag"] == want:
                return e
    return entries[0]
