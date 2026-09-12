r"""The project's numbers, as deltas against the last time this ran.

The owner's rule for opening a session: say the numbers, and say what is
NEW rather than summarising around it. A total on its own says nothing -
"18.4k downloads" reads the same on a good day and a dead one. What matters
is the change since the last look, which means something has to remember
the last look.

    python _tools\stats.py             the numbers, with deltas
    python _tools\stats.py --save      ...and record them as the baseline
    python _tools\stats.py --json      machine-readable

The snapshot is kept beside the tool's own cache
(%LOCALAPPDATA%\dlss5-autopilot\stats-seen.json), not in the repository:
it is about this account's view of a public page, and it would be noise in
anybody else's checkout.

GitHub's anonymous allowance is 60 calls an hour and this costs three, so
it is safe to run at the start of every round. The git credential helper's
token is used when there is one.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

REPO = "Kizzuwatnaa/DLSS5-Autopilot"
SEEN = (Path(os.environ.get("LOCALAPPDATA", Path.home()))
        / "dlss5-autopilot" / "stats-seen.json")


def _token() -> str:
    """The git credential helper's token, or "" - it only raises the limit."""
    try:
        p = subprocess.run(["git", "credential", "fill"],
                           input="protocol=https\nhost=github.com\n\n",
                           capture_output=True, text=True, timeout=20)
        for line in p.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return ""


def _get(path: str, token: str):
    from core import sources, net
    req = urllib.request.Request("https://api.github.com/repos/" + REPO + path,
                                 headers=dict(sources.UA))
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30,
                                context=net.ssl_context()) as r:
        return json.load(r)


def gather() -> dict:
    token = _token()
    repo = _get("", token)
    rels = _get("/releases?per_page=100", token)
    downloads = {}
    for rel in rels:
        n = sum(a.get("download_count", 0) for a in rel.get("assets", []))
        if n:
            downloads[rel["tag_name"]] = n
    out = {
        "when": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "watchers": repo.get("subscribers_count", 0),
        "open_issues": repo.get("open_issues_count", 0),
        "downloads": downloads,
        "downloads_total": sum(downloads.values()),
    }
    try:
        v = _get("/traffic/views", token)
        out["views_14d"] = v.get("count", 0)
        out["uniques_14d"] = v.get("uniques", 0)
    except Exception:
        pass
    return out


def _delta(now, was) -> str:
    if was is None:
        return ""
    d = now - was
    return f"  ({d:+})" if d else "  (=)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    now = gather()
    try:
        was = json.loads(SEEN.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        was = {}

    if a.json:
        print(json.dumps({"now": now, "was": was}, indent=1))
    else:
        print("=" * 78)
        print("THE NUMBERS" + (f"  (since {was['when']})" if was.get("when")
                               else "  (no earlier snapshot)"))
        print("=" * 78)
        for key, label in (("stars", "stars"), ("forks", "forks"),
                           ("watchers", "watching"),
                           ("open_issues", "open issues"),
                           ("downloads_total", "downloads, all releases"),
                           ("views_14d", "views, 14 days"),
                           ("uniques_14d", "unique visitors, 14 days")):
            if key not in now:
                continue
            print(f"  {now[key]:>8}  {label}{_delta(now[key], was.get(key))}")
        print()
        print("  per release:")
        old = was.get("downloads", {})
        for tag, n in sorted(now["downloads"].items(),
                             key=lambda kv: kv[1], reverse=True)[:8]:
            print(f"    {n:>7}  {tag}{_delta(n, old.get(tag))}")
        new_tags = [t for t in now["downloads"] if t not in old] if old else []
        if new_tags:
            print("  released since the last look: " + ", ".join(new_tags))

    if a.save:
        try:
            SEEN.parent.mkdir(parents=True, exist_ok=True)
            SEEN.write_text(json.dumps(now, indent=1), encoding="utf8")
            print(f"\nrecorded as the baseline: {SEEN}")
        except OSError as e:
            print(f"\ncould not record the baseline: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
