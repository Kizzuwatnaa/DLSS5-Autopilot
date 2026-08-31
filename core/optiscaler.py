r"""The OptiScaler route: DLSS 5 neural rendering without ReShade at all.

Dagherbou's OptiScaler fork runs the neural-rendering model as an extra pass
over OptiScaler's upscaler output. What makes it interesting is where the
inputs come from: rather than synthesising depth and motion vectors, it reads
the ones the game already hands to DLSS every frame.

    feeder      game -> ReShade -> depth copy + motion-vector shader ->
                synthetic contract -> DLSS. Always DLAA, and the shader work
                is real per-frame cost.
    optiscaler  game -> OptiScaler (proxy DLL) -> the game's own DLSS inputs
                -> NR pass. Real upscaling, so a lower render resolution
                genuinely costs less to draw.

Read FPS comparisons between the two carefully: at 75% resolution OptiScaler
is drawing fewer pixels while the feeder is always at native, so part of any
gap is upscaling rather than efficiency.

REQUIREMENTS the author states:
  - RTX 50 series. They note builds compiled for older cards exist but say
    they have not tested them - the SF/RTX40 nvngx_dlssnr builds this tool
    picks do carry sm_86/sm_89, so older cards are worth a try but off the
    tested path.
  - a driver shipping nvngx_dlssnr.dll (>= 616.56)
  - a D3D12 or D3D11 game that ALREADY uses DLSS. It reads that game's own
    DLSS inputs, so a game without DLSS gives it nothing to read.

Two things it does better than the feeder, beyond speed:
  - the pass runs right after the upscaler and BEFORE the interface is drawn,
    so the model never sees the HUD. The feeder processes the HUD along with
    the scene, a known limitation upstream.
  - with frame generation it runs once per RENDERED frame; generated frames
    inherit the result.

Licensing: OptiScaler is GPL-3.0. It is fetched at run time from its own
release page and never bundled here, like every other component.
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from . import net, sources

API = "https://api.github.com/repos/Dagherbou/OptiScaler_DLSSNR/releases/latest"

# Insert opens OptiScaler's own overlay (0x2D / VK_INSERT).
OVERLAY_KEY = "Insert"

MAIN_DLL = "OptiScaler.dll"
FORWARDER = "nvngx.dll_dlssnr.dll"
INI = "OptiScaler.ini"
BACKUP_SUFFIX = ".dlss5-autopilot-backup"

# Names a game's loader will pick up. OptiScaler's setup offers exactly these.
# dxgi suits most D3D12 titles; the rest exist for games that do not load dxgi
# early enough, or that ship their own dxgi.dll already.
PROXY_NAMES = ("dxgi.dll", "winmm.dll", "version.dll", "dbghelp.dll",
               "d3d12.dll", "wininet.dll", "winhttp.dll")
DEFAULT_PROXY = "dxgi.dll"

PROXY_HELP = {
    "dxgi.dll": "default - works for most D3D12 and D3D11 games",
    "winmm.dll": "when the game ships its own dxgi.dll, or dxgi does nothing",
    "version.dll": "another loader hook; try after winmm",
    "dbghelp.dll": "loads very early - some Unreal Engine titles need this",
    "d3d12.dll": "D3D12 only, and only if dxgi is already taken",
    "wininet.dll": "last resort for games that load none of the above",
    "winhttp.dll": "last resort for games that load none of the above",
}

# Left behind by OptiScaler releases before 0.9. Its setup script flags these
# as conflicting with the current version and advises removing them.
LEGACY_FILES = ("nvapi64.dll", "nvngx.dll", "OptiScaler.asi",
                "Remove OptiScaler.bat", "Remove_OptiScaler.bat")

# The setup scripts do by hand what this tool does itself.
SKIP = {"setup_windows.bat", "setup_linux.sh"}


def resolve() -> tuple[str, str]:
    """(tag, zip url) of the latest OptiScaler + DLSS-NR build.

    Goes through the shared cached fetcher, so this route survives GitHub's
    anonymous rate limit the same way every other component does - a stale
    cached answer beats refusing to install.
    """
    rel = sources._json(API)
    for a in rel.get("assets", []):
        if a["name"].lower().endswith(".zip"):
            return rel.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("The OptiScaler DLSS-NR release has no .zip asset.")


def is_optiscaler(path: Path) -> bool:
    """Is this DLL OptiScaler wearing a different name?

    OptiScaler's own setup answers this by reading the PE version resource's
    OriginalFilename, which stays "OptiScaler.dll" whatever the file is called.
    Version resources are UTF-16, so looking for the name in that encoding
    finds it without a full resource walk.

    It matters because an OptiScaler already installed as winmm.dll would load
    alongside a second copy installed as dxgi.dll, and the two fight.
    """
    try:
        if not path.is_file() or path.stat().st_size < (1 << 20):
            return False
        return MAIN_DLL.encode("utf-16-le") in path.read_bytes()
    except OSError:
        return False


def find_existing(exe_dir: Path, ignore: str = "") -> list[Path]:
    """Any OptiScaler already installed here, under whatever proxy name."""
    return [exe_dir / n for n in PROXY_NAMES
            if n.lower() != ignore.lower() and is_optiscaler(exe_dir / n)]


def find_legacy(exe_dir: Path) -> list[Path]:
    """Pre-0.9 OptiScaler leftovers its setup warns about."""
    return [exe_dir / n for n in LEGACY_FILES if (exe_dir / n).exists()]


def suggest_proxy(exe_dir: Path) -> str:
    """A proxy name that is not already taken by something else.

    A game shipping its own dxgi.dll (an ENB, a DXVK build, its own wrapper)
    would have it replaced. Backups make that reversible, but stepping aside
    is better than relying on the undo.
    """
    for name in PROXY_NAMES:
        p = exe_dir / name
        if not p.exists() or is_optiscaler(p):
            return name
    return DEFAULT_PROXY


def install(exe_dir: Path, proxy: str = DEFAULT_PROXY, dl=None, log=None,
            backup=None, release: tuple[str, str] | None = None) -> list[str]:
    """Extract OptiScaler into the game folder under the chosen proxy name.

    `backup(path)` is called before anything existing is overwritten, so the
    caller can record it in the install manifest and put it back on uninstall.
    `release` is an already-resolved (tag, url); pass it when the caller has
    looked the release up, so one install does not spend two API requests
    against a 60-per-hour allowance.
    Returns the relative paths written.
    """
    log = log or (lambda *_: None)
    dl = dl or (lambda url, name: net.download(url, name))
    if proxy not in PROXY_NAMES:
        raise ValueError(f"{proxy} is not a proxy name OptiScaler supports.")

    written: list[str] = []

    def _keep(target: Path) -> None:
        """Preserve an existing file, through the caller's manifest if given."""
        if not target.is_file():
            return
        if backup is not None:
            backup(target)
            return
        bak = target.with_name(target.name + BACKUP_SUFFIX)
        if not bak.exists():
            try:
                shutil.copy2(target, bak)
                written.append(str(bak.relative_to(exe_dir)).replace("\\", "/"))
            except OSError:
                pass

    # A copy under a different name would load alongside this one. OptiScaler's
    # own setup only warns; since we can undo it, move it aside properly.
    for other in find_existing(exe_dir, ignore=proxy):
        _keep(other)
        try:
            other.unlink()
            log(f"      moved aside {other.name} - it is also OptiScaler, and "
                f"two copies conflict")
        except OSError:
            log(f"      WARNING: {other.name} is also OptiScaler but is locked; "
                f"remove it by hand or the two will fight")

    for old in find_legacy(exe_dir):
        _keep(old)
        try:
            old.unlink()
            log(f"      moved aside {old.name} (pre-0.9 OptiScaler leftover)")
        except OSError:
            pass

    tag, url = release if release else resolve()
    log(f"      OptiScaler DLSS-NR {tag}")
    z = dl(url, f"OptiScaler-DLSSNR-{tag}.zip")

    with zipfile.ZipFile(z) as arc:
        for member in arc.namelist():
            if member.endswith("/"):
                continue
            if Path(member).name in SKIP:
                continue
            # OptiScaler.dll has to carry whatever name the game will load.
            rel = proxy if member == MAIN_DLL else member
            target = exe_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _keep(target)          # someone may have a tuned OptiScaler.ini
            with arc.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            written.append(rel.replace("\\", "/"))

    log(f"      OptiScaler.dll installed as {proxy}")
    log(f"      {FORWARDER} placed - the model refuses calls from a module "
        f"whose path does not contain 'nvngx.dll'")
    return written


def enable_nr(exe_dir: Path, log=None) -> None:
    """Ask for neural rendering in OptiScaler.ini.

    The shipped ini has no [DLSSNR] section - OptiScaler writes its own keys on
    first run - so the section is appended rather than the file rewritten. The
    documented way to turn it on is the overlay; this only saves a step, and
    the overlay stays the authority.
    """
    log = log or (lambda *_: None)
    p = exe_dir / INI
    try:
        text = p.read_text(encoding="utf8", errors="replace") if p.is_file() else ""
        if "[DLSSNR]" in text:
            log("      OptiScaler.ini already has a [DLSSNR] section, left alone")
            return
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n[DLSSNR]\nEnabled=true\n"
        p.write_text(text, encoding="utf8")
        log("      OptiScaler.ini: [DLSSNR] Enabled=true")
        log(f"      if it does not come on, press {OVERLAY_KEY} in game and "
            f"tick it under DLSS Neural Rendering")
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
