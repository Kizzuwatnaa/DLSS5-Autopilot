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

An edit under core/ui (the 2.0 window) gets a fast check of its own: the
file parses and `import core.ui.app` still works - a few seconds, and silent
unless one of them fails. The window's behaviour is walkthrough.py's job;
this only catches the edit that leaves the window unable to start.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent          # ...\src
TOOLS = SRC / "_tools"

# Which check guards which file. First match wins.
GUARDS = (
    ("core/diagnose", TOOLS / "verdict_check.py",
     "every saved report, replayed"),
    ("core/pe.py", TOOLS / "detect_check.py",
     "what this machine's library detects as"),
    ("core/games.py", TOOLS / "detect_check.py",
     "what this machine's library detects as"),
    ("core/dlss.py", TOOLS / "detect_check.py",
     "what this machine's library detects as"),
)
UI_IMPORT_TIMEOUT = 60


def _say(summary: str, context: str) -> None:
    print(json.dumps({
        "systemMessage": summary,
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context},
    }))


def _ui_check(path: str) -> None:
    """core/ui: does the edited file parse, and does the window still import?"""
    p = Path(path)
    if p.suffix != ".py":
        return
    problems = []
    try:
        ast.parse(p.read_text(encoding="utf8", errors="replace"), str(p))
    except SyntaxError as e:
        problems.append(f"{p.name}: line {e.lineno}: {e.msg}")
    except OSError:
        return
    if not problems:
        try:
            r = subprocess.run([sys.executable, "-c", "import core.ui.app"], capture_output=True,
                               text=True, timeout=UI_IMPORT_TIMEOUT, cwd=str(SRC))
            if r.returncode != 0:
                problems.append("import core.ui.app failed:\n" + (r.stderr or r.stdout or "")[-2500:])
        except subprocess.TimeoutExpired:
            problems.append(f"import core.ui.app did not finish in {UI_IMPORT_TIMEOUT}s")
        except Exception:
            return
    if problems:
        _say("core/ui: the window no longer starts",
             "An edit under core/ui left the window unable to start. Fix this before anything "
             "else:\n\n" + "\n".join(problems))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = payload.get("tool_input") or {}
    resp = payload.get("tool_response") or {}
    path = (tool_input.get("file_path") or resp.get("filePath") or "")
    low = str(path).replace("\\", "/").lower()
    if "/core/ui/" in low or low.startswith("core/ui/"):
        _ui_check(str(path))
        return 0
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
        _say(f"{script.name}: something moved ({what})",
             f"{script.name} ran because {needle} changed, and it is "
             f"not clean. Read every line below and decide whether you "
             f"meant each one; record them with --save only when you "
             f"did.\n\n{out[-4000:]}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
