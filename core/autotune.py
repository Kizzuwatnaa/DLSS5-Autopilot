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
HISTORY_KEY = "autotune"
MAX_SAMPLES = 8

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

    @property
    def frame_ms(self) -> float | None:
        return 1000.0 / self.fps if self.fps else None


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


def ran_at(install_dir, route: str, fallback: int) -> int:
    """The work area the session really ran at, read from the config file.

    The slider in the window is live state and a route change rewrites it,
    so it says what the NEXT install would use, not what this session used.
    The add-ons read these two files at startup, which makes them the only
    honest record of it.
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
    return int(fallback)


# ------------------------------------------------------------- the history
def _key(install_dir) -> str:
    return str(install_dir).lower()


def history(install_dir) -> list[dict]:
    all_ = prefs.get(HISTORY_KEY) or {}
    rows = all_.get(_key(install_dir)) or []
    return [r for r in rows if isinstance(r, dict)]


def remember(install_dir, m: Measured) -> None:
    """Keep one sample per resolution - the newest wins."""
    all_ = dict(prefs.get(HISTORY_KEY) or {})
    rows = [r for r in (all_.get(_key(install_dir)) or [])
            if isinstance(r, dict) and r.get("resolution") != m.resolution]
    rows.append({"resolution": m.resolution, "fps": m.fps,
                 "model_ms": m.model_ms, "route": m.route,
                 "at": int(time.time())})
    all_[_key(install_dir)] = rows[-MAX_SAMPLES:]
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


def suggest(rows: list[dict], target_fps: float, current: int,
            route: str, latest: Measured | None = None) -> Suggestion | None:
    """The resolution to run next, and why - or None with nothing to say."""
    if not target_fps or not latest:
        return None
    target_ms = 1000.0 / float(target_fps)

    if latest.fps:
        points = sorted({int(r["resolution"]): 1000.0 / float(r["fps"])
                         for r in rows
                         if r.get("fps") and r.get("resolution")}.items())
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
                    f"{1000.0 / base:.0f} fps with the model's cost taken "
                    f"out - so the work area is not what is holding it back.",
                ])
            want = _clamp(100.0 * (head / k) ** 0.5)
            return Suggestion(want, [
                f"{now_fps:.0f} fps at {latest.resolution}% "
                f"({latest.frames} frames, from {latest.source}).",
                f"Two sessions at different work areas are enough to split "
                f"the frame: {base:.1f} ms the model does not touch, "
                f"{k:.1f} ms of model at full size.",
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
        seen = len({int(r["resolution"]) for r in rows
                    if r.get("fps") and r.get("resolution")})
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
