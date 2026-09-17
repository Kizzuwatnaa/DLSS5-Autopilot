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
  architecture, the driver, this tool's version, the outcome, and - where
  the session was measured - the work area it ran at, the milliseconds a frame
  spent on what grows with that area, and the frame rate. No paths, no user name,
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
# A measurement is not a verdict: it is one number with its own count
# printed beside it, so three of them can be shown where three outcomes
# could not. Below this it is one person's machine, said as if it were a
# setting for the game.
MIN_MEASURED = 3
MARKER = "autopilot-report"


def record(game, route: str, result: str, *, api: str = "", build: str = "",
           gpu_sm=None, gpu_name: str = "", driver: str = "",
           version: str = "", measured: dict | None = None) -> dict:
    """The one block a shared result carries. Nothing identifying in it.

    `measured` is what the session cost - the work area it ran at, the
    milliseconds a frame spent on what grows with that area, the frame
    rate - as autotune.shared() returns
    it. Three numbers about a game, and nothing about a machine beyond
    the card that is named here anyway. The keys are taken one at a time
    rather than merged wholesale, so a future caller cannot widen what
    leaves this machine by handing in a bigger dict.
    """
    exe = getattr(getattr(game, "exe", None), "name", "") or ""
    rec = {
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
    for key in ("res", "ms", "fps"):
        value = (measured or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # The work area is a whole percent - "a 75.0% work area" in a
            # published list reads as a precision nobody has.
            rec[key] = int(value) if key == "res" else round(float(value), 2)
    return rec


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


def measured_note(entry: dict | None, route: str = "") -> str:
    """What this game really cost other people, or "".

    A rate answers "does this route work here". The question directly
    after it is "and what do I set the work area to", which everybody
    has been answering by feel - including this tool, which measured the
    answer on one machine and left it there.
    """
    rows = (entry or {}).get("measured") or {}
    if not isinstance(rows, dict):
        return ""

    def count(row) -> int:
        n = row.get("n") if isinstance(row, dict) else None
        return int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 0

    pick = None
    if route and isinstance(rows.get(route), dict):
        pick = (route, rows[route])
    else:
        ranked = sorted(((n, r) for n, r in rows.items()
                         if isinstance(r, dict)),
                        key=lambda x: count(x[1]), reverse=True)
        pick = ranked[0] if ranked else None
    if not pick:
        return ""
    name, row = pick
    n, res = count(row), row.get("res")
    if n < MIN_MEASURED or not isinstance(res, (int, float)) or not res:
        return ""
    # `n` counts the results that said which work area they ran at; the
    # cost and the frame rate are medians over whichever of those carried
    # them, which can be fewer. So the count is attached to the work area,
    # and the other two are clauses that do not inherit it.
    line = (f"{n} shared results where this game worked on the {name} "
            f"route say what they ran at: a {int(res)}% work area")
    def number(key):
        v = row.get(key)
        return (float(v) if isinstance(v, (int, float))
                and not isinstance(v, bool) and v else None)

    ms, fps = number("ms"), number("fps")
    if ms and name == "feeder":
        # The feeder logs no model cost. Its number is solved from frame
        # rates at two work areas, so it is everything that grows with the
        # area - the model, the feed and its shaders - and only an upper
        # bound on the model's own share.
        line += (f". The model and the feed together cost about {ms:.1f} ms "
                 f"a frame there")
    elif ms:
        line += f". The model cost about {ms:.1f} ms a frame there"
    if fps:
        line += f", at around {fps:.0f} fps"
    return line + "."


def totals(data: dict) -> dict:
    """Every game's numbers added up: {"routes": {...}, "drivers": {...}}.

    The published file is keyed by game, so a game nobody has reported - the
    usual case - got no sentence at all, and the numbers that ARE known
    across the whole set (which routes work at all, which drivers fail
    everywhere) were never said to anybody. They are worth saying: the
    feeder route works in about a quarter of the games it is tried in, and
    that is a fact somebody choosing a route should have.
    """
    out = {"routes": {}, "drivers": {}, "games": 0, "reports": 0}
    for entry in (data.get("games") or {}).values():
        if not isinstance(entry, dict):
            continue
        out["games"] += 1
        for kind in ("routes", "drivers"):
            for name, r in (entry.get(kind) or {}).items():
                if not isinstance(r, dict):
                    continue
                got = out[kind].setdefault(name, {"worked": 0, "failed": 0})
                got["worked"] += int(r.get("worked", 0) or 0)
                got["failed"] += int(r.get("failed", 0) or 0)
    out["reports"] = sum(r["worked"] + r["failed"]
                         for r in out["routes"].values())
    return out


def rate(counts: dict) -> tuple[int, int]:
    """(worked, tried) for one {"worked": n, "failed": n} entry."""
    w = int((counts or {}).get("worked", 0) or 0)
    f = int((counts or {}).get("failed", 0) or 0)
    return w, w + f


def driver_note(data: dict, driver: str) -> str:
    """What this driver does across every game, or "".

    Said with its denominator. "616.92 faults" is a rumour; "7 of the 25
    reports on 616.92 worked, against 7 of 11 on 616.56" is the reason to
    roll back, and it is the tool's own shared data saying it.
    """
    if not driver:
        return ""
    all_ = totals(data).get("drivers") or {}
    w, n = rate(all_.get(driver))
    if n < MIN_REPORTS:
        return ""
    best = max(((d, *rate(c)) for d, c in all_.items() if rate(c)[1] >= MIN_REPORTS),
               key=lambda x: (x[1] / x[2] if x[2] else 0), default=None)
    line = f"On driver {driver}, {w} of {n} shared results worked."
    if best and best[0] != driver and best[1] * n > w * best[2]:
        line += (f" The best reported driver is {best[0]}: {best[1]} of "
                 f"{best[2]}.")
    return line


def next_route(data: dict, game, tried: str, offer: list[str] | None = None) -> str:
    """One sentence naming the route to try next, or "".

    After a route has failed the tool used to say nothing about what else to
    do - which is why, over 47 games and 60 shared results, not one row
    reads "this route rescued a game another could not". Nobody was ever
    told to try the second one, so the data that would prove a route worth
    recommending cannot come into being. This is the sentence that starts it.
    """
    if not tried:
        return ""
    allowed = {r for r in (offer or []) if r} or None
    entry = for_game(data, game)
    routes = (entry or {}).get("routes") or {}
    # This game first, when anybody has reported it: a route that worked
    # HERE beats any general rate.
    here = [(n, *rate(r)) for n, r in routes.items()
            if n != tried and rate(r)[0] and (allowed is None or n in allowed)]
    if here:
        here.sort(key=lambda x: (x[1], x[1] / x[2] if x[2] else 0), reverse=True)
        n, w, t = here[0]
        return (f"In this game the {n} route is reported working by {w} of "
                f"{t}. Try that next - pick it in the route dropdown and "
                f"install again.")
    # Otherwise the whole set, and only where there is enough of it.
    all_ = totals(data).get("routes") or {}
    mine_w, mine_n = rate(all_.get(tried))
    ranked = [(n, *rate(c)) for n, c in all_.items()
              if n != tried and rate(c)[1] >= MIN_REPORTS
              and (allowed is None or n in allowed)]
    if not ranked:
        return ""
    ranked.sort(key=lambda x: x[1] / x[2] if x[2] else 0, reverse=True)
    n, w, t = ranked[0]
    if mine_n and w * mine_n <= mine_w * t:
        return ""                    # nothing on offer does better
    return (f"Nobody has reported this game yet. Across every game shared, "
            f"the {n} route worked in {w} of {t} tries"
            + (f", against {mine_w} of {mine_n} for {tried}" if mine_n else "")
            + " - it is the next one to try.")


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
            + (f"- measured: {rec.get('res')}% work area"
               + ((f", {rec.get('ms')} ms a frame for the model and feed "
                   f"together" if rec.get("route") == "feeder"
                   else f", {rec.get('ms')} ms of model a frame")
                  if rec.get("ms") else "")
               + (f", {rec.get('fps')} fps" if rec.get("fps") else "")
               + "\n" if rec.get("res") else "")
            + "\nThe block below is what the compatibility list reads. Once "
            "a game has five results, the next person with it is told which "
            "route worked most often on it; three results on the same route "
            "that worked and carried a measurement tell them what those "
            "sessions ran at. "
            "Delete it if you would rather not share it - the rest of the "
            "report still stands."
            + block(rec))
    return (f"https://github.com/{update.REPO}/issues/new"
            f"?labels=result&title={quote(title)}&body={quote(body)}")


__all__ = ["record", "block", "parse", "fetch", "for_game", "advice",
           "measured_note", "issue_url", "FEED_URL", "MIN_REPORTS",
           "MIN_MEASURED", "MARKER"]
