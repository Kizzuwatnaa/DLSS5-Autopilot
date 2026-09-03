"""Capcom's RE Engine: ReShade's add-on support is documented to crash it.

Not an anti-cheat in the usual sense - no BattlEye, no EAC - but the effect
on this tool is the same: the game can go down the instant ReShade (with
add-on support, which every route here needs) loads, worst on titles that
also carry Denuvo (Resident Evil Requiem). Community reports are consistent
across RE2/RE3/RE4 remakes, RE7, RE8 (Village) and Requiem; older titles are
sometimes fine, newer ones less so, and there is no reliable way to tell
from here which side of that line a given release falls on.

Detection is by the engine's own marker file rather than a name list, so it
covers a title nobody has told us about yet - the same reasoning as
anticheat.py.
"""
from __future__ import annotations

from pathlib import Path

MARKER = "re_chunk_000.pak"

WARNING = (
    "This looks like a Capcom RE Engine game ({marker} is in the folder) - "
    "the engine behind the Resident Evil 2/3/4 remakes, RE7, RE8 (Village) "
    "and Requiem.\n\n"
    "ReShade's add-on support, which every route here needs, is documented "
    "to crash several RE Engine titles the moment it loads - Resident Evil "
    "Requiem worst of all, since it also carries Denuvo. This is the "
    "engine's own tamper protection, not a mistake in the setup, and this "
    "tool cannot work around it.\n\n"
    "The community workaround (REFramework, not part of this tool) loads "
    "ReShade as 'dinput8.dll' instead of the usual 'dxgi.dll' - it sits "
    "under 'reshade proxy' on the install page if you want to try it."
)


def detected(folder: Path) -> bool:
    try:
        return (Path(folder) / MARKER).is_file()
    except OSError:
        return False


def message() -> str:
    return WARNING.format(marker=MARKER)
