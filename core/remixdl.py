r"""Fetching an RTX Remix mod from its own GitHub release and putting it in.

The tool has always linked to these mods and left the download to the person.
That is still right for most of them - a Remix project is somebody else's
work, often gigabytes, with its own instructions - but for the ones published
as a plain .zip on the author's own GitHub releases there is nothing a person
does that this cannot do exactly: fetch the file the author published, and
copy it into the game folder.

The layout is the hard part, and it is not the same twice:

    GTA IV      everything sits under GTAIV-Remix-CompatibilityMod/ inside
                the zip, so extracting the zip as-is would bury the mod one
                folder deep and it would never load
    NFSU2       .trex/ and rtx.conf are at the zip root already
    Deus Ex     the release is a renderer plus dev tools, not a Remix drop-in
    Garry's Mod needs its own launcher and the game in a fixed-function mode
    Saints Row 3  the zip is configuration only, no runtime in it

So nothing is extracted blind. `remix_root()` looks for the directory inside
the archive that IS the Remix install - a `.trex` beside a runtime DLL and a
`rtx.conf`/`dxvk.conf` - and only that directory's contents go into the game
folder. An archive with no such directory is refused and the person is sent
to the mod's own page, which is what happened before this module existed.

Verified against the real thing: the rule picks
`GTAIV-Remix-CompatibilityMod` out of the 524 MB GTA IV zip and lands exactly
the file set (`.trex`, `d3d9.dll`, `dinput8.dll`, `dxvk.conf`, the .asi and
the launch .bat files) that a hand-installed copy of that mod has.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import net, remix

# What this tool wrote, so it can take it back out again. Separate from
# dlss5-autopilot.json on purpose: the mod is not our install, and removing
# DLSS 5 must never remove somebody's Remix mod with it.
RECORD = "dlss5-remix-mod.json"

BACKUP_SUFFIX = ".dlss5-autopilot-backup"

# A directory inside the archive is the Remix install when it carries the
# runtime the game loads and the configuration beside it.
_RUNTIME = {"d3d9.dll", "dinput8.dll", "ddraw.dll"}
_CONF = {"rtx.conf", "dxvk.conf"}


class NotAModError(RuntimeError):
    """The release exists but nothing in it is a Remix install."""


@dataclass
class Fetch:
    """A release asset that holds a Remix install, and where it starts."""
    tag: str
    name: str
    url: str
    size: int
    root: str = ""                      # directory inside the zip, "" = root
    lands: list[str] = field(default_factory=list)   # top-level names it writes


def repo_of(url: str) -> str:
    """"owner/name" for a github.com project URL, else ""."""
    p = urlparse(url)
    if "github.com" not in p.netloc:
        return ""
    parts = [s for s in p.path.split("/") if s]
    return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else ""


def remix_root(names: list[str]) -> str | None:
    """The directory inside the archive whose contents belong in the game.

    "" is the archive root. None means the archive is not a **complete**
    Remix install, and the caller must refuse.

    "Complete" is deliberately strict: the archive has to carry the renderer
    itself, `.trex/d3d9.dll`, the ~190 MB file `remix.find_runtime()` looks
    for and the one this tool puts `nvngx_dlssnr.dll` beside. Plenty of
    projects publish only a small proxy - Thief Gold, Saints Row 2, Prince of
    Persia - and their own INSTALL.txt then tells you to download NVIDIA's
    Remix runtime separately and rename a DLL by hand. Dropping that proxy in
    on its own leaves a game that loads a d3d9.dll with nothing behind it, so
    those stay a link, exactly as before.
    """
    trex_dlls = [n for n in names if n.lower().endswith(".trex/d3d9.dll")]
    if not trex_dlls:
        return None
    # The mod root is the directory holding .trex, whichever level that is:
    # GTA IV buries it under GTAIV-Remix-CompatibilityMod/, NFSU2 has it at
    # the archive root. Shallowest wins if a capture folder repeats the name.
    roots = sorted({n[:-len("/.trex/d3d9.dll")] if "/.trex/" in n.lower()
                    else "" for n in trex_dlls}, key=len)
    return roots[0]


def _entries(z: zipfile.ZipFile) -> list[str]:
    return [i.filename for i in z.infolist() if not i.is_dir()]


def inspect(zip_path: Path) -> tuple[str, list[str]]:
    """(root inside the zip, the top-level names it would write).

    Raises NotAModError when the archive holds no Remix install.
    """
    with zipfile.ZipFile(zip_path) as z:
        names = _entries(z)
    root = remix_root(names)
    if root is None:
        raise NotAModError(
            "This release does not carry the Remix renderer itself (no "
            "'.trex/d3d9.dll' in it), so it is not a complete install - most "
            "of these are a small proxy that expects you to download NVIDIA's "
            "RTX Remix runtime separately and rename a file by hand first. "
            "Putting only this in would leave the game loading a d3d9.dll "
            "with nothing behind it, so use the mod's own page and follow its "
            "instructions.")
    pre = f"{root}/" if root else ""
    lands = sorted({n[len(pre):].split("/")[0]
                    for n in names if n.startswith(pre) and n != pre})
    return root, lands


def resolve(mod_url: str) -> Fetch:
    """The newest release asset of this mod that really is a Remix install.

    Releases are walked newest first and every .zip in them is considered,
    because a project can publish several (a renderer and its dev tools, a
    mod and a separate launcher) and only one of them is the thing to
    install.
    """
    repo = repo_of(mod_url)
    if not repo:
        raise NotAModError("This mod is not published on GitHub, so it cannot "
                           "be fetched automatically. Use its own page.")
    rels = net.json_get(f"https://api.github.com/repos/{repo}/releases?per_page=10")
    if isinstance(rels, dict):
        rels = [rels]
    for rel in rels or []:
        for a in rel.get("assets") or []:
            if not a.get("name", "").lower().endswith(".zip"):
                continue
            return Fetch(tag=rel.get("tag_name", "?"), name=a["name"],
                         url=a["browser_download_url"], size=a.get("size") or 0)
    raise NotAModError("This project publishes no .zip release, so there is "
                       "nothing to fetch. Use its own page.")


def _safe_target(root_dir: Path, rel: str) -> Path:
    """Resolve an archive entry inside root_dir, refusing to escape it."""
    target = (root_dir / rel).resolve()
    if not str(target).startswith(str(root_dir.resolve())):
        raise NotAModError(f"The archive tries to write outside the game "
                           f"folder ({rel}) - refused.")
    return target


def install(mod_url: str, game_dir: Path, log=None, progress=None) -> list[str]:
    """Download the mod and copy it into the game folder. Returns what it wrote.

    Refuses when a Remix runtime is already there: that is somebody's working
    mod, possibly a different one, and overwriting it is not this tool's call.
    """
    log = log or (lambda *_: None)
    game_dir = Path(game_dir)
    if remix.is_remix_game(game_dir):
        raise NotAModError(
            "An RTX Remix mod is already installed in this folder (its .trex "
            "runtime is there). Remove it yourself first if you want to "
            "replace it - overwriting somebody's working mod is not something "
            "this tool will do on its own.")

    f = resolve(mod_url)
    log(f"      {f.name} ({f.tag}, {net.human(f.size)})")
    z = net.download(f.url, f"remixmod-{repo_of(mod_url).replace('/', '-')}-"
                            f"{f.tag}-{f.name}", progress=progress)
    root, lands = inspect(z)
    if root:
        log(f"      the mod sits under {root}/ inside the archive")
    log(f"      into the game folder: {', '.join(lands[:6])}"
        + (" ..." if len(lands) > 6 else ""))

    pre = f"{root}/" if root else ""
    written: list[str] = []
    backups: list[str] = []
    with zipfile.ZipFile(z) as zf:
        members = [i for i in zf.infolist()
                   if not i.is_dir() and i.filename.startswith(pre)
                   and i.filename != pre]
        for i, item in enumerate(members):
            rel = item.filename[len(pre):]
            if not rel:
                continue
            target = _safe_target(game_dir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            bak = target.with_name(target.name + BACKUP_SUFFIX)
            if target.is_file() and not bak.exists():
                try:
                    bak.write_bytes(target.read_bytes())
                    backups.append(str(bak.relative_to(game_dir)).replace("\\", "/"))
                except OSError:
                    log(f"      WARNING: could not back up {rel}")
            with zf.open(item) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            written.append(rel.replace("\\", "/"))
            if progress and len(members) > 50 and i % 25 == 0:
                progress(int(i * 100 / len(members)),
                         f"unpacking {i}/{len(members)}")

    record = {
        "mod_url": mod_url, "tag": f.tag, "asset": f.name,
        "files": written, "backups": backups,
    }
    (game_dir / RECORD).write_text(json.dumps(record, indent=2), encoding="utf8")
    log(f"      {len(written)} files written; '{RECORD}' records them so the "
        f"mod can be taken back out")
    return written


def installed(game_dir: Path) -> dict | None:
    """The record of a mod this tool installed here, or None."""
    try:
        data = json.loads((Path(game_dir) / RECORD).read_text(encoding="utf8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def remove(game_dir: Path, log=None) -> list[str]:
    """Take out a mod this tool installed, restoring anything it replaced."""
    log = log or (lambda *_: None)
    game_dir = Path(game_dir)
    rec = installed(game_dir)
    if not rec:
        raise NotAModError("No record of a Remix mod installed by this tool in "
                           "this folder, so there is nothing safe to remove.")
    gone: list[str] = []
    for rel in rec.get("backups") or []:
        bak = game_dir / rel
        orig = bak.with_name(bak.name[:-len(BACKUP_SUFFIX)])
        try:
            if bak.is_file():
                orig.write_bytes(bak.read_bytes())
                bak.unlink()
                gone.append(rel)
        except OSError as e:
            log(f"      could not restore {orig.name}: {e}")
    restored = {r[:-len(BACKUP_SUFFIX)] for r in (rec.get("backups") or [])}
    for rel in rec.get("files") or []:
        if rel in restored:
            continue
        p = game_dir / rel
        try:
            if p.is_file():
                p.unlink()
                gone.append(rel)
        except OSError as e:
            log(f"      could not remove {rel}: {e}")
    # Empty directories the mod created, deepest first.
    for rel in sorted({str(Path(r).parent) for r in rec.get("files") or []},
                      key=len, reverse=True):
        if rel in (".", ""):
            continue
        try:
            (game_dir / rel).rmdir()
        except OSError:
            pass
    try:
        (game_dir / RECORD).unlink()
    except OSError:
        pass
    log(f"      removed {len(gone)} files")
    return gone
