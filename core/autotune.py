"""Aim for a frame rate, instead of guessing at a percentage.

Neural rendering has one dial that costs frames: the resolution the model
runs at. Everyone sets it by feel - drop it to 75%, play, decide it is
still heavy, drop it again. The measurements to do better were already
being written down by the add-ons and thrown away:

* the feeder logs "N frames: feed CPU X ms/frame ... Y fps" - the frame
  rate the person actually had;
* the OptiScaler forks log "DLSS-NR cost: 7.41 ms total = 7.23 ms model"
  every frame the model draws - the cost of the dial itself.

So: play, quit, and the tool reads what the session really cost and works
out the resolution that meets your target for the next one.

The maths, because a number that appears from nowhere is not trustworthy.
The model runs over an area, and area goes with the square of the
resolution, so the frame time splits into a part the dial does not touch
and a part that scales:

    frame_ms(r) = base + k * r^2

With two sessions at two different resolutions that is two equations and
two unknowns, so `base` and `k` are solved exactly, and the answer for a
target T is r = sqrt((1000/T - base) / k). With only one session there is
nothing to solve - one point does not fix a line - so the first suggestion
is a bounded step in the right direction, and the tool says as much rather
than dressing a guess up as a calculation.

Nothing here changes anything during a game: OptiScaler reads its ini once,
at startup (Config::Reload is called from load, and nothing watches the
file), and the feeder reads its cfg the same way. Between sessions is what
this can honestly do, and it is what it does.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import prefs

MIN_RES, MAX_RES = 25, 100
# A first suggestion moves by at most this much: enough to be felt, small
# enough that a wrong guess does not ruin the next session.
FIRST_STEP = 15
# Two sessions at resolutions closer than this are, for the arithmetic, the
# same point: solving from them turns measurement noise into a huge k.
MIN_SPREAD = 5
# How much of the target frame the model may take when the frame rate
# itself was never logged (the OptiScaler route logs the model's cost, not
# the game's fps). A rule of thumb, and named as one wherever it is shown.
BUDGET_SHARE = 0.25
# The work areas a cost table is printed for, plus whatever this session
# ran at. Three rows bracket the dial; twenty rows are a table nobody reads.
SHOWN_AREAS = (50, 75, 100)
# Two sessions this far apart may be published as a measured cost. The
# solve itself accepts MIN_SPREAD, which is enough to print a table the
# person can disbelieve - it is not enough to put a number in front of
# strangers: at 5% apart, ordinary run-to-run noise solves to a model that
# costs a fifth of a millisecond.
SHARE_SPREAD = 15
HISTORY_KEY = "autotune"
MAX_SAMPLES = 8
# How many games' histories are kept. Every diagnosis of a game on a route
# with a work area records one now, not only the ones where a frame rate
# was typed, and settings.json is read and written whole.
MAX_GAMES = 60

FEED_PERF = re.compile(r"(\d+) frames: feed CPU ([\d.,]+) ms/frame"
                       r"[^\n]*?([\d.,]+) fps")
OPTI_COST = re.compile(r"DLSS-NR cost:\s*([\d.,]+) ms total"
                       r"(?:\s*=\s*([\d.,]+) ms model)?")


@dataclass
class Measured:
    """What one session actually cost."""
    route: str
    resolution: int
    fps: float | None = None
    model_ms: float | None = None
    frames: int = 0
    source: str = ""
    # Did the work area come from the file the add-on read, or is it the
    # slider's value because nothing could be read? A guess is fine for a
    # table on screen and is not fine in a published record that the next
    # person sets their game by.
    from_config: bool = True

    @property
    def frame_ms(self) -> float | None:
        return 1000.0 / self.fps if self.fps else None


@dataclass
class Cost:
    """What the model costs at one work area, in this game, on this card."""
    resolution: int
    model_ms: float
    fps: float | None = None
    played: bool = False         # this row is a session, not arithmetic


@dataclass
class Suggestion:
    resolution: int
    lines: list[str] = field(default_factory=list)
    exact: bool = False          # solved from two sessions, not stepped


def _num(s: str) -> float | None:
    """A number out of a file somebody else's program wrote, or None.

    Both add-ons are C++ and both format with the machine's locale, so
    "0,75" is a real thing to find in a config; and a truncated line can
    leave "1.2.3" where a frame rate belongs. Neither may raise here - one
    is a number to read, the other is a session to skip.
    """
    s = (s or "").strip().replace(",", ".")
    if s.count(".") > 1:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def measure(text_feed: str, text_opti: str, route: str,
            resolution: int) -> Measured | None:
    """Read one session out of the logs already trimmed to its last run.

    The caller passes the text because the log readers, the session
    splitting and the "which run is this" rules all live in diagnose, and
    there is no reason to have a second copy of them here.
    """
    if route == "optiscaler":
        costs = [c for c in (_num(m.group(2) or m.group(1))
                             for m in OPTI_COST.finditer(text_opti or ""))
                 if c is not None]
        if not costs:
            return None
        return Measured(route=route, resolution=resolution,
                        model_ms=_median(costs), frames=len(costs),
                        source="the model's own cost lines")
    m = None
    for m in FEED_PERF.finditer(text_feed or ""):
        pass                    # the settled rate, not the loading screen
    if not m:
        return None
    fps = _num(m.group(3))
    frames = _num(m.group(1))
    if fps is None or frames is None:
        return None
    return Measured(route=route, resolution=resolution,
                    fps=fps, frames=int(frames),
                    source="the feed's frame-rate line")


# A config written this long after the log stopped is a config written for
# the NEXT session, not the one that was played. A minute of slack: an
# install writes the cfg and the game may still be flushing its log.
CONFIG_GRACE = 60


def config_name(route: str) -> str:
    """The file this route's add-on reads its work area from."""
    return "OptiScaler.ini" if route == "optiscaler" else "dlss5-feed.cfg"


def written_after(install_dir, route: str, log_path) -> bool:
    """Was the work area written after the session it would be read for?

    An install rewrites the config. Pressing "did it work?" afterwards
    without playing again reads the NEW work area against the OLD log, and
    what comes out of that is a frame rate attributed to a setting nobody
    played at - then published.
    """
    from pathlib import Path as _P
    try:
        cfg = _P(install_dir) / config_name(route)
        return cfg.stat().st_mtime > _P(log_path).stat().st_mtime + CONFIG_GRACE
    except OSError:
        return False


def ran_at_exact(install_dir, route: str) -> int | None:
    """The work area in the add-on's own config, or None if it is not there.

    None means "nobody knows", which is a different answer from any number
    - see ran_at() for why the difference matters now.
    """
    from pathlib import Path as _P
    d = _P(install_dir)
    try:
        if route == "optiscaler":
            txt = (d / "OptiScaler.ini").read_text(encoding="utf8",
                                                   errors="replace")
            m = re.search(r"^\s*WorkingScale\s*=\s*([\d.,]+)", txt,
                          re.M | re.I)
            v = _num(m.group(1)) if m else None
            if v is not None:
                return _clamp(v * 100)
        else:
            txt = (d / "dlss5-feed.cfg").read_text(encoding="utf8",
                                                   errors="replace")
            m = re.search(r"^\s*work_resolution\s*=\s*([\d.,]+)", txt,
                          re.M | re.I)
            v = _num(m.group(1)) if m else None
            if v is not None:
                return _clamp(v)
    except (OSError, ValueError):
        pass
    return None


def ran_at(install_dir, route: str, fallback: int) -> int:
    """The work area the session really ran at, read from the config file.

    The slider in the window is live state and a route change rewrites it,
    so it says what the NEXT install would use, not what this session used.
    The add-ons read these two files at startup, which makes them the only
    record of it - the fallback is the slider, and a session measured
    against the fallback is not published (Measured.from_config).
    """
    got = ran_at_exact(install_dir, route)
    return int(fallback) if got is None else got


# ------------------------------------------------------------- the history
def _key(install_dir) -> str:
    return str(install_dir).lower()


def history(install_dir) -> list[dict]:
    all_ = prefs.get(HISTORY_KEY) or {}
    rows = all_.get(_key(install_dir))
    return [r for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, dict)]


def remember(install_dir, m: Measured) -> None:
    """Keep one sample per resolution, per route - the newest wins.

    Per route, because the two routes do not measure the same thing: the
    feeder writes a frame rate and the OptiScaler fork writes the model's
    own cost. A session on one used to overwrite the other's point at the
    same work area, and the two-point solve quietly lost a leg.
    """
    all_ = dict(prefs.get(HISTORY_KEY) or {})
    rows = [r for r in (all_.get(_key(install_dir)) or [])
            if isinstance(r, dict)
            and not (r.get("resolution") == m.resolution
                     and (r.get("route") or m.route) == m.route)]
    rows.append({"resolution": m.resolution, "fps": m.fps,
                 "model_ms": m.model_ms, "route": m.route,
                 "at": int(time.time())})
    all_[_key(install_dir)] = rows[-MAX_SAMPLES:]
    if len(all_) > MAX_GAMES:
        def newest(item):
            # item[1] is whatever is in the file under that game's key.
            # Same file, same junk: settings.json is read back off a disk,
            # and a raise here happens inside the diagnosis.
            seen = [int(r["at"])
                    for r in (item[1] if isinstance(item[1], list) else [])
                    if isinstance(r, dict)
                    and isinstance(r.get("at"), (int, float))
                    and not isinstance(r.get("at"), bool)]
            return max(seen, default=0)
        all_ = dict(sorted(all_.items(), key=newest)[-MAX_GAMES:])
    prefs.set_(HISTORY_KEY, all_)


def forget(install_dir) -> None:
    all_ = dict(prefs.get(HISTORY_KEY) or {})
    if all_.pop(_key(install_dir), None) is not None:
        prefs.set_(HISTORY_KEY, all_)


# ------------------------------------------------------------ the arithmetic
def _clamp(r: float) -> int:
    return int(max(MIN_RES, min(MAX_RES, round(r))))


def _solve(points: list[tuple[int, float]]) -> tuple[float, float] | None:
    """(base, k) from two (resolution %, frame ms) points, or None."""
    if len(points) < 2:
        return None
    (r1, t1), (r2, t2) = points[0], points[-1]
    if abs(r1 - r2) < MIN_SPREAD:
        return None
    a1, a2 = (r1 / 100.0) ** 2, (r2 / 100.0) ** 2
    if abs(a1 - a2) < 1e-9:
        return None
    k = (t1 - t2) / (a1 - a2)
    base = t1 - k * a1
    # A model that costs nothing, or a base frame time that is negative,
    # means the two sessions were not comparable - a different scene, a
    # different resolution, a driver change. Step instead of pretending.
    if k <= 0.05 or base <= 0:
        return None
    return base, k


def split(rows: list[dict],
          latest: Measured | None) -> tuple[float | None, float] | None:
    """(base ms, model ms at 100%) for this game, or None.

    `base` is the part of the frame the work area does not touch. It is
    None on the routes that log the model's own cost without a frame rate:
    the model's cost still goes with the area there, but what is left of
    the frame was never measured, and inventing it is how a tool ends up
    printing a frame rate nobody had.
    """
    if latest is None:
        return None
    if latest.fps:
        return _solve(_points(rows, latest.route))
    if latest.model_ms and latest.resolution:
        area = (int(latest.resolution) / 100.0) ** 2
        if area > 0:
            return None, latest.model_ms / area
    return None


def _points(rows: list[dict], route: str = "") -> list[tuple[int, float]]:
    """(work area, frame ms) per session, from what was written down.

    The rows come back out of prefs.json, which is a file on a disk: a
    hand-edited or half-written one has strings and nulls in it, and this
    is called from the diagnosis, where a raise costs the crash correction
    that runs after it.
    """
    out: dict[int, float] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        # Only this route's sessions: a frame rate measured on the feeder
        # and one measured under OptiScaler are two different games.
        if route and (r.get("route") or route) != route:
            continue
        try:
            res, fps = int(r["resolution"]), float(r["fps"])
        except (KeyError, TypeError, ValueError):
            continue
        if res and fps > 0:
            out[res] = 1000.0 / fps
    return sorted(out.items())


def costs(rows: list[dict], latest: Measured | None,
          areas=SHOWN_AREAS) -> list[Cost]:
    """What each work area costs, from the split. Empty when there is none."""
    got = split(rows, latest)
    if not got or latest is None:
        return []
    base, k = got
    out = []
    for r in sorted({_clamp(x) for x in (*areas, latest.resolution)}):
        ms = k * (r / 100.0) ** 2
        out.append(Cost(r, ms, (1000.0 / (base + ms)) if base else None,
                        played=(bool(latest.resolution)
                                and r == _clamp(latest.resolution))))
    return out


def cost_lines(rows: list[dict], latest: Measured | None) -> list[str]:
    """The cost table as lines. This is the number, not the advice.

    Every other tool in this ecosystem sets this dial by feel. The
    add-ons write down what it really cost, so it can be read out instead
    of guessed at - and what the other settings would cost follows from
    the same two sessions the suggestion is solved from.
    """
    table = costs(rows, latest)
    if not table or latest is None:
        return []
    # The log prints "=== what the work area costs here ===" above this,
    # so saying it again here is the same sentence twice, one line apart.
    out = ["in this game, on this card:"]
    # Where the route logs the model's own cost there is no frame rate, and
    # the number is the model. Where it is solved from two frame rates - the
    # feeder - it is everything that grows with the work area: the model and
    # the feed together. The shared result says so; this is the screen, where
    # the number is largest and read most often, so it says so too.
    what = "of model" if table[0].fps is None else "of model and feed"
    for c in table:
        line = f"  {c.resolution:>3}%   {c.model_ms:>5.1f} ms {what}"
        if c.fps:
            line += f"   ->  {c.fps:>3.0f} fps"
        if c.played:
            line += "   (this session)"
        out.append(line)
    if table[0].fps is None:
        out.append("this route writes down what the model cost but not "
                   "your frame rate, so there is no fps here - only the "
                   "cost of the dial itself.")
    if not getattr(latest, "from_config", True):
        # The row marked "(this session)" is the slider's number, not the
        # add-on's. Said here, because the table otherwise reads as a
        # measurement of a setting nobody confirmed.
        out.append("the work area above is the slider's: the add-on's own "
                   "config could not be read, so which setting this session "
                   "ran at is not certain.")
    return out


def shared(rows: list[dict], latest: Measured | None) -> dict:
    """The measured part of a shared result: {"res": 75, "ms": 7.2, ...}.

    "ms" is the model's own cost where the route logs it (OptiScaler). On
    the feeder it is solved from frame rates at two work areas, so it is
    all of what grows with the area - model, feed and shaders - an upper
    bound on the model, and every sentence that prints it says so.

    Only what was measured, or solved from measurements. An empty dict
    when the session did not say enough: a published number that was
    guessed at is worse than no number, because the next person reads it
    as somebody's real setting.
    """
    if latest is None or not latest.resolution:
        return {}
    if not getattr(latest, "from_config", True):
        # The work area is the slider's, because the add-on's config could
        # not be read. Good enough for a table on screen, not good enough
        # to publish as what somebody ran.
        return {}
    res = _clamp(latest.resolution)
    out: dict = {"res": res}
    if latest.fps:
        out["fps"] = round(float(latest.fps), 1)
    ms = latest.model_ms
    if ms is None:
        points = _points(rows, latest.route)
        wide = points and abs(points[-1][0] - points[0][0]) >= SHARE_SPREAD
        got = split(rows, latest) if wide else None
        if got:
            ms = got[1] * (res / 100.0) ** 2
    if ms is not None:
        out["ms"] = round(float(ms), 2)
    return out if len(out) > 1 else {}


def suggest(rows: list[dict], target_fps: float, current: int,
            route: str, latest: Measured | None = None) -> Suggestion | None:
    """The resolution to run next, and why - or None with nothing to say."""
    if not target_fps or not latest:
        return None
    target_ms = 1000.0 / float(target_fps)

    if latest.fps:
        points = _points(rows, latest.route)
        solved = _solve(points)
        now_fps = latest.fps
        if solved:
            base, k = solved
            head = target_ms - base
            if head <= 0:
                # `base` is the part of the frame the dial does not touch,
                # extrapolated - not a measurement of the game with the
                # model off, and the wording must not say it is. The
                # suggestion stays where it is, too: offering "set it to
                # 25%" under a sentence saying the dial is not the problem
                # is the tool arguing with itself.
                return Suggestion(current, [
                    f"{now_fps:.0f} fps at {latest.resolution}%. Even at the "
                    f"smallest work area this game does not reach "
                    f"{target_fps:.0f} fps here - about "
                    f"{1000.0 / base:.0f} fps with everything the work area "
                    f"drives taken out - so the work area is not what is "
                    f"holding it back.",
                ])
            want = _clamp(100.0 * (head / k) ** 0.5)
            return Suggestion(want, [
                f"{now_fps:.0f} fps at {latest.resolution}% "
                f"({latest.frames} frames, from {latest.source}).",
                f"Two sessions at different work areas are enough to split "
                f"the frame: {base:.1f} ms the work area does not touch, "
                f"{k:.1f} ms that grows with it - the model and the feed "
                f"together - at full size.",
                (f"{want}% should land on {target_fps:.0f} fps."
                 if want != current else
                 f"{current}% is already the right setting for "
                 f"{target_fps:.0f} fps."),
            ], exact=True)
        # Not solvable: either one session, or two that cannot be told
        # apart (too close together, or disagreeing about the physics).
        # Saying "first measurement" in the second case is false, and the
        # promise that the next session will solve it has already failed
        # once.
        if now_fps < target_fps * 0.97:
            want = _clamp(current - FIRST_STEP)
            why = f"below {target_fps:.0f}"
        elif now_fps > target_fps * 1.15 and current < MAX_RES:
            want = _clamp(current + FIRST_STEP)
            why = f"comfortably above {target_fps:.0f}"
        else:
            return Suggestion(current, [
                f"{now_fps:.0f} fps at {current}% - that is your target. "
                f"Nothing to change."])
        seen = len(_points(rows, latest.route))
        return Suggestion(want, [
            f"{now_fps:.0f} fps at {latest.resolution}% "
            f"({latest.frames} frames), {why}.",
            (f"One session is not enough to work the setting out, so this is "
             f"a step rather than a calculation: try {want}%. A second "
             f"session at a different work area is what the arithmetic "
             f"needs." if seen <= 1 else
             f"The sessions recorded for this game are too alike to split "
             f"the frame "
             f"between the model and everything else, so this is a step "
             f"rather than a calculation: try {want}%."),
        ])

    if latest.model_ms:
        # No frame rate in this route's log - only what the model itself
        # cost. Aim at a share of the target frame and say so plainly.
        budget = target_ms * BUDGET_SHARE
        want = _clamp(latest.resolution * (budget / latest.model_ms) ** 0.5)
        share = latest.model_ms / target_ms * 100
        lines = [
            f"The model cost {latest.model_ms:.1f} ms a frame at "
            f"{latest.resolution}% "
            + (f"({latest.frames} frames measured)." if latest.frames != 1
               else "(one frame measured - play a little longer for a "
                    "steadier number)."),
            f"At {target_fps:.0f} fps a frame is {target_ms:.1f} ms, so the "
            f"model was taking {share:.0f}% of it.",
        ]
        if want == current:
            lines.append(f"{current}% keeps it near a quarter of the frame, "
                         f"which is the rule of thumb here - nothing to "
                         f"change.")
        else:
            lines.append(
                f"{want}% brings it to about {budget:.1f} ms, a quarter of "
                f"the frame. This route logs the model's cost but not your "
                f"frame rate, so that share is a rule of thumb, not a "
                f"measurement of the fps you will get.")
        return Suggestion(want, lines)
    return None
