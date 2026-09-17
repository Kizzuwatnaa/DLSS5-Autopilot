r"""The project's numbers for a fixed window - the last two days by default.

The owner's rule for opening a session: say the numbers, and say what is
NEW. An earlier version of this reported "since the last time this ran",
which drifts: after a quiet week it compares against a week ago and calls
that news. A window that is always the same length reads the same way every
day, so a good day and a dead one look different.

    python _tools\stats.py             the last 2 days
    python _tools\stats.py --days 7    a week
    python _tools\stats.py --json      machine-readable

Most of it is counted from dates GitHub itself records, not from a local
snapshot: every star carries the day it was given, every issue the day it
was opened, every comment the day it was written, and the traffic endpoint
is already a per-day table. Those are exact however long ago this last ran.

Download counts are the exception - GitHub publishes a running total per
asset and no history at all - so every run appends its totals to
%LOCALAPPDATA%\dlss5-autopilot\stats-history.jsonl and the window's downloads
are measured against the newest entry at or before the cutoff. It says so
when the history does not reach back far enough, rather than printing a
number that means something else. The history lives beside the tool's own
cache, not in the repository: it is this account's view of a public page.

The git credential helper's token is used when there is one. With a token
this costs a dozen or so calls of the 5000 an hour; without one it is close
to the anonymous allowance of 60, so run it with a token.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

REPO = "Kizzuwatnaa/DLSS5-Autopilot"
CACHE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dlss5-autopilot"
HISTORY = CACHE / "stats-history.jsonl"


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


def _get(path: str, token: str, accept: str = "", full: bool = False):
    """One page. With full=True, the headers come back too (for Link)."""
    from core import sources, net
    req = urllib.request.Request("https://api.github.com/repos/" + REPO + path,
                                 headers=dict(sources.UA))
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=30,
                                context=net.ssl_context()) as r:
        return (json.load(r), dict(r.headers)) if full else json.load(r)


def _when(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)


def _paged_until_old(path: str, token: str, cutoff: datetime, field: str,
                     accept: str = "", pages: int = 6) -> list:
    """Newest-first pages, stopping at the first entry older than cutoff."""
    out = []
    for page in range(1, pages + 1):
        sep = "&" if "?" in path else "?"
        rows = _get(f"{path}{sep}per_page=100&page={page}", token, accept)
        if not rows:
            break
        for row in rows:
            if _when(row[field]) >= cutoff:
                out.append(row)
        if len(rows) < 100 or _when(rows[-1][field]) < cutoff:
            break
    return out


def _stars_since(token: str, cutoff: datetime, total: int) -> int | None:
    """Stargazers are oldest-first, so the new ones are on the last pages."""
    star = "application/vnd.github.star+json"
    last = max(1, math.ceil(total / 100))
    seen = 0
    for page in range(last, max(0, last - 6), -1):
        try:
            rows = _get(f"/stargazers?per_page=100&page={page}", token, star)
        except urllib.error.HTTPError:
            return None          # past the 40k listing limit, or no access
        if not rows:
            continue
        fresh = [r for r in rows if _when(r["starred_at"]) >= cutoff]
        seen += len(fresh)
        if len(fresh) < len(rows):
            break
    return seen


def _traffic(token: str, what: str) -> list:
    try:
        d = _get("/traffic/" + what, token)
        return d.get(what, [])
    except Exception:
        return []


def gather(days: float) -> dict:
    token = _token()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    repo = _get("", token)
    rels = _get("/releases?per_page=100", token)
    downloads = {}
    for rel in rels:
        n = sum(a.get("download_count", 0) for a in rel.get("assets", []))
        if n:
            downloads[rel["tag_name"]] = n

    opened = [i for i in _paged_until_old(
        "/issues?state=all&sort=created&direction=desc", token, cutoff,
        "created_at") if "pull_request" not in i]
    closed = [i for i in _paged_until_old(
        "/issues?state=closed&sort=updated&direction=desc", token, cutoff,
        "updated_at") if "pull_request" not in i
        and i.get("closed_at") and _when(i["closed_at"]) >= cutoff]
    since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    comments = _get(f"/issues/comments?since={since}&per_page=100"
                    "&sort=created&direction=desc", token)
    comments = [c for c in comments if _when(c["created_at"]) >= cutoff]

    out = {
        "when": now.strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "cutoff": cutoff.strftime("%Y-%m-%d %H:%M"),
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "watchers": repo.get("subscribers_count", 0),
        "open_issues": repo.get("open_issues_count", 0),
        "downloads": downloads,
        "downloads_total": sum(downloads.values()),
        "new_stars": _stars_since(token, cutoff,
                                  repo.get("stargazers_count", 0)),
        "new_forks": len(_paged_until_old("/forks?sort=newest", token, cutoff,
                                          "created_at")),
        "opened": [(i["number"], i["title"]) for i in opened],
        "closed": [(i["number"], i["title"]) for i in closed],
        "comments": len(comments),
        "comment_threads": _threads(comments),
        "released": [(r["tag_name"], r["published_at"][:10]) for r in rels
                     if r.get("published_at")
                     and _when(r["published_at"]) >= cutoff],
        "views_by_day": _traffic(token, "views"),
        "clones_by_day": _traffic(token, "clones"),
    }
    return out


def _threads(comments: list) -> list:
    """Which issues people are talking on, busiest first."""
    count: dict[int, int] = {}
    for c in comments:
        m = re.search(r"/issues/(\d+)$", c.get("issue_url", ""))
        if m:
            n = int(m.group(1))
            count[n] = count.get(n, 0) + 1
    return sorted(count.items(), key=lambda kv: -kv[1])[:6]


def _history_before(cutoff: datetime) -> dict | None:
    """The newest recorded totals at or before the cutoff."""
    best = None
    try:
        for line in HISTORY.read_text(encoding="utf8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                stamp = datetime.strptime(row["when"], "%Y-%m-%d %H:%M"
                                          ).replace(tzinfo=timezone.utc)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if stamp <= cutoff and (best is None or stamp > best[0]):
                best = (stamp, row)
    except OSError:
        return None
    return best[1] if best else None


def _history_starts() -> str:
    try:
        for line in HISTORY.read_text(encoding="utf8").splitlines():
            if line.strip():
                return json.loads(line).get("when", "")
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def _record(now: dict) -> None:
    row = {"when": now["when"], "stars": now["stars"], "forks": now["forks"],
           "watchers": now["watchers"], "open_issues": now["open_issues"],
           "downloads_total": now["downloads_total"],
           "downloads": now["downloads"]}
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _days_back(rows: list, days: float) -> list:
    """The last `days` day-buckets of a traffic table, newest first."""
    keep = max(1, int(math.ceil(days)))
    return sorted(rows, key=lambda r: r["timestamp"], reverse=True)[:keep]


def report(now: dict) -> None:
    days = now["days"]
    label = "DAY" if days == 1 else f"{days:g} DAYS"
    print("=" * 78)
    print(f"THE LAST {label}  (since {now['cutoff']} UTC)")
    print("=" * 78)

    stars = now["new_stars"]
    print(f"  {'+%d' % stars if stars is not None else '?':>8}  stars"
          f"{'':14}{now['stars']} in all")
    print(f"  {'+%d' % now['new_forks']:>8}  forks{'':14}{now['forks']} in all")

    cut = datetime.strptime(now["cutoff"], "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc)
    was = _history_before(cut)
    if was:
        d = now["downloads_total"] - was.get("downloads_total", 0)
        print(f"  {'+%d' % d:>8}  downloads{'':10}"
              f"{now['downloads_total']} in all, against {was['when']}")
        per = []
        for tag, n in sorted(now["downloads"].items(), key=lambda kv: -kv[1]):
            gap = n - was.get("downloads", {}).get(tag, n)
            if gap:
                per.append(f"{tag} +{gap}")
        if per:
            print(f"  {'':8}  {' | '.join(per[:6])}")
    else:
        start = _history_starts()
        print(f"  {'':>8}  downloads{'':10}{now['downloads_total']} in all"
              f" - no total recorded {days:g} days back"
              + (f" (history starts {start})" if start else
                 " (history starts now)"))

    views = _days_back(now["views_by_day"], days)
    if views:
        print(f"  {sum(v['count'] for v in views):>8}  views"
              f"{'':14}{sum(v['uniques'] for v in views)} unique")
        for v in views:
            mark = "  (so far today)" if v["timestamp"][:10] == now["when"][:10] else ""
            print(f"  {'':8}    {v['timestamp'][5:10]}  {v['count']:>6} views"
                  f"  {v['uniques']:>5} unique{mark}")
    clones = _days_back(now["clones_by_day"], days)
    if clones:
        print(f"  {sum(c['count'] for c in clones):>8}  clones"
              f"{'':13}{sum(c['uniques'] for c in clones)} unique")

    print()
    print(f"  {len(now['opened']):>8}  issues opened{'':6}"
          f"{now['open_issues']} open now")
    for n, title in now["opened"][:12]:
        print(f"  {'':8}    #{n:<5} {title[:60]}")
    if len(now["opened"]) > 12:
        print(f"  {'':8}    ...and {len(now['opened']) - 12} more")
    print(f"  {len(now['closed']):>8}  issues closed")
    for n, title in now["closed"][:6]:
        print(f"  {'':8}    #{n:<5} {title[:60]}")
    print(f"  {now['comments']:>8}  comments")
    if now["comment_threads"]:
        busy = ", ".join(f"#{n} ({c})" for n, c in now["comment_threads"])
        print(f"  {'':8}    busiest: {busy}")
    if now["released"]:
        print(f"  {len(now['released']):>8}  released"
              f"{'':11}{', '.join(t for t, _ in now['released'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=2,
                    help="how far back the window reaches (default 2)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    now = gather(a.days)
    if a.json:
        print(json.dumps(now, indent=1))
    else:
        report(now)
    _record(now)          # so the next run has a total to measure against
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
