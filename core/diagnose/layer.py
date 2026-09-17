r"""Whether ReShade reaches this install as a Vulkan layer, and what that says.

On the DXVK route (and a native Vulkan game) there is no proxy DLL in the
folder: ReShade is a registry entry the Vulkan loader reads, and the DLLs
beside the exe are DXVK's. So "our DLLs are in the process and ReShade is
not" means the layer never reached the game, not a missing proxy (#238) -
both evidence readers ask this before they say so, and body.py lists the
DXVK files in a report. Below evidence.py, which imports it.

Part of core/diagnose; see __init__.py.
"""
from __future__ import annotations
from pathlib import Path

from . import model


__all__ = ["_addon_in", "_dxvk_files", "_layer_clash", "_layer_detail",
           "_through_layer"]


def _through_layer(man: dict) -> bool:
    """Does ReShade reach this install as a Vulkan layer, not a proxy DLL?"""
    return bool(man.get("dxvk")) or man.get("proxy") == model.VULKAN_LAYER


def _addon_in(names) -> bool:
    """An add-on in the process means ReShade DID load: not a layer problem."""
    return any(str(n).lower().endswith((".addon32", ".addon64"))
               for n in names or ())


def _layer_clash(man: dict):
    """A 32-bit install whose layer name the 64-bit one already holds.

    The same question chain.py asks before it blames the registration: two
    manifests may not share a layer name, and that is what killed every
    32-bit DXVK game (#31, #2). Asked here too, so the more specific
    answer wins in both readers rather than only in one.
    """
    if man.get("bitness") != 32:
        return None
    try:
        from .. import vulkan as _vk
        return _vk.name_clash()
    except Exception:
        return None


def _layer_detail(loaded, missing, man: dict, tense: str) -> str:
    """What was in the process, what was not, and what to do about it.

    Two facts make this verdict true, and neither was on the screen: that
    the file which IS loaded belongs to DXVK rather than to ReShade, and
    that the add-on is not in the process at all. The reply for #238 had
    to supply both by hand - which is the test for whether the tool should
    be saying them itself.

    The advice is chain.py's, which answers this same symptom with
    something to do: the layer's registration is per user and per
    architecture, so installing again rewrites it. "Open an issue" is what
    is left after that, not instead of it.
    """
    dxvk = {Path(d).name.lower() for d in _dxvk_files(man)}
    names = [Path(p).name for p in loaded]
    said = ", ".join(sorted(set(names)))
    if dxvk and {n.lower() for n in names} <= dxvk:
        said += " (DXVK's, on this route - not ReShade's)"
    out = f"Loaded {tense}: {said}. "
    gone = ", ".join(sorted({Path(m).name for m in missing or ()}))
    if gone:
        out += f"Not in the process: {gone}. "
    out += ("ReShade reaches this game as a Vulkan layer, and the layer "
            "writes its log the moment it loads - so it is not reaching "
            "the game. Its registration is per user and per architecture: "
            "install again with this version to rewrite it, and start the "
            "game once more. If there is still no log, another Vulkan "
            "layer on this PC (an overlay, a capture tool, another ReShade "
            "install) is the next thing to rule out - say so in an issue "
            "with this report.")
    return out


def _dxvk_files(man: dict) -> list[str]:
    """The DXVK DLLs this install wrote. Taken from the recorded file list,
    not from the manifest's api: by the time the manifest is written the
    game has been re-labelled Vulkan, which says nothing about whether DXVK
    came in as d3d9.dll or as dxgi.dll + d3d11.dll."""
    if not man.get("dxvk"):
        return []
    from .. import dxvk as _dxvk
    # As recorded, subfolder included: a Source game takes DXVK in bin
    # (#224). Only the folders DXVK is put in - host64/dxgi.dll is the
    # 32-bit helper's ReShade, not DXVK.
    out = []
    for f in man.get("files") or []:
        if not isinstance(f, str):
            continue
        low = f.replace("\\", "/").lower()
        head, _, base = low.rpartition("/")
        if base in _dxvk.ALL_FILES and head in _dxvk.TARGET_DIRS \
                and low not in out:
            out.append(low)
    return out
