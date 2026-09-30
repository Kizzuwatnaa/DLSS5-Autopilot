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


def route_name(route: str) -> str:
    """The name the route dropdown shows: the shared results keep the short
    keys ("standalone", "renodx", "upstream"), and a sentence saying "the
    standalone route" sent people looking for an entry the list calls
    standalone-dlssnr (gate 2.0.6)."""
    try:
        from . import dlss
        return dlss.LABELS[route].split(" - ")[0]
    except Exception:
        return str(route)

# The aggregate the workflow publishes. raw.githubusercontent, not the API:
# no rate limit worth speaking of, and it keeps working when the 60
# anonymous API requests an hour are gone.
FEED_URL = ("https://raw.githubusercontent.com/Kizzuwatnaa/DLSS5-Autopilot/"
            "main/docs/compatibility.json")
CACHE_NAME = "compatibility.json"
# The same results as a page people can search, one row per card, driver and
# build (#304). The compatibility workflow builds it and GitHub Pages serves it.
PAGE_URL = "https://kizzuwatnaa.github.io/DLSS5-Autopilot/"
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
# A class of games is one graphics api plus what the game ships to upscale
# with: its own DLSS, FSR 2/3, XeSS, or nothing. It is what the tool knows
# about a game before anybody has reported it, so it is the only honest
# "games like this". The api alone is not: 53 of 69 OptiScaler results on
# DX12 were games with their own DLSS, and 9 of 27 feeder results on DX12
# were games without - put side by side they compare two kinds of game,
# not two routes. Results carry the class from 2.0.6 on ("up" in the record).
UPSCALER_CLASSES = ("dlss", "fsr", "xess", "none")
# The class moves the recommended route only when both routes have this many
# results in it AND the ranges they allow do not overlap (see class_pick).
# Below it the counts are shown, never acted on - a route that worked for
# eleven people is a lead.
MIN_CLASS = 20
# A driver's count within one route, before it is said at all.
MIN_DRIVER = 10


def game_class(api: str, native_dlss: bool, upscaler: str = "") -> str:
    """'DX12/dlss', 'DX11/none'... - the key the published tables use."""
    up = "dlss" if native_dlss else (upscaler if upscaler in ("fsr", "xess") else "none")
    api = str(api or "").strip().upper()
    return f"{api}/{up}" if api else ""


def record(game, route: str, result: str, *, api: str = "", build: str = "",
           gpu_sm=None, gpu_name: str = "", driver: str = "",
           version: str = "", measured: dict | None = None,
           said_by: str = "", upscaler: str = "") -> dict:
    """The one block a shared result carries. Nothing identifying in it.

    `measured` is what the session cost - the work area it ran at, the
    milliseconds a frame spent on what grows with that area, the frame
    rate - as autotune.shared() returns
    it. Three numbers about a game, and nothing about a machine beyond
    the card that is named here anyway. The keys are taken one at a time
    rather than merged wholesale, so a future caller cannot widen what
    leaves this machine by handing in a bigger dict.

    `said_by` is "person" when the tool could not see the outcome and the
    person answered it (verdicts.outcome() is None), and "crash" when
    Windows recorded the game faulting: the list then knows where the answer
    came from, not only what it was.

    `upscaler` is what the game ships - "dlss", "fsr", "xess" or "none" -
    so a result can be counted with the games like it (game_class).
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
    if said_by in ("person", "crash"):
        rec["by"] = said_by
    if upscaler in UPSCALER_CLASSES:
        rec["up"] = upscaler
    return rec


def said_verdict(body: str) -> str:
    """The verdict line a shared result was written with, or "".

    issue_url() puts it between the first line and the "- api:" list. The
    records before 2.0.5 carry no "by" key, and this line is the only thing
    that says whether the tool could see the outcome it wrote down."""
    parts = str(body or "").replace("\r\n", "\n").split("\n\n", 2)
    if len(parts) < 3 or parts[1].lstrip().startswith("- api:"):
        return ""
    return parts[1].strip()


def counts(rec: dict, body: str) -> bool:
    """Does this record go into the list as an answer?

    A record the person answered does. One written before 2.0.5 on a
    verdict that could not see the outcome does not: those were filed as
    "failed" whatever happened in the game (#414 said "WORKED FINE"), so
    they are an unknown, and an unknown is left out rather than counted."""
    from . import verdicts
    if rec.get("by") in ("person", "crash"):
        return True
    said = said_verdict(body)
    # A setup mistake - another executable or folder ran, a proxy name the
    # game does not load - says nothing about the route, and counting it
    # "failed" steers the next person away from one that works (gate 2.0.8).
    if said and verdicts.not_a_route(said):
        return False
    # Only the stages that cannot see. A verdict nobody mapped, or a body a
    # reporter rewrote, is not evidence of an unseen outcome: 16 real
    # results (#193 a "worked" among them) were left out on it (gate 2.0.5).
    return (not said or verdicts.stage(said)[0] not in verdicts.UNSEEN
            or verdicts.outcome(said) is not None)


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


def cached() -> dict:
    """The list as it was last fetched, from disk only. {} when there is none.

    For the route recommendation, which runs on every game entry and may not
    wait on the network: the first run without a copy recommends by the
    tool's own rules, the same as offline. The library asks once per game,
    so the parsed file is kept until the file itself changes.
    """
    global _CACHED
    p = _cache()
    try:
        stamp = p.stat().st_mtime_ns
    except OSError:
        return {}
    if _CACHED is not None and _CACHED[0] == stamp:
        return _CACHED[1]
    try:
        data = json.loads(p.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    _CACHED = (stamp, data)
    return data


_CACHED: tuple | None = None


def for_game(data: dict, game) -> dict | None:
    """The entry for this game, keyed by executable name."""
    exe = getattr(getattr(game, "exe", None), "name", "") or ""
    if not exe:
        return None
    return (data.get("games") or {}).get(exe.lower())


def reports_for(data: dict, exe) -> int:
    """How many shared results this executable has, over every route."""
    name = getattr(exe, "name", exe) or ""
    entry = ((data or {}).get("games") or {}).get(str(name).lower()) if name else None
    return sum(rate(r)[1] for r in ((entry or {}).get("routes") or {}).values()
               if isinstance(r, dict))


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
                   f"The {route_name(best[0])} route worked in {best[1]} of them"
                   + (f" and failed in {best[2]}" if best[2] else "") + ".")
    else:
        out.append(f"{total} reports for this game, and no route is "
                   f"reported working.")
    if route and route in routes:
        mine = routes[route]
        w, f = mine.get("worked", 0), mine.get("failed", 0)
        if f and not w:
            out.append(f"The route chosen here ({route_name(route)}) failed in all "
                       f"{f} of its reports.")
        elif best and best[0] != route and best[1] > (w or 0):
            out.append(f"The route chosen here ({route_name(route)}) worked in "
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


def driver_note(data: dict, driver: str, route: str = "") -> str:
    """What this driver does across every game, or "".

    Said with its denominator. "616.92 faults" is a rumour; "7 of the 25
    reports on 616.92 worked, against 7 of 11 on 616.56" is the reason to
    roll back, and it is the tool's own shared data saying it.

    With a `route`, the count is the one inside that route (by_driver in the
    published file), and another driver is named only when it is NEWER, has
    MIN_DRIVER results on the same route, and its range clears this one's.
    Across all routes a driver's rate is mostly its route mix: 616.64 was
    7 of 28 and 617.14 16 of 22, but 22 of 616.64's results were feeder
    ones against 5 of 617.14's. So a route that has no table of its own
    gets the plain count and nothing compared.
    """
    if not driver:
        return ""
    if route:
        return _driver_in_route(data, driver, route)
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


def confidence(worked: int, tried: int) -> float:
    """How much a rate is worth, given how few results are behind it.

    The lower end of a 95% Wilson interval. Two people out of two is a
    higher rate than nine out of twelve and a worse bet, and sorting on the
    plain rate put the two in front - "one person's machine, said as if it
    were a setting for the game", which this module already refuses to do
    elsewhere. Same arithmetic, one place.
    """
    if tried <= 0:
        return 0.0
    worked = max(0, min(int(worked), int(tried)))    # a hand-edited file can
    tried = int(tried)                               # say 10 of 1, and did
    z = 1.96
    phat = worked / tried
    denom = 1 + z * z / tried
    centre = phat + z * z / (2 * tried)
    spread = z * ((phat * (1 - phat) / tried + z * z / (4 * tried * tried)) ** 0.5)
    return max(0.0, (centre - spread) / denom)


def ceiling(worked: int, tried: int) -> float:
    """The upper end of the same interval: the best this rate could be."""
    if tried <= 0:
        return 1.0
    worked = max(0, min(int(worked), int(tried)))
    return 1.0 - confidence(int(tried) - worked, int(tried))


def _table(data: dict, name: str, key: str) -> dict:
    """{route: {"worked": n, "failed": n}} for one row of a published table.

    The tables arrived with 2.0.6; a list published before has none, and
    then every caller behaves as it did before."""
    row = ((data or {}).get(name) or {}).get(key) if isinstance((data or {}).get(name), dict) else None
    return {r: c for r, c in row.items() if isinstance(c, dict)} if isinstance(row, dict) else {}


def _driver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return ()


def _driver_in_route(data: dict, driver: str, route: str) -> str:
    table = (data or {}).get("by_driver")
    if isinstance(table, dict) and table:
        w, n = rate(_table(data, "by_driver", driver).get(route))
        if n < MIN_DRIVER:
            return ""
        line = (f"Shared results with the {route_name(route)} route on driver {driver}: "
                f"{w} of {n} worked.")
        mine = _driver_key(driver)
        better = []
        for other in table:
            if not mine or _driver_key(other) <= mine:
                continue                  # never an older driver
            w2, n2 = rate(_table(data, "by_driver", other).get(route))
            if n2 >= MIN_DRIVER and confidence(w2, n2) > ceiling(w, n):
                better.append((confidence(w2, n2), other, w2, n2))
        if better:
            _c, other, w2, n2 = max(better)
            line += f" On {other}: {w2} of {n2}."
        return line
    # An older list: the driver across every route, with no comparison.
    w, n = rate((totals(data).get("drivers") or {}).get(driver))
    if n < MIN_DRIVER:
        return ""
    return f"On driver {driver}, {w} of {n} shared results worked (all routes)."


def class_counts(data: dict, klass: str) -> dict:
    """{route: counts} for a class of games (game_class), or {}."""
    return _table(data, "by_class", klass) if klass else {}


def class_pick(data: dict, klass: str, offer: list[str] | None,
               current: str) -> tuple[str, str]:
    """(route, why) when the shared results for games like this one clearly
    favour another offered route over `current`; ("", "") otherwise.

    Clearly means: both routes have MIN_CLASS results in the class, and the
    lowest rate the other route's results allow is above the highest rate
    the current route's allow (95% Wilson, both ends). Two overlapping
    ranges are not a finding, however far apart the plain rates look. The
    per-game rule (MIN_REPORTS results for THIS game) is advice() and
    rank_routes() and runs before any of this; this only speaks for a game
    too few people have reported.
    """
    rows = class_counts(data, klass)
    w, n = rate(rows.get(current))
    if not rows or n < MIN_CLASS:
        return "", ""
    best = None
    for name in offer or []:
        if name == current:
            continue
        w2, n2 = rate(rows.get(name))
        if n2 >= MIN_CLASS and confidence(w2, n2) > ceiling(w, n):
            if best is None or confidence(w2, n2) > best[0]:
                best = (confidence(w2, n2), name, w2, n2)
    if best is None:
        return "", ""
    _c, name, w2, n2 = best
    return name, (f"Shared results for {class_words(klass)}: the {route_name(name)} route "
                  f"worked in {w2} of {n2}, the {route_name(current)} route in {w} of {n}.")


def class_words(klass: str) -> str:
    """'DX12/dlss' -> 'DirectX 12 games with their own DLSS'."""
    api, _, up = str(klass or "").partition("/")
    tail = {"dlss": " with their own DLSS", "fsr": " with FSR and no DLSS",
            "xess": " with XeSS and no DLSS",
            "none": " with no DLSS, FSR or XeSS"}.get(up, "")
    name = {"DX8": "DirectX 8", "DX9": "DirectX 9", "DX10": "DirectX 10",
            "DX11": "DirectX 11", "DX12": "DirectX 12", "VULKAN": "Vulkan",
            "OPENGL": "OpenGL", "UNKNOWN": "unread-API"}.get(api.upper(), api)
    return f"{name} games{tail}"


def class_line(data: dict, klass: str, route: str,
               offer: list[str] | None = None) -> str:
    """The counts for this route in this class of games, with the offered
    route that did best beside it, or "". Said, never acted on."""
    rows = class_counts(data, klass)
    w, n = rate(rows.get(route))
    if n < MIN_REPORTS:
        return ""
    line = (f"Shared results for {class_words(klass)}: the {route_name(route)} route "
            f"worked in {w} of {n}.")
    others = [(confidence(*rate(rows.get(o))), o, *rate(rows.get(o)))
              for o in (offer or []) if o != route
              and rate(rows.get(o))[1] >= MIN_REPORTS]
    if others:
        _c, o, w2, n2 = max(others)
        line += f" The {route_name(o)} route: {w2} of {n2}."
    return line


def rank_routes(data: dict, game, offer: list[str] | None = None,
                klass: str = "") -> list[tuple[str, str]]:
    """`offer`, ordered by what other people's results say, best first, each
    with the counts that put it there ("" for a route nobody has reported).

    What the autopilot needs is not a sentence, it is an order: it installs
    one route, tests it, and goes on to the next. Until now that next one
    was whatever the dropdown happened to list next, with a single route
    promoted by matching a sentence written for a person to read. This
    answers the question directly, and in the same order of evidence the
    rest of this module uses:

      1. what happened to THIS game, for anybody who reported it;
      2. what happened to that route in games like this one (`klass`, see
         game_class) - or, with no such table, across every game - once
         there are enough reports to mean anything (MIN_REPORTS);
      3. the order it was offered in, for everything nobody has tried.

    It never drops a route and never adds one: the same names come back.
    """
    entry = for_game(data, game) or {}
    here = entry.get("routes") or {}
    everywhere = (totals(data) or {}).get("routes") or {}
    alike = class_counts(data, klass)
    # One yardstick for every route: once games like this one have enough
    # results on any offered route, that table ranks them all - the same
    # table the recommendation reads (class_pick). Half class, half every
    # game would put a route's DX12-with-DLSS rate against another's rate
    # over every kind of game.
    if any(rate(alike.get(o))[1] >= MIN_REPORTS for o in offer or []):
        everywhere, where = alike, f"in {class_words(klass)}"
    else:
        where = "across every game shared"
    scored = []
    for i, name in enumerate(offer or []):
        w, n = rate(here.get(name))
        w2, n2 = rate(everywhere.get(name))
        if w:                                   # somebody got THIS game going
            rank, share, seen = 3, confidence(w, n), n
            why = f"{w} of {n} in this game"
        elif w2 and n2 >= MIN_REPORTS:           # or that route, somewhere
            rank, share, seen = 2, confidence(w2, n2), n2
            why = f"{w2} of {n2} {where}"
        elif n:
            # tried in this game and never once worked. Below a route nobody
            # has tried: ordering these by their own numbers put the route
            # with the MOST failures second, and with three attempts in a
            # pass the untried one was never reached.
            rank, share, seen = 0, 0.0, -n
            why = f"0 of {n} in this game"
        else:
            rank, share, seen, why = 1, 0.0, 0, ""
        scored.append((rank, share, seen, i, name, why))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2], x[3]))
    return [(name, why) for _r, _s, _n, _i, name, why in scored]


def next_route(data: dict, game, tried: str, offer: list[str] | None = None,
               klass: str = "") -> str:
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
        return (f"In this game the {route_name(n)} route is reported working by {w} of "
                f"{t}. Try that next - pick it in the route dropdown and "
                f"install again.")
    # Otherwise games like this one when enough of them are in the list
    # (rank_routes reads the same table), else the whole set - and only
    # where there is enough of it.
    all_ = totals(data).get("routes") or {}
    where = "Across every game shared"
    alike = class_counts(data, klass)
    if any(rate(c)[1] >= MIN_REPORTS for n, c in alike.items()
           if n != tried and (allowed is None or n in allowed)):
        all_, where = alike, f"Across {class_words(klass)}"
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
    return (f"Nobody has reported this game yet. {where}, "
            f"the {route_name(n)} route worked in {w} of {t} tries"
            + (f", against {mine_w} of {mine_n} for {route_name(tried)}" if mine_n else "")
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
            + ("The tool could not see this from the logs; the person who "
               "played it said so.\n\n" if rec.get("by") == "person" else "")
            + ("Windows recorded the game crashing in this session.\n\n"
               if rec.get("by") == "crash" else "")
            + f"- api: {rec.get('api') or '-'}\n"
            + ({"dlss": "- the game ships: DLSS\n", "fsr": "- the game ships: FSR, no DLSS\n",
                "xess": "- the game ships: XeSS, no DLSS\n",
                "none": "- the game ships: no DLSS, FSR or XeSS\n"}.get(rec.get("up"), ""))
            + f"- build: {rec.get('build') or '-'}\n"
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


__all__ = ["record", "block", "parse", "said_verdict", "counts", "fetch", "for_game", "advice",
           "measured_note", "issue_url", "FEED_URL", "MIN_REPORTS",
           "MIN_MEASURED", "MARKER"]
