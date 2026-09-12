r"""Every real report, through the current diagnosis, against last time.

The fixes in this project keep breaking each other, and they do it in one
place above all others: `diagnose.py` is 236 if/elif branches and 62
verdicts over ONE input - the logs somebody sent. A rule put in front of
the others changes what all of them see, and nothing says so. That is how
`ours(written)` made `ambiguous()` unreachable, how the `cost:` rule
answered "neural rendering did not start" to a working install, and how a
report can get a verdict nobody meant to change.

So: keep every report anybody ever sent, replay all of them on every
change, and print the ones whose answer moved.

    python _tools\verdict_check.py            compare against the baseline
    python _tools\verdict_check.py --save     record the answers as they are
    python _tools\verdict_check.py --list     print them, change nothing
    python _tools\verdict_check.py --only 175 one report, in full

A changed verdict is not a failure - most changes here are meant. It is a
question: did you mean to change THIS one too? Answer it, then --save.

The corpus is `_tools\reports\<issue>.txt`: the issue bodies as posted, with
the tool's own autopilot.log block dropped and Windows account names
replaced by <user>. The baseline beside it is committed, so the answer is
the same on any machine - the driver comes from each report's own header
rather than from the card in this PC, and the standalone add-on's log is
read from the report instead of from this machine's AppData.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import replay_report                       # noqa: E402
from core import diagnose                  # noqa: E402
from core import gpu as _gpu               # noqa: E402  (patched per report)

REPORTS = HERE / "reports"
BASELINE = HERE / "verdict_baseline.json"

API_RE = re.compile(r"(DX9|DX10|DX11|DX12|Vulkan|OpenGL)")
DRIVER_RE = re.compile(r"driver\s+([\d.]+)")


def _answer(path: Path) -> dict:
    """What the diagnosis says about one saved report, on any machine."""
    text = path.read_text(encoding="utf8", errors="replace")
    logs = replay_report._blocks(text)
    head = replay_report._header(text)
    route = head.get("route", "feeder")
    exe = head.get("exe", "Game.exe")
    api, bitness = "DX12", 64
    if "arch/api" in head:
        m = API_RE.search(head["arch/api"])
        if m:
            api = m.group(1)
        if "32-bit" in head["arch/api"]:
            bitness = 32
    drv = DRIVER_RE.search(head.get("gpu", ""))
    driver = drv.group(1) if drv else None

    d = replay_report.build(route, api, exe, logs, bitness)
    # The standalone add-on keeps its log outside the game folder, and the
    # driver rules ask this PC what it has. Both would make the answer
    # depend on the machine running the check, so both come from the report.
    sa = d / "standalone-dlssnr.log"
    if logs.get("standalone"):
        sa.write_text(logs["standalone"], encoding="utf8")
    try:
        # diagnose imports gpu inside the two functions that ask for it, so
        # the patch goes on the gpu module itself.
        with patch.object(diagnose, "STANDALONE_LOG", sa), \
                patch.object(_gpu, "driver_version", lambda: driver):
            rep = diagnose.analyse(d)
        return {
            "route": rep.route or "",
            "ran": bool(rep.ran),
            "never_ran": bool(getattr(rep, "never_ran", False)),
            "verdict": rep.verdict or "",
            "findings": [f"{f.level}: {f.title}" for f in rep.findings],
        }
    finally:
        shutil.rmtree(d, ignore_errors=True)


def answers(only: str = "") -> dict:
    out = {}
    for p in sorted(REPORTS.glob("*.txt")):
        n = p.stem.lstrip("0") or p.stem
        if only and n != only.lstrip("#"):
            continue
        try:
            out[n] = _answer(p)
        except Exception as e:                     # a crash IS the finding
            out[n] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _diff(old: dict, new: dict) -> list[str]:
    """Every report whose answer moved, said in one place."""
    lines: list[str] = []
    for n in sorted(set(old) | set(new), key=lambda x: int(x) if x.isdigit() else 0):
        a, b = old.get(n), new.get(n)
        if a == b:
            continue
        if a is None:
            lines.append(f"  NEW  #{n}: {b.get('verdict', b)}")
            continue
        if b is None:
            lines.append(f"  GONE #{n}")
            continue
        lines.append(f"  #{n}")
        if a.get("error") or b.get("error"):
            lines.append(f"     error: {a.get('error', '-')} -> {b.get('error', '-')}")
        if a.get("verdict") != b.get("verdict"):
            lines.append(f"     verdict was: {a.get('verdict')}")
            lines.append(f"             now: {b.get('verdict')}")
        for k in ("ran", "never_ran", "route"):
            if a.get(k) != b.get(k):
                lines.append(f"     {k}: {a.get(k)} -> {b.get(k)}")
        gone = [f for f in a.get("findings", []) if f not in b.get("findings", [])]
        came = [f for f in b.get("findings", []) if f not in a.get("findings", [])]
        for f in gone:
            lines.append(f"     - {f}")
        for f in came:
            lines.append(f"     + {f}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    if not REPORTS.is_dir() or not any(REPORTS.glob("*.txt")):
        print(f"no reports in {REPORTS}")
        return 2
    new = answers(a.only)

    if a.list or a.only:
        for n, r in new.items():
            print("=" * 78)
            print(f"#{n}  route={r.get('route')}  ran={r.get('ran')}")
            print(f"  VERDICT: {r.get('verdict', r.get('error'))}")
            for f in r.get("findings", []):
                print(f"    {f}")
        return 0

    if a.save:
        BASELINE.write_text(json.dumps(new, indent=1, sort_keys=True) + "\n",
                            encoding="utf8")
        print(f"recorded {len(new)} answers in {BASELINE.name}")
        return 0

    if not BASELINE.is_file():
        print(f"no baseline yet - run --save once: {BASELINE}")
        return 2
    old = json.loads(BASELINE.read_text(encoding="utf8"))
    lines = _diff(old, new)
    crashed = [n for n, r in new.items() if r.get("error")]
    print("=" * 78)
    print(f"{len(new)} REAL REPORTS THROUGH THE CURRENT DIAGNOSIS")
    print("=" * 78)
    if crashed:
        print(f"  !! the diagnosis raised on: {', '.join('#' + n for n in crashed)}")
    if not lines:
        print("  nothing moved: every report still gets the answer it got.")
        return 1 if crashed else 0
    print(f"  {len([x for x in lines if x.startswith('  #')])} report(s) answer "
          f"differently than they did:")
    print()
    for ln in lines:
        print(ln)
    print()
    print("If every one of those was meant, record them: "
          "python _tools\\verdict_check.py --save")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
