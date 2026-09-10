r"""What changed upstream since the last time this was run.

Half of every issue round turns out to be an upstream project that moved -
a release page that changed shape, a new fork someone dropped into their
game folder, a component that stopped publishing the file name we ask for.
Finding that out from bug reports costs a release each time, so this asks
GitHub directly, and remembers what it saw.

    python _tools\upstream_watch.py            # what changed since last run
    python _tools\upstream_watch.py --all      # everything, changed or not
    python _tools\upstream_watch.py --save     # ...and record this as seen

It reports three things:

1. **The components this tool installs** (core/sources.py and friends): the
   newest release of each, and whether the asset names this tool asks for
   are still there. This is the check that would have caught the ffmpeg
   break (#75) and the DLSS5-Reshade-AIO zip (#65) before a user did.
2. **Projects that are watched but not installed** - the ones on the "maybe
   one day" list, so a first release does not go unnoticed.
3. **A search** for repositories that look like they belong in this
   ecosystem and were pushed recently, so a project nobody has mentioned
   yet still shows up.

Nothing here writes to a game folder or downloads a component; it only
reads release pages.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Release notes are written by other people, and they put emoji in them. A
# Turkish console is cp1254, and printing one killed the whole run half way
# down the list - the same crash the Windows event reader had. Print what
# can be printed and carry on.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core import dxvk, mfg, optiscaler, prefs, refw, sources, video   # noqa: E402

SEEN = prefs.FILE.parent / "upstream-seen.json"

# name -> (releases API, asset names this tool asks for by exact name)
# The asset check is the point: a release that still exists but no longer
# carries the file we ask for is exactly how a download breaks silently.
# "{tag}" in a name is replaced with the release's own tag before checking,
# because half of these are versioned. Ask for what the resolver asks for
# TODAY, not what it used to: a check that cries wolf every run is worse
# than no check.
INSTALLED = {
    "DLSS5-Feeder": (sources.FEEDER_API, ()),
    "dlss5-bridge": (sources.BRIDGE_API, ()),
    "DLSS5-Reshade-AIO": (sources.STANDALONE_API,
                          (sources.STANDALONE_ZIP_NAME,)),
    "OptiScaler (Dagherbou)": (optiscaler.API, ()),
    "OptiScaler fork (y4my4my4m)": (optiscaler.FORK_API, ()),
    "OptiScaler fork (wilsjo2)": (optiscaler.PRESR_API, ()),
    "dxvk-remix-plus-dlssnr": (sources.REMIX_RUNTIME_API, sources.REMIX_RUNTIME_ASSETS),
    "ffmpeg (BtbN)": (video.FFMPEG_API, (video.FFMPEG_ASSET,)),
    # The unlock resolves by prefix across its release list and skips a
    # release of another shape, so an exact name here would cry every run
    # since v1.3.2; the tag moving is the signal to look (#141).
    "RTX40MFG-Unlock (dashdogy)": (mfg.API, ()),
    "Ultimate ASI Loader": (mfg.LOADER_API, (mfg.LOADER_ASSET,)),
    "REFramework nightly": (refw.API, ("REFramework.zip",)),
    "DXVK": (dxvk.API, ()),
    "neural-upstream": (sources.UPSTREAM_API, (sources.UPSTREAM_ASSET,)),
}

# Watched, deliberately not installed. The note says why, so a future
# session does not have to work it out again.
WATCHED = {
    "sdli1995/dlssg_for_sm86":
        "frame generation on RTX 30 (sm_86). No release, no licence, and it "
        "is a version.dll proxy - the same slot as the MFG loader and one of "
        "the OptiScaler proxies. Add only if it publishes releases under a "
        "licence that allows fetching.",
    "ShyVortex/dlss-unlocked":
        "MFG 2x-4x on RTX 20/30/40 by bundling DLLs their publishers do not "
        "ship. Bundled binaries - watch for a build that fetches its own.",
    "xenmods/DLSSNR-Cost-Scaler":
        "a nvngx_dlssnr proxy with a resolution dial; a candidate for the "
        "native/renodx routes.",
    "kibblerz/DLSS5-Reshade-AIO":
        "already installed as the standalone route; here too so its shape "
        "changes are noticed.",
    "bmitch87/DLSS5VKLayer":
        "a Vulkan layer, and the name reads like a collision with ReShade's "
        "- but it is Linux only (deb/rpm/tar.gz, no Windows build), so it "
        "cannot share a registry with ours. Nothing to do unless it gains a "
        "Windows layer.",
    "RedDukeDev/dlss5-image-enhancer-zluda":
        "runs the network over a single picture, and on AMD through ZLUDA. "
        "MIT with releases. Not a game route, but it is the third project "
        "getting the network onto Radeon - evidence for the AMD answer in "
        "gpu.AMD_ANSWER, not an install.",
    "banbanzhige/DLSS5Tool":
        "a standalone Windows video and image upscaler (MIT). Writes into "
        "no game folder and hooks nothing, so it is not an OTHER_NGX_HOOKS "
        "entry; overlaps the video player instead.",
    "m0chs/DLSS5-32bit":
        "the same 32-bit approach this tool already ships - ReShade plus a "
        "separate 64-bit helper. Nothing to take from it while it is a beta "
        "with no users; watch in case it lands on marker files that would "
        "share a folder with ours.",
}

# Repositories pushed recently whose name or description looks like this
# ecosystem. Deliberately broad - the point is to be surprised.
SEARCHES = ("dlss5", "dlssnr neural rendering", "dlssg frame generation",
            "nvngx_dlssnr", "reshade dlss addon")


def _iso(s: str) -> str:
    return (s or "")[:16].replace("T", " ")


def _age(s: str) -> str:
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - d).days
        return "today" if days == 0 else f"{days}d ago"
    except Exception:
        return "?"


def _get(url: str):
    try:
        return sources._json(url)
    except Exception as e:
        return {"__error__": str(e)}


# What a release SAYS it fixed. A component that starts surviving driver
# 616.64, or a fork that fixes a leak, changes the answer this tool gives
# people - and that is never in the version number.
_NOTES: dict[str, str] = {}


def _newest(api: str):
    """(tag, published, assets) of the newest usable release."""
    data = _get(api)
    if isinstance(data, dict) and data.get("__error__"):
        return None, data["__error__"], []
    rels = data if isinstance(data, list) else [data]
    rels = [r for r in rels if isinstance(r, dict) and not r.get("draft")]
    rels.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    if not rels:
        return None, "no releases", []
    r = rels[0]
    _NOTES[api] = "\n".join((r.get("name") or "", r.get("body") or "")).strip()
    return (r.get("tag_name", "?"), r.get("published_at", ""),
            [a.get("name", "") for a in r.get("assets", [])])


def _what_they_fixed(api: str, lines: int = 6) -> list[str]:
    """The first few lines of the release notes, tidied for reading."""
    out = []
    for ln in (_NOTES.get(api) or "").splitlines():
        ln = ln.strip().lstrip("#*-").strip()
        if ln and not ln.startswith(("!", "|", "<", "```")):
            out.append(ln[:110])
        if len(out) >= lines:
            break
    return out


def main() -> int:
    show_all = "--all" in sys.argv
    save = "--save" in sys.argv
    try:
        seen = json.loads(SEEN.read_text(encoding="utf8"))
    except Exception:
        seen = {}
    fresh: dict = {}
    news: list[str] = []

    print("=" * 78)
    print("COMPONENTS THIS TOOL INSTALLS")
    print("=" * 78)
    for name, (api, want) in INSTALLED.items():
        tag, when, assets = _newest(api)
        if tag is None:
            print(f"  !! {name:<30} could not be read: {when}")
            news.append(f"{name}: release page unreadable ({when})")
            continue
        fresh[name] = tag
        moved = seen.get(name) not in (None, tag)
        missing = [w for w in (x.replace("{tag}", tag) for x in want)
                   if w not in assets]
        flag = "NEW " if moved else "    "
        if moved or missing or show_all:
            print(f"  {flag}{name:<30} {tag:<28} {_iso(when)}  ({_age(when)})")
        if moved:
            news.append(f"{name}: {seen.get(name)} -> {tag}")
            # The owner's rule: before a release, read what THEY fixed, not
            # just that a number moved. A component that starts surviving a
            # driver, or fixes a leak, changes what this tool should say.
            for ln in _what_they_fixed(api):
                print(f"         | {ln}")
        if missing:
            print(f"      !! ASKS FOR FILES THAT ARE NOT THERE: {', '.join(missing)}")
            print(f"         release carries: {', '.join(assets[:6]) or '(none)'}")
            news.append(f"{name}: the asset names this tool asks for are gone "
                        f"({', '.join(missing)}) - either a download is broken "
                        f"or it is running on its fallback; check the fallback "
                        f"in sources.resolve_* / video.ensure_ffmpeg still "
                        f"matches what the release carries")

    print()
    print("=" * 78)
    print("WATCHED, NOT INSTALLED")
    print("=" * 78)
    for repo, why in WATCHED.items():
        r = _get(f"https://api.github.com/repos/{repo}")
        if r.get("__error__"):
            print(f"  !! {repo:<40} {r['__error__'][:40]}")
            continue
        tag, when, _ = _newest(f"https://api.github.com/repos/{repo}/releases?per_page=5")
        key = f"watch:{repo}"
        state = f"{tag or 'no release'} | *{r.get('stargazers_count')}"
        fresh[key] = state
        moved = seen.get(key) not in (None, state)
        if moved or show_all:
            print(f"  {'NEW ' if moved else '    '}{repo:<40} {state:<22} "
                  f"pushed {_age(r.get('pushed_at'))}")
            print(f"        {why}")
        if moved:
            news.append(f"{repo}: {seen.get(key)} -> {state}")
            for ln in _what_they_fixed(
                    f"https://api.github.com/repos/{repo}/releases?per_page=5"):
                print(f"         | {ln}")

    print()
    print("=" * 78)
    print("PUSHED IN THE LAST WEEK, LOOKS LIKE THIS ECOSYSTEM")
    print("=" * 78)
    known = {r.lower() for r in WATCHED} | {
        "kizzuwatnaa/dlss5-autopilot", "y4my4my4m/optiscaler_dlssnr_multipass_mfg",
        "wilsjo2/optiscaler-dlssnr-presr-multipass", "dagherbou/optiscaler_dlssnr"}
    hits: dict[str, dict] = {}
    for q in SEARCHES:
        r = _get("https://api.github.com/search/repositories?sort=updated&order=desc&q="
                 + urllib.parse.quote(f"{q} pushed:>2026-09-01"))
        for x in (r.get("items") or [])[:10]:
            if x["full_name"].lower() not in known:
                hits[x["full_name"]] = x
    for full, x in sorted(hits.items(), key=lambda kv: kv[1].get("pushed_at", ""),
                          reverse=True)[:20]:
        key = f"seen:{full}"
        fresh[key] = "1"
        if key not in seen:
            news.append(f"new repository: {full} (*{x['stargazers_count']})")
        print(f"  {'NEW ' if key not in seen else '    '}{full:<48} "
              f"*{x['stargazers_count']:<5} {_age(x.get('pushed_at'))}")
        if x.get("description"):
            print(f"        {x['description'][:100]}")

    print()
    print("=" * 78)
    if news:
        print(f"{len(news)} THING(S) CHANGED SINCE THE LAST RUN")
        for n in news:
            print("  -", n)
    else:
        print("nothing changed since the last run")
    print("=" * 78)

    if save:
        try:
            SEEN.parent.mkdir(parents=True, exist_ok=True)
            SEEN.write_text(json.dumps(fresh, indent=1), encoding="utf8")
            print(f"recorded as seen: {SEEN}")
        except OSError as e:
            print(f"could not record: {e}")
    else:
        print("(run again with --save to record this as the new baseline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
