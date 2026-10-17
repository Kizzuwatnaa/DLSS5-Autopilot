r"""Where this project's own improvement loop stands, right now.

The checks exist and they run themselves. What was still missing is the
part that makes the loop close: something that MEASURES the tool's answers,
ranks what is worst, and says so at the start of every round without being
asked. Otherwise the next round is whatever the last report happened to be
about, and the same classes of mistake come round again - which is exactly
what the owner said was happening.

    python _tools\state.py              offline, fast: measure and rank
    python _tools\state.py --sync       also fetch new reports into the corpus
    python _tools\state.py --json       machine-readable, for the hook

Four questions, answered from what is on disk:

  1. What do the tool's answers look like across every report anybody has
     sent? Which class is the biggest, and is it bigger than last time?
  2. Is the corpus current, or has the tracker moved on without it?
  3. Are the guard rails alive - the hooks wired, the checks clean?
  4. What is uncommitted, untagged, or left half done?

The ranking is the backlog. A class near the top is not a bug report, it is
a shape: "34 reports get told the game was never started" was found this
way, and it was a third of everyone.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
ROOT = SRC.parent                      # the folder holding .claude and the notes
sys.path.insert(0, str(SRC))

BASELINE = HERE / "verdict_baseline.json"
REPORTS = HERE / "reports"
SETTINGS = ROOT / ".claude" / "settings.json"
HOOKS = HERE / "hooks"

# The shape of an answer, not its words. Ordered: the first that matches
# names the class, so the specific ones come before the vague ones.
CLASSES = (
    # Something that is not ours sits in front of our file or beside our
    # hook: another DLSS hook (#250, #439), ReShade's own d3d9.dll found
    # before DXVK's (#348).
    ("something else stands in front of ours or beside it",
     ("another dlss hook", "in front of dxvk", "older bridge add-on")),
    ("nothing we wrote ever loaded",
     ("not started since the install", "closed during start-up",
      "nothing this install wrote was loaded", "never loaded",
      "is not in the game folder",
      # #328: the game took d3d9.dll from System32, ours was never reached
      "ours is never reached",
      # #403, #238, #367, #400, #11, #191: ran and loaded nothing of ours,
      # the Vulkan layer never reached it, or it went beside a launcher
      "loaded nothing from this folder", "no log was written",
      "vulkan layer is not registered", "dxvk ran and reshade did not",
      "beside a launcher")),
    # The game draws in a way the route cannot use: it refused the swap
    # chain (#58, #89), made no DLSS call (#390), drew with Direct3D 9 on a
    # D3D11/12 install (#479), or Remix never tried the pass (#211).
    ("the game does not draw the way the route needs",
     ("refused", "no d3d12 dlss call", "never called dlss",
      "direct3d 9 device", "never even attempted",
      # #682: the running game held another renderer and no DXGI
      "no directx 10/11/12 when the tool looked", "only directdraw loaded",
      # 2.0.7: the bridge attached, nothing called DLSS, no frame
      "delivered no frame - turn dlss on",
      "delivered no frame - its log shows no dlss")),
    ("upstream: the driver's NGX runtime faults",
     ("driver 616.64", "faults inside", "no dlss 5 entry point")),
    # The person said the game closed itself or never started, and the
    # logs could not say why: the answer is what to take out first (#412).
    # Before "cannot tell" and the overlay ask, which it replaces.
    ("they say it closed or never started - we name what to take out",
     ("closed itself", "never started with this install",
      "windows recorded the game faulting")),
    # 2.0.7: the bridge's own log counts the frames it handed on, and no log
    # says whether the neural pass drew them. Was the overlay ask below.
    ("frames reach the add-on, and no log says if the pass drew them",
     ("whether the neural pass drew them",)),
    ("we cannot tell from the logs",
     ("inconclusive", "did not get far enough",
      # 2.0.7: the bridge's synthetic path handed on no frame
      "delivered no frame from the driver",
      # #252: frames went to the 64-bit helper and its log was not there
      "only its own log says")),
    ("we ask the person to look in the overlay",
     # "...and the switch is on. This route logs no frames, so the panel is
     # the only live picture" (#287, #352) is the same ask in other words
     ("confirm in the", "check '", "switched on", "the switch is on")),
    # Two shapes that used to hide inside "nothing we wrote ever loaded",
    # because the replay put every file back and invented a finished install
    # record: the answer was measured against a folder nobody had. They are
    # their own classes - one is the installer's to fix, the other is a
    # person pointed at a folder we never installed into.
    ("the install stopped part way",
     ("install never finished", "install stopped for a reason",
      "drive was full", "uninstall left files",
      # 2.0.6: the connection verdicts (#434, #438) - "The install
      # stopped: Windows blocked the download ...", and the crash rule's
      # own "The install crashed before it finished" (#213 once had it)
      "the install stopped:", "install crashed before it finished")),
    ("we were never installed in that folder",
     ("nothing is installed in this folder", "no install record here")),
    ("the install is incomplete on disk",
     ("is missing - install again", "reinstall", "missing a runtime file")),
    ("it ran and then something stopped",
     ("the feed stopped", "crashed", "no neural frame",
      # 2.0.7: the bridge's own stop line, after frames or before the first,
      # and DLSS called with no frame handed on
      "the bridge stopped", "the bridge delivered no frame")),
    ("working", ("working.",)),
)


def classify(verdict: str) -> str:
    low = (verdict or "").lower()
    for name, needles in CLASSES:
        if any(n in low for n in needles):
            return name
    return "other"


def _run(args: list[str], timeout: int = 240) -> tuple[int, str]:
    try:
        r = subprocess.run(args, cwd=str(SRC), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def corpus() -> dict:
    """The distribution of answers over every saved report."""
    if not BASELINE.is_file():
        return {}
    base = json.loads(BASELINE.read_text(encoding="utf8"))
    counts: Counter = Counter()
    where: dict = defaultdict(list)
    for n, r in base.items():
        c = classify(r.get("verdict", ""))
        counts[c] += 1
        where[c].append(int(n) if str(n).isdigit() else 0)
    return {"total": len(base), "counts": counts, "where": where}


def new_on_the_tracker(online: bool) -> list[int]:
    """Issue numbers with logs that the corpus does not have yet."""
    if not online:
        return []
    have = {int(p.stem) for p in REPORTS.glob("*.txt") if p.stem.isdigit()}
    try:
        from core import sources, net       # noqa: F401
        import urllib.request
        url = ("https://api.github.com/repos/Kizzuwatnaa/DLSS5-Autopilot/"
               "issues?state=all&per_page=100&sort=created&direction=desc")
        req = urllib.request.Request(url, headers=sources.UA)
        with urllib.request.urlopen(req, timeout=30,
                                    context=net.ssl_context()) as r:
            data = json.load(r)
    except Exception:
        return []
    out = []
    for i in data:
        if "pull_request" in i:
            continue
        body = i.get("body") or ""
        if "**ReShade.log**" not in body:
            continue
        if i["number"] not in have:
            out.append(i["number"])
    return sorted(out)


def guards() -> list[str]:
    """What is wrong with the guard rails themselves, if anything."""
    bad: list[str] = []
    if not SETTINGS.is_file():
        # Another checkout: the hook wiring lives beside the repository and
        # travels with the machine, not with the code. Nothing to report.
        return []
    try:
        cfg = json.loads(SETTINGS.read_text(encoding="utf8"))
        wired = json.dumps(cfg.get("hooks") or {})
    except Exception as e:
        bad.append(f"{SETTINGS.name} does not read: {e}")
        wired = ""
    for name in ("no_backslash_heredoc.py", "after_edit.py", "on_stop.py"):
        p = HOOKS / name
        if not p.is_file():
            bad.append(f"the hook script {name} is gone")
        elif name not in wired:
            bad.append(f"{name} exists but nothing is wired to it")
    return bad


def repo() -> dict:
    out = {}
    _, out["branch"] = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], 30)
    _, dirty = _run(["git", "status", "--porcelain"], 30)
    out["dirty"] = [ln for ln in dirty.splitlines() if ln.strip()]
    _, tag = _run(["git", "describe", "--tags", "--abbrev=0"], 30)
    out["last_tag"] = tag.splitlines()[0] if tag and "fatal" not in tag else "(none)"
    if out["last_tag"] != "(none)":
        _, ahead = _run(["git", "rev-list", "--count",
                         f"{out['last_tag']}..HEAD"], 30)
        out["since_tag"] = ahead.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true",
                    help="fetch reports the corpus does not have yet")
    ap.add_argument("--online", action="store_true",
                    help="ask the tracker what is new (implied by --sync)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    online = a.sync or a.online

    c = corpus()
    new = new_on_the_tracker(online)
    g = guards()
    r = repo()

    if a.json:
        print(json.dumps({
            "reports": c.get("total", 0),
            "classes": dict(c.get("counts", {})),
            "new_on_tracker": new,
            "guards_broken": g,
            "repo": {k: v for k, v in r.items() if k != "dirty"},
            "dirty": len(r.get("dirty", [])),
        }, indent=1))
        return 0

    print("=" * 78)
    print("WHERE THE LOOP STANDS")
    print("=" * 78)
    if c:
        print(f"\n  {c['total']} reports replayed. What the tool tells people:")
        for name, n in c["counts"].most_common():
            nums = sorted(x for x in c["where"][name] if x)[-6:]
            share = 100.0 * n / max(1, c["total"])
            print(f"    {n:3}  {share:4.0f}%  {name:<44} "
                  + " ".join("#" + str(x) for x in nums))
        worst = c["counts"].most_common(1)[0]
        print(f"\n  The backlog, in order: '{worst[0]}' is {worst[1]} of "
              f"{c['total']}.")
        print("  A class at the top is a shape to fix, not a report to answer.")
    else:
        print("\n  no verdict baseline yet - run _tools/verdict_check.py --save")

    if online:
        if new:
            print(f"\n  {len(new)} report(s) on the tracker the corpus has not "
                  f"seen: " + " ".join("#" + str(n) for n in new[-12:]))
            print("  Add them: python _tools/state.py --sync")
        else:
            print("\n  the corpus is current with the tracker")
    else:
        print("\n  (offline: run with --online to ask the tracker what is new)")

    rc, out = _run([sys.executable, str(HERE / "memory_check.py")], 120)
    first = [ln for ln in out.splitlines() if "memories," in ln]
    if first:
        print("\n  the notes: " + first[0].strip()
              + ("" if rc == 0 else " - SOME NAME THINGS THAT ARE GONE, run "
                                   "python _tools/memory_check.py"))

    print("\n  guard rails: " + ("all wired and present" if not g
                                 else "\n    !! " + "\n    !! ".join(g)))
    print(f"  repo: {r.get('branch')}, {r.get('since_tag', '?')} commit(s) "
          f"since {r.get('last_tag')}, {len(r.get('dirty', []))} file(s) "
          f"uncommitted")

    if a.sync and new:
        added = sync(new)
        print(f"\n  fetched {added} report(s) into {REPORTS.name}/ - now run "
              f"python _tools/verdict_check.py to see what they answer, "
              f"then --save")
    return 0


def sync(numbers: list[int]) -> int:
    """Write the tracker's new reports into the corpus, scrubbed."""
    from core import sources, net
    import urllib.request
    user_win = re.compile(r"(?i)([A-Z]:\\Users\\)[^\\\r\n]+")
    user_nix = re.compile(r"(?i)(/home/)[^/\r\n]+")
    autopilot = re.compile(
        r"\*\*autopilot\.log[^\n]*\*\*\s*\n```[^\n]*\n.*?```", re.S)
    REPORTS.mkdir(parents=True, exist_ok=True)
    done = 0
    for n in numbers:
        url = ("https://api.github.com/repos/Kizzuwatnaa/DLSS5-Autopilot/"
               f"issues/{n}")
        try:
            req = urllib.request.Request(url, headers=sources.UA)
            with urllib.request.urlopen(req, timeout=30,
                                        context=net.ssl_context()) as r:
                body = (json.load(r).get("body") or "")
        except Exception:
            continue
        if not body.strip():
            continue
        body = user_win.sub(r"\1<user>", body)
        body = user_nix.sub(r"\1<user>", body)
        body = autopilot.sub("", body)
        (REPORTS / f"{n:03d}.txt").write_text(body.strip() + "\n",
                                              encoding="utf8", newline="\n")
        done += 1
    return done


if __name__ == "__main__":
    raise SystemExit(main())
