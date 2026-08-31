r"""DirectX 9 support - DX9 to D3D11 translation via dgVoodoo2.

DLSS5-Feeder cannot feed DX9 directly. The chain is:

    game (DX9) -> D3D9.dll (dgVoodoo2, translates to D3D11)
               -> dxgi.dll (ReShade)
               -> dlss5-feed.addon32  ->  host64\ (64-bit DLSS)

Everything after dgVoodoo2 is identical to the 32-bit path.

WARNING: in practice this is the least reliable path of all. The DLSS feature
often fails to create on top of a translated device. Treat it as experimental.

dgVoodoo2 is not open source but is freely redistributed by its author;
releases are published at github.com/dege-diosg/dgVoodoo2.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import net

API = "https://api.github.com/repos/dege-diosg/dgVoodoo2/releases"

D3D9 = "D3D9.dll"
CONF = "dgVoodoo.conf"
CPL = "dgVoodooCpl.exe"


def resolve() -> tuple[str, str]:
    """(version, download_url) of the latest regular package (not dbg/dev64)."""
    rels = net.json_get(API + "?per_page=10")
    for r in rels:
        for a in r.get("assets", []):
            n = a["name"].lower()
            if n.startswith("dgvoodoo2_") and n.endswith(".zip") \
                    and "dbg" not in n and "dev" not in n:
                return r.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("Could not find a dgVoodoo2 package.")


def tune_conf(text: str, vram_mb: int = 1024) -> str:
    """Apply the settings DLSS5-Feeder requires.

    From the documentation:
      [DirectX] DisableAndPassThru=false, VRAM=1024, VideoCard=internal3D
      [General] OutputAPI=d3d11_fl11_0
      watermark off (turn it on manually if you want to verify dgVoodoo loads)
    """
    wanted = {
        "OutputAPI": "d3d11_fl11_0",
        "DisableAndPassThru": "false",
        "VRAM": str(vram_mb),
        "VideoCard": "internal3D",
        "dgVoodooWatermark": "false",
        # dgVoodoo confines the cursor to the game window by default. ReShade's
        # overlay (Home) then cannot take mouse input and the game looks frozen
        # - it is waiting for a click it can never receive. Measured on GTA IV:
        # DLSS was delivering frames fine, but pressing Home appeared to hang
        # the game until this was turned off.
        "CaptureMouse": "false",
        # NOT touching FullScreenMode. Forcing windowed output looked like a
        # sensible companion to CaptureMouse, but GTA IV threw a DirectX fatal
        # error with it - the game's own display settings and dgVoodoo's have
        # to agree. Exclusive fullscreen is still worth avoiding; that belongs
        # in the game's own options, not in a config we rewrite.
    }
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$", line)
        if m and m.group(2) in wanted:
            key = m.group(2)
            out.append(f"{m.group(1)}{key} = {wanted[key]}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


BACKUP_SUFFIX = ".dlss5-autopilot-backup"


def install(exe_dir: Path, log=None) -> list[str]:
    """Install dgVoodoo2 next to the game. Returns the files written.

    A D3D9.dll already sitting there is somebody else's wrapper - DXVK is a
    common choice for GTA IV - so it is preserved before being replaced.
    """
    log = log or (lambda *_: None)
    ver, url = resolve()
    log(f"      dgVoodoo2 {ver}")
    z = net.download(url, f"dgVoodoo2_{ver}.zip")

    written: list[str] = []
    # A 32-bit game needs the MS/x86 build
    existing = exe_dir / D3D9
    bak = existing.with_name(D3D9 + BACKUP_SUFFIX)
    if existing.is_file() and not bak.exists():
        try:
            import shutil
            shutil.copy2(existing, bak)
            written.append(bak.name)
            log(f"      kept your existing {D3D9} as {bak.name}")
        except OSError:
            log(f"      WARNING: could not back up the existing {D3D9}")
    net.extract_one(z, "MS/x86/D3D9.dll", exe_dir / D3D9)
    written.append(D3D9)
    log(f"      {D3D9} (MS/x86 build, for the 32-bit game)")

    # Keep an existing conf if the user already tuned one
    conf_path = exe_dir / CONF
    if conf_path.is_file():
        text = conf_path.read_text(encoding="utf8", errors="replace")
        log("      existing dgVoodoo.conf found, only required keys updated")
    else:
        tmp = exe_dir / (CONF + ".tmp")
        net.extract_one(z, CONF, tmp)
        text = tmp.read_text(encoding="utf8", errors="replace")
        tmp.unlink(missing_ok=True)
    conf_path.write_text(tune_conf(text), encoding="utf8")
    written.append(CONF)
    log("      dgVoodoo.conf -> OutputAPI=d3d11_fl11_0, VRAM=1024, VideoCard=internal3D")

    # Control panel, in case the user wants to adjust things by hand
    try:
        net.extract_one(z, CPL, exe_dir / CPL)
        written.append(CPL)
        log(f"      {CPL} (for manual tweaking)")
    except Exception:
        pass
    return written
