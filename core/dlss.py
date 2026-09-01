"""Does this game ship its own DLSS, and which install path suits it?

This decides between five ways of getting DLSS 5 neural rendering into a
game. Getting it right matters: the feeder path is always DLAA and needs
motion-vector shaders, whereas a game with its own DLSS can be hooked
directly and keeps its own Quality/Balanced/Performance modes.

    RENODX   ShortFuse's renodx-dlss add-on (the "SF" build). Hooks D3D9,
             D3D11 and D3D12 presentation in-process through a same-adapter
             D3D12 endpoint - no bridge, no synthetic contract, no shaders.
             64-bit only. The add-on the feeder and bridge authors now point
             to for those three APIs.

    NATIVE   the game has DLSS and renders with D3D12.
             Krish's renodx-dlss5 add-on detours the game's own NGX D3D12
             calls. Nothing synthetic, and the game's own quality mode applies.
             The most proven route for D3D12 games that ship DLSS.

    OPTI     Dagherbou's OptiScaler fork. Replaces the upscaler and runs the
             model over its output, with a model-resolution dial (25-100%)
             that is the biggest fps lever there is. RTX 50 only, and the
             game must already use DLSS.

    BRIDGE   dlss5-bridge: reproduces the DLSS contract on a private D3D12
             session. The route for Vulkan games with DLSS (mirror), and a
             fallback for D3D11. Its author has ended development at 1.3.0.

    FEEDER   DLSS5-Feeder builds a synthetic DLAA contract out of ReShade's
             depth buffer and shader-estimated motion vectors. Works without
             any DLSS in the game, on D3D11/D3D12/Vulkan/OpenGL, and is the
             only way in for 32-bit games (through its host64 helper).

DirectX 10 is supported by none of them: the feeder dropped it and nothing
else hooks D3D10.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

NATIVE, BRIDGE, FEEDER, OPTI, RENODX = "native", "bridge", "feeder", "optiscaler", "renodx"
ALL_ROUTES = (RENODX, NATIVE, OPTI, BRIDGE, FEEDER)

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
    supported: bool = True                # False: no component reaches this API
    why_not: str = ""

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


def detect(install_dir: Path, folder: Path, api: str, bitness: int,
           sm: int | None = None) -> Support:
    """Work out what the game supports and which path to recommend.

    `sm` is the card's CUDA architecture when known (gpu.detect). It changes
    one recommendation: on RTX 50 a D3D12 game with DLSS is steered to
    OptiScaler, whose model-resolution dial is the biggest fps lever there is
    and whose model only runs on that generation.
    """
    s = _detect(install_dir, folder, api, bitness)
    if sm is not None and sm >= 120 and OPTI in s.options and s.recommended == NATIVE:
        s.recommended = OPTI
        s.reason = ("This game ships its own DLSS and renders with D3D12, and "
                    "you have an RTX 50: OptiScaler runs the model with a "
                    "model-resolution dial - 75% costs about half of full "
                    "size, and the frame itself stays full detail. The native "
                    "route is the simpler, most proven alternative.")
    return s


def fit(route: str, api: str, native_dlss: bool, sm: int | None) -> tuple[bool, str]:
    """(usable on this machine, short note) for a route the game offers.

    The route list says what the GAME allows; this says what the CARD and
    the route's own rules add to it, so the dropdown can label each entry.
    """
    if route == OPTI:
        if sm is not None and sm < 120:
            return False, "needs an RTX 50 - the model refuses older cards"
        if not native_dlss:
            return False, "the game must already use DLSS"
        if api == "DX11":
            return True, "on D3D11 the upscaler becomes FSR on D3D12"
        return True, "model resolution dial: the fps lever"
    if route == NATIVE:
        return True, "most proven for D3D12 games with DLSS"
    if route == RENODX:
        return True, "newest add-on; in-process, no bridge"
    if route == BRIDGE:
        if api == "Vulkan":
            return True, "mirrors the game's DLSS onto D3D12"
        return True, "author has stopped development"
    if route == FEEDER:
        if api in ("Vulkan", "OpenGL") or not native_dlss:
            return True, "always DLAA, shader motion vectors"
        return True, "always DLAA; ignores the game's DLSS"
    return True, ""


def _detect(install_dir: Path, folder: Path, api: str, bitness: int) -> Support:
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
    if api == "DX10":
        # DXGI-based, so ReShade attaches, but no DLSS 5 component reaches
        # D3D10: the feeder lists it as unsupported, the add-ons hook
        # D3D9/D3D11/D3D12 and the bridge D3D11/Vulkan. Saying so beats
        # installing something that can never work.
        s.options = [FEEDER]
        s.recommended = FEEDER
        s.supported = False
        s.why_not = ("DirectX 10 is not supported by any DLSS 5 component: "
                     "the feeder dropped it, and nothing else hooks D3D10.")
        s.reason = s.why_not
        return s

    if bitness != 64:
        # Every add-on that hooks the game directly is 64-bit only, and a
        # 32-bit process cannot load one. The feeder's host64 helper is the
        # only way in - DX9 games get dgVoodoo2 in front of it.
        s.options = [FEEDER]
        s.recommended = FEEDER
        s.reason = ("32-bit games can only use the feeder path: every other "
                    "add-on is 64-bit and a 32-bit process cannot load it, so "
                    "the feeder's host64 helper process is the only way in."
                    + (" DirectX 9 is translated to D3D11 by dgVoodoo2 first."
                       if api == "DX9" else ""))
        return s

    if api == "DX9":
        # 64-bit D3D9 is rare, and only ShortFuse's add-on reaches it: it
        # switches device creation to D3D9Ex and evaluates the presentation
        # backbuffer. The feeder's D3D9 support is the 32-bit dgVoodoo path.
        s.options = [RENODX]
        s.recommended = RENODX
        s.reason = ("64-bit DirectX 9: only the renodx-dlss add-on hooks this "
                    "directly (D3D9Ex, presentation backbuffer). No motion "
                    "vectors on this path, so expect the result to be softer.")
        return s

    if api == "Vulkan":
        # A Vulkan game never loads dxgi.dll; ReShade goes in as a layer, and
        # from there either the bridge mirrors the game's own DLSS contract or
        # the feeder builds one with its own interop.
        if s.native_dlss:
            s.options = [BRIDGE, FEEDER]
            s.recommended = BRIDGE
            s.reason = ("Vulkan game with its own DLSS: the bridge mirrors its "
                        "DLSS contract onto a private D3D12 session, so the "
                        "game's quality mode still applies. The feeder is the "
                        "fallback - always DLAA, with shader motion vectors.")
        else:
            s.options = [FEEDER, BRIDGE]
            s.recommended = FEEDER
            s.reason = ("Vulkan without DLSS: the feeder builds a DLAA contract "
                        "through its own Vulkan interop (no launcher needed "
                        "since 0.5.2). The bridge can instead synthesise one "
                        "from the driver's optical flow - fewer parts, less "
                        "proven.")
        return s

    if api == "OpenGL":
        # Nothing but the feeder reaches OpenGL. It imports a D3D12 device's
        # resources into GL through the memory-object extensions.
        s.options = [FEEDER]
        s.recommended = FEEDER
        s.reason = ("OpenGL is only reachable through the feeder path, which "
                    "shares a D3D12 device's textures into GL. The game must "
                    "actually render on the NVIDIA card.")
        return s

    # 64-bit D3D11 / D3D12 / unknown-but-DXGI from here on.
    if s.native_dlss:
        if api == "DX11":
            s.options = [RENODX, BRIDGE, OPTI, FEEDER]
            s.recommended = RENODX
            s.reason = ("This game has its own DLSS but renders with D3D11. "
                        "The renodx-dlss add-on hooks D3D11 in-process and "
                        "shares the game's real depth and motion vectors "
                        "across to D3D12 - the route its author and the "
                        "bridge's now recommend. The bridge does the same in a "
                        "private session; OptiScaler works too but needs a "
                        "bridged upscaler on D3D11.")
        else:                              # DX12 or unknown -> assume DXGI/D3D12
            s.options = [NATIVE, RENODX, OPTI, BRIDGE, FEEDER]
            s.recommended = NATIVE
            s.reason = ("This game ships its own DLSS and renders with D3D12, "
                        "so the DLSS 5 add-on hooks it directly. No synthetic "
                        "contract, and your in-game DLSS quality setting "
                        "(Quality / Balanced / Performance) still applies. "
                        "OptiScaler adds a model-resolution dial for more fps "
                        "on RTX 50.")
        return s

    # No DLSS of its own, D3D11/D3D12.
    s.options = [FEEDER, RENODX, BRIDGE]
    s.recommended = FEEDER
    s.reason = ("No DLSS in this game. The feeder builds a DLAA contract from "
                "ReShade's depth and shader motion vectors - the most proven "
                "way. The renodx-dlss add-on can instead evaluate the finished "
                "frame in-process with no shaders at all (simpler, no motion "
                "vectors), and the bridge can build a contract from the "
                "driver's optical flow.")
    return s


LABELS = {
    RENODX: "renodx-dlss - hooks the game in-process (D3D9/11/12)",
    NATIVE: "native - hook the game's own DLSS",
    OPTI: "optiscaler - replace the upscaler, model resolution dial",
    BRIDGE: "bridge - private D3D12 session",
    FEEDER: "feeder - synthetic DLAA contract",
}

BLURB = {
    RENODX: ("ShortFuse's renodx-dlss add-on. Hooks D3D9, D3D11 and D3D12 "
             "presentation in-process - no bridge, no shaders, no synthetic "
             "contract. On D3D11 it shares the game's real depth and motion "
             "vectors; without DLSS it evaluates the finished frame. New "
             "(September 2026) and the build the other authors point to."),
    NATIVE: ("Simplest and best quality: no synthetic contract, no motion "
             "vector shaders, and the game's own DLSS quality mode applies."),
    OPTI: ("No ReShade at all. OptiScaler takes over upscaling and runs the "
           "model over its output. Its model-resolution dial is the biggest "
           "fps lever there is: cost falls with the square of it, and the "
           "frame itself stays full detail. RTX 50 only - the author's FP8 "
           "model refuses older cards - and the game must already use DLSS."),
    BRIDGE: ("Reproduces the DLSS contract on a private D3D12 session. The "
             "route for Vulkan games with DLSS. Its author has stopped "
             "development at 1.3.0."),
    FEEDER: ("Builds a DLAA contract from ReShade's depth buffer and "
             "shader-estimated motion vectors. Always DLAA, never upscaling. "
             "The only route for 32-bit and OpenGL games."),
}
