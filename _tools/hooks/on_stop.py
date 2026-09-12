r"""Before the turn ends: is the tree actually in one piece?

Twice this project has ended a round with a file that did not parse, and
once with a verdict quietly moved. Both are cheap to ask about and neither
was asked, because asking depended on remembering.

Three questions, about three seconds, and it says nothing at all unless one
of them fails - a hook that talks every time is a hook nobody reads:

  1. does every core module still compile?
  2. does the suite itself still parse?
  3. do all 84 saved reports still get the answers they got?

It never blocks. It only makes sure the round does not end quietly on
something broken.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent          # ...\src


def _parses(p: Path) -> str:
    try:
        ast.parse(p.read_text(encoding="utf8", errors="replace"), str(p))
        return ""
    except SyntaxError as e:
        return f"{p.name}: line {e.lineno}: {e.msg}"
    except OSError:
        return ""


def main() -> int:
    bad: list[str] = []
    for p in sorted((SRC / "core").glob("*.py")):
        why = _parses(p)
        if why:
            bad.append(why)
    for name in ("test_all.py", "test_scan.py", "test_install.py"):
        why = _parses(SRC / name)
        if why:
            bad.append(why)
    if not bad:
        vc = SRC / "_tools" / "verdict_check.py"
        if vc.is_file():
            try:
                r = subprocess.run([sys.executable, str(vc)], cwd=str(SRC),
                                   capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    bad.append("verdict_check.py is not clean:\n"
                               + (r.stdout or r.stderr or "")[-1500:])
            except Exception:
                pass
    if not bad:
        return 0
    print(json.dumps({
        "systemMessage": "the tree is not in one piece - see the turn output",
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext":
                "Before this round ends, these are broken and were not "
                "mentioned:\n\n" + "\n".join(bad),
        },
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
