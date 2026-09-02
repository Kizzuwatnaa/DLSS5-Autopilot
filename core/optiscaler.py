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
  - RTX 50 series in the author's own testing. They note builds compiled for
    older cards exist but have not tested them - the SF/RTX40 nvngx_dlssnr
    builds this tool picks carry sm_75/86/89, and RTX 40 is confirmed working
    in the field (KCD2 on a 4060 Ti), so older cards are on: the dial is what
    makes the pass affordable there.
  - a driver shipping nvngx_dlssnr.dll (>= 616.56)
  - a D3D12 or D3D11 game that ALREADY uses DLSS. It reads that game's own
    DLSS inputs, so a game without DLSS gives it nothing to read - unless
    the game ships FSR 2/3 or XeSS: upstream OptiScaler hooks those calls
    as its input (its [Inputs] section) and runs DLSS in their place, and
    the fork's pass then sits behind that DLSS. enable_inputs() below sets
    that up; the tool puts a nvngx_dlss.dll beside it, since such a game
    has none.

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


# The fork's [DlssNr] section (Config.cpp reads it case-insensitively). Only
# the handful worth a control are surfaced; the rest stay on the overlay.
NR_SECTION = "DlssNr"
NR_PRESETS = {0: "Default", 1: "Preset 1", 2: "Preset 2", 3: "Preset 3"}
NR_STYLES = {0: "Standard", 1: "Natural", 2: "Cinematic"}
NR_SCALE_MIN, NR_SCALE_MAX = 25, 100
# Where the dial pays off. At 100% the pass costs about half your fps; the
# author's note is that cost falls with the square, so 75% is roughly half
# the cost and 50% a quarter, while the frame itself stays at full detail.
NR_SCALE_DEFAULT = 75


def _ini_set(text: str, section: str, values: dict[str, str]) -> str:
    """Set keys inside one ini section, creating the section if needed.

    Lines outside the section are untouched, comments included, so a tuned
    OptiScaler.ini keeps everything the user put in it.
    """
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if start is not None and end is None:
                end = i
            if s[1:-1].lower() == section.lower():
                start = i
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        lines += [f"{k}={v}" for k, v in values.items()]
        return "\n".join(lines) + "\n"
    if end is None:
        end = len(lines)
    pending = dict(values)
    for i in range(start + 1, end):
        s = lines[i].strip()
        if not s or s[0] in ";#" or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        for want in list(pending):
            if want.lower() == k.lower():
                lines[i] = f"{want}={pending.pop(want)}"
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    for k, v in pending.items():
        lines.insert(insert_at, f"{k}={v}")
        insert_at += 1
    return "\n".join(lines) + "\n"


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{v:.1f}"
    return str(v)


def enable_nr(exe_dir: Path, log=None, settings: dict | None = None) -> None:
    """Turn neural rendering on in OptiScaler.ini and apply the chosen dials.

    Only the keys given are written; everything else in the file - including
    keys the user tuned in the [DlssNr] section - is left exactly as it was.
    The overlay stays the authority for the rest.
    """
    log = log or (lambda *_: None)
    p = exe_dir / INI
    values = {"Enabled": "true"}
    for k, v in (settings or {}).items():
        values[k] = _fmt(v)
    try:
        text = p.read_text(encoding="utf8", errors="replace") if p.is_file() else ""
        # An older release of this tool wrote the section in capitals. The
        # reader does not mind, but two spellings in one file are confusing.
        text = text.replace("[DLSSNR]", f"[{NR_SECTION}]")
        p.write_text(_ini_set(text, NR_SECTION, values), encoding="utf8")
        log(f"      OptiScaler.ini: [{NR_SECTION}] "
            + ", ".join(f"{k}={v}" for k, v in values.items()))
        log(f"      if it does not come on, press {OVERLAY_KEY} in game and "
            f"tick it under DLSS Neural Rendering")
    except OSError:
        log("      could not write OptiScaler.ini")


def set_dx11_bridged_upscaler(exe_dir: Path, log=None) -> None:
    """On D3D11 the model only runs on OptiScaler's D3D12 bridge.

    The author's words: the model flat out refuses to run on DX11, so a
    bridged upscaler has to be selected - and DLSS cannot be that upscaler.
    fsr22_12 is built into OptiScaler itself, so nothing extra is needed.
    """
    log = log or (lambda *_: None)
    p = exe_dir / INI
    try:
        text = p.read_text(encoding="utf8", errors="replace") if p.is_file() else ""
        p.write_text(_ini_set(text, "Upscalers", {"Dx11Upscaler": "fsr22_12"}),
                     encoding="utf8")
        log("      OptiScaler.ini: [Upscalers] Dx11Upscaler=fsr22_12 (the "
            "model does not run on D3D11 itself; FSR 2.2 on D3D12 carries it)")
    except OSError:
        log("      could not write OptiScaler.ini")


# What OptiScaler_dlssnr.ini (the fork's shipped ini, in _research/) says
# about its [Inputs] section - every default is already "auto" = true, but a
# hand-tuned ini or an older one may say otherwise, so they are written out:
#   ; OptiScaler will hook (libxess.dll) and use XeSS Inputs
#   ; true or false - Default (auto) is true
#   EnableXeSSInputs=auto
#   ; OptiScaler will hook Fsr2 Inputs
#   ; true or false - Default (auto) is true
#   EnableFsr2Inputs=auto
#   ; OptiScaler will hook Fsr2 Dx11 Inputs instead of Dx12 one
#   ; true or false - Default (auto) is false
#   UseFsr2Dx11Inputs=auto
#   ; OptiScaler will use Fsr2 Inputs
#   ; true or false - Default (auto) is true
#   UseFsr2Inputs=auto
#   ; OptiScaler will hook Fsr3 Inputs
#   ; true or false - Default (auto) is true
#   EnableFsr3Inputs=auto
#   ; OptiScaler will use Fsr3 Inputs
#   ; true or false - Default (auto) is true
#   UseFsr3Inputs=auto
#   ; OptiScaler will hook FidelityFX (amd_fidelityfx_dx12.dll) API Inputs
#   ; true or false - Default (auto) is true
#   EnableFfxInputs=auto
#   ; OptiScaler will use FidelityFX API Inputs
#   ; true or false - Default (auto) is true
#   UseFfxInputs=auto
# Fsr2Pattern / Fsr3Pattern ("Try to find FSR2 methods with pattern
# matching - Will slow down the loading of the game") stay at their default:
# the export hooks come first, and the overlay can turn pattern search on.
INPUT_KEYS = {
    "fsr": {"EnableFsr2Inputs": "true", "UseFsr2Inputs": "true",
            "EnableFsr3Inputs": "true", "UseFsr3Inputs": "true",
            "EnableFfxInputs": "true", "UseFfxInputs": "true"},
    "xess": {"EnableXeSSInputs": "true"},
}

# And its [Upscalers] section, for the API the game renders with:
#   ; Select Upscaler for Dx12 games
#   ; xess, fsr21, fsr22, ffx (FSR 2.3; 3.1; 4.x), dlss
#   ; Default (auto) is DLSS when capable gpu, FSR4 when capable gpu, XeSS otherwise
#   Dx12Upscaler=auto
# "auto" already lands on DLSS on an RTX card, but the whole point of this
# route is DLSS, so it is pinned. D3D11 is different - see
# set_dx11_bridged_upscaler: the model refuses D3D11, so the bridged FSR
# stays there and DLSS is never the D3D11 upscaler.
DX12_UPSCALER = "dlss"


def enable_inputs(exe_dir: Path, upscaler: str, api: str, log=None) -> None:
    """Make OptiScaler take the game's FSR/XeSS calls as input and run DLSS.

    Only the keys for the upscaler actually seen are touched; the rest of the
    [Inputs] section keeps whatever the file says.
    """
    log = log or (lambda *_: None)
    keys = dict(INPUT_KEYS.get(upscaler, {}))
    if not keys:
        return
    if upscaler == "fsr" and api == "DX11":
        # A D3D11 game calls the D3D11 FSR2 entry points, which OptiScaler
        # hooks "instead of Dx12 one" only when told to.
        keys["UseFsr2Dx11Inputs"] = "true"
    p = exe_dir / INI
    try:
        text = p.read_text(encoding="utf8", errors="replace") if p.is_file() else ""
        text = _ini_set(text, "Inputs", keys)
        if api != "DX11":
            text = _ini_set(text, "Upscalers", {"Dx12Upscaler": DX12_UPSCALER})
        p.write_text(text, encoding="utf8")
        log("      OptiScaler.ini: [Inputs] "
            + ", ".join(f"{k}={v}" for k, v in keys.items())
            + (f"; [Upscalers] Dx12Upscaler={DX12_UPSCALER}" if api != "DX11" else ""))
        log(f"      the game's {'FSR' if upscaler == 'fsr' else 'XeSS'} calls "
            f"go into OptiScaler, which runs DLSS in their place")
    except OSError:
        log("      could not write OptiScaler.ini")


def describe_nr(settings: dict | None) -> list[str]:
    """Human-readable summary of the dials chosen, for the notes."""
    out = []
    if not settings:
        return out
    ws = settings.get("WorkingScale")
    if ws is not None:
        pct = int(round(float(ws) * 100))
        cost = int(round(100 * float(ws) ** 2))
        out.append(f"model resolution {pct}% - about {cost}% of the full-size "
                   f"cost; the frame itself stays full detail")
    pr = settings.get("Preset")
    if pr:
        out.append(f"model preset: {NR_PRESETS.get(int(pr), pr)}")
    st = settings.get("Style")
    if st:
        out.append(f"style: {NR_STYLES.get(int(st), st)}")
    return out


DRIVER_MIN = "616.56"


def requirements_note(sm: int | None) -> str | None:
    """A warning when this card or driver is outside what the author supports."""
    if sm is None:
        return ("The OptiScaler author states an RTX 50 series card is "
                "required. Your card could not be detected.")
    if sm < 120:
        return ("The OptiScaler author tested RTX 50 only. This tool installs "
                "the community nvngx_dlssnr build compiled for your card, which "
                "is what makes it run here; if the overlay ever says the model "
                "refused, the native route is one click away.")
    from . import gpu
    ok = gpu.driver_at_least(DRIVER_MIN)
    if ok is False:
        return (f"OptiScaler's DLSS-NR needs NVIDIA driver {DRIVER_MIN} or "
                f"newer; you have {gpu.driver_version()}. Update the driver "
                f"first (the 3 September Game Ready driver qualifies).")
    return None
