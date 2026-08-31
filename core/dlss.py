"""Does this game ship its own DLSS, and which install path suits it?

This decides between three ways of getting DLSS 5 neural rendering into a
game. Getting it right matters: the feeder path is always DLAA and needs
motion-vector shaders, whereas a game with its own DLSS can be hooked
directly and keeps its own Quality/Balanced/Performance modes.

    NATIVE   the game has DLSS and renders with D3D12.
             The DLSS 5 add-on detours the game's own NGX D3D12 calls.
             Nothing synthetic, and the game's own DLSS quality mode applies.

    BRIDGE   the game has DLSS but renders with D3D11 or Vulkan, which the
             add-on does not hook. dlss5-bridge reproduces the contract on a
             private D3D12 session. Also covers games with no DLSS at all by
             building a synthetic contract from the NVIDIA driver's optical
             flow engine - no ReShade motion-vector shader involved.

    FEEDER   DLSS5-Feeder builds a synthetic DLAA contract out of ReShade's
             depth buffer and shader-estimated motion vectors. Works without
             any DLSS in the game, but is always DLAA.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

NATIVE, BRIDGE, FEEDER, OPTI = "native", "bridge", "feeder", "optiscaler"

# Streamline is NVIDIA's own plugin layer; if a game ships it, it ships DLSS.
# These are never files this tool installs, so they are unambiguous.
STREAMLINE = ("sl.interposer.dll", "sl.dlss.dll", "sl.common.dll",
              "sl.dlss_g.dll", "sl.reflex.dll")
# A game's own runtime, or one renamed by a user to disable it.
OWN_RUNTIME = ("nvngx_dlss.dlsss", "nvngx_dlss.dll.bak", "_nvngx.dll")


@dataclass
class Support:
    native_dlss: bool = False
    evidence: list[str] = None            # type: ignore[assignment]
    recommended: str = FEEDER
    reason: str = ""
    options: list[str] = None             # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []
        if self.options is None:
            self.options = []


def _ours(folder: Path, name: str) -> bool:
    """Is this file one we installed, rather than the game's own?"""
    from . import installer
    if (folder / (name + installer.BACKUP_SUFFIX)).is_file():
        return True                       # we replaced the game's copy
    man = folder / installer.MANIFEST
    if man.is_file():
        try:
            import json
            data = json.loads(man.read_text(encoding="utf8"))
            return name in data.get("files", [])
        except Exception:
            return False
    return False


def detect(install_dir: Path, folder: Path, api: str, bitness: int) -> Support:
    """Work out what the game supports and which path to recommend."""
    s = Support()

    for d in {install_dir, folder}:
        for m in STREAMLINE:
            if (d / m).is_file():
                s.native_dlss = True
                s.evidence.append(m)
        for m in OWN_RUNTIME:
            if (d / m).is_file():
                s.native_dlss = True
                s.evidence.append(m)
        # A plain nvngx_dlss.dll counts only when we did not put it there.
        if (d / "nvngx_dlss.dll").is_file() and not _ours(d, "nvngx_dlss.dll"):
            s.native_dlss = True
            s.evidence.append("nvngx_dlss.dll")
    s.evidence = sorted(set(s.evidence))

    # --- pick a path ----------------------------------------------------
    if bitness != 64:
        # dlss5-bridge ships only as a 64-bit add-on, so a 32-bit process
        # cannot load it. The feeder's host64 helper is the only way in.
        s.options = [FEEDER]
        s.recommended = FEEDER
        s.reason = ("32-bit games can only use the feeder path: the bridge is "
                    "a 64-bit add-on and a 32-bit process cannot load it, so "
                    "the host64 helper is the only way in.")
        return s

    if api == "DX9":
        # dgVoodoo2 turns the game into a D3D11 application, and D3D11 is
        # exactly what the bridge hooks - so after translation the bridge is
        # available and avoids the 32-bit cross-process helper entirely.
        s.options = [BRIDGE, FEEDER]
        s.recommended = BRIDGE
        s.reason = ("DirectX 9 is translated to D3D11 by dgVoodoo2 first. The "
                    "bridge can then hook that D3D11 device directly, which "
                    "skips the 32-bit helper process the feeder needs. Both "
                    "routes are long shots on DX9.")
        return s

    if api == "DX10":
        # DXGI-based, so ReShade attaches, but neither the add-on (D3D12) nor
        # the bridge (D3D11/Vulkan) hooks D3D10. Only the feeder's synthetic
        # contract can reach it, and no DX10-era game has DLSS anyway.
        s.options = [FEEDER]
        s.recommended = FEEDER
        s.reason = ("DirectX 10 is reachable only through the feeder's "
                    "synthetic contract - nothing hooks D3D10 itself. Very "
                    "few games are DX10-only and none of them have DLSS.")
        return s

    if s.native_dlss:
        if api in ("DX11",):
            # OptiScaler handles D3D11 too: its author states the pass rides
            # the D3D11-on-D3D12 bridge that already carries the upscaler.
            s.options = [BRIDGE, OPTI, FEEDER]
            s.recommended = BRIDGE
            s.reason = ("This game has its own DLSS but renders with D3D11, "
                        "which the add-on cannot hook directly. The bridge "
                        "reproduces the contract on a private D3D12 session, "
                        "and the game's own DLSS quality mode still applies. "
                        "OptiScaler also works here and is cheaper, but "
                        "replaces the upscaler rather than sitting beside it.")
        elif api == "Vulkan":
            s.options = [BRIDGE]
            s.recommended = BRIDGE
            s.reason = ("Vulkan game with its own DLSS: the bridge mirrors its "
                        "DLSS contract onto a private D3D12 session. This is "
                        "the only way Vulkan works.")
        elif api == "OpenGL":
            # The add-on and the bridge both hook NGX's D3D11/D3D12/Vulkan
            # entry points. An OpenGL game reaches none of them, so shipping
            # its own DLSS does not help - the feeder is the only route.
            s.options = [FEEDER]
            s.recommended = FEEDER
            s.reason = ("This game ships DLSS files, but it renders with "
                        "OpenGL and the add-on only hooks D3D11/D3D12/Vulkan. "
                        "Only the feeder path can reach it.")
        else:                              # DX12 or unknown -> assume DXGI/D3D12
            # OptiScaler needs exactly this case: D3D12 plus the game's own
            # DLSS, whose depth and motion vectors it reads directly.
            s.options = [NATIVE, OPTI, BRIDGE, FEEDER]
            s.recommended = NATIVE
            s.reason = ("This game ships its own DLSS and renders with D3D12, "
                        "so the add-on can hook it directly. No synthetic "
                        "contract, and your in-game DLSS quality setting "
                        "(Quality / Balanced / Performance) still applies.")
        return s

    # No DLSS of its own
    if api == "Vulkan":
        s.options = [BRIDGE]
        s.recommended = BRIDGE
        s.reason = ("Vulkan without native DLSS: only the bridge's synthetic "
                    "contract can work here, and it needs synth_after set.")
    elif api == "OpenGL":
        s.options = [FEEDER]
        s.recommended = FEEDER
        s.reason = "OpenGL is only reachable through the feeder path."
    else:
        s.options = [FEEDER, BRIDGE]
        s.recommended = FEEDER
        s.reason = ("No DLSS in this game. The feeder builds a DLAA contract "
                    "from ReShade's depth and shader motion vectors. The "
                    "bridge can instead build one from the driver's optical "
                    "flow engine - fewer moving parts, but newer and less "
                    "proven.")
    return s


LABELS = {
    NATIVE: "native - hook the game's own DLSS",
    OPTI: "optiscaler - replace the upscaler, no reshade",
    BRIDGE: "bridge - private D3D12 session",
    FEEDER: "feeder - synthetic DLAA contract",
}

BLURB = {
    NATIVE: ("Simplest and best quality: no synthetic contract, no motion "
             "vector shaders, and the game's own DLSS quality mode applies."),
    OPTI: ("No ReShade at all. OptiScaler takes over upscaling and reads the "
           "game's own DLSS depth and motion vectors, so it is measurably "
           "cheaper than the feeder - and it really upscales, so a lower "
           "render resolution costs less to draw. Author states RTX 50 and a "
           "D3D12 game with DLSS."),
    BRIDGE: ("Reproduces the DLSS contract on a private D3D12 session. The "
             "only route for Vulkan, and the right one for D3D11 games with "
             "DLSS."),
    FEEDER: ("Builds a DLAA contract from ReShade's depth buffer and "
             "shader-estimated motion vectors. Always DLAA, never upscaling."),
}
