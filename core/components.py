r"""Are the parts installed in a game still the current ones?

A fresh install always fetches the newest of everything, so the day you set a
game up it is current. Nothing told you afterwards. ReShade, renodx, the
neural-rendering runtime and OptiScaler all move, and someone whose game was
set up a month ago had no way to know they were a version behind short of
reinstalling and reading the log.

This compares the versions recorded in a game's manifest against what the
sources offer now. It only reports - the fix is to install again, which
already pulls the newest of everything.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import log, optiscaler, sources

MANIFEST = "dlss5-autopilot.json"

LABELS = {
    "reshade":    "ReShade",
    "renodx":     "DLSS 5 add-on (renodx-dlss5)",
    "renodx_sf":  "DLSS 5 add-on (renodx-dlss SF)",
    "dlssnr":     "nvngx_dlssnr",
    "dlss":       "nvngx_dlss",
    "bridge":     "dlss5-bridge",
    "upstream":   "neural-upstream",
    "feeder":     "DLSS5-Feeder",
    "optiscaler": "OptiScaler",
}

# Resolved once per process, not once per game: a library with thirty
# installs must not spend thirty API requests on the same question.
_LATEST: dict[str, str] = {}


def _latest(name: str) -> str:
    if name in _LATEST:
        return _LATEST[name]
    latest = ""
    if name == "reshade":
        latest = sources.resolve_reshade()[0]
    elif name == "optiscaler":
        latest = optiscaler.resolve()[0]
    elif name == "bridge":
        latest = sources.resolve_bridge()[0]
    elif name == "upstream":
        latest = sources.resolve_upstream()[0]
    elif name == "feeder":
        latest = sources.resolve_feeder()[0]
    elif name in ("renodx", "renodx_sf", "dlssnr", "dlss"):
        entries = sources.rhi_catalog().get(name) or []
        if entries:
            latest = entries[0]["label"]
    _LATEST[name] = latest
    return latest


@dataclass
class Item:
    name: str
    installed: str
    latest: str
    outdated: bool


def _read(root: Path) -> dict:
    for name in (MANIFEST, "dlss5kur-kurulum.json", "dlss5-installer.json"):
        p = root / name
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def _key(v: str) -> tuple:
    """Compare version-ish labels by their numbers, not alphabetically.

    "310.8.0-RTX40" and "310.8.SF-v2" are not ordered by any scheme we can
    infer, so anything that does not reduce to numbers is compared as text and
    simply reported as different rather than older.
    """
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4])


def check(root: Path) -> list[Item]:
    """What is installed in this folder against what is current.

    Network failures are not errors - a component we cannot resolve is left
    out rather than reported as up to date or as stale.
    """
    man = _read(root)
    have: dict = man.get("components") or {}
    if not have:
        # Installs from before versions were recorded structurally still have
        # them in the notes, as "<name> version: <label>".
        for note in man.get("notes", []):
            m = re.match(r"(\w+) version: (.+)", str(note))
            if m and m.group(1) in LABELS:
                have[m.group(1)] = m.group(2).strip()

    if not have:
        return []

    out: list[Item] = []
    for name, installed in have.items():
        try:
            latest = _latest(name)
        except Exception as e:
            log.write(f"component check: could not resolve {name} ({e})", "warn")
            continue
        # "latest" is what an install records when GitHub's API was out
        # of reach and the download redirect was used: a real file, but no
        # version to compare. Not behind, not current - left out.
        if not latest or latest == "latest" or installed == "latest":
            continue
        # Only call it outdated when the numbers actually go up. A different
        # build family (an -RTX40 against an SF build, say) is a choice, not a
        # version behind, and must not be nagged about. The neural-rendering
        # runtime is picked per card, so a newer label there is not "behind"
        # either.
        if name == "dlssnr":
            outdated = False
        else:
            outdated = (latest != installed and _key(latest) > _key(installed))
        out.append(Item(LABELS.get(name, name), installed, latest, outdated))
    return out


def stale_counts(roots: list[Path]) -> dict[str, int]:
    """{install folder: number of outdated components} across a library.

    Cheap after the first game: every source is resolved once and the rest is
    reading manifests. Meant for the game list, so a person whose games were
    set up a month ago sees "update" next to them without asking.
    """
    out: dict[str, int] = {}
    for root in roots:
        try:
            items = check(root)
        except Exception:
            continue
        n = len([i for i in items if i.outdated])
        if n:
            out[str(root)] = n
    return out


def summary(items: list[Item]) -> str:
    stale = [i for i in items if i.outdated]
    if not items:
        return "nothing recorded to check"
    if not stale:
        return f"all {len(items)} components are current"
    return f"{len(stale)} of {len(items)} components have a newer version"
