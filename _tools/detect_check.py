r"""What this machine's library detects as - and whether that changed.

Every release moves a detection rule, and the same question follows each
time: did that rule move anything else? It has been answered by hand,
game by game, which is slow enough that it gets skipped exactly when it
matters. This records the answer instead.

    python _tools\detect_check.py            compare against the baseline
    python _tools\detect_check.py --save     record the current answers
    python _tools\detect_check.py --list     print them, change nothing

The baseline lives beside this file as `detect_baseline.json` and stays on
the machine that wrote it - it is git-ignored. It is a list of the games
somebody has installed, by name and executable, which is theirs and not
something this repository should carry; and it is only meaningful against
the library it was taken from, so it would be noise in anybody else's
checkout. The first run writes one; `--save` records the current answers
after a change that was meant.

What it records is what the tool decided - the executable it picked,
32/64-bit, the graphics API, the reason it gives for that API, and the
route it recommends. No paths, and nothing read out of the games.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import dlss, games, gpu     # noqa: E402

BASELINE = Path(__file__).resolve().parent / "detect_baseline.json"
FIELDS = ("exe", "bitness", "api", "api_why", "route")


def _row(g) -> dict:
    """What the tool decided about one game."""
    sm = None
    try:
        _, sm = gpu.detect()
    except Exception:
        pass
    route = ""
    try:
        sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm)
        route = sup.recommended
    except Exception as e:
        route = f"<error: {type(e).__name__}>"
    return {
        "exe": getattr(getattr(g, "exe", None), "name", "") or "",
        "bitness": g.bitness,
        "api": g.api,
        "api_why": g.api_why,
        "route": route,
    }


def collect() -> dict:
    found: dict[str, dict] = {}
    t0 = time.time()
    for g in games.scan_all():
        try:
            games.enrich(g)
        except Exception as e:
            found[g.name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        found[g.name] = _row(g)
    return {"generated": time.strftime("%Y-%m-%d %H:%M"),
            "seconds": round(time.time() - t0, 1),
            "games": dict(sorted(found.items()))}


def diff(old: dict, new: dict) -> list[str]:
    out: list[str] = []
    a, b = old.get("games") or {}, new.get("games") or {}
    for name in sorted(set(a) | set(b)):
        if name not in a:
            out.append(f"NEW   {name}: {b[name].get('api')} / "
                       f"{b[name].get('route')}")
            continue
        if name not in b:
            out.append(f"GONE  {name}")
            continue
        for f in FIELDS:
            if a[name].get(f) != b[name].get(f):
                out.append(f"MOVED {name}: {f}\n"
                           f"        was: {a[name].get(f)}\n"
                           f"        now: {b[name].get(f)}")
    return out


def main() -> int:
    args = sys.argv[1:]
    new = collect()
    n = len(new["games"])
    print(f"{n} games read in {new['seconds']}s")

    if "--list" in args:
        for name, row in new["games"].items():
            print(f"  {name}")
            print(f"      {row.get('exe')}  {row.get('bitness')}-bit  "
                  f"{row.get('api')}  -> {row.get('route')}")
            if row.get("api_why"):
                print(f"      why: {row['api_why']}")
        return 0

    if "--save" in args or not BASELINE.is_file():
        BASELINE.write_text(json.dumps(new, indent=1) + "\n", encoding="utf8")
        print(f"baseline written: {BASELINE}")
        return 0

    old = json.loads(BASELINE.read_text(encoding="utf8"))
    changes = diff(old, new)
    print(f"baseline from {old.get('generated')} "
          f"({len(old.get('games') or {})} games)")
    if not changes:
        print("nothing moved")
        return 0
    print(f"\n{len(changes)} change(s):")
    for line in changes:
        print("  " + line)
    print("\nIf these are the changes you meant, run again with --save.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
