"""Does this game ship its own DLSS, and which install path suits it?

This decides between eight ways of getting DLSS 5 neural rendering into a
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
             fight. Tested on two games by its author.

    OPTI     Dagherbou's OptiScaler fork. Replaces the upscaler and runs the
             model over its output, with a model-resolution dial (25-100%)
             that is the biggest fps lever there is. The author tested RTX 50
             only; with the per-card runtime this tool installs it runs on
             RTX 20/30/40 as well. The game must already use DLSS - or FSR
             2/3 or XeSS: OptiScaler hooks those calls as its INPUT and
             runs DLSS in their place, which is what it is known for.

    BRIDGE   dlss5-bridge: reproduces the DLSS contract on a private D3D12
             session. The route for Vulkan games with DLSS (mirror), and a
             fallback for D3D11.

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

    REMIX    the game already has an RTX Remix mod. Remix replaces d3d9.dll
             and path traces the frame in its own runtime (a `.trex` folder),
             and the community forks of that runtime run DLSS-NR inside it,
             after DLSS. No ReShade, no feeder, no add-on - a ReShade proxy
             DLL in the folder crashes a Remix game before it draws.

DirectX 10 is reached by the feeder alone (0.13.1 and later), through a
private D3D11 relay device; nothing else hooks D3D10.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from . import log, remix

NATIVE, BRIDGE, FEEDER, OPTI, RENODX = "native", "bridge", "feeder", "optiscaler", "renodx"
UPSTREAM = "upstream"
STANDALONE = "standalone"
REMIX = "remix"
ALL_ROUTES = (RENODX, NATIVE, UPSTREAM, OPTI, BRIDGE, FEEDER, STANDALONE,
              REMIX)

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
# ...but a content folder is skipped for its size, not because a runtime
# cannot be under it: NBA 2K27 keeps Streamline in `data\streamline` (#119),
# and "data" is as generic a folder name as there is. So a skipped folder is
# still entered when one of its own children is named like a place runtimes
# live. One scandir per skipped folder, against a walk that would otherwise
# miss the game's DLSS entirely.
_RUNTIME_HOMES = {"streamline", "dlss", "nvidia", "ngx", "nvngx",
                  "binaries", "plugins", "win64", "x64", "bin"}
_WALK_DEPTH = 9
_WALK_DIRS = 6000
_WALK_SECONDS = 6.0

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


# folder -> what the bounded walk found, for this run of the program. The
# walk is the expensive part of detection and the game's own runtimes do
# not move while the tool is open, so asking twice is waste. Cleared by
# forget_walk() after an install writes into the folder.
_WALK_CACHE: dict[tuple[str, str], list[str]] = {}


def forget_walk(folder=None) -> None:
    """Drop the remembered walk - for one folder, or all of them."""
    if folder is None:
        _WALK_CACHE.clear()
        return
    key = _walk_key(folder)
    for k in [k for k in _WALK_CACHE if k[0] == key]:
        _WALK_CACHE.pop(k, None)


def _walk_key(p) -> str:
    """One spelling of a folder, so forget_walk() clears what walked() set.

    find_dlss_files resolves the path itself, so a trailing separator, an
    8.3 name or a junction would otherwise make two entries for one folder
    and leave a stale answer behind after an install.
    """
    try:
        return str(Path(p).resolve()).lower()
    except OSError:
        return str(p).lower()


def walked(folder: Path, skip_dir: Path | None = None) -> list[str]:
    """find_dlss_files, remembered for as long as the program runs."""
    key = (_walk_key(folder), _walk_key(skip_dir) if skip_dir else "")
    hit = _WALK_CACHE.get(key)
    if hit is None:
        hit = find_dlss_files(folder, skip_dir=skip_dir)
        try:
            Path(folder).resolve()
            reachable = True
        except OSError:
            # The walk bails out and returns nothing when the folder cannot
            # be resolved - an offline network drive, a sharing violation.
            # Nothing is not an answer worth keeping for the session.
            reachable = False
        if reachable:
            _WALK_CACHE[key] = hit
    return list(hit)          # never the cached list itself


def _holds_runtime(d: Path) -> bool:
    r"""Does this folder have a child named like a place runtimes live?

    The skip list exists so a walk does not crawl a game's whole asset
    tree. It is a size heuristic, and a game that keeps its runtimes under
    one of those names (`data\streamline`, #119) was being read as a game
    with no DLSS at all. Looking one level in costs a single scandir.
    """
    import os as _os
    try:
        with _os.scandir(d) as it:
            for e in it:
                if e.is_dir() and e.name.lower() in _RUNTIME_HOMES:
                    return True
    except OSError:
        pass
    return False


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
        # Resolve the roots once. Resolving every directory (and skip_dir
        # again for each one) made large engine trees take minutes on Windows.
        folder = Path(folder).resolve()
        skip_dir = Path(skip_dir).resolve() if skip_dir is not None else None
    except OSError:
        return out
    base_depth = len(folder.parts)
    deadline = time.monotonic() + _WALK_SECONDS
    # Skipped folders that were entered only because a runtime-named child
    # is in them: inside one, only those children are followed. Walking all
    # of a big `data` for the sake of its `streamline` cost the budget the
    # rest of the game needed.
    narrow: set[str] = set()
    for seen, (root, dirs, files) in enumerate(os.walk(folder), 1):
        # A depth limit and a match limit do not bound trees with thousands
        # of directories and no runtime DLLs. Check even in the skipped root.
        if seen > _WALK_DIRS or time.monotonic() >= deadline:
            log.write(f"stopped looking for runtime DLLs under {folder} "
                      f"after {seen - 1} folders - search budget reached", "warn")
            break
        rp = Path(root)
        depth = len(rp.parts) - base_depth
        if depth >= _WALK_DEPTH:
            dirs[:] = []
        elif root in narrow:
            dirs[:] = [d for d in dirs if d.lower() in _RUNTIME_HOMES]
        else:
            keep = []
            for d in dirs:
                low = d.lower()
                if low in extra_skip or d.startswith("."):
                    continue
                if low in _SKIP_DIRS:
                    if not _holds_runtime(rp / d):
                        continue
                    narrow.add(os.path.join(root, d))
                keep.append(d)
            dirs[:] = keep
        if rp == skip_dir:
            continue
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
    # GUI passes this into fit() and Options.upscaler.
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
                    f"those calls as its input - once FSR/XeSS is switched on "
                    f"in the game's own settings; with no such setting it has "
                    f"nothing to hook - runs DLSS in their place and then "
                    f"neural rendering, with the model-resolution dial. "
                    f"Works in many games, not all - the feeder is the proven "
                    f"fallback.")
    return s


def fit(route: str, api: str, native_dlss: bool, sm: int | None,
        upscaler: str = "") -> tuple[bool, str]:
    """(usable on this machine, short note) for a route the game offers.

    The route list says what the GAME allows; this says what the CARD and
    the route's own rules add to it, so the dropdown can label each entry.

    `upscaler` is Support.upscaler: with no DLSS in the game, OptiScaler is
    usable only when there is an FSR/XeSS call for it to redirect. The
    default keeps the old answers for callers that do not pass it.
    """
    if route == OPTI:
        if not native_dlss and not upscaler:
            return False, "the game must already use DLSS, FSR 2/3 or XeSS"
        note = "model resolution dial: the fps lever"
        if not native_dlss:
            # The evidence for this route is a runtime DLL on disk, and a
            # DLL on disk is not an upscaler the player can switch on: Risk
            # of Rain 2 ships one and exposes no setting, so the route was
            # recommended and then had nothing to hook (#116). The
            # diagnosis said so afterwards; it belongs here, before the
            # install, where the choice is still being made.
            kind = "FSR" if upscaler == "fsr" else "XeSS"
            note = (f"the game's {kind} calls are redirected into DLSS, then "
                    f"neural rendering - so {kind} has to be on in the game's "
                    f"own settings, and if it has no such setting this route "
                    f"has nothing to hook and the feeder route is the one to "
                    f"use; " + note)
        if api == "DX11":
            # Added to, not replaced: a D3D11 game offered this route for its
            # FSR or XeSS still needs to hear that the upscaler has to be on.
            note = ("on D3D11 the upscaler becomes FSR on D3D12"
                    + ("; " + note if not native_dlss else ""))
        if api == "Vulkan":
            # Said before the install now, not only in the diagnosis after
            # it. OptiScaler replaces the game's upscaler through NGX, a
            # Direct3D path; on Vulkan its overlay keeps asking for an
            # upscaler however the game is set (RDR2 twice, #66 and #70).
            note = ("on Vulkan the model has had nothing to attach to in "
                    "the reports received on this route - set the game to "
                    "DirectX 12 if it offers the choice, or use the feeder "
                    "route")
        if sm is not None and sm < 120:
            note += "; author tested RTX 50 only, works here on the community runtime"
        return True, note
    if route == REMIX:
        return True, ("runs inside the Remix runtime, after DLSS - no "
                      "ReShade, no add-on")
    if route == NATIVE:
        return True, "most proven for D3D12 games with DLSS"
    if route == UPSTREAM:
        if not native_dlss:
            return False, "the game must already use DLSS"
        return True, ("runs the network before the game's DLSS, at render "
                      "resolution - cheaper; tested on two games")
    if route == STANDALONE:
        return True, ("own feed: DLAA at native resolution, DLSS SR below it, "
                      "frame generation; experimental - presents through a "
                      "window of its own")
    if route == RENODX:
        return True, "unproven - reported not working in many games; try the others first"
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
    """The route list, then the Remix override.

    A Remix game is a Remix game: the mod has already replaced the renderer,
    DLSS 5 goes inside that runtime, and every ReShade route would be
    injecting a proxy DLL into a process that dies on one. The other routes
    are still listed - people want to see what exists - but none of them is
    ever the recommendation here.
    """
    s = _detect_routes(install_dir, folder, api, bitness)
    trex = remix.find_runtime(install_dir) or remix.find_runtime(folder)
    if trex is None:
        return s
    s.options = [REMIX] + [o for o in s.options if o != REMIX]
    s.recommended = REMIX
    s.supported = True
    s.why_not = ""
    s.reason = (
        "This game has an RTX Remix mod installed (found " + trex.name + "). "
        "Remix path traces the frame in its own runtime, and DLSS 5 runs "
        "INSIDE that runtime, after DLSS - no ReShade, no feeder, no add-on. "
        "The other routes are listed for completeness only: they all install "
        "a ReShade proxy DLL, and a ReShade proxy in a Remix game's folder "
        "crashes it before it draws a frame.")
    return s


DLSSD_NAME = "nvngx_dlssd.dll"
DLSSG_NAME = "nvngx_dlssg.dll"


def _theirs(folder: Path, name: str) -> bool:
    """Did the GAME ship this runtime, rather than this tool writing it?

    Not the same question as `not _ours`. The standalone route ADDS
    nvngx_dlssg.dll to games that never had one, so ours must not be
    reported back as the game's own ("own files are never game evidence").
    Ray reconstruction is only ever SWAPPED, and a swap leaves the game's
    own file beside it as a backup - which is proof the game shipped it,
    even though the file on disk is now ours.
    """
    from . import installer
    if (folder / (name + installer.BACKUP_SUFFIX)).is_file():
        return True
    return not _ours(folder, name)


def _detect_routes(install_dir: Path, folder: Path, api: str,
                   bitness: int) -> Support:
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
        # The other two runtimes are evidence in their own right, and the
        # GUI reads the ray-reconstruction one out of here to decide whether
        # to offer the swap.
        for m in (DLSSD_NAME, DLSSG_NAME):
            if (d / m).is_file() and _theirs(d, m):
                s.evidence.append(m)
    # The walk runs when the loop above left something to find: the usual
    # "no DLSS beside the executable" case, and one more. An Unreal game
    # puts sl.interposer.dll beside the exe and the runtimes themselves
    # under Engine/Plugins, so native_dlss gets answered without any NGX
    # runtime having been seen - and the GUI reads that answer to decide
    # whether to offer the ray-reconstruction swap.
    #
    # The ray-reconstruction runtime is nested often enough that "found
    # something beside the exe" is not an answer about it: an Unreal game
    # keeps sl.interposer.dll by the executable and the runtimes under
    # Engine/Plugins, and plenty of games keep nvngx_dlss.dll by the
    # executable with nvngx_dlssd.dll deeper in. The walk answers both, and
    # `walked` remembers it, so the budget is paid once per game rather
    # than on every click - which is what #8, #18 and #32 were about.
    answered = s.native_dlss
    seen_rr = any(str(e).lower().endswith(DLSSD_NAME) for e in s.evidence)
    if not answered or not seen_rr:
        # Only the install folder is excluded - anything we wrote lives there.
        for rel in walked(folder, skip_dir=install_dir):
            base = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
            # `answered` is the state BEFORE this loop. Reading the flag
            # while the loop sets it dropped every hit after the first, so
            # a Streamline game reported less evidence than it had.
            if answered and base not in (DLSSD_NAME, DLSSG_NAME):
                continue
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
        # DXGI-based, so ReShade attaches as dxgi.dll like any D3D11 game.
        # Only the feeder reaches D3D10, and only from 0.13.1: it creates a
        # private D3D11 relay device inside the game, because a D3D10 device
        # has no shared NT handles, no fences and no UAVs to hand a frame to
        # the helper with. The add-ons hook D3D9/11/12, the bridge D3D11 and
        # Vulkan - none of them will ever see a D3D10 device.
        s.options = [FEEDER]
        s.recommended = FEEDER
        s.reason = ("Direct3D 10: only the feeder reaches it, from build "
                    "0.13.1 on (it relays the frame through a private D3D11 "
                    "device inside the game). Older feeder builds refuse "
                    "D3D10, so the install checks the build it picked.")
        return s

    if bitness != 64:
        # Every add-on that hooks the game directly is 64-bit only, and a
        # 32-bit process cannot load one. The feeder's host64 helper is the
        # only way in - DX9 games get DXVK in front of it.
        s.options = [FEEDER]
        s.recommended = FEEDER
        s.reason = ("32-bit games can only use the feeder path: every other "
                    "add-on is 64-bit and a 32-bit process cannot load it, so "
                    "the feeder's host64 helper process is the only way in."
                    + (" DirectX 9 is translated to Vulkan by DXVK first."
                       if api == "DX9" else ""))
        return s

    if api == "DX9":
        # 64-bit D3D9 is rare, and only ShortFuse's add-on reaches it: it
        # switches device creation to D3D9Ex and evaluates the presentation
        # backbuffer. The feeder's D3D9 support is the 32-bit DXVK path.
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
                        "D3D11. The renodx-dlss add-on is reported working in "
                        "few games.")
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
    NATIVE: "native - hooks the game's own DLSS (D3D12)",
    UPSTREAM: "neural-upstream - runs before the game's DLSS, cheaper",
    OPTI: "optiscaler - replaces the upscaler, no ReShade",
    BRIDGE: "bridge - the game's DLSS mirrored onto D3D12 (D3D11, Vulkan)",
    FEEDER: "feeder - for games without DLSS (depth + shader motion vectors)",
    STANDALONE: "standalone-dlssnr - own feed, DLSS SR and frame generation",
    RENODX: "renodx-dlss - in-process hook (D3D9/11/12), unproven",
    REMIX: "remix - DLSS 5 inside RTX Remix (path tracing)",
}

# One paragraph per route, shown under the dropdown. What it does, what it
# needs, what it costs - in that order, and nothing a person choosing a
# route does not need.
BLURB = {
    NATIVE: ("Krish's renodx-dlss5 add-on hooks the DLSS calls the game "
             "already makes and adds the neural pass after them. Nothing "
             "synthetic, no shaders; the game's own DLSS quality mode "
             "still applies. 64-bit D3D12 games with DLSS."),
    UPSTREAM: ("matiasLombo's neural-upstream add-on runs the network at "
               "render resolution, BEFORE the game's DLSS upscales - the "
               "same result on a smaller image, so it costs a fraction. "
               "Does the neural pass itself, so no renodx add-on goes in "
               "beside it. Configured from its own tab in ReShade. Its "
               "author tested GTA V Enhanced and Bright Memory Infinite."),
    OPTI: ("No ReShade. OptiScaler takes over the game's upscaler (DLSS, or "
           "FSR 2/3 and XeSS redirected into DLSS) and runs the model over "
           "its output. Its model-resolution dial is the biggest fps lever "
           "there is: the cost falls with the square of it and the frame "
           "keeps full detail. The author tested RTX 50; on RTX 20/30/40 "
           "it runs on the community runtime this tool installs."),
    BRIDGE: ("NIGos' dlss5-bridge mirrors the game's own DLSS contract onto "
             "a private D3D12 session, so a D3D11 or Vulkan game keeps its "
             "DLSS quality mode. Actively maintained; every release is "
             "tested on both APIs."),
    FEEDER: ("jlrouzies-fr's DLSS5-Feeder builds a DLAA contract out of "
             "ReShade's depth buffer and shader-estimated motion vectors, "
             "for games that have no DLSS at all. Always DLAA, never "
             "upscaling. D3D10/11/12, Vulkan, OpenGL, 32-bit (through a "
             "host64 helper) and DirectX 9 (through DXVK)."),
    STANDALONE: ("kibblerz's standalone-dlssnr add-on does the whole "
                 "pipeline itself: its own feed (VORT motion vectors, "
                 "ReShade depth), the network, then DLAA at native "
                 "resolution or real DLSS Super Resolution below it, and "
                 "frame generation on top. Presents through a window of its "
                 "own, which is the fragile part: a resolution or display "
                 "mode change needs a restart. F10 compares. Experimental."),
    RENODX: ("ShortFuse's renodx-dlss add-on hooks D3D9, D3D11 and D3D12 "
             "presentation in-process - no bridge, no shaders. Reported "
             "not working in many games; the only route for 64-bit DirectX "
             "9, otherwise try the recommended one first."),
    REMIX: ("This game has an RTX Remix mod. Remix path traces the frame in "
            "its own runtime (the '.trex' folder) and the neural pass runs "
            "there, after DLSS, so the game's DLSS quality setting applies. "
            "Nothing is injected into the game - no ReShade, no feeder, no "
            "add-on; a ReShade proxy DLL in a Remix folder crashes the game "
            "before it draws. The tool puts nvngx_dlssnr.dll into .trex and "
            "switches the pass on in rtx.conf. If the runtime has no neural "
            "pass (NVIDIA's own has none), 'swap the Remix runtime' puts in "
            "a community build that does - experimental, it can undo fixes "
            "the mod's own runtime carried."),
}

# Per-executable facts the route texts cannot know. Found by people who did
# the install by hand and wrote down why it failed; shown beside the
# conflicts before INSTALL. Keys are lower-case executable names.
QUIRKS: dict[str, str] = {
    "quake3.exe": ("this Quake III executable loads opengl32.dll from "
                   "System32 and never sees ReShade's copy beside it - use "
                   "the ioquake3 build (ioquake3.org), which loads from its "
                   "own folder"),
    "openmw.exe": ("OpenGL: the renodx-dlss5 add-on is pinned to 4.60 here "
                   "(4.70 stalls after four frames on GL) and motion vectors "
                   "come from VORT - LumeniteFX reads none on OpenGL"),
}


# Every route on this list ends up inside the driver's own NGX runtime -
# the add-on asks the driver to evaluate the model. The Remix route does
# not: its runtime carries the neural pass itself.
_NGX_ROUTES = (NATIVE, BRIDGE, FEEDER, OPTI, RENODX, UPSTREAM, STANDALONE)
# ...and these are the ones that get a renodx-dlss5 add-on, so they are the
# only ones the 4.55 pin says anything about. OptiScaler runs the model
# through its own build, the upstream and standalone add-ons through theirs.
_RENODX_ROUTES = (NATIVE, BRIDGE, FEEDER, RENODX)
# The first driver documented to ship the neural-rendering runtime.
# optiscaler.DRIVER_MIN says the same thing for its own route; kept here as
# its own name so this module does not import that one for a constant.
_DRIVER_KNOWN_GOOD = "616.56"


def standalone_fits(api: str, bitness: int | None) -> bool:
    """Is the standalone route one this game is offered at all?

    The same test the route list uses: 64-bit, and D3D11 or D3D12 (or not
    yet known). Anything that suggests standalone asks this first - it was
    being suggested to 32-bit and Vulkan games whose dropdown has no such
    entry. A bitness that is not known is not 64: the route list does not
    offer standalone then either.
    """
    return bitness == 64 and str(api or "").upper() in (
        "DX11", "DX12", "UNKNOWN", "", "?")


def driver_warning(route: str, driver: str | None,
                   offered: list[str] | tuple[str, ...] | None = None) -> str | None:
    """What to say about this driver BEFORE the install, or None.

    616.64 is behind 31 of the first 87 reports - by a distance the largest
    single cause in the tracker, and until now the tool only mentioned it in
    the diagnosis, which is after the person has already lost an evening.
    The renodx-dlss5 build is pinned to 4.55 automatically on this driver,
    but 4.55 faults in some games too (issue #37), so the pin is a
    mitigation and saying otherwise would be a promise the tool cannot keep.
    """
    if not driver or route not in _NGX_ROUTES:
        return None
    from . import gpu, sources
    # The other end of the range, and the one nobody was ever told about: a
    # driver from before DLSS 5 existed cannot create the feature at all.
    # DOOM on 475.14 got "the game has not been started since the install"
    # (#16) - true, and useless. 616.56 is the first driver documented to
    # carry the neural-rendering runtime.
    if gpu.driver_at_least(_DRIVER_KNOWN_GOOD, driver) is False:
        return (f"driver {driver} is older than DLSS 5 itself. The "
                f"neural-rendering runtime first appears in "
                f"{_DRIVER_KNOWN_GOOD}: the install will finish and the model "
                f"will not run. Update the driver before trying anything "
                f"else here.")
    if not gpu.driver_at_least(sources.DRIVER_FAULT_MIN, driver):
        return None

    tail = ("If this game crashes the moment neural rendering comes on, the "
            "driver is the first thing to roll back - 616.56 is the newest "
            "one with no reports of this fault.")
    if route in _RENODX_ROUTES:
        # The fault lives in renodx-dlss5's path through the driver. The
        # shared results showed a game fail three times on the feeder route
        # on 616.92 and then work on standalone, reported by the same person - so
        # for a game without DLSS there is a way round that needs no
        # rollback, and it was never mentioned here. Said as the mechanism,
        # not as a count: the counts belong to the compatibility note,
        # which is per game and keeps itself current.
        # Only where standalone is actually on offer: 32-bit, DX9, Vulkan
        # and OpenGL games are not given it, and naming a route that is not
        # in the dropdown is worse than naming none.
        # Every route here loads renodx-dlss5, so every one of them has the
        # same way round; the evidence is one game on the feeder route.
        sa = (STANDALONE in offered) if offered is not None else route == FEEDER
        other = ("The standalone route does not load renodx-dlss5 at all, and "
                 "is worth trying before the driver. If that crashes too the "
                 "moment neural rendering comes on, roll the driver back - "
                 "616.56 is the newest one with no reports of this fault."
                 if sa else tail)
        return (f"driver {driver}: the 4.6/4.7 renodx-dlss5 builds fault "
                f"inside the driver's NGX runtime on "
                f"{sources.DRIVER_FAULT_MIN} and newer, on every evaluate - "
                f"the tool pins {sources.DRIVER_FAULT_RENODX_PIN} here "
                f"instead. That is a way round it, not a fix: some games "
                f"fault on {sources.DRIVER_FAULT_RENODX_PIN} too. " + other)
    if route == STANDALONE:
        # This route was being warned off with the renodx fault's record,
        # which is not its own: it does not load that add-on, and the one
        # game in the shared results that failed on feeder on 616.92 worked
        # here. It still runs NVIDIA's runtime on this driver, so the tail
        # stays - but the scare does not.
        return (f"driver {driver}: the fault known on "
                f"{sources.DRIVER_FAULT_MIN} and newer is reached through the "
                f"renodx-dlss5 add-on, which this route does not load - so on "
                f"these drivers this is the route to try when a route that "
                f"loads it (native, bridge, feeder) faults. It still runs NVIDIA's runtime: if the game crashes "
                f"the moment neural rendering comes on, roll the driver back "
                f"to 616.56.")
    return (f"driver {driver}: {sources.DRIVER_FAULT_MIN} and newer fault "
            f"inside the driver's own NGX runtime in a good number of games, "
            f"and {sources.DRIVER_FAULT_MIN} itself is named in more "
            f"reports here than any other single cause. This route does not "
            f"install the renodx add-on, "
            f"so the pin that works around it elsewhere does not apply. "
            + tail)


def quirks(exe, api: str = "") -> tuple[str, ...]:
    """The QUIRKS lines for this executable and API, if any."""
    name = getattr(exe, "name", exe or "") or ""
    out: list[str] = []
    if api == "OpenGL":
        out.append("OpenGL: motion vectors come from VORT (LumeniteFX reads "
                   "none here) and the renodx-dlss5 add-on is pinned to 4.60 - "
                   "both applied at install, whatever the dropdowns say")
    hit = QUIRKS.get(str(name).lower())
    if hit and hit not in out:
        out.append(hit)
    return tuple(out)


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
           "not with a frame-gen unlocker or dlss-enabler in the folder",
           "frame generation (tick below, D3D12): the game's own frame "
           "generation must be OFF"),
    BRIDGE: ("not with the feeder or renodx-dlss add-on in the same folder "
             "- both build a contract and the game dies before its swap chain",
             "NVIDIA Smooth Motion off for this game",
             "an older dlss5-dx11-bridge.addon64 is removed - the two conflict"),
    FEEDER: ("always DLAA; the game's own DLSS is ignored",
             "NVIDIA Smooth Motion off",
             "not with the bridge or renodx-dlss add-on in the same folder"),
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
    REMIX: ("no ReShade, no feeder, no add-ons in the folder - a ReShade "
            "proxy DLL crashes a Remix game before it draws",
            "the neural pass runs inside the Remix runtime, after DLSS, so "
            "the game's own DLSS/RR settings still apply",
            "toggle it in the Remix menu: Alt+X -> Developer Settings Menu "
            "-> Post-Processing"),
}
