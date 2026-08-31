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

from . import net

VERSION = "1.1.0"

REPO = "Kizzuwatnaa/DLSS5-Autopilot"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"


def _parse(v: str) -> tuple:
    """'v1.2.3' -> (1, 2, 3); unparseable -> (0,)"""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def check() -> tuple[bool, str, str]:
    """(update_available, latest_version, page_url).

    Returns (False, VERSION, page) on any failure - a missing check must never
    block the tool.
    """
    try:
        rel = net.json_get(API)
        tag = rel.get("tag_name") or ""
        latest = tag.lstrip("vV")
        if not latest:
            return False, VERSION, RELEASES_PAGE
        newer = _parse(latest) > _parse(VERSION)
        return newer, latest, rel.get("html_url") or RELEASES_PAGE
    except Exception:
        return False, VERSION, RELEASES_PAGE
