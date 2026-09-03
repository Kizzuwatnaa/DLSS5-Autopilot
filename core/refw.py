"""RE Engine (Capcom) support: REFramework, so ReShade does not crash it.

reengine.py explains the problem - RE Engine's own tamper checks kill ReShade
the instant it loads, add-on or no add-on. REFramework (praydog) is a
separate, actively maintained mod that installs itself as `dinput8.dll`,
loads before the game's own checks run, and patches around them - after
which ReShade loads normally (as `dxgi.dll`, unchanged) and survives.
Community reports on Resident Evil Requiem confirm ReShade with an add-on
staying up once REFramework is present, the same crash class this fixes.

Two REFramework release trains exist:
  - praydog/REFramework: tagged releases, one zip per game. Lags newer
    titles by a long way - Resident Evil Requiem (internal codename RE9)
    has no build there at all.
  - praydog/REFramework-nightly: a continuous build. Since some point in
    2026 it ships ONE universal `dinput8.dll` that detects the running
    game itself, covering RE9/Requiem along with everything older. This is
    the one that actually reaches the game people are asking about, so it
    is the one used here.
"""
from __future__ import annotations

from pathlib import Path

from . import net

API = "https://api.github.com/repos/praydog/REFramework-nightly/releases/latest"

DINPUT8 = "dinput8.dll"
BACKUP_SUFFIX = ".dlss5-autopilot-backup"


def resolve() -> tuple[str, str]:
    """(tag, download url) of the newest nightly REFramework.zip."""
    r = net.json_get(API)
    tag = r.get("tag_name", "?")
    for a in r.get("assets", []):
        if a.get("name", "").lower() == "reframework.zip":
            return tag, a["browser_download_url"]
    raise RuntimeError("Could not find REFramework.zip in the latest nightly.")


def install(exe_dir: Path, log=None) -> list[str]:
    """Drop REFramework's dinput8.dll beside the game. Returns files written.

    A dinput8.dll already there - an earlier REFramework copy the person put
    in by hand, most likely - is backed up once, the same way dgVoodoo2
    treats a D3D9.dll that was already there.
    """
    log = log or (lambda *_: None)
    tag, url = resolve()
    log(f"      REFramework {tag}")
    z = net.download(url, f"REFramework_{tag}.zip")

    written: list[str] = []
    existing = exe_dir / DINPUT8
    bak = existing.with_name(DINPUT8 + BACKUP_SUFFIX)
    if existing.is_file() and not bak.exists():
        import shutil
        try:
            shutil.copy2(existing, bak)
            written.append(bak.name)
            log(f"      kept your existing {DINPUT8} as {bak.name}")
        except OSError:
            log(f"      WARNING: could not back up the existing {DINPUT8}")

    net.extract_one(z, DINPUT8, existing)
    written.append(DINPUT8)
    log(f"      {DINPUT8} (REFramework - loads first and patches around RE "
        f"Engine's tamper checks, so ReShade survives)")
    return written
