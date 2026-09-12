r"""Open every session with where the loop stands, not with the last report.

The round that starts from whatever arrived overnight fixes whatever
arrived overnight. The measurement - which class of answer the tool gives
most often, and whether the guard rails are alive - is what turns a pile of
reports into a backlog with an order. It is worth nothing if it is only run
when somebody remembers to run it, so it runs here.

Offline and bounded: no network at session start. The corpus freshness
check (`state.py --online`) belongs at the start of an issue round, where
the triage skill asks for it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent


def main() -> int:
    state = SRC / "_tools" / "state.py"
    if not state.is_file():
        return 0
    try:
        r = subprocess.run([sys.executable, str(state)], cwd=str(SRC),
                           capture_output=True, text=True, timeout=120)
    except Exception:
        return 0
    out = (r.stdout or "").strip()
    if not out:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext":
                "Where this project's improvement loop stands, measured from "
                "disk at session start. The top class is the backlog: a shape "
                "to fix, not a report to answer.\n\n" + out[-4000:],
        },
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
