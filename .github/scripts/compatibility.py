"""Turn the shared results into docs/compatibility.json.

Run by .github/workflows/compatibility.yml. It reads the machine-readable
block the tool writes into each shared result (core/community.py owns the
format and the parser, and this script imports it so the two can never
drift apart), and writes one small aggregate:

    {"generated": "2026-09-09T11:00:00Z",
     "reports": 812,
     "games": {"cyberpunk2077.exe": {
        "name": "Cyberpunk 2077",
        "routes": {"optiscaler": {"worked": 41, "failed": 6}},
        "drivers": {"616.64": {"worked": 3, "failed": 22}}}}}

Counts only. No user names, no issue numbers, nothing that ties a row back
to a person - the whole point of the file is that it can be published
without anyone having agreed to be published.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.community import parse            # noqa: E402  the one parser

OUT = ROOT / "docs" / "compatibility.json"
API = "https://api.github.com/repos/{repo}/issues"


def issues(repo: str, token: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while page <= 20:                        # 2000 issues is plenty of head room
        # No label filter: GitHub silently drops ?labels= from a
        # prefilled new-issue URL for anyone without triage rights on the
        # repository, so most results arrive unlabelled. parse() is the
        # filter that matters - it ignores anything without a valid record.
        url = (f"{API.format(repo=repo)}?state=all"
               f"&per_page=100&page={page}")
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "dlss5-autopilot-compatibility",
            "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            batch = json.load(r)
        if not batch:
            break
        out += [i for i in batch if "pull_request" not in i]
        if len(batch) < 100:
            break
        page += 1
    return out


def main() -> int:
    repo = os.environ.get("REPO") or "Kizzuwatnaa/DLSS5-Autopilot"
    token = os.environ.get("GH_TOKEN") or ""
    games: dict[str, dict] = {}
    seen = 0
    for issue in issues(repo, token):
        rec = parse(issue.get("body") or "")
        if not rec or not rec.get("exe") or not rec.get("route"):
            continue
        seen += 1
        g = games.setdefault(rec["exe"], {"name": rec.get("game") or "",
                                          "routes": {}, "drivers": {}})
        if rec.get("game") and not g["name"]:
            g["name"] = rec["game"]
        for key, bucket in (("route", "routes"), ("driver", "drivers")):
            value = (rec.get(key) or "").strip()
            if not value:
                continue
            row = g[bucket].setdefault(value, {"worked": 0, "failed": 0})
            row[rec["result"]] += 1

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reports": seen,
        "games": dict(sorted(games.items())),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                   encoding="utf8")
    print(f"{seen} results across {len(games)} games -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
