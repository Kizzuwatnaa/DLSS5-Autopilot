r"""A log file, so a bug report can carry something other than "it crashes".

Someone reported the tool finds no games and crashes often. There was no way
to act on that: nothing was written down, so there was nothing to read. This
records what the tool did and every exception it hit, including the ones on
background threads and inside tkinter callbacks, which otherwise vanish
because the window has no console behind it.

It stays out of the way:
  - one file, %LOCALAPPDATA%\dlss5-autopilot\autopilot.log, trimmed when it
    grows past a megabyte, so it never becomes a disk problem;
  - only paths and versions, never anything typed in;
  - a failure to write is ignored - logging must never be the thing that
    breaks the tool.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dlss5-autopilot"
FILE = DIR / "autopilot.log"
MAX_BYTES = 1 << 20

_lock = threading.Lock()
_crashed = False          # has anything gone wrong this run?
_last_error = ""          # the most recent traceback, for a bug report


def path() -> Path:
    return FILE


def crashed() -> bool:
    """True once an unhandled exception has been recorded this run."""
    return _crashed


def last_error() -> str:
    return _last_error


def tail(lines: int = 40, max_chars: int = 3500) -> str:
    """The end of the log, for pasting into a report. Never raises."""
    try:
        text = FILE.read_text(encoding="utf8", errors="replace")
    except OSError:
        return ""
    out = "\n".join(text.splitlines()[-lines:])
    return out[-max_chars:]


def write(text: str, level: str = "info") -> None:
    """Append one line. Never raises."""
    try:
        with _lock:
            DIR.mkdir(parents=True, exist_ok=True)
            # Trim from the front rather than deleting, so a crash that
            # happens right after a rollover still has context above it.
            try:
                if FILE.stat().st_size > MAX_BYTES:
                    tail = FILE.read_bytes()[-(MAX_BYTES // 2):]
                    FILE.write_bytes(b"[earlier entries trimmed]\n" +
                                     tail.split(b"\n", 1)[-1])
            except OSError:
                pass
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(FILE, "a", encoding="utf8", errors="replace") as f:
                f.write(f"{stamp} {level:5} {text}\n")
    except Exception:
        pass


def exception(where: str, exc: BaseException | None = None) -> None:
    """Record a full traceback under a label saying what was being done."""
    global _crashed, _last_error
    _crashed = True
    if exc is None:
        tb = traceback.format_exc()
    else:
        tb = "".join(traceback.format_exception(type(exc), exc,
                                                exc.__traceback__))
    _last_error = f"{where}\n{tb.rstrip()}"
    write(_last_error, "ERROR")


def start(version: str) -> None:
    """Open the log for a run and record what this machine is."""
    write("=" * 70)
    write(f"DLSS 5 Autopilot {version} starting")
    try:
        import platform
        write(f"  windows : {platform.platform()}")
        write(f"  python  : {sys.version.split()[0]}"
              f"  frozen={getattr(sys, 'frozen', False)}")
        write(f"  exe     : {sys.executable}")
    except Exception:
        pass
    try:
        from . import gpu
        name, sm = gpu.detect()
        write(f"  gpu     : {name}  sm_{sm}")
    except Exception as e:
        write(f"  gpu     : could not detect ({e})", "warn")


def install_handlers(root=None) -> None:
    """Catch what would otherwise disappear.

    A windowed build has no console, so an unhandled exception on a worker
    thread or inside a tkinter callback is simply lost. All three routes are
    sent to the log instead.
    """
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        exception("unhandled exception", exc)
        prev(exc_type, exc, tb)

    sys.excepthook = hook

    def thread_hook(args):
        exception(f"unhandled exception on thread {args.thread_name}",
                  args.exc_value)

    try:
        threading.excepthook = thread_hook
    except Exception:
        pass

    if root is not None:
        def tk_hook(_self, exc_type, exc, tb):
            exception("exception in a tkinter callback", exc)
        try:
            type(root).report_callback_exception = tk_hook
        except Exception:
            pass
