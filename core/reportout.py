"""Getting a bug report out of the tool without it ever arriving empty.

Two of the four reports sent over GitHub's link-length cap arrived holding
only the line that asked for a paste (#751 #770). So delivery is not one
call that hopes: the whole report is saved to a file first, put on the
clipboard (and read back, so "it is on your clipboard" is a fact), the link
carries the longest start of it that fits, and the person is told in one
plain line which of those happened. "copy report" and "save zip" stay
available afterwards.

Nothing is sent. The browser shows the text and the person submits it.
"""
from __future__ import annotations

import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import log

MAX_LOG = 4 << 20            # the end of any one log that goes into the zip


@dataclass
class Delivery:
    cut: bool                # the link carries only the start of the report
    opened: bool             # the browser was started
    clip_ok: bool            # the whole report is on the clipboard (read back)
    saved: Path | None       # the whole report as a file, or None
    text: str                # the one line the person reads


def reports_dir() -> Path:
    return Path(log.path()).parent / "reports"


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(name or "")).strip("-")[:40]
    return s or "game"


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def save_text(body: str, name: str = "") -> Path | None:
    """The whole report as a file. None when nothing can be written."""
    try:
        d = reports_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"report-{_slug(name)}-{_stamp()}.md"
        p.write_text(body, encoding="utf8")
        return p
    except OSError:
        return None


def _tail_bytes(p: Path, limit: int = MAX_LOG) -> bytes:
    with open(p, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - limit))
        return f.read()


def save_zip(body: str, install_dir, name: str = "") -> Path | None:
    """The report and the logs it was read from, in one zip on this PC.
    Goes nowhere until the person attaches it. None when it cannot be made."""
    from . import diagnose
    try:
        d = reports_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"report-{_slug(name)}-{_stamp()}.zip"
        logs: list[tuple[str, Path]] = []
        if install_dir:
            folder = Path(install_dir)
            names = [diagnose.RESHADE_LOG, diagnose.FEED_LOG, diagnose.BRIDGE_LOG]
            logs += [(n, folder / n) for n in names]
            try:
                opti = diagnose._opti_log(folder)
            except Exception:
                opti = None
            if opti is not None:
                logs.append((Path(opti).name, Path(opti)))
            # the 32-bit helper's log names the fault #917 needed
            logs.append(("dlss5-feed-host.log", folder / diagnose.model.HOST_LOG))
        try:
            # the standalone add-on keeps its log outside the game folder
            logs.append(("standalone-dlssnr.log", Path(diagnose.model.STANDALONE_LOG)))
        except Exception:
            pass
        logs.append(("autopilot.log", Path(log.path())))
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("report.md", body)
            for n, src in logs:
                try:
                    if src.is_file():
                        z.writestr(n, _tail_bytes(src))
                except OSError:
                    continue
        return p
    except (OSError, zipfile.BadZipFile):
        return None


def line_for(cut: bool, opened: bool, clip_ok: bool, saved: Path | None) -> str:
    """The one line that says what happened. Only what is true: a paste is
    asked for only when the clipboard really holds the report."""
    if opened and not cut:
        return "the report is open on github with everything in it - read it, then submit"
    if opened and cut:
        if clip_ok:
            return ("report open on github with its start - the whole report is on your "
                    "clipboard: click the box, Ctrl+A, Ctrl+V")
        return ("github has only the start of the report and the clipboard did not take it - "
                "use 'save zip' and attach it to the issue")
    if clip_ok:
        return "no browser opened - the report is on your clipboard, paste it into a new issue on github"
    return ("no browser opened and the clipboard did not take it - use 'save zip' "
            "and attach it to a new issue on github")


def deliver(body: str, url: str, cut: bool, clip, opener, name: str = "",
            link=None) -> Delivery:
    """Save, copy, open - in that order - and say which of them worked.

    `clip(text) -> bool` puts the text on the clipboard and reports whether
    it reads back identical; `opener(url) -> bool` starts the browser.
    `link(clip_ok) -> (url, cut)`, when given, builds the link once the
    clipboard is known, so its first line never asks for a paste that
    cannot work."""
    saved = save_text(body, name)
    try:
        clip_ok = bool(clip(body))
    except Exception:
        clip_ok = False
    if link is not None:
        try:
            url, cut = link(clip_ok)
        except Exception:
            pass
    try:
        opened = bool(opener(url))
    except Exception:
        opened = False
    return Delivery(cut=cut, opened=opened, clip_ok=clip_ok, saved=saved,
                    text=line_for(cut, opened, clip_ok, saved))
