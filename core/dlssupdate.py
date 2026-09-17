r"""Keeping a game's own NVIDIA DLSS runtimes current, with or without DLSS 5.

A game ships super resolution (nvngx_dlss.dll), frame generation
(nvngx_dlssg.dll) and ray reconstruction (nvngx_dlssd.dll) where its engine
loads them, and keeps whatever build it shipped with for years. This module
finds them, compares them with NVIDIA's own newest build, replaces them, and
puts the game's own back on request.

The record
----------
`dlss5-dlss-update.json` in the game's root folder lists every file this
module replaced: its path relative to that folder, the family, the game's
own version, and the version, size and SHA-256 of what was written. The
game's own file is kept beside it as `<name>.dlss5-dlss-original`.

That suffix is deliberately NOT the DLSS 5 installer's `.dlss5-autopilot-
backup`. The installer's uninstall restores every backup with its own suffix
it finds in the install folder, and reads one beside a runtime as "this
install swapped it". Sharing it would make uninstalling DLSS 5 silently undo
a DLSS update, and would make the next DLSS 5 install treat the game's
runtime as its own. With separate suffixes the two writers stack:

    game's own  ->  DLSS update  ->  DLSS 5 install swap
                    (.dlss5-dlss-original)  (.dlss5-autopilot-backup)

and each one takes back only its own layer.

Invariants
----------
1. The game's own file exists on disk at all times: as the runtime itself,
   as our `.dlss5-dlss-original`, as the DLSS 5 install's backup, or set
   aside under a suffix of ours. No operation here deletes a runtime that is
   not a build this module wrote itself, or a backup that is not an exact
   copy of the file on disk; a runtime in the way is renamed
   `.dlss5-dlss-displaced-<time>`.
2. A backup is made from the file on disk when there is none, and never
   overwritten. Updating a build this module wrote keeps the first backup,
   and with that backup gone makes none: the file on disk is then byte for
   byte what this module wrote (the record's hash of it), not the game's
   own, and filing it as the original would let restore put it over a
   genuine file later. A backup found with no record (the record was lost)
   is adopted as the original.
3. A file the DLSS 5 install swapped (its backup sits beside it) is not
   updated here: the install owns that layer, and its uninstall would put
   back what was under it. The page says to change it in the game's
   settings.
4. A DLSS 5 install over a file updated here backs up OUR build as "what was
   there", and its uninstall restores it - so uninstalling DLSS 5 leaves the
   DLSS update in place. Restoring here while DLSS 5 is installed on top
   hands the game's own file to the install's backup slot - only when that
   backup is the build this module wrote; otherwise nothing moves and the
   page says to uninstall DLSS 5 first.
5. A file that is no longer what was written (a launcher verified its files,
   the game updated itself, another tool) is left alone. Its backup and
   record entry stay - another tool may put our build back, and only the
   backup holds the game's own - until the file on disk is an exact copy of
   the backup (the launcher put the game's own back): then the backup is a
   duplicate, and it and the entry go.
6. Restore with the backup missing leaves the updated file in place, drops
   the entry and says so. A runtime that disappeared while its backup is
   there is put back by restore.
7. Everything read from the record is untrusted: an entry must resolve
   inside the game (or its install folder) and name one of the three
   runtimes, and the backup path is derived from it, never read. Whether a
   backup is needed is decided from the disk, not from the record; the
   record only withholds one when the file's own bytes hash to what it says
   was written. Its version and size are not enough: a record naming the
   game's own build would otherwise send that file away unkept.
8. The download is a 64-bit Windows DLL of a sane size before anything is
   touched, the runtime is re-read right before it is replaced (another
   writer may have been at it), and it is replaced whole (written beside,
   then renamed over), never half.
9. One writer at a time: the window runs these jobs under the same busy flag
   installs, autopilot and uninstall wait for.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import anticheat, dlss, installer, log, net, pe, prefs, sources, watch

FAMILIES = (("dlss", "nvngx_dlss.dll", "super resolution"),
            ("dlssg", "nvngx_dlssg.dll", "frame generation"),
            ("dlssd", "nvngx_dlssd.dll", "ray reconstruction"))
FILE_OF = {f: n for f, n, _ in FAMILIES}
FAMILY_OF = {n: f for f, n, _ in FAMILIES}
LABEL = {f: lab for f, _, lab in FAMILIES}
ORDER = {f: i for i, (f, _, _) in enumerate(FAMILIES)}

RECORD = "dlss5-dlss-update.json"
ORIGINAL_SUFFIX = ".dlss5-dlss-original"
DISPLACED_SUFFIX = ".dlss5-dlss-displaced"      # a runtime restore or update found in the way
PART_SUFFIX = ".dlss5-dlss-part"
CACHE_NAME = "dlss-scan.json"

ORIGINAL, UPDATED, INSTALL, MISSING = "original", "updated", "install", "missing"

# Refusal kinds, so the page can draw each one as its own state.
R_32BIT, R_RUNNING, R_ANTICHEAT, R_READONLY, R_OFFLINE, R_NOTHING = (
    "32bit", "running", "anticheat", "readonly", "offline", "nothing")


@dataclass
class Entry:
    path: Path
    rel: str
    family: str
    version: str
    state: str = ORIGINAL
    original: str = ""            # the game's own version, once swapped
    label: str = ""               # the build this module wrote there
    backup: Path | None = None    # our copy of the game's own, when present

    @property
    def name(self) -> str:
        return FILE_OF[self.family]

    def to_json(self) -> dict:
        return {"rel": self.rel, "family": self.family, "version": self.version,
                "state": self.state, "original": self.original, "label": self.label,
                "backup": self.backup is not None}


@dataclass
class Report:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""
    refused: str = ""             # one of the R_ kinds when nothing was tried

    @property
    def ok(self) -> bool:
        return not self.error and not self.refused


# "Nothing was changed" is true of one runtime; after another went through it is not
_NOTHING = __import__("re").compile(r"[;,.]?\s*[Nn]othing was changed[.;]?")


class _Stop(Exception):
    def __init__(self, text: str, kind: str = ""):
        super().__init__(text)
        self.kind = kind


# ---------------------------------------------------------------- helpers

def _ver(s: str) -> tuple:
    return installer._ver(s)


def _number(label: str) -> str:
    """"310.9.1" from "310.9.1 (NVIDIA SDK)"."""
    import re
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", label or "")
    return m.group(1) if m else ""


def _key(p) -> str:
    try:
        return os.path.normcase(os.path.realpath(str(p)))
    except OSError:
        return os.path.normcase(os.path.abspath(str(p)))


def _rel(folder: Path, p: Path) -> str:
    """One spelling per file: resolved, relative to the resolved game folder.

    A junction or an 8.3 name would otherwise give the same runtime two
    record keys, or an absolute one the record then refuses.
    """
    try:
        return Path(os.path.realpath(str(p))).relative_to(os.path.realpath(str(folder))).as_posix()
    except (ValueError, OSError):
        try:
            return Path(p).relative_to(folder).as_posix()
        except ValueError:
            return str(p).replace("\\", "/")


def _is_x64(p: Path) -> bool:
    try:
        return pe.exe_bitness(p) == 64
    except (pe.PEError, OSError):
        return False


def _install_backup(p: Path) -> Path | None:
    """The DLSS 5 install's backup beside this runtime, when it swapped it."""
    for s in (installer.BACKUP_SUFFIX,) + tuple(installer.LEGACY_BACKUP_SUFFIXES):
        b = p.with_name(p.name + s)
        if b.is_file():
            return b
    return None


def _ours(p: Path) -> Path:
    return p.with_name(p.name + ORIGINAL_SUFFIX)


def _same_build(a: Path, b: Path) -> bool:
    """Two files of one build (version and size) - a copy, not a swap."""
    va = pe.file_version(a)
    return bool(va) and _ver(va) == _ver(pe.file_version(b)) and _size(a) == _size(b) >= 0


def _set_aside(p: Path, suffix: str, runtime: Path | None = None) -> Path:
    """Rename a file out of the way, never over another file.

    Named after the runtime it belongs to (`nvngx_dlss.dll<suffix>-<time>`),
    so a backup set aside does not end up with two suffixes.
    """
    stamp = int(time.time())
    for n in range(100):
        target = p.with_name(f"{(runtime or p).name}{suffix}-{stamp}" + (f"-{n}" if n else ""))
        if not target.exists():
            os.replace(p, target)
            return target
    raise OSError(f"no free name to set {p.name} aside")


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


# ---------------------------------------------------------------- the record

def record_path(g) -> Path:
    return Path(g.folder) / RECORD


def load_record(g) -> list[dict]:
    """The record's entries that are safe to act on (invariant 7)."""
    p = record_path(g)
    try:
        data = json.loads(p.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return []
    rows = data.get("files") if isinstance(data, dict) else None
    out: list[dict] = []
    seen: set[str] = set()
    for e in rows if isinstance(rows, list) else []:
        if not isinstance(e, dict):
            continue
        rel, fam = e.get("path"), e.get("family")
        if not isinstance(rel, str) or fam not in FILE_OF:
            continue
        target = None
        for base, absolute in ((Path(g.folder), False), (Path(g.install_dir), True)):
            try:
                target = net.inside(base, rel, absolute_ok=absolute)
                break
            except (net.OutsideError, OSError):
                continue
        if target is None:
            log.write(f"dlss update record: ignored an entry outside the game folder: {rel}", "warn")
            continue
        if target.name.lower() != FILE_OF[fam]:
            log.write(f"dlss update record: ignored an entry that is not a {FILE_OF[fam]}: {rel}", "warn")
            continue
        import posixpath
        rel = posixpath.normpath(rel.replace("\\", "/"))
        # one entry per FILE: an absolute and a relative spelling of one
        # runtime would otherwise be two entries, one of them always stale
        ident = _key(target)
        if ident in seen:
            continue
        seen.add(ident)
        if Path(rel).is_absolute():
            rel = _rel(Path(g.folder), target)
        try:
            size = int(e.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        digest = str(e.get("sha256") or "").lower()
        out.append({"path": rel, "family": fam, "original": str(e.get("original") or ""),
                    "written": str(e.get("written") or ""), "size": size,
                    "label": str(e.get("label") or ""), "at": e.get("at") or 0,
                    "sha256": digest if len(digest) == 64
                    and all(ch in "0123456789abcdef" for ch in digest) else ""})
    return out


def _save_record(g, rows: list[dict]) -> None:
    p = record_path(g)
    if not rows:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return
    installer._write_atomic(p, json.dumps({"tool": "dlss5-autopilot", "kind": "dlss-update",
                                           "version": 1, "files": rows}, indent=2))


def _find(rows: list[dict], rel: str) -> dict | None:
    return next((r for r in rows if r["path"].lower() == rel.lower()), None)


def _written_here(p: Path, r: dict) -> bool:
    """Is the file on disk, byte for byte, what this module wrote there?

    The record's hash of the written file, taken right after writing it. A
    record without one (older, or hand-made) proves nothing."""
    want = r.get("sha256") or ""
    if len(want) != 64:
        return False
    try:
        return net.sha256(p) == want
    except OSError:
        return False


def _matches(p: Path, version: str, r: dict) -> bool:
    """Is the file on disk still what this module wrote?"""
    if not r.get("written") or not version:
        return False
    if _ver(version) != _ver(r["written"]):
        return False
    return not r.get("size") or _size(p) == r["size"]


# ---------------------------------------------------------------- finding

def _manifest_files(g, extra_dirs) -> set[str]:
    """Every path a DLSS 5 install recorded as written, as comparable keys."""
    folder = Path(g.folder)
    roots = {Path(g.install_dir), folder} | set(extra_dirs)
    try:
        for d in prefs.installs():
            dp = Path(d)
            if dp == folder or folder in dp.parents:
                roots.add(dp)
    except Exception:
        pass
    out: set[str] = set()
    for r in roots:
        for name in (installer.MANIFEST,) + tuple(installer.LEGACY_MANIFESTS):
            m = r / name
            try:
                if not m.is_file():
                    continue
                data = json.loads(m.read_text(encoding="utf8", errors="replace"))
            except (OSError, ValueError):
                continue
            files = (data.get("files") or data.get("dosyalar") or []) if isinstance(data, dict) else []
            for f in files if isinstance(files, list) else []:
                if isinstance(f, str) and f.lower().endswith(tuple(FAMILY_OF)):
                    out.add(_key(r / f))
    return out


# find_dlss_files stops after this many matches; a remembered walk with fewer
# ran to its end (or to the budget, which a narrower walk would hit too).
_WALK_MATCH_STOP = 6


def _walk_hits(g, folder: Path) -> list[str]:
    """The three runtimes under the folder, from detection's walk when it has one.

    The library's detection walks every game with all of DLSS_FILES and the
    install folder skipped (dlss.detect); asking again with three names is
    another key in the walk cache, so the page's read 5 s after start walked
    every game a second time. A walk that ran to its end holds every one of
    the three; the install folder's own files are added by the caller.
    """
    names = tuple(FAMILY_OF)
    try:
        wide = dlss._WALK_CACHE.get((dlss._walk_key(folder), dlss._walk_key(g.install_dir),
                                     tuple(dlss.DLSS_FILES)))
    except Exception:
        wide = None
    if wide is not None and len(wide) < _WALK_MATCH_STOP:
        return [h for h in wide if h.replace("\\", "/").rsplit("/", 1)[-1].lower() in names]
    return dlss.walked(folder, names=names)


def _candidates(g, fresh: bool) -> list[Path]:
    folder = Path(g.folder)
    if fresh:
        dlss.forget_walk(folder)
    found: list[Path] = []
    try:
        # The same bounded, remembered walk detection uses (scan budgets),
        # asked only for the three runtimes this page is about.
        for h in _walk_hits(g, folder):
            found.append(folder / h)
    except Exception:
        log.exception(f"looking for DLSS files in {folder}")
    for base in {Path(g.install_dir), folder}:
        for n in FAMILY_OF:
            if (base / n).is_file():
                found.append(base / n)
    # A file this module updated is looked at even when the walk ran out of
    # budget before reaching it: the record knows where it is.
    for r in load_record(g):
        p = folder / r["path"]
        if p.is_file():
            found.append(p)
    seen, out = set(), []
    for p in found:
        k = _key(p)
        if k not in seen and p.name.lower() in FAMILY_OF:
            seen.add(k)
            out.append(p)
    return out


def scan(g, fresh: bool = False) -> list[Entry]:
    """The DLSS runtimes this game loads, with what each one is. Read-only.

    64-bit games only, and 64-bit files only. Not listed: anything under our
    host64 helper, and any runtime a DLSS 5 install wrote where the game had
    none (it is in the install's record with no backup beside it). Listed as
    MISSING: a runtime this module replaced that is gone while the game's own
    is still beside where it was.
    """
    if getattr(g, "bitness", None) != 64 or not getattr(g, "folder", None):
        return []
    folder = Path(g.folder)
    cands = _candidates(g, fresh)
    rows = load_record(g)
    written = _manifest_files(g, {p.parent for p in cands}) if cands else set()
    out: list[Entry] = []
    for p in cands:
        rel = _rel(folder, p)
        if any(part.lower() == installer.HOST_DIR for part in Path(rel).parts):
            continue
        if not _is_x64(p):
            continue
        ibak = _install_backup(p)
        if ibak is None and _key(p) in written:
            continue                      # our own file, never the game's
        e = Entry(path=p, rel=rel, family=FAMILY_OF[p.name.lower()], version=pe.file_version(p))
        r = _find(rows, rel)
        bak = _ours(p)
        has_bak = bak.is_file()
        if ibak is not None:
            # the DLSS 5 install's layer is on top - even when it wrote the
            # very build this module wrote before it
            e.state = INSTALL
            if has_bak:
                e.backup = bak
                e.original = r["original"] if r is not None else pe.file_version(bak)
        elif r is not None and _matches(p, e.version, r):
            e.state = UPDATED
            e.original, e.label = r["original"], r["label"]
            e.backup = bak if has_bak else None
        elif has_bak and not _same_build(p, bak):
            # Our copy of the game's own, beside a runtime that is not what the
            # record says was written: the record is gone, or something changed
            # the file since. Restorable; restore sets the file there aside.
            e.state = UPDATED
            e.backup = bak
            e.original = pe.file_version(bak)
        # else the game's own - also a backup of the very same build, which is
        # what a crash between the backup and the swap leaves
        out.append(e)
    for r in rows:
        p = folder / r["path"]
        if not p.exists() and _ours(p).is_file():
            out.append(Entry(path=p, rel=r["path"], family=r["family"], version="", state=MISSING,
                             original=r["original"], label=r["label"], backup=_ours(p)))
    out.sort(key=lambda e: (ORDER[e.family], e.rel.lower()))
    return out


# ---------------------------------------------------------------- newest

_NEWEST: dict[str, dict] = {}


def newest(catalog: dict | None = None, refresh: bool = False) -> dict[str, dict]:
    """{family: catalog entry + "version"} - NVIDIA's own newest per family.

    Network, so called from a worker. Remembered for the run once it has an
    answer; an empty one is asked again next time. `refresh` asks again
    anyway - the page's "check again", where a window left open for days
    would otherwise never see a build NVIDIA published meanwhile.
    """
    global _NEWEST
    remember = catalog is None
    if refresh and catalog is None:
        # sources remembers the tag for the run as well; forgetting only ours
        # would read the same old answer back. What we had stays until a new
        # answer replaces it, so an update right after a failed check still
        # has a build to take.
        sources._NVIDIA_CACHE = None
    if catalog is None:
        if _NEWEST and not refresh:
            return dict(_NEWEST)
        try:
            catalog = sources.nvidia_dlss()
        except Exception as e:
            log.write(f"reading NVIDIA's DLSS builds failed: {e}", "warn")
            catalog = {}
    out: dict[str, dict] = {}
    for fam in FILE_OF:
        entries = [x for x in (catalog or {}).get(fam) or [] if isinstance(x, dict) and x.get("url")]
        if not entries:
            continue
        best = max(entries, key=lambda x: _ver(_number(str(x.get("label", "")))))
        out[fam] = dict(best, version=_number(str(best.get("label", ""))))
    if remember and out:
        _NEWEST = out
    return out


def outdated(e: Entry, new: dict | None) -> bool:
    """Is there a newer build than the one on disk? Never for an install's swap."""
    if not new or e.state in (INSTALL, MISSING) or not e.version:
        return False
    if e.label and e.label == new.get("label"):
        # NVIDIA versions each runtime on its own: a file written from this
        # build is current even when its stamp reads lower than the SDK tag.
        return False
    return _ver(e.version) < _ver(new.get("version", ""))


# ---------------------------------------------------------------- guards

def running(g, all_procs=None) -> str:
    """The name of a process running out of the game's folder, or "".

    `all_procs` is one process snapshot shared by a scan of many games:
    taking a fresh one per game reads every process's image path each time.
    """
    try:
        ps = watch.from_folder(Path(g.folder), all_procs)
        if not ps and Path(g.install_dir) != Path(g.folder):
            ps = watch.from_folder(Path(g.install_dir), all_procs)
    except Exception:
        return ""
    return ps[0].name if ps else ""


def anticheat_name(g) -> str:
    return anticheat_info(g)[0]


def anticheat_info(g) -> tuple[str, str]:
    """(the anti-cheat's name, "found: <files>") or ("", "").

    The files go into the page's question too: every other anti-cheat
    warning names what it rests on, so the person can check it is real."""
    try:
        f = anticheat.detect(Path(g.install_dir), Path(g.folder))
        return (f.summary, f.found) if f.present else ("", "")
    except Exception:
        return "", ""


def _probe_write(folder: Path) -> str:
    """"" when a file can be created here, else where Windows refused."""
    try:
        with tempfile.NamedTemporaryFile(prefix=".dlss5-dlss-write-test-", dir=folder):
            pass
        return ""
    except OSError as e:
        why = e.strerror or type(e).__name__
        hint = games_hint(folder)
        return f"Windows refused to write into {folder} ({why}).{hint}"


def games_hint(folder: Path) -> str:
    from . import games
    if games.is_locked_store_path(folder):
        return " " + games.XBOX_HINT
    return (" Close the game and its launcher; if it is still refused, the folder needs "
            "administrator rights.")


def _guards(g, allow_anticheat: bool, rep: Report) -> bool:
    if getattr(g, "bitness", None) != 64:
        rep.refused = R_32BIT
        rep.error = "Only 64-bit games are offered: NVIDIA publishes these runtimes for 64-bit Windows."
        return False
    name = running(g)
    if name:
        rep.refused = R_RUNNING
        rep.error = (f"{name} is running from this game's folder. Close it first - Windows does not let "
                     f"anything replace a file a running program has open.")
        return False
    ac = anticheat_name(g)
    if ac and not allow_anticheat:
        rep.refused = R_ANTICHEAT
        rep.error = (f"{ac} is installed with this game. An anti-cheat can treat a changed DLSS file as "
                     f"tampering; for anything played online, keep the game's own.")
        return False
    return True


# ---------------------------------------------------------------- acting

def settle(g) -> list[str]:
    """Drop record entries whose file is no longer what was written (invariant 5).

    Never deletes a backup: it cannot tell a launcher putting the game's own
    back from another tool writing a newer build, and only the first leaves a
    copy of the game's file anywhere. Writes only when there is a record, so
    for a game this module never touched it is a read.
    """
    rows = load_record(g)
    if not rows:
        return []
    folder = Path(g.folder)
    keep, said = [], []
    for r in rows:
        p = folder / r["path"]
        name = p.name
        bak = _ours(p)
        if not p.exists():
            if bak.is_file():
                keep.append(r)            # restore puts the game's own back (invariant 6)
            else:
                said.append(f"{name} is no longer at {r['path']}")
            continue
        v = pe.file_version(p)
        if _matches(p, v, r) or _install_backup(p) is not None:
            keep.append(r)
            continue
        if bak.is_file():
            old = pe.file_version(bak)
            if _same_build(p, bak):
                # The launcher put exactly the game's own back: the backup is a
                # second copy of the file on disk, and only then is it removed.
                try:
                    bak.unlink()
                except OSError:
                    pass
                said.append(f"{name}: the game's own {v} is back (a launcher verified its files)")
            else:
                # Anything else - a newer build from the game, or another tool
                # that may put our build back later - keeps the game's own
                # where scan and restore find it, and keeps the entry: a later
                # settle still sees the launcher's exact copy, and our build
                # coming back still reads as ours.
                keep.append(r)
                said.append(f"{name}: changed to {v or 'another build'} since the update; "
                            f"restore original still puts the game's own {old} back")
        else:
            said.append(f"{name}: changed to {v or 'another build'} by something else - left as it is")
    if len(keep) != len(rows):
        try:
            _save_record(g, keep)
        except OSError as e:
            said.append(f"the record could not be written ({e})")
    for s_ in said:
        log.write(f"dlss update, {getattr(g, 'name', folder)}: {s_}")
    return said


def _copy_whole(src: Path, dst: Path) -> None:
    part = dst.with_name(dst.name + PART_SUFFIX)
    try:
        shutil.copyfile(src, part)
        if _size(part) != _size(src):
            raise OSError(f"the copy of {src.name} came out short")
        os.replace(part, dst)
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


def _replace_one(g, e: Entry, build: dict, progress) -> str:
    folder = e.path.parent
    why = _probe_write(folder)
    if why:
        raise _Stop(why, R_READONLY)
    try:
        got = net.download(build["url"], f"{e.family}-{build['label']}.dll", progress=progress)
    except Exception as ex:
        raise _Stop(f"{e.name} could not be downloaded: {str(ex).splitlines()[0] if str(ex) else ex}. "
                    f"Nothing was changed.", R_OFFLINE)
    ok, why = installer._is_win64_dll(Path(got))
    want = int(build.get("size") or 0)
    if ok and want and _size(Path(got)) != want:
        ok, why = False, f"the download is {_size(Path(got))} bytes where {want} were published"
    if not ok:
        try:
            Path(got).unlink()
        except OSError:
            pass
        raise _Stop(f"{e.name}: {why}. Nothing was changed.")
    # Invariant 8: re-read it now. The download takes a while, and a launcher
    # or an install may have written this very file in the meantime.
    now = pe.file_version(e.path) if e.path.is_file() else ""
    if not e.path.is_file() or _ver(now) != _ver(e.version) or _install_backup(e.path) is not None:
        raise _Stop(f"{e.name} changed while the update was downloading ({e.version or '?'} -> "
                    f"{now or 'gone'}). Nothing was changed; check again.")
    rows = load_record(g)
    r = _find(rows, e.rel)
    bak = _ours(e.path)
    made, aside = False, None
    if r is not None and _matches(e.path, e.version, r) and bak.is_file():
        pass                             # our own build, the game's own beside it: that backup stands
    elif r is not None and _matches(e.path, e.version, r) and _written_here(e.path, r):
        # Our own build with the game's copy gone (deleted by hand, cleaned
        # by a tool). A "backup" made now would be OUR build filed as the
        # game's own: restore would later put it over a genuine file a
        # launcher verified, and the page would label it the game's. The
        # record keeps the game's own version; the page says its backup is
        # gone, as it did before this update. Version and size alone do not
        # prove it (a record naming the game's own build is not trusted,
        # invariant 7): the bytes must be the ones this module wrote.
        log.write(f"dlss update: the backup of the game's own {e.name} is gone; "
                  f"{e.version} there is the build this tool wrote, so it is not backed up", "warn")
    elif not bak.is_file():
        # Invariant 2/7: no copy of what is there yet, and it is not the
        # build the record says was written - so it is the game's.
        try:
            _copy_whole(e.path, bak)
        except OSError as ex:
            raise _Stop(f"the game's own {e.name} could not be backed up ({ex}); nothing was changed.",
                        R_READONLY)
        made = True
        if r is None:
            r = {"path": e.rel, "family": e.family}
            rows.append(r)
        r["original"] = e.version
    else:
        # A backup of the game's own is there, and the file is not a build
        # this module wrote (the record is gone, or the file changed since).
        if not _same_build(e.path, bak):
            try:
                aside = _set_aside(e.path, DISPLACED_SUFFIX)
            except OSError as ex:
                raise _Stop(f"Windows refused to move {e.path} aside ({ex.strerror or ex}). Nothing was changed.",
                            R_READONLY)
        if r is None:
            r = {"path": e.rel, "family": e.family}
            rows.append(r)
        r["original"] = pe.file_version(bak)
    try:
        _copy_whole(Path(got), e.path)
    except OSError as ex:
        if aside is not None:
            try:
                os.replace(aside, e.path)
            except OSError:
                pass
        if made:
            try:
                bak.unlink()             # the game's own is still in place
            except OSError:
                pass
        raise _Stop(f"Windows refused to replace {e.path} ({ex.strerror or ex}). Nothing was changed. "
                    f"Close the game and its launcher; a read-only file needs its attribute cleared.",
                    R_READONLY)
    was = e.version
    try:
        digest = net.sha256(e.path)
    except OSError:
        digest = ""
    r.update(written=pe.file_version(e.path) or _number(build["label"]), size=_size(e.path),
             label=build["label"], at=round(time.time()), sha256=digest)
    try:
        _save_record(g, rows)
    except OSError as ex:
        # The backup beside it still makes it restorable (scan adopts it).
        log.write(f"dlss update record not written: {ex}", "warn")
    line = f"{LABEL[e.family]}  {was or '?'} -> {r['written']}"
    return line + (f" (the file that was there is kept as {aside.name})" if aside is not None else "")


def update(g, families=None, build: str | None = None, allow_anticheat: bool = False,
           progress=None, catalog: dict | None = None) -> Report:
    """Replace the game's DLSS runtimes with NVIDIA's newest (or `build`).

    `families` limits it ("dlss", "dlssg", "dlssd"); None is every outdated
    one. `progress(done, total)` follows the download.
    """
    rep = Report()
    if not _guards(g, allow_anticheat, rep):
        return rep
    rep.notes += settle(g)
    entries = [e for e in scan(g) if families is None or e.family in families]
    if not entries:
        rep.refused = R_NOTHING
        rep.error = "No 64-bit DLSS runtime of this game was found to update."
        return rep
    news = newest(catalog)
    if not news:
        rep.refused = R_OFFLINE
        rep.error = ("NVIDIA's build list could not be read (raw.githubusercontent.com). "
                     "Check the connection and try again.")
        return rep
    for e in entries:
        if e.state == INSTALL:
            rep.skipped.append(f"{LABEL[e.family]}: set by the DLSS 5 install - change it in the game's settings")
            continue
        if e.state == MISSING:
            rep.skipped.append(f"{LABEL[e.family]}: the file is gone - restore original puts the game's own back")
            continue
        new = news.get(e.family)
        if new is None:
            rep.skipped.append(f"{LABEL[e.family]}: NVIDIA publishes no build of it")
            continue
        if build:
            pool = (catalog if catalog is not None else sources.nvidia_dlss()).get(e.family) or []
            wanted = next((x for x in pool if x.get("label") == build or x.get("tag") == build), None)
            if wanted is None:
                rep.skipped.append(f"{LABEL[e.family]}: no build {build}")
                continue
            new = dict(wanted, version=_number(wanted["label"]))
        elif not outdated(e, new):
            rep.skipped.append(f"{LABEL[e.family]}: {e.version} is current")
            continue
        try:
            rep.done.append(_replace_one(g, e, new, progress))
        except _Stop as ex:
            # after a runtime that did go through, "nothing was changed" is untrue
            rep.error = _NOTHING.sub("", str(ex)).rstrip(" ;,") if rep.done else str(ex)
            if ex.kind == R_READONLY and not rep.done:
                rep.refused = R_READONLY
            break
    for line in rep.done:
        log.write(f"dlss update, {getattr(g, 'name', '')}: {line}")
    if rep.error:
        log.write(f"dlss update, {getattr(g, 'name', '')}: {rep.error}", "warn")
    return rep


def restore(g, families=None) -> Report:
    """Put the game's own runtimes back and drop the record.

    Driven by the record and the backups on disk, not by scan(): a runtime
    that is gone, unreadable or no longer 64-bit still has its original put
    back.
    """
    rep = Report()
    rep.notes += settle(g)
    rows = load_record(g)
    folder = Path(g.folder)
    paths: dict[str, Path] = {}
    for r in rows:
        paths.setdefault(_key(folder / r["path"]), folder / r["path"])
    for p in _candidates(g, False):
        if _ours(p).is_file():
            paths.setdefault(_key(p), p)
    targets = sorted((p for p in paths.values() if p.name.lower() in FAMILY_OF
                      and (families is None or FAMILY_OF[p.name.lower()] in families)),
                     key=lambda p: (ORDER[FAMILY_OF[p.name.lower()]], str(p).lower()))
    if not targets:
        return rep
    name = running(g)
    if name:
        rep.refused = R_RUNNING
        rep.error = f"{name} is running from this game's folder. Close it first."
        return rep
    for p in targets:
        rel = _rel(folder, p)
        r = _find(rows, rel)
        fam = FAMILY_OF[p.name.lower()]
        label = LABEL[fam]
        bak = _ours(p)
        ibak = _install_backup(p)
        drop = True
        if ibak is not None:
            if bak.is_file() and r is not None and _matches(ibak, pe.file_version(ibak), r):
                # Invariant 4: the install's backup is the build we wrote; hand
                # it the game's own, so the install's uninstall restores that.
                try:
                    os.replace(bak, ibak)
                except OSError as ex:
                    rep.error = f"Windows refused to move {bak} ({ex.strerror or ex}). Nothing was changed."
                    break
                rep.notes.append(f"{label}: DLSS 5 is installed over it - the game's own "
                                 f"{r['original'] or 'file'} comes back when DLSS 5 is uninstalled")
            elif bak.is_file():
                drop = False
                rep.notes.append(f"{label}: DLSS 5 was installed over a file that changed since the update - "
                                 f"uninstall DLSS 5 first, then restore original")
            else:
                rep.notes.append(f"{label}: set by the DLSS 5 install; nothing of ours is left under it")
        elif not bak.is_file():
            if p.exists():
                v = pe.file_version(p)
                rep.notes.append(f"{label}: the backup of the game's own {p.name} is gone - the {v or 'file there'} "
                                 f"stays. Verify the game's files in its launcher to get the original back.")
            else:
                rep.notes.append(f"{label}: {p.name} and its backup are both gone. Verify the game's files in "
                                 f"its launcher to get it back.")
        else:
            was = pe.file_version(p) if p.exists() else ""
            aside = None
            try:
                if p.exists() and not (r is not None and _matches(p, was, r)) and not _same_build(p, bak):
                    aside = _set_aside(p, DISPLACED_SUFFIX)     # invariant 1: not ours, not deleted
                _copy_whole(bak, p)
            except OSError as ex:
                if aside is not None and not p.exists():
                    try:
                        os.replace(aside, p)
                    except OSError:
                        pass
                rep.error = (f"Windows refused to replace {p} ({ex.strerror or ex}). Nothing was changed. "
                             f"Close the game and its launcher.")
                break
            try:
                bak.unlink()
            except OSError:
                pass
            rep.done.append(f"{label}  {was or 'missing'} -> {pe.file_version(p) or '?'}")
            if aside is not None:
                rep.notes.append(f"{label}: the {was or 'file'} that was there is kept as {aside.name}")
        if drop and r is not None:
            rows.remove(r)
        try:
            _save_record(g, rows)
        except OSError as ex:
            rep.notes.append(f"the record could not be written ({ex})")
    for line in rep.done + rep.notes:
        log.write(f"dlss restore, {getattr(g, 'name', '')}: {line}")
    if rep.error:
        log.write(f"dlss restore, {getattr(g, 'name', '')}: {rep.error}", "warn")
    return rep


# ---------------------------------------------------------------- the page's cache

def cache_path() -> Path:
    # Read at call time: tests point prefs.FILE at a temporary folder.
    return prefs.FILE.parent / CACHE_NAME


def load_cache() -> dict:
    try:
        data = json.loads(cache_path().read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("games"), dict):
        return {}
    return data


def save_cache(data: dict) -> None:
    try:
        p = cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        installer._write_atomic(p, json.dumps(data, indent=1))
    except OSError as e:
        log.write(f"the dlss scan could not be saved: {e}", "warn")
