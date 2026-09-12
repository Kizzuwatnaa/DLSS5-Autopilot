r"""Refuse a shell heredoc whose body carries backslash escapes.

The Bash tool eats one level of backslash on the way to the shell. Inside a
heredoc that is silent and it has cost this project a day at a time:

  "\\n" in a Python string  -> a real newline in the file being written
  "\\0"                     -> a real NUL byte
  "\\" before a newline      -> Python reads it as a line continuation and
                               JOINS the two lines, so the search string can
                               never match and the edit "fails" for no
                               visible reason
  a docstring path          -> SyntaxWarning: invalid escape sequence

There is a memory about it. It did not stop the mistake being made four
times in one session, because a memory is advice and advice loses to
momentum. This is not advice: the command does not run.

The fix is always the same - write the script with the Write tool and run
it by path - so that is what the refusal says.

Single backslashes are left alone: r"C:\Users\..." in a heredoc is fine and
common. Only the two shapes that actually break are refused.
"""
from __future__ import annotations

import json
import re
import sys

# <<EOF, <<'EOF', <<"EOF", <<-EOF ... everything after the marker is body.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# A line whose last character is a lone backslash: inside a quoted string
# Python reads it as a line continuation once the shell has eaten one level.
CONTINUATION = re.compile(r"[^\\\n]\\\n")


def trouble(command: str) -> str:
    m = HEREDOC.search(command)
    if not m:
        return ""
    body = command[m.end():]
    if "\\\\" in body:
        return ("it contains a doubled backslash (\\\\), which arrives at "
                "the shell as a single one")
    if CONTINUATION.search(body):
        return ("a line in it ends with a backslash, which arrives as a line "
                "continuation and silently joins that line to the next")
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    command = str((payload.get("tool_input") or {}).get("command") or "")
    why = trouble(command)
    if not why:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason":
                f"This command is a heredoc and {why}. The Bash tool eats one "
                f"level of backslash escaping on the way to the shell, so "
                f"what lands in the file is not what you wrote - a real "
                f"newline, a NUL byte, or two source lines joined into one "
                f"that no search string will ever match. Write the script to "
                f"the scratchpad with the Write tool and run it by path "
                f"instead, or use the Edit tool on the file directly.",
        },
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
