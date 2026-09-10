"""What happened to everybody else in this game, before you install.

The tool decides a route from the executable alone: what it imports, what
ships beside it, how the engine behaves. That is a good guess about one
machine. It is not what a thousand machines actually found - and the answer
to "will this work in Red Dead Redemption 2" has been sitting in the issue
tracker the whole time, in somebody's head, instead of in the tool.

How it works, with no server anywhere:

* People press "it worked" / "it did not" after playing. That opens a
  GitHub issue - the same way the bug report does - carrying one machine
  readable block and nothing that identifies them: the game's name and
  executable, the route, the build, the graphics API, the card's model and
  architecture, the driver, this tool's version, and the outcome. No paths, no user name,
  no machine id, and nothing at all leaves without the button being pressed.
* A workflow in the repository adds those up into `docs/compatibility.json`.
* Every tool downloads that file and reads it before an install.

So the write side is a browser window the person can read and cancel, and
the read side is one static file. Nothing here phones home.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import net

# The aggregate the workflow publishes. raw.githubusercontent, not the API:
# no rate limit worth speaking of, and it keeps working when the 60
# anonymous API requests an hour are gone.
FEED_URL = ("https://raw.githubusercontent.com/Kizzuwatnaa/DLSS5-Autopilot/"
            "main/docs/compatibility.json")
CACHE_NAME = "compatibility.json"
FRESH_SECONDS = 6 * 3600
# Below this many reports a line about "what worked for others" is noise
# dressed as knowledge - two people are not a finding.
MIN_REPORTS = 5
MARKER = "autopilot-report"


def record(game, route: str, result: str, *, api: str = "", build: str = "",
           gpu_sm=None, gpu_name: str = "", driver: str = "",
           version: str = "") -> dict:
    """The one block a shared result carries. Nothing identifying in it."""
    exe = getattr(getattr(game, "exe", None), "name", "") or ""
    return {
        "v": 1,
        "exe": exe.lower(),
        "game": (getattr(game, "name", "") or "")[:80],
        "api": (api or getattr(game, "api", "") or "").upper(),
        "route": route or "",
        "build": build or "",
        "sm": f"sm_{gpu_sm}" if gpu_sm else "",
        "gpu": gpu_name[:40],
        "driver": driver or "",
        "tool": version or "",
        "result": result,          # "worked" | "failed"
    }


def block(rec: dict) -> str:
    """The record as it goes into an issue: visible, and parseable."""
    return (f"\n\n<!-- {MARKER}\n"
            + json.dumps(rec, separators=(",", ":"), sort_keys=True)
            + f"\n{MARKER} -->\n")


def parse(text: str) -> dict | None:
    """Read a record back out of an issue body. Used by the workflow."""
    start = text.find(f"<!-- {MARKER}")
    if start < 0:
        return None
    end = text.find(f"{MARKER} -->", start + 1)
    if end < 0:
        return None
    raw = text[start + len(f"<!-- {MARKER}"):end].strip()
    try:
        rec = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict) or rec.get("v") != 1:
        return None
    if rec.get("result") not in ("worked", "failed"):
        return None
    return rec


def _cache() -> Path:
    return net.cache_dir() / CACHE_NAME


def fetch(force: bool = False) -> dict:
    """The published aggregate, cached on disk. {} when it cannot be had.

    Never raises and never blocks an install: this is advice, and advice
    that fails to download is simply not shown.
    """
    p = _cache()
    try:
        if not force and time.time() - p.stat().st_mtime < FRESH_SECONDS:
            return json.loads(p.read_text(encoding="utf8"))
    except (OSError, ValueError):
        pass
    try:
        raw = net.fetch_text(FEED_URL).decode("utf8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(raw, encoding="utf8")
        except OSError:
            pass
        return data
    except Exception:
        try:
            return json.loads(p.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {}


def for_game(data: dict, game) -> dict | None:
    """The entry for this game, keyed by executable name."""
    exe = getattr(getattr(game, "exe", None), "name", "") or ""
    if not exe:
        return None
    return (data.get("games") or {}).get(exe.lower())


def advice(entry: dict | None, route: str = "", driver: str = "") -> list[str]:
    """Plain lines about what other people found. Empty when there is no
    finding - a route with three reports behind it gets no sentence."""
    if not entry:
        return []
    out: list[str] = []
    routes = entry.get("routes") or {}
    total = sum((r.get("worked", 0) + r.get("failed", 0))
                for r in routes.values() if isinstance(r, dict))
    if total < MIN_REPORTS:
        return []

    ranked = sorted(
        ((name, r.get("worked", 0), r.get("failed", 0))
         for name, r in routes.items() if isinstance(r, dict)),
        key=lambda x: (x[1], -x[2]), reverse=True)
    best = ranked[0] if ranked else None
    if best and best[1]:
        out.append(f"{total} reports for this game. "
                   f"The {best[0]} route worked in {best[1]} of them"
                   + (f" and failed in {best[2]}" if best[2] else "") + ".")
    else:
        out.append(f"{total} reports for this game, and no route is "
                   f"reported working.")
    if route and route in routes:
        mine = routes[route]
        w, f = mine.get("worked", 0), mine.get("failed", 0)
        if f and not w:
            out.append(f"The route chosen here ({route}) failed in all "
                       f"{f} of its reports.")
        elif best and best[0] != route and best[1] > (w or 0):
            out.append(f"The route chosen here ({route}) worked in "
                       f"{w} of its reports.")

    bad = (entry.get("drivers") or {}).get(driver or "")
    if isinstance(bad, dict):
        w, f = bad.get("worked", 0), bad.get("failed", 0)
        if f >= MIN_REPORTS and f > w * 2:
            # With no denominator this read as though it were counting all
            # the reports, and a driver's own tally can be larger than any
            # one route's. Say what it is a share of.
            allf = sum(int((r or {}).get("failed", 0) or 0)
                       for r in routes.values() if isinstance(r, dict))
            out.append(f"{f} of the {allf} failures reported here were on "
                       f"driver {driver}."
                       if allf >= f else
                       f"{f} failures reported here were on driver {driver}.")
    return out


def issue_url(rec: dict, note: str = "") -> str:
    """A pre-filled issue that carries the record. Nothing is posted by the
    tool itself - the person reads it in their browser and decides."""
    from urllib.parse import quote
    from . import update
    verb = "worked" if rec.get("result") == "worked" else "did not work"
    title = f"result: {rec.get('game') or rec.get('exe') or '?'} - {verb}"
    body = (f"**{rec.get('game') or rec.get('exe')}** {verb} on the "
            f"`{rec.get('route')}` route.\n\n"
            + (note.strip() + "\n\n" if note.strip() else "")
            + f"- api: {rec.get('api') or '-'}\n"
            f"- build: {rec.get('build') or '-'}\n"
            f"- gpu: {rec.get('gpu') or '-'} ({rec.get('sm') or '-'}), "
            f"driver {rec.get('driver') or '-'}\n"
            f"- tool: {rec.get('tool') or '-'}\n"
            "\nThe block below is what the compatibility list reads, so "
            "the next person with this game is told what happened here. "
            "Delete it if you would rather not share it - the rest of the "
            "report still stands."
            + block(rec))
    return (f"https://github.com/{update.REPO}/issues/new"
            f"?labels=result&title={quote(title)}&body={quote(body)}")


__all__ = ["record", "block", "parse", "fetch", "for_game", "advice",
           "issue_url", "FEED_URL", "MIN_REPORTS", "MARKER"]
