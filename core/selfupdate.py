r"""Downloading and applying an update to this executable.

A running .exe cannot overwrite itself on Windows, so the swap is done by a
tiny batch file that waits for this process to exit, replaces the file, and
starts the new one. The old executable is kept as .old until the next update,
so a bad build can be rolled back by hand.

Deliberately conservative:
  - the download is verified to be a real 64-bit PE of a sane size before
    anything is replaced;
  - the swap only happens after the user asks for it, never on its own;
  - nothing is deleted, only renamed.

Two release layouts are understood. Today's is one self-contained .exe.
A release may instead ship the .exe with an `_internal` folder beside it
(PyInstaller's one-folder build, which antivirus heuristics take far less
exception to than a self-extracting single file); then the whole folder is
fetched and the swap replaces the folder as well as the .exe. This updater
has to know both BEFORE such a release exists: the copy already on
someone's PC is the one that will install it.
"""
from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from . import net, update

MIN_BYTES = 4 * 1024 * 1024          # a real one-file build is ~11 MB
# PyInstaller's one-folder layout keeps the runtime beside the .exe here.
INTERNAL = "_internal"


class UpdateError(RuntimeError):
    pass


def running_exe() -> Path | None:
    """The .exe to replace, or None when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def _is_win64_pe(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                return False
            (off,) = struct.unpack_from("<I", head, 0x3C)
            f.seek(off)
            sig = f.read(6)
            return len(sig) == 6 and sig[:4] == b"PE\0\0" and \
                struct.unpack_from("<H", sig, 4)[0] == 0x8664
    except OSError:
        return False


def fetch(progress=None) -> Path:
    """Download the latest release and return the extracted .exe."""
    rel = net.json_get(update.API)
    assets = rel.get("assets", [])
    zip_url = next((a["browser_download_url"] for a in assets
                    if a["name"].lower().endswith(".zip")), None)
    exe_url = next((a["browser_download_url"] for a in assets
                    if a["name"].lower().endswith(".exe")), None)
    sums_url = next((a["browser_download_url"] for a in assets
                     if a["name"] == "SHA256SUMS.txt"), None)
    tag = (rel.get("tag_name") or "new").lstrip("vV")

    workdir = Path(tempfile.mkdtemp(prefix="dlss5-autopilot-update-"))
    if zip_url:
        z = net.download(zip_url, f"update-{tag}.zip", progress=progress)
        with zipfile.ZipFile(z) as arc:
            member = next((n for n in arc.namelist()
                           if n.lower().endswith(".exe")), None)
            if not member:
                raise UpdateError("The release archive contains no executable.")
            base = member.rsplit("/", 1)[0] + "/" if "/" in member else ""
            folder = [n for n in arc.namelist()
                      if n.startswith(base + INTERNAL + "/") and not n.endswith("/")]
            if folder:
                # One-folder build: the .exe and everything under _internal,
                # kept together - the .exe alone cannot start.
                for n in folder + [member]:
                    # Member names come out of a downloaded archive, so they
                    # are somebody else's strings: a `../` in one writes
                    # outside the temporary folder, and it happens before the
                    # build checks below can reject the download (#79).
                    try:
                        dest = net.inside(workdir, n[len(base):])
                    except net.OutsideError as e:
                        raise UpdateError(
                            f"The release archive contains a path that writes "
                            f"outside the update folder - refusing it. ({e})"
                        ) from None
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with arc.open(n) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
                out = workdir / Path(member).name
            else:
                out = workdir / Path(member).name
                with arc.open(member) as src, open(out, "wb") as dst:
                    dst.write(src.read())
    elif exe_url:
        got = net.download(exe_url, f"update-{tag}.exe", progress=progress)
        out = workdir / got.name
        out.write_bytes(got.read_bytes())
    else:
        raise UpdateError("The release has no downloadable build.")

    # A one-folder build keeps its bulk in _internal; the exe alone is the
    # bootloader plus the app, a couple of MB. Judge the whole download.
    got = out.stat().st_size
    if (out.parent / INTERNAL).is_dir():
        got += sum(p.stat().st_size for p in (out.parent / INTERNAL).rglob("*")
                   if p.is_file())
    if got < MIN_BYTES:
        raise UpdateError(f"The downloaded build is only {got} "
                          f"bytes - refusing to install it.")
    if not _is_win64_pe(out):
        raise UpdateError("The downloaded file is not a 64-bit Windows "
                          "executable - refusing to install it.")
    # The release workflow publishes SHA256SUMS.txt next to the build. When
    # it is there, the executable must match it: a swapped or corrupted
    # download is refused rather than started. When it is missing (an older
    # release, or a hand-made one) the shape checks above still apply.
    if sums_url:
        try:
            sums = net.fetch_text(sums_url).decode("utf8", "replace")
        except Exception as e:
            raise UpdateError(f"Could not read SHA256SUMS.txt for the release "
                              f"({e}) - refusing to install an unverified build.") from e
        want = _expected_hash(sums, "dlss5-autopilot.exe")
        if not want:
            raise UpdateError("SHA256SUMS.txt does not list dlss5-autopilot.exe "
                              "- refusing to install an unverified build.")
        have = _sha256(out)
        if have != want:
            raise UpdateError(f"The downloaded build does not match the hash "
                              f"GitHub published ({have[:12]}... vs "
                              f"{want[:12]}...) - refusing to install it.")
    return out


def _expected_hash(sums: str, name: str) -> str | None:
    """The hash for `name` from a sha256sum-style listing (paths allowed)."""
    for line in sums.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1].lstrip("*").replace("\\", "/").endswith(name):
            return parts[0].lower()
    return None


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def swap_script(current: Path, new_exe: Path) -> str:
    """The batch file that replaces `current` with `new_exe` once we exit.

    ping is the portable way to wait a moment in a .bat; the loop retries
    while the old process still holds the file handle. When the new build
    brings an _internal folder, the folder is swapped the same way - the old
    one kept as _internal.old beside the .old.exe, so the pair can be put
    back by hand.
    """
    old = current.with_suffix(".old.exe")
    new_int = new_exe.parent / INTERNAL
    cur_int = current.parent / INTERNAL
    old_int = current.parent / (INTERNAL + ".old")
    lines = [
        "@echo off",
        "setlocal",
        f'set "TARGET={current}"',
        f'set "SOURCE={new_exe}"',
        f'set "BACKUP={old}"',
        "for /L %%i in (1,1,30) do (",
        "  ping -n 2 127.0.0.1 >nul",
        '  if exist "%BACKUP%" del /q "%BACKUP%" >nul 2>&1',
        '  move /y "%TARGET%" "%BACKUP%" >nul 2>&1 && goto swap',
        ")",
        "echo Could not replace the executable; it is still running.",
        "pause",
        "exit /b 1",
        ":swap",
    ]
    if new_int.is_dir():
        # The download is staged under %TEMP%, which may be another drive
        # than the install: cmd's `move` cannot move a directory across
        # drives (and reports success anyway), so the folder is copied with
        # xcopy and the copy is checked before anything is committed. The
        # exe is copied too, for the same reason; the old pair stays as
        # .old.exe + _internal.old until the next update.
        lines += [
            f'if exist "{old_int}" rmdir /s /q "{old_int}" >nul 2>&1',
            f'if exist "{cur_int}" move /y "{cur_int}" "{old_int}" >nul 2>&1',
            f'xcopy "{new_int}" "{cur_int}\\" /E /I /Q /Y /H >nul 2>&1',
            f'if errorlevel 1 goto rollback',
            f'if not exist "{cur_int}\\*" goto rollback',
            'copy /y "%SOURCE%" "%TARGET%" >nul 2>&1',
            'if errorlevel 1 goto rollback',
            f'rmdir /s /q "{new_int}" >nul 2>&1',
            'del /q "%SOURCE%" >nul 2>&1',
            'goto done',
            ':rollback',
            f'if exist "{cur_int}" rmdir /s /q "{cur_int}" >nul 2>&1',
            f'if exist "{old_int}" move /y "{old_int}" "{cur_int}" >nul 2>&1',
            'if exist "%TARGET%" del /q "%TARGET%" >nul 2>&1',
            'move /y "%BACKUP%" "%TARGET%" >nul 2>&1',
            'echo The update could not be applied; the previous build was restored.',
            ':done',
        ]
    else:
        lines += [
            'copy /y "%SOURCE%" "%TARGET%" >nul 2>&1',
            'if not exist "%TARGET%" move /y "%BACKUP%" "%TARGET%" >nul 2>&1',
            'del /q "%SOURCE%" >nul 2>&1',
        ]
    lines += [
        'start "" "%TARGET%"',
        'del /q "%~f0" >nul 2>&1',
    ]
    return "\r\n".join(lines) + "\r\n"


def apply_and_restart(new_exe: Path) -> None:
    """Hand the swap to a helper batch file and quit so it can run."""
    current = running_exe()
    if current is None:
        raise UpdateError("Running from source, not a built executable - "
                          "nothing to replace.")

    bat = Path(tempfile.gettempdir()) / "dlss5-autopilot-update.bat"
    bat.write_text(swap_script(current, new_exe), encoding="utf8")

    creation = 0x00000008 | 0x08000000        # DETACHED_PROCESS | NO_WINDOW
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=creation,
                     close_fds=True, cwd=str(Path(tempfile.gettempdir())))
    os._exit(0)
