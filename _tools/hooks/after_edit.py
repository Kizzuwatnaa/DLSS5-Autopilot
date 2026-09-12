r"""After an edit to a core file, run the check that guards it.

The project has the checks it needs - verdict_check for the diagnosis,
detect_check for the detection rules - and the thing that keeps going wrong
is not the checks, it is remembering to run them. The record is blunt about
it: a memory existed for two of the traps I fell into the same afternoon.
So the running does not depend on remembering any more.

Reads the PostToolUse payload on stdin, works out which check the edited
file belongs to, runs it, and hands anything that MOVED back to the model
as context. Never blocks and never fails the edit: a hook that gets in the
way of work is a hook that gets turned off.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent          # ...\src
TOOLS = SRC / "_tools"

# Which check guards which file. First match wins.
GUARDS = (
    ("core/diagnose.py", TOOLS / "verdict_check.py",
     "every saved report, replayed"),
    ("core/pe.py", TOOLS / "detect_check.py",
     "what this machine's library detects as"),
    ("core/games.py", TOOLS / "detect_check.py",
     "what this machine's library detects as"),
    ("core/dlss.py", TOOLS / "detect_check.py",
     "what this machine's library detects as"),
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = payload.get("tool_input") or {}
    resp = payload.get("tool_response") or {}
    path = (tool_input.get("file_path") or resp.get("filePath") or "")
    low = str(path).replace("\\", "/").lower()
    for needle, script, what in GUARDS:
        if not low.endswith(needle):
            continue
        if not script.is_file():
            return 0
        try:
            r = subprocess.run([sys.executable, str(script)],
                               capture_output=True, text=True, timeout=180,
                               cwd=str(SRC))
        except Exception:
            return 0
        if r.returncode == 0:
            return 0
        out = (r.stdout or r.stderr or "").strip()
        # Only the part that says what moved; the banner is noise here.
        print(json.dumps({
            "systemMessage": f"{script.name}: something moved ({what})",
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext":
                    f"{script.name} ran because {needle} changed, and it is "
                    f"not clean. Read every line below and decide whether you "
                    f"meant each one; record them with --save only when you "
                    f"did.\n\n{out[-4000:]}",
            },
        }))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
