"""Does this game ship its own DLSS, and which install path suits it?

This decides between seven ways of getting DLSS 5 neural rendering into a
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

    UPSTREAM matiasLombo's neural-upstream add-on. Hooks the same NGX
             EvaluateFeature call but runs the network ITSELF, on the
             render-resolution colour buffer, and hands the result to the
             game's own DLSS as its input. Same-resolution network on a
             smaller image: proportionally cheaper, and the game's quality
             mode still applies. No renodx add-on beside it - two NGX hooks
             fight. Days old, tested on two games by its author.

    OPTI     Dagherbou's OptiScaler fork. Replaces the upscaler and runs the
             model over its output, with a model-resolution dial (25-100%)
             that is the biggest fps lever there is. The author tested RTX 50
             only; with the per-card runtime this tool installs it runs on
             RTX 20/30/40 as well. The game must already use DLSS - or FSR
             2/3 or XeSS: OptiScaler hooks those calls as its INPUT and
             runs DLSS in their place, which is what it is known for.

    BRIDGE   dlss5-bridge: reproduces the DLSS contract on a private D3D12
             session. The route for Vulkan games with DLSS (mirror), and a
             fallback for D3D11. Its author has ended development at 1.3.0.

    FEEDER   DLSS5-Feeder builds a synthetic DLAA contract out of ReShade's
             depth buffer and shader-estimated motion vectors. Works without
             any DLSS in the game, on D3D11/D3D12/Vulkan/OpenGL, and is the
             only way in for 32-bit games (through its host64 helper).

    STANDALONE kibblerz's standalone-dlssnr add-on (DLSS5-Reshade-AIO). Its
             own feed and its own private NGX session - no feeder, no renodx
             add-on. Copies the back buffer at Present, runs the network,
             then DLAA at native resolution or DLSS Super Resolution when the
             game renders below it, frame generation on top, and shows the
             result in a topmost window of its own. Works with or without
             DLSS in the game. Experimental: the window trick is fragile.

DirectX 10 is supported by none of them: the feeder dropped it and nothing
else hooks D3D10.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

NATIVE, BRIDGE, FEEDER, OPTI, RENODX = "native", "bridge", "feeder", "optiscaler", "renodx"
UPSTREAM = "upstream"
STANDALONE = "standalone"
ALL_ROUTES = (RENODX, NATIVE, UPSTREAM, OPTI, BRIDGE, FEEDER, STANDALONE)

# Streamline is NVIDIA's own plugin layer; if a game ships it, it ships DLSS.
# These are never files this tool installs, so they are unambiguous.
STREAMLINE = ("sl.interposer.dll", "sl.dlss.dll", "sl.common.dll",
              "sl.dlss_g.dll", "sl.reflex.dll")
# A game's own runtime, or one renamed by a user to disable it.
OWN_RUNTIME = ("nvngx_dlss.dlsss", "nvngx_dlss.dll.bak", "_nvngx.dll")

# Games do not keep DLSS next to the executable. Unreal ships it under
# Engine/Plugins/Runtime/Nvidia/DLSS/Binaries/ThirdParty/Win64, CryEngine
# (Kingdom Come 2) under Bin/Win64Shared, others under their own names. A
# bounded walk finds them; the folders below never hold DLLs and can be
# hundreds of thousands of files, so they are skipped outright.
DLSS_FILES = ("nvngx_dlss.dll", "sl.interposer.dll", "sl.dlss.dll",
              "nvngx_dlssg.dll", "nvngx_dlssd.dll")
_SKIP_DIRS = {"content", "paks", "saved", "logs", "movies", "sounds", "music",
              "videos", "localization", "shadercache", "derivedcache", "cache",
              "textures", "maps", "levels", "audio", "data", "assets", "mods",
              "screenshots", "steamapps", "redist", "_commonredist", "host64",
              "reshade-shaders"}
_WALK_DEPTH = 9

# A game without DLSS may still ship AMD FSR 2/3 or Intel XeSS. OptiScaler
# hooks those calls as its input ([Inputs] EnableFsr2Inputs / EnableFsr3Inputs
# / EnableFfxInputs / EnableXeSSInputs in its ini) and runs DLSS in their
# place, so such a game can take the OPTI route with no DLSS of its own.
# These are the runtime DLLs the SDKs ship under; a game that statically
# links FSR shows none of them and cannot be told apart from one without.
FSR_FILES = ("ffx_fsr2_api_x64.dll", "ffx_fsr2_api_dx12_x64.dll",
             "ffx_fsr2_api_vk_x64.dll", "ffx_fsr3upscaler_x64.dll",
             "ffx_fsr3_x64.dll", "amd_fidelityfx_dx12.dll",
             "amd_fidelityfx_vk.dll", "ffx_backend_dx12_x64.dll")
XESS_FILES = ("libxess.dll", "libxess_dx11.dll", "libxess_fg.dll")
UPSCALER_FILES = FSR_FILES + XESS_FILES
UPSCALER_NAMES = {"fsr": "AMD FSR 2/3", "xess": "Intel XeSS"}
# OptiScaler's own package carries libxess.dll and amd_fidelityfx_*.dll under
# OptiScaler/ - its bundled upscalers, not the game's. Never evidence.
_UPSCALER_SKIP = ("optiscaler", "licenses")


def find_dlss_files(folder: Path, skip_dir: Path | None = None,
                    names: tuple[str, ...] = DLSS_FILES,
                    extra_skip: tuple[str, ...] = ()) -> list[str]:
    """Relative paths of the game's own DLSS files anywhere under `folder`.

    `skip_dir` is the folder this tool installs into: a runtime we put there
    must not count as the game's own (that is what _ours handles there).
    `names` lets the same bounded walk look for other runtimes (FSR, XeSS).
    """
    import os
    out: list[str] = []
    try:
        base_depth = len(Path(folder).resolve().parts)
    except OSError:
        return out
    for root, dirs, files in os.walk(folder):
        rp = Path(root)
        depth = len(rp.resolve().parts) - base_depth if rp.exists() else 0
        if depth >= _WALK_DEPTH:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS
                       and d.lower() not in extra_skip
                       and not d.startswith(".")]
        if skip_dir is not None:
            try:
                if rp.resolve() == Path(skip_dir).resolve():
                    continue
            except OSError:
                pass
        for f in files:
            if f.lower() in names:
                try:
                    out.append(str((rp / f).relative_to(folder)))
                except ValueError:
                    out.append(str(rp / f))
        if len(out) >= 6:
            break
    return out


def find_upscaler_files(folder: Path, skip_dir: Path | None = None) -> list[str]:
    """Relative paths of the game's own FSR 2/3 and XeSS runtimes."""
    return find_dlss_files(folder, skip_dir, names=UPSCALER_FILES,
                           extra_skip=_UPSCALER_SKIP)


def upscaler_kind(evidence: list[str]) -> str:
    """"fsr", "xess" or "" for a list of runtime files seen.

    A game shipping both gets "fsr": OptiScaler's FSR 2/3 input hooks are its
    oldest and most exercised, and the ini enables both anyway.
    """
    names = {Path(e).name.lower() for e in evidence}
    if names & set(FSR_FILES):
        return "fsr"
    if names & set(XESS_FILES):
        return "xess"
    return ""


@dataclass
class Support:
    native_dlss: bool = False
    evidence: list[str] = None            # type: ignore[assignment]
    recommended: str = FEEDER
    reason: str = ""
    options: list[str] = None             # type: ignore[assignment]
    supported: bool = True                # False: no component reaches this API
    why_not: str = ""
    # "fsr" / "xess" / "": the upscaler the game ships when it has no DLSS.
    # OptiScaler can take those calls as its input and run DLSS instead; the
    # GUI passes this into fit() and Options.upscaler (wired in a follow-up).
    upscaler: str = ""
    upscaler_evidence: list[str] = None   # type: ignore[assignment]
    # Where DLSS comes from on the OPTI route, for the reason texts.
    dlss_source: str = ""

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []
        if self.options is None:
            self.options = []
        if self.upscaler_evidence is None:
            self.upscaler_evidence = []


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

    `sm` is the card's CUDA architecture when known (gpu.detect). On any RTX
    card a D3D12 game with DLSS is steered to OptiScaler, whose
    model-resolution dial is the biggest fps lever there is - and the lever
    matters most on the cards where the pass is heaviest. A card below RTX
    20 gets no such steer; nothing runs there anyway. A D3D12 game with FSR
    or XeSS instead of DLSS is steered only on RTX 50, the cards the author
    tested - one more hook has to land there.
    """
    s = _detect(install_dir, folder, api, bitness)
    if OPTI in s.options and s.recommended == NATIVE and (sm is None or sm >= 75):
        s.recommended = OPTI
        s.reason = ("This game ships its own DLSS and renders with D3D12: "
                    "OptiScaler runs the model with a model-resolution dial - "
                    "75% costs about half of full size, and the frame itself "
                    "stays full detail. The native route is the simpler, most "
                    "proven alternative."
                    + ("" if (sm or 120) >= 120 else
                       " The author tested RTX 50 only; on your card it runs "
                       "on the community runtime this tool installs."))
    elif (OPTI in s.options and s.upscaler and not s.native_dlss
          and api == "DX12" and sm is not None and sm >= 120):
        # An FSR/XeSS game is a step further from what the author tested
        # (OptiScaler must hook the game's upscaler first), so only the cards
        # they actually used are steered off the feeder; the rest keep the
        # proven route and see OptiScaler as an option.
        s.recommended = OPTI
        s.reason = (f"This game has no DLSS but ships {UPSCALER_NAMES[s.upscaler]} "
                    f"({', '.join(s.upscaler_evidence[:3])}). OptiScaler takes "
                    f"those calls as its input, runs DLSS in their place and "
                    f"then neural rendering, with the model-resolution dial. "
                    f"Works in many games, not all - the feeder is the proven "
                    f"fallback.")
    return s


def fit(route: str, api: str, native_dlss: bool, sm: int | None,
        upscaler: str = "") -> tuple[bool, str]:
    """(usable on this machine, short note) for a route the game offers.

    The route list says what the GAME allows; this says what the CARD and
    the route's own rules add to it, so the dropdown can label each entry.

    `upscaler` is Support.upscaler: with no DLSS in the game, OptiScaler is
    usable only when there is an FSR/XeSS call for it to redirect. The GUI
    still calls this positionally without it (a follow-up wires it in), so
    the default must keep the old answers for every other route.
    """
    if route == OPTI:
        if not native_dlss and not upscaler:
            return False, "the game must already use DLSS, FSR 2/3 or XeSS"
        note = "model resolution dial: the fps lever"
        if not native_dlss:
            note = (f"the game's {'FSR' if upscaler == 'fsr' else 'XeSS'} calls "
                    f"are redirected into DLSS, then neural rendering; " + note)
        if api == "DX11":
            note = "on D3D11 the upscaler becomes FSR on D3D12"
        if sm is not None and sm < 120:
            note += "; author tested RTX 50 only, works here on the community runtime"
        return True, note
    if route == NATIVE:
        return True, "most proven for D3D12 games with DLSS"
    if route == UPSTREAM:
        if not native_dlss:
            return False, "the game must already use DLSS"
        return True, ("runs the network before the game's DLSS, at render "
                      "resolution - cheaper; days old, two games tested")
    if route == STANDALONE:
        return True, ("own feed: DLAA at native resolution, DLSS SR below it, "
                      "frame generation; experimental - presents through a "
                      "window of its own")
    if route == RENODX:
        return True, "new and unproven - reported not working in many games; try the others first"
    if route == BRIDGE:
        if api == "Vulkan":
            return True, "mirrors the game's DLSS onto D3D12"
        return True, "mirrors the game's DLSS onto D3D12 - maintained, every release tested on D3D11 and Vulkan"
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
    if not s.native_dlss:
        # Nothing beside the executable: look where engines actually keep it.
        # Only the install folder is excluded - anything we wrote lives there.
        for rel in find_dlss_files(folder, skip_dir=install_dir):
            s.native_dlss = True
            s.evidence.append(rel)
    s.evidence = sorted(set(s.evidence))

    if not s.native_dlss:
        # No DLSS: does the game ship FSR 2/3 or XeSS for OptiScaler to hook?
        # Same two-stage look as DLSS - beside the executable first (the
        # walk skips the install folder, and _ours rules out anything we
        # wrote), then the engine's own folders.
        for d in {install_dir, folder}:
            for m in UPSCALER_FILES:
                if (d / m).is_file() and not _ours(d, m):
                    s.upscaler_evidence.append(m)
        if not s.upscaler_evidence:
            s.upscaler_evidence = find_upscaler_files(folder, skip_dir=install_dir)
        s.upscaler_evidence = sorted(set(s.upscaler_evidence))
        s.upscaler = upscaler_kind(s.upscaler_evidence)
    if s.native_dlss:
        s.dlss_source = "the game's own DLSS"
    elif s.upscaler:
        s.dlss_source = (f"the game's {'FSR 2/3' if s.upscaler == 'fsr' else 'XeSS'} "
                         f"calls, redirected into DLSS by OptiScaler")

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
            s.options = [BRIDGE, OPTI, FEEDER, STANDALONE, RENODX]
            s.recommended = BRIDGE
            s.reason = ("This game has its own DLSS but renders with D3D11, "
                        "which the add-on cannot hook directly. The bridge "
                        "reproduces the contract on a private D3D12 session "
                        "and the game's own quality mode still applies. "
                        "OptiScaler works too but replaces DLSS with FSR on "
                        "D3D11. The renodx-dlss add-on is new and has not "
                        "proven itself in the field yet.")
        else:                              # DX12 or unknown -> assume DXGI/D3D12
            s.options = [NATIVE, UPSTREAM, OPTI, BRIDGE, FEEDER, STANDALONE,
                         RENODX]
            s.recommended = NATIVE
            s.reason = ("This game ships its own DLSS and renders with D3D12, "
                        "so the DLSS 5 add-on hooks it directly. No synthetic "
                        "contract, and your in-game DLSS quality setting "
                        "(Quality / Balanced / Performance) still applies. "
                        "OptiScaler adds a model-resolution dial for more fps; "
                        "neural-upstream runs the network before the game's "
                        "DLSS instead, at render resolution, for much less.")
        return s

    # No DLSS of its own, D3D11/D3D12.
    if s.upscaler:
        # OptiScaler's founding trick: hook the game's FSR 2/3 or XeSS calls
        # and run DLSS in their place - then the fork's neural rendering on
        # top. The feeder stays the proven recommendation (detect() steers
        # RTX 50 to OptiScaler); the bridge and renodx-dlss are unchanged.
        # Vulkan is not offered: this tool installs OptiScaler as a DXGI
        # proxy, which a Vulkan game never loads.
        s.options = [FEEDER, OPTI, BRIDGE, STANDALONE, RENODX]
        s.recommended = FEEDER
        s.reason = (f"No DLSS in this game, but it ships "
                    f"{UPSCALER_NAMES[s.upscaler]} "
                    f"({', '.join(s.upscaler_evidence[:3])}). The feeder "
                    f"builds a DLAA contract from ReShade's depth and shader "
                    f"motion vectors - the most proven way. OptiScaler can "
                    f"instead take the game's "
                    f"{'FSR' if s.upscaler == 'fsr' else 'XeSS'} calls as its "
                    f"input and run DLSS in their place, then neural "
                    f"rendering - real upscaling, works in many games, not "
                    f"all. The bridge and the renodx-dlss add-on are the "
                    f"less proven alternatives.")
        return s
    s.options = [FEEDER, BRIDGE, STANDALONE, RENODX]
    s.recommended = FEEDER
    s.reason = ("No DLSS in this game. The feeder builds a DLAA contract from "
                "ReShade's depth and shader motion vectors - the most proven "
                "way. The bridge can instead build one from the driver's "
                "optical flow. standalone-dlssnr brings its own feed with "
                "real DLSS upscaling and frame generation, but presents "
                "through a window of its own - experimental. The renodx-dlss "
                "add-on is the least proven of the four.")
    return s


LABELS = {
    RENODX: "renodx-dlss - new in-process add-on (D3D9/11/12), unproven",
    NATIVE: "native - hook the game's own DLSS",
    UPSTREAM: "neural-upstream - the network before the upscaler, cheaper",
    OPTI: "optiscaler - replace the upscaler, model resolution dial",
    BRIDGE: "bridge - private D3D12 session",
    FEEDER: "feeder - synthetic DLAA contract",
    STANDALONE: "standalone-dlssnr - own feed, DLAA or DLSS SR, frame generation",
}

BLURB = {
    RENODX: ("ShortFuse's renodx-dlss add-on. Hooks D3D9, D3D11 and D3D12 "
             "presentation in-process - no bridge, no shaders. Days old, and "
             "reported not working in many games so far: try the recommended "
             "route first and come here only if that fails."),
    NATIVE: ("Simplest and best quality: no synthetic contract, no motion "
             "vector shaders, and the game's own DLSS quality mode applies."),
    UPSTREAM: ("matiasLombo's neural-upstream add-on runs the network at "
               "render resolution, BEFORE the game's own DLSS upscales - "
               "the same enhancement on a smaller image, so it costs much "
               "less, and your DLSS quality mode still applies. It does "
               "the neural rendering itself, so no renodx add-on goes in "
               "beside it. Configured from its tab in the ReShade overlay. "
               "Days old; its author tested GTA V Enhanced and Bright "
               "Memory Infinite."),
    OPTI: ("No ReShade at all. OptiScaler takes over upscaling and runs the "
           "model over its output. Its model-resolution dial is the biggest "
           "fps lever there is: cost falls with the square of it, and the "
           "frame itself stays full detail. The author tested RTX 50 only; "
           "on RTX 20/30/40 it runs on the community runtime this tool "
           "installs. The game must already use DLSS - or FSR 2/3 or XeSS, "
           "whose calls OptiScaler redirects into DLSS."),
    BRIDGE: ("Reproduces the DLSS contract on a private D3D12 session. The "
             "route for Vulkan games with DLSS. Its author has stopped "
             "development at 1.3.0."),
    FEEDER: ("Builds a DLAA contract from ReShade's depth buffer and "
             "shader-estimated motion vectors. Always DLAA, never upscaling. "
             "The only route for 32-bit and OpenGL games."),
    STANDALONE: ("kibblerz's standalone-dlssnr add-on does the whole pipeline "
                 "itself: its own feed (VORT motion vectors, ReShade depth), "
                 "the network, then DLAA when the game runs at the monitor's "
                 "resolution or real DLSS Super Resolution when it runs "
                 "below it, and frame generation on top - no feeder, no "
                 "renodx add-on, and the game needs no DLSS of its own. The "
                 "result is shown through a topmost window of its own, which "
                 "is the fragile part: change resolution or display mode and "
                 "it needs a restart. F10 compares. Experimental."),
}

# What must NOT sit in the folder or run beside each route, and what breaks
# when it does. Short and honest: these are the conflicts people actually
# hit, not a legal disclaimer. ReShade loads every .addon64 it finds, and
# two things hooking the same NGX calls means flicker or nothing at all.
CONFLICTS: dict[str, tuple[str, ...]] = {
    NATIVE: ("not with OptiScaler or a frame-gen unlocker in the same folder "
             "- two NGX hooks: flicker, greyed-out frame-gen or nothing",
             "NVIDIA Smooth Motion off for this game"),
    UPSTREAM: ("not with the renodx-dlss5 add-on, OptiScaler or another NGX "
               "hook in the folder - installing removes ours, name theirs",
               "with DLSS Frame Generation set its cadence to Quality in the "
               "overlay, or expect stutter",
               "does not upscale - the game's own DLSS still does"),
    OPTI: ("no ReShade at all on this route; other RenoDX add-ons will not load",
           "the game must already use DLSS, FSR 2/3 or XeSS",
           "not with a frame-gen unlocker or dlss-enabler in the folder"),
    BRIDGE: ("not with the feeder or renodx-dlss add-on in the same folder "
             "- both build a contract and the game dies before its swap chain",
             "NVIDIA Smooth Motion off for this game",
             "an older dlss5-dx11-bridge.addon64 is removed - the two conflict"),
    FEEDER: ("always DLAA; the game's own DLSS is ignored",
             "NVIDIA Smooth Motion off",
             "not with the bridge or renodx-dlss add-on in the same folder",
             "exactly one neural add-on: renodx-dlss5, Deep Fried Chicken or Alex's Toolkit - with two of them loaded the second one does nothing, silently"),
    RENODX: ("not with the renodx-dlss5 add-on, the feeder or the bridge in "
             "the folder - both hook NGX",
             "reported not working in many games; nothing to tune if it does "
             "nothing, switch route"),
    STANDALONE: ("the game's own DLSS, frame generation and anti-aliasing "
                 "must be OFF - it brings its own",
                 "presents through its own topmost window; resolution or "
                 "display-mode changes need a restart",
                 "not with the renodx add-on, OptiScaler or neural-upstream "
                 "in the folder"),
}
