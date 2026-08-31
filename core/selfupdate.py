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
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from . import net, update

MIN_BYTES = 4 * 1024 * 1024          # a real build is ~11 MB


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
    tag = (rel.get("tag_name") or "new").lstrip("vV")

    workdir = Path(tempfile.mkdtemp(prefix="dlss5-autopilot-update-"))
    if zip_url:
        z = net.download(zip_url, f"update-{tag}.zip", progress=progress)
        with zipfile.ZipFile(z) as arc:
            member = next((n for n in arc.namelist()
                           if n.lower().endswith(".exe")), None)
            if not member:
                raise UpdateError("The release archive contains no executable.")
            out = workdir / Path(member).name
            with arc.open(member) as src, open(out, "wb") as dst:
                dst.write(src.read())
    elif exe_url:
        got = net.download(exe_url, f"update-{tag}.exe", progress=progress)
        out = workdir / got.name
        out.write_bytes(got.read_bytes())
    else:
        raise UpdateError("The release has no downloadable build.")

    if out.stat().st_size < MIN_BYTES:
        raise UpdateError(f"The downloaded build is only {out.stat().st_size} "
                          f"bytes - refusing to install it.")
    if not _is_win64_pe(out):
        raise UpdateError("The downloaded file is not a 64-bit Windows "
                          "executable - refusing to install it.")
    return out


def apply_and_restart(new_exe: Path) -> None:
    """Hand the swap to a helper batch file and quit so it can run."""
    current = running_exe()
    if current is None:
        raise UpdateError("Running from source, not a built executable - "
                          "nothing to replace.")

    bat = Path(tempfile.gettempdir()) / "dlss5-autopilot-update.bat"
    old = current.with_suffix(".old.exe")
    # ping is the portable way to wait a moment in a .bat; the loop retries
    # while the old process still holds the file handle.
    bat.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "TARGET={current}"\r\n'
        f'set "SOURCE={new_exe}"\r\n'
        f'set "BACKUP={old}"\r\n'
        "for /L %%i in (1,1,30) do (\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        '  if exist "%BACKUP%" del /q "%BACKUP%" >nul 2>&1\r\n'
        '  move /y "%TARGET%" "%BACKUP%" >nul 2>&1 && goto swap\r\n'
        ")\r\n"
        "echo Could not replace the executable; it is still running.\r\n"
        "pause\r\n"
        "exit /b 1\r\n"
        ":swap\r\n"
        'move /y "%SOURCE%" "%TARGET%" >nul 2>&1\r\n'
        'if not exist "%TARGET%" move /y "%BACKUP%" "%TARGET%" >nul 2>&1\r\n'
        'start "" "%TARGET%"\r\n'
        'del /q "%~f0" >nul 2>&1\r\n',
        encoding="utf8")

    creation = 0x00000008 | 0x08000000        # DETACHED_PROCESS | NO_WINDOW
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=creation,
                     close_fds=True, cwd=str(Path(tempfile.gettempdir())))
    os._exit(0)
