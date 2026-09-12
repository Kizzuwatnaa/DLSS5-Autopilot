r"""Run ONE section of test_all.py on its own, before spending ten minutes.

The suite is a single script: a NameError anywhere ends the run, every
section after it never executes, and the exit code is the same as a failed
check's. Three ten-minute runs were once spent finding three forward
references one at a time, because each run died at the first one.

    python _tools\dryrun_section.py              the last section before RESULT
    python _tools\dryrun_section.py 1.8.2        the first section whose title
                                                 contains that

It executes the section's own text in a namespace that mimics the suite's -
the same imports, `check`, `src_of`, `X64`, `SRC_DIR`, `patch` - so every
name it borrows from earlier in the file shows up as a NameError here, in
seconds, all of them in one pass. A section that passes here can still fail
in the full run (the suite rebinds names as it goes); a section that fails
here would have ended the run.
"""
from __future__ import annotations

import inspect
import io
import json
import os
import re as _re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent
os.chdir(SRC)
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "_tools"))

SRC_DIR = SRC
X64 = Path(os.environ.get("DLSS5_TEST_X64",
                          r"C:\Users\Mustafa\Downloads\dlss5-feed-host64.exe"))

from core import (diagnose, dlss, games, gpu, installer, net, optiscaler,  # noqa: E402
                  pe, prefs, reshade_ini, sources, update, vulkan)
from core import dxvk, refw, video  # noqa: E402
from core import mfg as _m141  # noqa: E402
from core import gui as _gui  # noqa: E402
import zipfile as _zf141  # noqa: E402,F811

_gsrc = inspect.getsource(_gui)
_readme = (SRC_DIR / "README.md").read_text(encoding="utf8")

FAILS: list[str] = []


def src_of(obj) -> str:
    try:
        return inspect.getsource(obj)
    except Exception as e:
        print("   (source unreadable)", e)
        return ""


def check(name, cond, detail=""):
    print(("   PASS  " if cond else "   FAIL  ") + name
          + (f"   {str(detail)[:110]}" if detail else ""))
    if not cond:
        FAILS.append(name)


def section(title):
    print("=" * 70)
    print(title)


def main() -> int:
    text = io.open(SRC / "test_all.py", encoding="utf-8").read()
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    heads = [m.start() for m in _re.finditer(r"^section\(", text, _re.M)]
    end = text.index('section("RESULT")')
    if want:
        start = next((h for h in heads
                      if want.lower() in text[h:h + 300].lower()), -1)
        if start < 0:
            print(f"no section title contains {want!r}")
            return 2
    else:
        start = max(h for h in heads if h < end)
        end = next((h for h in heads if h > start), end)
    body = text[start:end]
    t0 = time.monotonic()
    exec(compile(body, "section", "exec"), globals())
    print()
    print("%.1fs" % (time.monotonic() - t0), "FAILED:", FAILS or "none")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
