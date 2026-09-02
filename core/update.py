"""Self-update check.

Asks GitHub whether a newer release of this tool exists. It never replaces
itself silently - a running .exe cannot be overwritten safely, and quietly
swapping a binary is exactly the behaviour you should not trust from a tool
like this. The user gets a notice and a button that opens the release page.

The check is a single request, runs in the background, and failing is not an
error: no internet simply means no notice.
"""
from __future__ import annotations

import re
import time

from . import net, prefs

VERSION = "1.4.1"

REPO = "Kizzuwatnaa/DLSS5-Autopilot"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"


def _parse(v: str) -> tuple:
    """'v1.2.3' -> (1, 2, 3); unparseable -> (0,)"""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (0,)


CACHE_HOURS = 6


def check(force: bool = False) -> tuple[bool, str, str]:
    """(update_available, latest_version, page_url).

    The result is cached for a few hours. GitHub allows 60 anonymous API calls
    an hour per address; spending one on every single launch would eat into
    the allowance an install actually needs.

    Returns (False, VERSION, page) on any failure - a failed check must never
    block the tool.
    """
    try:
        if not force:
            seen = prefs.get("update_checked_at", 0)
            cached = prefs.get("update_latest")
            if cached and (time.time() - float(seen)) < CACHE_HOURS * 3600:
                return (_parse(cached) > _parse(VERSION), cached, RELEASES_PAGE)
    except Exception:
        pass

    try:
        rel = net.json_get(API)
        tag = rel.get("tag_name") or ""
        latest = tag.lstrip("vV")
        if not latest:
            return False, VERSION, RELEASES_PAGE
        try:
            prefs.set_("update_latest", latest)
            prefs.set_("update_checked_at", time.time())
        except Exception:
            pass
        newer = _parse(latest) > _parse(VERSION)
        return newer, latest, rel.get("html_url") or RELEASES_PAGE
    except Exception:
        return False, VERSION, RELEASES_PAGE
