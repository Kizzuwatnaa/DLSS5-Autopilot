r"""The OptiScaler route: DLSS 5 neural rendering without ReShade at all.

Dagherbou's OptiScaler fork runs the neural-rendering model as an extra pass
over OptiScaler's upscaler output. It matters because of where it gets its
inputs: rather than synthesising depth and motion vectors, it reads the ones
the game already hands to DLSS every frame. No ReShade, no feeder, no
motion-vector shaders - which is why people measure it faster than the
ReShade route.

    ours (feeder)  game -> ReShade -> depth copy + MV shader -> synthetic
                   contract -> DLSS. Always DLAA. Measured at ~5 ms/frame of
                   overhead on top of the DLSS pass itself.
    optiscaler     game -> OptiScaler (proxy DLL) -> reads the game's own DLSS
                   inputs -> NR pass. Real upscaling, so a lower render
                   resolution genuinely costs less to draw.

Be careful reading FPS comparisons between the two: at 75% resolution
OptiScaler is drawing fewer pixels, while the feeder is always at native. Part
of that gap is upscaling, not efficiency.

REQUIREMENTS the author states:
  - RTX 50 series ("the model does not run on anything older" - they note
    builds compiled for older cards exist but have not tested them; the
    SF/RTX40 nvngx_dlssnr builds this tool already picks do carry sm_89)
  - a driver shipping nvngx_dlssnr.dll (>= 616.56)
  - a DirectX 12 game that ALREADY uses DLSS - it reads that game's own DLSS
    inputs, so a game without DLSS gives it nothing to read

Licensing: OptiScaler is GPL-3.0. It is downloaded at run time from its own
release page and never bundled here, exactly like every other component.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from . import net

API = "https://api.github.com/repos/Dagherbou/OptiScaler_DLSSNR/releases/latest"

MAIN_DLL = "OptiScaler.dll"
FORWARDER = "nvngx.dll_dlssnr.dll"
INI = "OptiScaler.ini"
# Names the game's loader will pick up. dxgi suits most D3D12 games; the
# others exist for titles that do not load dxgi early enough.
PROXY_NAMES = ("dxgi.dll", "winmm.dll", "version.dll", "dbghelp.dll",
               "d3d12.dll", "wininet.dll", "winhttp.dll")
DEFAULT_PROXY = "dxgi.dll"

# Files the setup script would otherwise ask about; we place them ourselves.
SKIP = {"setup_windows.bat", "setup_linux.sh"}


def resolve() -> tuple[str, str]:
    """(tag, zip url) of the latest OptiScaler + DLSS-NR build."""
    rel = net.json_get(API)
    for a in rel.get("assets", []):
        if a["name"].lower().endswith(".zip"):
            return rel.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("The OptiScaler DLSS-NR release has no .zip asset.")


def install(exe_dir: Path, proxy: str = DEFAULT_PROXY, dl=None,
            log=None) -> list[str]:
    """Extract OptiScaler into the game folder under the chosen proxy name.

    Returns the relative paths written, so uninstall can undo exactly this.
    """
    log = log or (lambda *_: None)
    dl = dl or (lambda url, name: net.download(url, name))

    tag, url = resolve()
    log(f"      OptiScaler DLSS-NR {tag}")
    z = dl(url, f"OptiScaler-DLSSNR-{tag}.zip")

    written: list[str] = []
    with zipfile.ZipFile(z) as arc:
        for member in arc.namelist():
            if member.endswith("/"):
                continue
            name = Path(member).name
            if name in SKIP:
                continue
            # OptiScaler.dll has to be named whatever the game loads.
            rel = proxy if member == MAIN_DLL else member
            target = exe_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with arc.open(member) as src, open(target, "wb") as out:
                import shutil
                shutil.copyfileobj(src, out, 1 << 20)
            written.append(rel.replace("\\", "/"))
    log(f"      {len(written)} files, OptiScaler.dll installed as {proxy}")
    log(f"      the forwarder {FORWARDER} is required: the model refuses "
        f"calls from a module whose path lacks 'nvngx.dll'")
    return written


def enable_nr(exe_dir: Path, log=None) -> None:
    """Ask for neural rendering in OptiScaler.ini.

    The shipped ini has no [DLSSNR] section - OptiScaler writes its own keys on
    first run - so we append the section rather than rewriting the file. It
    stays off until switched on in the overlay if the key is not honoured,
    which is the documented default.
    """
    log = log or (lambda *_: None)
    p = exe_dir / INI
    try:
        text = p.read_text(encoding="utf8", errors="replace") if p.is_file() else ""
        if "[DLSSNR]" not in text:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n[DLSSNR]\nEnabled=true\n"
            p.write_text(text, encoding="utf8")
            log("      OptiScaler.ini: [DLSSNR] Enabled=true")
        else:
            log("      OptiScaler.ini already has a [DLSSNR] section, left alone")
    except OSError:
        log("      could not write OptiScaler.ini")


def requirements_note(sm: int | None) -> str | None:
    """A warning when this card is outside what the author has tested."""
    if sm is None:
        return ("The OptiScaler author states an RTX 50 series card is "
                "required. Your card could not be detected.")
    if sm < 120:
        return ("The OptiScaler author states an RTX 50 series card is "
                "required and has not tested older ones. Builds of "
                "nvngx_dlssnr compiled for your architecture do exist - this "
                "tool installs one - so it may work, but you are off the "
                "tested path.")
    return None
