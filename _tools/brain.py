r"""What the shared results say, read the way a maintainer needs them.

    python _tools\brain.py            the published list
    python _tools\brain.py --local    docs\compatibility.json in this checkout

The tool already reads the compatibility list before every install and tells
the person what happened to others on that game. This is the other half: the
same numbers, turned round to answer the questions that decide what to fix
next.

  - Which routes work, overall, and which fail?
  - Which drivers are behind the failures?
  - Where did a route RESCUE a game another route could not - the pattern
    that should change what the tool recommends? (UBOAT on driver 616.92:
    three failures on feeder, then two successes on standalone, on the same
    machine. That one row is what put the standalone route into the
    driver-fault verdict.)
  - Which games fail on everything tried - the ones to look at by hand?

Nothing here is written anywhere. Run it at the start of a round, next to
the download numbers and the issue list.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core import community, net   # noqa: E402

LOCAL = Path(__file__).resolve().parent.parent / "docs" / "compatibility.json"


def load(local: bool) -> dict:
    if local:
        return json.loads(LOCAL.read_text(encoding="utf8"))
    return json.loads(net.fetch_text(community.FEED_URL).decode("utf8"))


def _pct(w: int, n: int) -> str:
    return f"{round(100 * w / n):>3}%" if n else "  - "


def main() -> int:
    d = load("--local" in sys.argv)
    games = d.get("games") or {}
    print(f"compatibility list: {len(games)} games, "
          f"generated {d.get('generated', '?')}\n")

    routes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    drivers: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    rescued, stuck, working = [], [], []
    for exe, e in games.items():
        name = e.get("name") or exe
        rs = e.get("routes") or {}
        for r, c in rs.items():
            routes[r][0] += int(c.get("worked", 0) or 0)
            routes[r][1] += int(c.get("failed", 0) or 0)
        for v, c in (e.get("drivers") or {}).items():
            drivers[v][0] += int(c.get("worked", 0) or 0)
            drivers[v][1] += int(c.get("failed", 0) or 0)
        good = [r for r, c in rs.items() if c.get("worked")]
        bad = [r for r, c in rs.items() if c.get("failed") and not c.get("worked")]
        if good and bad:
            rescued.append((name, bad, good))
        elif bad and not good:
            stuck.append((name, bad))
        elif good:
            working.append((name, good))

    total_w = sum(w for w, _ in routes.values())
    total_f = sum(f for _, f in routes.values())
    print(f"reports: {total_w + total_f}  -  worked {total_w}, "
          f"failed {total_f}\n")

    print("by route")
    for r, (w, f) in sorted(routes.items(), key=lambda x: -(x[1][0] + x[1][1])):
        print(f"  {r:<12} {w:>3} worked  {f:>3} failed  {_pct(w, w + f)} work")

    print("\nby driver")
    for v, (w, f) in sorted(drivers.items(), key=lambda x: -(x[1][0] + x[1][1])):
        print(f"  {v:<12} {w:>3} worked  {f:>3} failed  {_pct(w, w + f)} work")

    print("\nwhere one route rescued a game another could not")
    print("  (the rows that should change what the tool recommends)")
    if not rescued:
        print("  none yet")
    for name, bad, good in rescued:
        print(f"  {name[:34]:<34} failed on {', '.join(bad)}; "
              f"worked on {', '.join(good)}")

    print("\nfailing on every route tried - worth a look by hand")
    if not stuck:
        print("  none")
    for name, bad in sorted(stuck):
        print(f"  {name[:34]:<34} {', '.join(bad)}")

    print(f"\nworking: {len(working)} game(s) - "
          + ", ".join(n for n, _ in sorted(working)[:12])
          + (" ..." if len(working) > 12 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
