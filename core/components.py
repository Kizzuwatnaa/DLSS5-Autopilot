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
    "dlssd":      "nvngx_dlssd",
    "dlssg":      "nvngx_dlssg",
    "bridge":     "dlss5-bridge",
    "upstream":   "neural-upstream",
    "standalone": "standalone-dlssnr (DLSS5-Reshade-AIO)",
    "uplift":     "GITC-Uplift",
    "feeder":     "DLSS5-Feeder",
    "optiscaler": "OptiScaler",
    "remix_runtime": "RTX Remix runtime (dxvk-remix-plus-dlssnr)",
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
    elif name == "standalone":
        latest = sources.resolve_standalone()[0]
    elif name == "uplift":
        latest = sources.resolve_uplift()[0]
    elif name == "feeder":
        latest = sources.resolve_feeder()[0]
    elif name == "remix_runtime":
        latest = sources.resolve_remix_runtime()[0]
    elif name in ("renodx", "renodx_sf", "dlssnr", "dlss", "dlssg", "dlssd"):
        entries = sources.rhi_catalog().get(name) or []
        if entries:
            latest = entries[0]["label"]
    _LATEST[name] = latest
    return latest


# The build that started picking wilsjo2's standard package. An install
# recorded by anything older got the RTX 40 MFG one.
_STANDARD_ZIP_FROM = (1, 9, 1)


def _before_191(tool) -> bool:
    """Was this install written by a build older than the two-zip fix?

    No recorded version means older than the day the tool recorded one,
    which is older than this.
    """
    k = _key(str(tool or ""))
    return not k or k < _STANDARD_ZIP_FROM


@dataclass
class Item:
    name: str
    installed: str
    latest: str
    outdated: bool
    # Why this one needs installing again when the version did not move -
    # the package behind that version changed. Printed in place of the
    # "installed -> latest" arrow, which would read "0.8.4 -> 0.8.4".
    note: str = ""


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


def _proxy_gone(root: Path, man: dict) -> bool:
    """Did this install write an OptiScaler proxy that is no longer there?

    Read off the folder rather than off the version on the record, so it
    holds whatever emptied it. Only says yes when the manifest names the
    file: an install from before the proxy was recorded says nothing.
    """
    proxy = str(man.get("proxy") or "")
    if not proxy or not man.get("complete", True):
        return False
    try:
        return not (root / proxy).is_file()
    except OSError:
        return False


# Files the install writes that a person or the game may lose without the
# install being damaged: ReShade and the add-ons write them again on the next
# start, and the tool's own switches rename them.
_REGENERATED = (".log", ".ini", ".cfg", ".txt", ".json", ".md", ".off")


def files_gone(root: Path, man: dict) -> list[str]:
    """Files the record says this install wrote that are no longer in the folder.

    A game update or 'verify files' takes everything it does not know about
    out of the folder, and nothing said so: the game started, no add-on
    loaded, and the report said 'nothing we wrote ever loaded'. Read off the
    folder against the record, so it holds whatever removed them.

    Not damage, so not listed: a record that is not complete (a failed or
    half-removed install has its own answers), backups of the game's own
    files, files the components write again, and an add-on the tool itself
    switched off (its name with '.off' on the end).
    """
    from . import installer, net
    if not man or not man.get("complete", True):
        return []
    if net.ROLLED_BACK_NOTE in (man.get("notes") or []):
        return []
    skip = _REGENERATED + (installer.BACKUP_SUFFIX,
                           installer.SIDELINE_SUFFIX) + installer.LEGACY_BACKUP_SUFFIXES
    out = []
    listed = man.get("files")
    for rel in listed if isinstance(listed, list) else []:
        if not isinstance(rel, str) or not rel or rel.endswith(("/", "\\")) or rel.lower().endswith(skip):
            continue
        try:
            if (root / rel).exists() or (root / (rel + ".off")).exists():
                continue
        except OSError:
            continue
        out.append(rel)
    return out


def _gone_item(gone: list[str]) -> Item:
    names = ", ".join(gone[:3]) + (f" and {len(gone) - 3} more" if len(gone) > 3 else "")
    return Item("files in the game folder", f"{len(gone)} missing", "install again", True,
                note=f"the folder no longer has {names} - install again")


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

    gone = files_gone(root, man)
    if not have:
        return [_gone_item(gone)] if gone else []

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
        elif name == "feeder" and not man.get("feeder_tag") \
                and not man.get("feeder_prerelease"):
            # Asked for "newest release" and got a test build: until 2.0.4
            # that setting followed GitHub's pre-release flag, which this
            # project publishes with turned off (#325, #348). The numbers go
            # the wrong way - 1.16.0-beta.5 is above v0.15.1 - so nothing
            # else here would ever say so, and the person who never reads a
            # release note would sit on a beta forever.
            # latest != installed as well: when github.com holds no stable
            # release either, the resolver's last resort hands back this
            # very build, and a mark that installing again cannot clear is
            # worse than no mark at all.
            if latest != installed \
                    and sources._is_prerelease({"tag_name": installed}):
                out.append(Item(LABELS.get(name, name), installed, installed,
                                True, note=("this is a test build - 'newest "
                                            "release' no longer picks one; "
                                            "install again")))
                continue
            outdated = (latest != installed and _key(latest) > _key(installed))
        elif name == "optiscaler" and _proxy_gone(root, man):
            # #364: the fork published a package that was not OptiScaler,
            # this folder got it, and no proxy was ever written. The version
            # on the record cannot say so - a tag is not the package, and
            # one of the forks really does publish under the tag "nightly" -
            # but the folder itself can: the manifest names the proxy this
            # install wrote, and it is not there. Whatever removed it
            # (nothing written, antivirus, somebody tidying up), the answer
            # is the same, and nothing else here would ever say it: the
            # version number on the record is perfectly current.
            out.append(Item(LABELS.get(name, name), installed, installed, True,
                            note=("the proxy this install wrote is not in "
                                  "the folder - install again")))
            continue
        elif name == "optiscaler" and man.get("opti_build"):
            # A fork publishes its own numbers on its own release page;
            # comparing them with Dagherbou's says nothing, and an
            # "outdated" here would never clear - installing again puts the
            # same fork back, because the manifest records which one.
            outdated = False
            # One exception, and it is not about the version: wilsjo2
            # publishes two packages per release, and every build before
            # 1.9.1 took the RTX 40 MFG one whatever the card (#196, #231).
            # The number on disk is right and the files are wrong, so
            # nothing else here would ever say so.
            if man.get("opti_build") == optiscaler.PRESR \
                    and _before_191(man.get("tool")):
                outdated = True
                latest = installed
                out.append(Item(LABELS.get(name, name), installed, latest,
                                True, note=("this is the RTX 40 MFG package"
                                            " - install again for the"
                                            " standard one")))
                continue
        elif name == "renodx":
            # The add-on is pinned on purpose in two cases, and the pin is
            # the newest build that works there - nagging "1 newer" would
            # send people back to the faulting one: 4.55 on driver 616.64+
            # (every evaluate faults with 4.6/4.7), 4.60 on OpenGL (4.70
            # stalls). See sources.py.
            # sources is imported at the top of the module; naming it here
            # too made it a local of this whole function, and the branch
            # above that reads it raised UnboundLocalError.
            from . import gpu
            cap = None
            if gpu.driver_at_least(sources.DRIVER_FAULT_MIN):
                cap = sources.DRIVER_FAULT_RENODX_PIN
            elif (man.get("api") or "") == "OpenGL":
                cap = sources.OPENGL_RENODX_PIN
            if cap and _key(latest) > _key(cap):
                latest = cap
            outdated = (latest != installed and _key(latest) > _key(installed))
        else:
            outdated = (latest != installed and _key(latest) > _key(installed))
        out.append(Item(LABELS.get(name, name), installed, latest, outdated))
    # The proxy has its own line above; do not count the same file twice.
    if any(i.note.startswith("the proxy this install wrote") for i in out):
        gone = [f for f in gone if f != str(man.get("proxy") or "")]
    if gone:
        out.append(_gone_item(gone))
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
    # A component whose package changed under the same version number is
    # not "a newer version", so say what is true of both kinds.
    if any(getattr(i, "note", "") for i in stale):
        return f"{len(stale)} of {len(items)} components need installing again"
    return f"{len(stale)} of {len(items)} components have a newer version"
