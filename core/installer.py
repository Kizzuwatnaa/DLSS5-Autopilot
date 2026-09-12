r"""Install engine: 64-bit, 32-bit and DX9 paths.

64-bit layout (next to the game executable):
    <proxy>.dll                 ReShade64.dll  (dxgi.dll or opengl32.dll)
    dlss5-feed.addon64          (feeder route)
    renodx-dlss5.addon64        (feeder / native / bridge routes)
    renodx-dlss.addon64         (renodx route - ShortFuse's SF build)
    nvngx.dll.addon64           (upstream route - neural-upstream, no renodx add-on)
    standalone-dlssnr.addon64   (standalone route - kibblerz's own feed, no renodx add-on)
    nvngx.dll                   (standalone route - the add-on's caller-identity bridge)
    nvngx_dlssnr.dll
    nvngx_dlss.dll
    nvngx_dlssg.dll             (standalone route - frame generation)
    ReShade.ini / ReShadePreset.ini / dlss5-feed.cfg
    reshade-shaders/Shaders/{headers, DLSS5_Feed.fx, lumenite_*.fx}
    reshade-shaders/Shaders/{DLSS5_AIO_Feed.fx, vort_Motion.fx}   (standalone)
    reshade-shaders/Shaders/include/lumenite_*.fxh
    reshade-shaders/Shaders/Includes/vort_*.fxh                    (standalone)
    reshade-shaders/Textures/lumenite_bluenoise256.png
    reshade-shaders/Textures/vort_BlueNoise.png                    (standalone)

32-bit: a 32-bit process cannot load 64-bit NGX, so a helper process is
needed. The game gets 32-bit ReShade + addon32; host64/ holds its own 64-bit
ReShade and all the DLSS parts.

DX9: DXVK translates to Vulkan first, then the 32-bit path applies.

REMIX: none of the above. The mod's own Remix runtime does the work, so the
only things written are inside its `.trex` folder -

    .trex/nvngx_dlssnr.dll      the neural-rendering snippet
    .trex/d3d9.dll              only when swapping the runtime itself
    .trex/remix_nvngx.dll       its caller-identity bridge, same swap

- plus one line in the game's rtx.conf. No ReShade, no add-on, no proxy DLL:
a ReShade proxy in a Remix game's folder crashes it before it draws.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (emulators, anticheat, dlss, dxvk, feedcfg, games, gpu, mfg, net,
               optiscaler, pe, prefs, reengine, refw, remix, reshade_ini, sources, vulkan)
# Imported by name as well: inside the Options class body the field
# `dlss: str | None` shadows the module, so `dlss.FEEDER` would read the
# field's default (None) instead of the module attribute.
from .dlss import BRIDGE, FEEDER, NATIVE, OPTI, STANDALONE, UPSTREAM
from .dlss import RENODX as ROUTE_RENODX   # the route; RENODX below is a file name
from .dlss import REMIX as ROUTE_REMIX

MANIFEST = "dlss5-autopilot.json"

FEEDER_ADDON64 = "dlss5-feed.addon64"
FEEDER_ADDON32 = "dlss5-feed.addon32"
FEEDER_HOST = "dlss5-feed-host64.exe"
FEEDER_FX = "DLSS5_Feed.fx"
RENODX = "renodx-dlss5.addon64"
# ShortFuse's add-on - the "SF" build - which hooks D3D9/D3D11/D3D12 itself.
RENODX_SF = "renodx-dlss.addon64"

UPSTREAM_ADDON = "nvngx.dll.addon64"
# kibblerz's DLSS5-Reshade-AIO. The add-on runs the whole pipeline on a
# private NGX session; the plain nvngx.dll beside it is its caller-identity
# bridge - the same "the caller's path must contain nvngx.dll" rule that
# neural-upstream satisfies with its file name. Without it the add-on logs
# "required private runtime dependency missing" and does nothing. Three
# files with nvngx.dll in the name, three routes: nvngx.dll.addon64
# (upstream), nvngx.dll (standalone), nvngx.dll_dlssnr.dll (OptiScaler).
STANDALONE_ADDON = "standalone-dlssnr.addon64"
STANDALONE_BRIDGE = "nvngx.dll"
STANDALONE_FX = "DLSS5_AIO_Feed.fx"
DLSSG = "nvngx_dlssg.dll"
# Ray reconstruction. Only ever swapped, never added: a game that does
# not ship it does not ask for it, and dropping one in changes nothing.
DLSSD = "nvngx_dlssd.dll"
# Vortigern's optical-flow shader the standalone add-on schedules for real
# motion vectors; without it the add-on runs on zero-motion guides.
VORT_FX = "vort_Motion.fx"
VORT_TEXTURE = "vort_BlueNoise.png"

# Other hooks on the same NGX entry points. A game with one of these plus
# our add-on gets two things rewriting the same calls: flicker, frame-gen
# multipliers greyed out, or nothing at all (Cyberpunk 2077 with OptiScaler
# and a frame-gen unlocker, issue #3). Not refused - stated.
OTHER_NGX_HOOKS = ("OptiScaler.ini", "nvngx.ini", "fakenvapi.ini",
                   "dlss-enabler.dll", "dlss-enabler-upscaler.dll",
                   "nvngx-wrapper.dll", "dlssg_to_fsr3_amd_is_better.dll",
                   "dlssg_to_fsr3.ini", "nvngx.dll_dlssnr.dll",
                   # sdli1995/dlssg_for_sm86: frame generation on RTX 30,
                   # shipped as a version.dll proxy with DLSSG 310.1 inside
                   # it. That is the same file name the MFG unlock's loader
                   # and one of the OptiScaler proxies use, so a folder
                   # holding both has two things in one slot - and the .ini
                   # is what says which one is there.
                   "dlssg_sm86.ini",
                   # xenmods/DLSSNR-Cost-Scaler: a proxy that replaces
                   # nvngx_dlssnr.dll with its own and adds a model-resolution
                   # dial. It brings an add-on of its own, so a folder with
                   # both has two things hooking the model and the runtime
                   # beside the game is not the one this tool put there.
                   "dlssnr-companion.addon64", "nvngx_dlssnr.ini",
                   # NGX loads a plain nvngx.dll from the game folder before
                   # the driver's: an OptiScaler installed by hand under its
                   # old name, or the standalone route's caller bridge.
                   STANDALONE_BRIDGE)


def other_ngx_hooks(root: Path, path: str = "") -> list[str]:
    """Files in the folder that belong to another DLSS/NGX hook.

    `path` is the route being looked at: the standalone route's own nvngx.dll
    is part of that route, and a foreign hook on every other.
    """
    found: list[str] = []
    try:
        names = {f.name.lower(): f.name for f in root.iterdir() if f.is_file()}
    except OSError:
        return found
    for n in OTHER_NGX_HOOKS:
        if path == STANDALONE and n.lower() == STANDALONE_BRIDGE.lower():
            continue
        if n.lower() in names:
            found.append(names[n.lower()])
    # A ReShade add-on we did not write - another RenoDX build, say - is
    # loaded by ReShade regardless and hooks the same swap chain.
    ours = {"dlss5-feed.addon64", "dlss5-bridge.addon64",
            RENODX.lower(), RENODX_SF.lower(), UPSTREAM_ADDON.lower(),
            STANDALONE_ADDON.lower(), "rtx40mfg-ui.addon64"}
    for low, orig in names.items():
        if low.endswith(".addon64") and low not in ours:
            found.append(orig)
    return found


def hook_warning(root: Path, path: str) -> str:
    """One sentence, or "" when the folder is clean."""
    if path == OPTI:
        found = [n for n in other_ngx_hooks(root) if n.lower() != "optiscaler.ini"
                 and n.lower() != "nvngx.dll_dlssnr.dll"]
    else:
        found = other_ngx_hooks(root, path)
    if not found:
        return ""
    return (f"another DLSS hook is already in this folder ({', '.join(found[:5])}"
            f"{', ...' if len(found) > 5 else ''}). Two things rewriting the "
            f"same NGX calls means flicker, greyed-out frame-gen multipliers "
            f"or nothing happening. If it misbehaves, remove that mod (or "
            f"uninstall this) and try one at a time.")
DLSSNR = "nvngx_dlssnr.dll"
DLSS = "nvngx_dlss.dll"
HOST_DIR = "host64"

# The RTX Remix route. Nothing of ours goes beside the executable: the
# runtime lives in the mod's .trex folder and everything we write goes in
# there, plus one line in rtx.conf.
REMIX_RUNTIME = remix.RUNTIME_DLL          # .trex/d3d9.dll - the runtime
REMIX_NVNGX = remix.REMIX_NVNGX            # the caller-identity bridge

SHADERS = Path("reshade-shaders") / "Shaders"
INCLUDE = SHADERS / "include"
VORT_INCLUDE = SHADERS / "Includes"    # VORT's own folder name, its .fx includes it
TEXTURES = Path("reshade-shaders") / "Textures"

BRIDGE_ADDON = "dlss5-bridge.addon64"
BRIDGE_CFG = "dlss5-bridge.cfg"

BACKUP_SUFFIX = ".dlss5-autopilot-backup"
# A backup of a runtime a mod has since replaced with its own: kept, but
# under a name uninstall does not restore (it would put the older one back).
SUPERSEDED_SUFFIX = ".dlss5-autopilot-superseded"

# Files a game ships that break the neural pass when Windows loads them in
# preference to System32's copy. They are renamed, not deleted, and the
# manifest records it so uninstall puts them back. MPC-HC and a number of
# older games bundle a D3DCompiler_47.dll that rejects the cs_5_1 target the
# DLSS 5 add-on compiles with; the feed then reports frames delivered while
# neural rendering silently does nothing.
SIDELINE = ("d3dcompiler_47.dll",)
SIDELINE_SUFFIX = ".dlss5-off"
# An add-on from another route, moved out of the way. A separate suffix on
# purpose: BACKUP_SUFFIX means "put this back on uninstall", which is the one
# thing that must not happen to a file that was causing a conflict.
ORPHAN_SUFFIX = ".dlss5-autopilot-orphan"

# Written by the components while the game runs, so they exist only because
# something was installed - but they are created after the install, which
# means the manifest has never heard of them and uninstall used to leave every
# one behind. Each is regenerated from scratch on the next launch, so removing
# them loses nothing even in the unlikely case one predates us.
RUNTIME_ARTIFACTS = (
    "ReShade.log",              # ReShade, every launch
    "dlss5-feed.log",           # the feeder add-on
    "dlss5-feed-host.log",
    "dlss5-bridge.log",
    "OptiScaler.log",
    "nvngx.log",                # NGX itself
    "nvngx_dlssnr.log",
    "nvngx_dlss.log",
)

# OptiScaler keeps its logs in a folder of its own. A game could plausibly
# have a folder by that name, so only OptiScaler's own files are taken out of
# it, and the folder itself only if that empties it.
OPTI_LOG_DIR = "Logs"

# The project was called "dlss5kur" up to v1.1. Anyone upgrading has installs
# recorded under the old names; without these the new build would not see them
# and Uninstall would leave files behind.
LEGACY_MANIFESTS = ("dlss5kur-kurulum.json", "dlss5-installer.json")
LEGACY_BACKUP_SUFFIXES = (".dlss5kur-yedek", ".dlss5-installer-backup")


class InstallError(Exception):
    pass


@dataclass
class Options:
    provider: int = 3                       # DLSS5_MV_PROVIDER
    renodx: str | None = sources.RENODX_DEFAULT
    renodx_local: Path | None = None        # user's own build
    dlssnr: str | None = None               # None = auto-pick for this GPU
    dlss: str | None = None                 # None = newest
    # Ray reconstruction, for a game that already ships it: "" leaves the
    # game's own alone, a label swaps in that build. Never installed into a
    # game that does not have one - nothing would ask for it.
    dlssd: str = ""
    keep_game_dlss: bool = True             # leave the game's own nvngx_dlss.dll alone
    feed: dict = field(default_factory=dict)   # dlss5-feed.cfg settings
    ignore_gpu_mismatch: bool = False
    path: str = FEEDER                      # native / bridge / feeder
    opti_proxy: str = ""                    # "" = pick a free name for this game
    opti_build: str = ""                    # key of optiscaler.BUILDS
    reshade_proxy: str = ""                 # "" = choose from the API
    native_dlss: bool = False               # game ships its own DLSS
    # "fsr" / "xess" / "": the upscaler a game WITHOUT DLSS ships. On the
    # OptiScaler route its calls become OptiScaler's input and DLSS runs in
    # their place, so the tool also puts a nvngx_dlss.dll in the folder.
    upscaler: str = ""
    # The feeder's pre-releases are where support for the newer DLSS 5 add-on
    # generations lives; the stable release only accepts renodx-dlss5 4.55.
    feeder_prerelease: bool = False
    feeder_tag: str = ""                    # "" = stable or newest pre-release
    dxvk: bool = False                      # run a D3D11 game on Vulkan via DXVK
    nr: dict = field(default_factory=dict)  # OptiScaler [DlssNr] settings
    # OptiScaler route, D3D12: FSR 3.1 frame generation from the libraries
    # OptiScaler ships, on any card. Off by default - it adds latency and
    # every game's HUD reacts differently.
    fg: bool = False
    # ReShade routes, RTX 40, D3D12/Vulkan games that ship DLSS Frame
    # Generation: dashdogy's RTX40MFG-Unlock for 3x/4x multipliers. Research
    # software; off by default and only offered where mfg.applies() says so.
    mfg: bool = False
    # ReShade routes: register ReShade's OpenXR layer as well, so the pass
    # runs on the image the VR headset shows rather than the desktop mirror
    # (#33). Global for the user, like the Vulkan layer. Not tried with a
    # headset by the author.
    vr: bool = False
    # REMIX route: replace the mod's Remix runtime with a community build
    # that HAS the neural pass. Off by default and deliberately opt-in - a
    # mod's runtime is often a fork carrying game-specific fixes, and
    # swapping it can break the mod itself.
    remix_swap: bool = False


@dataclass
class Report:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # component name -> version installed, recorded in the manifest so a
    # game set up weeks ago can be told what has moved on since.
    components: dict = field(default_factory=dict)
    # Relative paths a PREVIOUS install of ours put in this folder. They must
    # never be backed up as "the game's own file" - see _backup.
    preinstalled: set = field(default_factory=set)
    # Game files renamed out of the way (see SIDELINE), relative paths.
    sidelined: list[str] = field(default_factory=list)
    # REMIX route: {"conf", "key", "trex", "flavour"}. rtx.conf is the user's
    # file and is never listed in `written` (uninstall deletes those); the
    # key is recorded here so uninstall can take exactly that line back out.
    remix: dict = field(default_factory=dict)


# ---------------------------------------------------------------- reliability

# Measured on real games, not guessed. DLSS 5 feeding was designed around
# DXGI; everything else is a bolt-on and fails far more often.
STABLE, BETA, EXPERIMENTAL = "stable", "beta", "experimental"

def reliability(g: games.Game, path: str = FEEDER,
                upscaler: str = "", remix_swap: bool = False) -> tuple[str, str]:
    """(level, explanation) - how likely this route is to actually work.

    `upscaler` is set on the OptiScaler route when the game has no DLSS and
    OptiScaler is redirecting its FSR/XeSS calls instead - one more hook
    that has to land, so it says so.

    `remix_swap` is set on the REMIX route when the mod's own Remix runtime
    is being replaced, which is a different risk from merely switching the
    neural pass on in a runtime that already has one.
    """
    if path == ROUTE_REMIX:
        if remix_swap:
            return EXPERIMENTAL, (
                "The Remix runtime installed by this mod has no neural pass, "
                "so it is being replaced with a community build that does. "
                "That build is not the mod author's: a mod's runtime is often "
                "a fork carrying fixes for this exact game, and swapping it "
                "can break them. The old one is backed up and 'Uninstall' "
                "puts it back.")
        return BETA, (
            "The Remix runtime already installed here has the DLSS 5 neural "
            "pass built in; all this does is put nvngx_dlssnr.dll beside it "
            "and switch the pass on in rtx.conf. Nothing is injected into "
            "the game. Only a handful of Remix mods ship a runtime "
            "with the pass at all.")
    if g.api == "DX10":
        return BETA, ("Direct3D 10 through the feeder's private D3D11 relay "
                      "device (feeder 0.13.1 and newer). A D3D10 game "
                      "installs like a D3D11 one.")
    if path == NATIVE:
        return STABLE, ("The game's own DLSS is hooked directly - no synthetic "
                        "contract, no motion-vector shaders, and your in-game "
                        "DLSS quality setting still applies.")
    if path == UPSTREAM:
        return BETA, ("neural-upstream runs the network at render resolution, "
                      "before the game's own DLSS. Its author tested two "
                      "games (GTA V Enhanced, Bright Memory Infinite).")
    if path == STANDALONE:
        return EXPERIMENTAL, ("standalone-dlssnr does everything itself - own "
                              "feed, DLAA or DLSS Super Resolution, frame "
                              "generation - and shows the result through a "
                              "topmost window of its own. That window trick "
                              "is the fragile part: resolution or display-mode "
                              "changes need a restart, and some games hang at "
                              "start. Few games tested.")
    if path == ROUTE_RENODX:
        if g.api == "DX9":
            return BETA, ("64-bit DirectX 9 through the renodx-dlss add-on: it "
                          "evaluates the presentation backbuffer with no "
                          "motion vectors, so expect a softer result.")
        return EXPERIMENTAL, ("The renodx-dlss add-on hooks the game in-process. "
                              "Reported not working in many "
                              "games. Try the recommended route first.")
    if path == OPTI and upscaler:
        return BETA, ("FSR/XeSS redirected into DLSS by OptiScaler - works in "
                      "many games, not all. OptiScaler has to hook the game's "
                      f"{'FSR 2/3' if upscaler == 'fsr' else 'XeSS'} calls "
                      "first; a game that links its upscaler statically gives "
                      "it nothing to hook, and the feeder is the fallback."
                      + (" On D3D11 it needs a bridged upscaler (FSR on D3D12) "
                         "in place of DLSS." if g.api == "DX11" else ""))
    if path == OPTI:
        return BETA, ("OptiScaler replaces the upscaler and runs the model over "
                      "its output. The game must already use DLSS; the author "
                      "tested RTX 50 only, older cards run the community runtime."
                      + (" On D3D11 it needs a bridged upscaler (FSR on D3D12) "
                         "in place of DLSS." if g.api == "DX11" else ""))
    if path == BRIDGE:
        if g.api == "Vulkan":
            return BETA, ("The bridge mirrors the game's DLSS contract onto a "
                          "private D3D12 session - the route for Vulkan games "
                          "that ship DLSS; the feeder is the fallback.")
        return BETA, ("The bridge reproduces the DLSS contract on a private "
                      "D3D12 session. Fewer moving parts than the feeder, but "
                      "less proven.")
    if g.api == "DX9":
        return EXPERIMENTAL, (
            "DirectX 9 is the least reliable path. The game runs through "
            "DXVK translation and then the 32-bit helper process; the "
            "DLSS feature frequently fails to create on top of that. Expect "
            "it not to work.")
    if g.api == "Vulkan":
        return BETA, ("Vulkan works through ReShade's layer registration and "
                      "the component's own D3D12 interop. Newer than the "
                      "DirectX paths, and it has met fewer real games.")
    if g.bitness == 32:
        return EXPERIMENTAL, (
            "32-bit games go through a cross-process helper (host64). "
            "Upstream marks this beta and it often fails to start the DLSS "
            "feature.")
    if g.api == "OpenGL":
        return BETA, (
            "OpenGL needs interop extensions the driver may not expose to "
            "this game, and the game must render on the NVIDIA card. Verified "
            "on six games with VORT motion vectors and add-on 4.60, which the "
            "tool applies.")
    if g.api in ("DX11", "DX12", "Unknown"):
        return STABLE, "DirectX 11/12 is the path DLSS 5 feeding is built around."
    return BETA, "Untested path."


# ---------------------------------------------------------------- helpers

def _is_reshade(path: Path) -> bool:
    """A ReShade proxy DLL carries the literal string "ReShade" and is >1 MB."""
    try:
        if not path.is_file() or path.stat().st_size < (1 << 20):
            return False
        return b"ReShade" in path.read_bytes()
    except OSError:
        return False


# The names ReShade can be installed under. It is the same DLL each time; the
# name decides which system library it stands in for, and therefore when in
# start-up the game loads it.
#
# This matters more than it looks. A game that imports dxgi.dll statically -
# MGS V does - has our proxy loaded by Windows before any of its own code
# runs, which is the earliest and least forgiving moment. One that loads dxgi
# later through LoadLibrary - Total War: Warhammer III does - picks it up when
# it is good and ready. When a game will not start, changing the name it
# comes in under is the first thing to try.
RESHADE_PROXIES = ("dxgi.dll", "d3d11.dll", "d3d12.dll", "d3d10.dll",
                   "d3d9.dll", "opengl32.dll")

RESHADE_PROXY_HELP = {
    "dxgi.dll": "default for Direct3D 10/11/12",
    "d3d11.dll": "try this if a D3D11 game will not start with dxgi",
    "d3d12.dll": "D3D12 alternative to dxgi",
    "d3d10.dll": "D3D10 only",
    "d3d9.dll": "DirectX 9 (rarely useful - DX9 goes through DXVK)",
    "opengl32.dll": "the only option for OpenGL",
}
# dinput8.dll is NOT offered here: it used to be, as a ReShade proxy name to
# try on RE Engine games, but that was the wrong fix. The real one is
# REFramework, a separate mod that installs itself under that exact name and
# patches around RE Engine's own tamper checks - see refw.py. ReShade
# claiming the same name would collide with it.


def _proxy_name(api: str, chosen: str = "") -> str:
    if api == "Vulkan":
        # No proxy DLL at all: ReShade reaches a Vulkan game as a layer.
        return VULKAN_LAYER
    if chosen in RESHADE_PROXIES:
        return chosen
    return "opengl32.dll" if api == "OpenGL" else "dxgi.dll"


# What the manifest and the labels say where a proxy name would go when the
# game is reached through the Vulkan layer instead. Never a file.
VULKAN_LAYER = "(vulkan layer)"


def wants_dxvk(g: games.Game) -> str | None:
    """The game's name when it is known to need DXVK, else None.

    These games close themselves the moment ReShade hooks D3D11 - no crash,
    no message. Through DXVK they render on Vulkan and ReShade stays outside.
    """
    return dxvk.wanted(g.exe) if g.api in dxvk.APIS else None


def uses_dxvk(g: games.Game, opt: "Options") -> bool:
    """Is this install going through DXVK? D3D11 or D3D9, on the ReShade
    routes only - OptiScaler is itself the dxgi.dll DXVK would need to be,
    and ShortFuse's renodx-dlss hooks D3D9 in-process. The standalone add-on
    reaches Vulkan only with an extra boundary shader and a per-launch layer
    script; its D3D11 path is the tested one, so it stays on D3D11.

    On D3D11 this is a choice. On DirectX 9 it is not: the feed needs a
    D3D11/D3D12 device to build its contract on, ReShade on a raw D3D9
    device cannot give it one, and DXVK is the only translation left since
    dgVoodoo2 was dropped. So a DX9 game takes it whether or not the box is
    ticked - the routes that handle D3D9 themselves are excluded above.
    """
    if g.api not in dxvk.APIS:
        return False
    if opt.path in (OPTI, ROUTE_RENODX, UPSTREAM, STANDALONE, ROUTE_REMIX):
        return False
    return bool(opt.dxvk) or g.api == "DX9"


def via_dxvk(g: games.Game, opt: "Options") -> games.Game:
    """The game as the rest of the install sees it: a Vulkan game."""
    if not uses_dxvk(g, opt):
        return g
    return replace(g, api="Vulkan", api_why=f"DXVK: {g.api} -> Vulkan")


def check_supported(g: games.Game) -> tuple[bool, str]:
    """Can this game be set up automatically?"""
    if not g.exe:
        return False, "No game executable found."
    if g.error:
        return False, g.error
    if g.exe_warning and (g.bitness not in (32, 64) or g.api not in games.APIS):
        return False, ("Choose this game's 'architecture' and 'graphics api' in "
                       "its details on the games page - Windows protects its "
                       "executable, so they cannot be read from it.")
    if g.bitness not in (32, 64):
        return False, "Could not read the architecture."
    if g.api == "Vulkan":
        # Reachable since the bridge landed: it mirrors the game's DLSS
        # contract onto a private D3D12 session. ReShade still has to be
        # attached to the Vulkan runtime, which its own installer does.
        return True, ""
    return True, ""


def _backup(dst: Path, rep: Report, root: Path) -> None:
    """Preserve the game's own file before overwriting it.

    If the game ships its own nvngx_dlss.dll and we replace it, uninstalling
    must be able to put it back - otherwise the game loses its DLSS for good.

    A file a PREVIOUS install of ours wrote is emphatically not the game's.
    Backing one up made uninstall RESTORE it instead of deleting it, so after
    installing twice the folder came out of an uninstall still fully set up -
    dxgi.dll, the add-ons and a 165 MB nvngx_dlssnr.dll all put back. That is
    the "uninstall does not remove everything" people were seeing.
    """
    if not dst.is_file():
        return
    bak = dst.with_name(dst.name + BACKUP_SUFFIX)
    try:
        rel = str(bak.relative_to(root))
    except ValueError:
        rel = str(bak)
    try:
        key = str(dst.relative_to(root))
    except ValueError:
        # A .trex outside the install folder is recorded by absolute path;
        # asked only relative, a file of ours there was backed up as if it
        # were the game's own.
        key = str(dst)
    try:
        if key.replace("\\", "/") in rep.preinstalled:
            # Ours from last time, so not backed up again - but the game's
            # original may already sit beside it from the FIRST install, and
            # the new record has to carry it. Returning without that dropped
            # the backup from every reinstall ("update all"), and uninstall
            # then deleted our file and left the game's own unrestored: the
            # swapped Remix runtime came out of an uninstall missing (#148).
            if bak.is_file() and rel.replace("\\", "/") not in \
                    {w.replace("\\", "/") for w in rep.written}:
                rep.written.append(rel)
            return
    except ValueError:
        pass
    # A backup left by an older release sits under a different suffix; adopt
    # it so this manifest can restore it, instead of orphaning a 50+ MB file.
    for old_s in LEGACY_BACKUP_SUFFIXES:
        legacy = dst.with_name(dst.name + old_s)
        if legacy.is_file() and not bak.exists():
            try:
                legacy.rename(bak)
                rep.notes.append(f"adopted an older backup of {dst.name}")
            except OSError:
                pass
    if bak.exists():
        # Already backed up by an earlier install. Do NOT copy again - that
        # would overwrite the game's original with our own file. But the entry
        # must still go into this manifest, otherwise a later uninstall reads
        # a manifest with no backup listed and never restores it.
        if rel not in rep.written:
            rep.written.append(rel)
            rep.notes.append(f"existing backup of {dst.name} kept")
        return
    try:
        shutil.copy2(dst, bak)
        rep.written.append(rel)
        rep.notes.append(f"backed up the game's own {dst.name}")
    except OSError:
        pass


def _overlay_key_pref() -> int:
    """The overlay key from the settings, or 0. Never raises.

    prefs.json is a file a person can edit, so it is untrusted input like
    any other: a hand-typed "F10" in there used to end the install with a
    ValueError at the last step.
    """
    try:
        return int(prefs.get("overlay_key") or 0)
    except (TypeError, ValueError):
        return 0


def _find_runtime(root: Path, name: str,
                  folder: Path | None = None) -> Path | None:
    """Where the game keeps this runtime, or None if it does not have one.

    Games do not keep these beside the executable - Unreal buries them under
    Engine/Plugins/..., so the same bounded walk the route detection uses
    finds them. `folder` is the game's own root, which is where the window
    looked: without it a game whose executable sits in a subfolder was
    offered the swap and then told it had no runtime. The FIRST one wins: a
    game with two copies is rare, and replacing one it does not load would
    be silent.
    """
    for base in (root, folder):
        if base is None:
            continue
        base = Path(base)
        if (base / name).is_file():
            return base / name
        try:
            hits = dlss.find_dlss_files(base, names=(name,))
        except Exception:
            hits = []
        if hits:
            return base / hits[0]
    return None


def _is_win64_dll(p: Path, least: int = 200_000) -> tuple[bool, str]:
    """(ok, why not) - is this really a 64-bit Windows DLL of a sane size?

    A plain download can come back as an error page, an HTML redirect, or
    half a file from a connection that dropped; a zip at least fails loudly
    when it is not a zip. This runs before anything overwrites a runtime the
    game needs to start.
    """
    try:
        size = p.stat().st_size
    except OSError as e:
        return False, f"the download could not be read ({e})"
    if size < least:
        return False, (f"the download is only {size} bytes, which is not a "
                       f"runtime - usually an error page from the download "
                       f"server, or a connection that was cut")
    try:
        with open(p, "rb") as f:
            head = f.read(0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                return False, "the download is not a Windows binary at all"
            (off,) = struct.unpack_from("<I", head, 0x3C)
            f.seek(off)
            sig = f.read(6)
            if len(sig) != 6 or sig[:4] != b"PE\0\0":
                return False, "the download is not a Windows binary at all"
            if struct.unpack_from("<H", sig, 4)[0] != 0x8664:
                return False, "the download is not an x64 binary"
    except OSError as e:
        return False, f"the download could not be read ({e})"
    return True, ""


def _place_family(entries: list, want, dest: Path, rep, root: Path, dl,
                  member: str, prefix: str, log) -> dict:
    """Install the chosen build, falling back to the next source if need be.

    The publisher's own builds come first in every family, and they are
    fetched from a different host (raw.githubusercontent.com) than the
    mirror. That host being blocked - a corporate proxy, a DNS filter -
    would otherwise abort the install after ReShade and the add-ons are
    already on disk. So when the chosen entry cannot be downloaded at all,
    the next one in the family is tried, and the log says what happened.
    """
    e = sources.pick(entries, want)
    tries = [e] + [x for x in entries if x is not e][:1]
    last = None
    first_err = None
    for i, entry in enumerate(tries):
        name = f"{prefix}-{entry['label']}" + (".dll" if entry.get("raw")
                                               else ".zip")
        try:
            _place_entry(entry, dest, rep, root, dl, member, name)
            if i:
                log(f"      {member}: {tries[0]['label']} could not be "
                    f"downloaded ({last}) - used {entry['label']} instead")
            return entry
        except InstallError as ex:
            # A proxy or a DNS filter blocking the download host answers 200
            # with an HTML page, which arrives here as "not a Windows binary
            # at all" - the exact failure this fallback exists for. So it is
            # tried like any other, and only the last one is raised.
            last = ex
            if first_err is None:
                first_err = ex
            if i == len(tries) - 1:
                if i:
                    # Both failed. The publisher's reason is the useful one
                    # - a proxy serving an HTML page, say - and only the
                    # fallback's is raised, so say the first one out loud
                    # before it is lost. `last` is this iteration's error by
                    # now, which is why it is kept separately.
                    log(f"      the first source said: {first_err}")
                raise
        except Exception as ex:
            last = ex
            if first_err is None:
                first_err = ex
            if i == len(tries) - 1:
                if i:
                    log(f"      the first source said: {first_err}")
                raise
    raise last if last else RuntimeError(f"{member}: nothing to install")


def _place_entry(e: dict, dest: Path, rep, root: Path, dl, member: str,
                 cache_name: str) -> None:
    """Put one catalog entry on disk, archive or plain file.

    NVIDIA publishes its runtimes as the DLL itself; the community mirror
    publishes zips. Both arrive here so the callers do not have to know
    which is which - and a plain file is checked before it is allowed to
    overwrite anything.
    """
    got = dl(e["url"], cache_name)
    if e.get("raw"):
        ok, why = _is_win64_dll(got)
        if not ok:
            try:
                got.unlink()          # never serve it from the cache again
            except OSError:
                pass
            raise InstallError(
                f"{member} was not installed: {why}.\n\n"
                f"That file was not written and the download was thrown "
                f"away. Try again - it is fetched fresh - or pick another "
                f"build.")
    _backup(dest, rep, root)
    if e.get("raw"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        # All or nothing: a copy interrupted half way through would leave a
        # truncated runtime where the game expects a whole one.
        part = dest.with_name(dest.name + ".part")
        try:
            shutil.copyfile(got, part)
            os.replace(part, dest)
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        _extract(got, member, dest, rep, root)
    try:
        rep.written.append(str(dest.relative_to(root)))
    except ValueError:
        # A runtime the game keeps above the install folder - the nested
        # executable layout _find_runtime searches for. _backup() and
        # _copy() record the absolute path in the same case, and uninstall
        # accepts it as long as it resolves inside the game.
        rep.written.append(str(dest))


def _extract(zpath: Path, member: str, dst: Path, rep: Report, root: Path) -> None:
    """Extract one member, preserving anything already at the destination."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    _backup(dst, rep, root)
    net.extract_one(zpath, member, dst)


def _copy(src: Path, dst: Path, rep: Report, root: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _backup(dst, rep, root)          # never overwrite anything unrecoverably
    shutil.copyfile(src, dst)
    try:
        rep.written.append(str(dst.relative_to(root)))
    except ValueError:
        rep.written.append(str(dst))


# ---------------------------------------------------------------- plan

def _opti_needs_dlss(opt: Options) -> bool:
    """OptiScaler running DLSS in place of the game's FSR/XeSS: the game
    ships no nvngx_dlss.dll, so the route has to bring one."""
    return opt.path == OPTI and bool(opt.upscaler) and not opt.native_dlss


def remix_runtime(g: games.Game) -> Path | None:
    """The game's `.trex` folder, looked for beside the executable first."""
    return remix.find_runtime(g.install_dir) or remix.find_runtime(g.folder)


def remix_state(g: games.Game, opt: Options) -> tuple[Path | None, str, bool]:
    """(.trex folder, fork flavour, are we replacing the runtime?).

    The flavour is read out of the runtime binary, so this is the one place
    that decides whether the route can work as-is or needs the swap.
    """
    trex = remix_runtime(g)
    if trex is None:
        return None, "", False
    flavour = remix.runtime_flavour(trex)
    if not opt.remix_swap:
        return trex, flavour, False
    # A runtime this tool swapped in has the pass, so "has none" alone would
    # skip it on every later install: "update all" cleared the update mark,
    # kept the old runtime, and dropped the swap from the record. One we put
    # there is ours to renew; a mod's own runtime with the pass is never
    # replaced.
    return trex, flavour, not flavour or _remix_runtime_is_ours(g, trex)


def _runtime_stamp(p: Path) -> list | None:
    """Size and modification time: cheap, and enough to see a file replaced.

    A hash would read 150-230 MB, and this is asked on the Tk thread when a
    route is drawn.
    """
    try:
        st = p.stat()
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


def _remix_swap_record(g: games.Game) -> dict:
    try:
        prev = _previous_manifest(g.install_dir) or {}
        comp = prev.get("components") or {}
        return comp if comp.get("remix_runtime") else {}
    except Exception:
        return {}


def _remix_runtime_is_ours(g: games.Game, trex: Path) -> bool:
    """Is the runtime in .trex the one this tool swapped in?

    The record alone is not enough: a mod can update its own runtime after
    our swap, and the record would still say "ours" - "update all" would
    then replace the mod's new runtime. So the swapped file's size and time
    are recorded, and it is ours while it still matches. A record from
    before that stamp existed has only the record to go on.
    """
    rec = _remix_swap_record(g)
    if not rec:
        return False
    return not _remix_runtime_replaced(g, trex)


def _remix_runtime_replaced(g: games.Game, trex: Path) -> bool:
    """We swapped it once, and the mod has put its own runtime there since.

    By size alone: the time moves without the file changing (FAT32 across
    daylight saving, a folder moved or restored), and a wrong "replaced"
    costs the backup of the mod's original. Two different 150-230 MB
    runtimes of the same size to the byte is not the case to design for.
    """
    rec = _remix_swap_record(g)
    want = rec.get("remix_runtime_stamp")
    # A stamp that is not [size, time] - an edited or damaged record - is no
    # stamp at all, as in a record from before stamps.
    if not (isinstance(want, list) and want and isinstance(want[0], int)):
        return False
    now = _runtime_stamp(trex / remix.RUNTIME_DLL)
    return now is not None and now[0] != want[0]


def plan(g: games.Game, opt: Options) -> list[str]:
    """The steps for this game on the selected path.

    The three paths need very different things. Only the feeder builds a
    synthetic contract out of ReShade shaders, so only it needs the shader
    headers, the .fx and a motion-vector provider.
    """
    if opt.path == ROUTE_REMIX:
        # Nothing goes beside the executable, and no ReShade at all: the
        # runtime the mod already installed does the work, so the whole
        # install is one runtime file, one config line - unless the runtime
        # has no neural pass and the user opted into replacing it.
        _trex, _flav, swap = remix_state(g, opt)
        return (["RTX Remix runtime (DLSS 5 build)"] if swap else []) \
            + [DLSSNR, remix.CONF]

    steps: list[str] = []
    if reengine.detected(g.install_dir):
        steps.append("REFramework (RE Engine - so ReShade survives)")
    if uses_dxvk(g, opt):
        steps.append(f"DXVK ({g.api} -> Vulkan)")
        g = via_dxvk(g, opt)
    if opt.path == OPTI:
        # OptiScaler replaces ReShade entirely - it is the proxy DLL itself.
        # A game with DLSS keeps its own nvngx_dlss.dll; one whose FSR/XeSS
        # calls are being redirected has none, so one goes in. REFramework
        # still has to go in first on an RE Engine game - OptiScaler is a
        # proxy DLL too, and RE Engine's tamper checks do not care which one.
        return (steps[:1] if steps[:1] and steps[0].startswith("REFramework") else []) \
            + [f"OptiScaler ({optiscaler.BUILDS.get(opt.opti_build, optiscaler.BUILDS['']).split('  -  ')[0]})",
               "nvngx_dlssnr.dll"] \
            + (["nvngx_dlss.dll"] if _opti_needs_dlss(opt) else []) \
            + ["OptiScaler configuration"]
    steps.append("ReShade (Vulkan layer)" if g.api == "Vulkan" else "ReShade")

    if opt.path == UPSTREAM:
        # neural-upstream does the neural rendering itself, so no renodx
        # add-on beside it (two NGX hooks), and the game's nvngx_dlss.dll is
        # never replaced: its DLSS is what the network feeds.
        return steps + ["neural-upstream", "nvngx_dlssnr.dll",
                        *([DLSSD] if opt.dlssd else []),
                        "ReShade configuration"]

    if opt.path == STANDALONE:
        # The add-on brings its own feed and runs the network itself: no
        # renodx add-on (it would process the frame twice), but the shader
        # headers, its companion shader with VORT for motion vectors, and
        # the three NVIDIA runtimes it loads privately.
        return steps + ["ReShade shader headers", "standalone-dlssnr",
                        "VORT Motion (motion vectors)", "nvngx_dlssnr.dll",
                        "nvngx_dlss.dll", "nvngx_dlssg.dll",
                        *([DLSSD] if opt.dlssd else []),
                        "ReShade configuration"]

    if opt.path == FEEDER:
        steps.append("ReShade shader headers")
        steps.append("DLSS5-Feeder")
        # install() moves an OpenGL game's provider to VORT before it plans,
        # so the switch is already visible here.
        provider = 2 if g.api == "OpenGL" and opt.provider in (3, 4) else opt.provider
        if provider in (3, 4):
            steps.append("LumeniteFX (motion vectors)")
        elif provider == 2:
            steps.append("VORT Motion (motion vectors)")
    elif opt.path == BRIDGE:
        steps.append("dlss5-bridge")

    steps += ["DLSS 5 add-on (renodx-dlss SF)" if opt.path == ROUTE_RENODX
              else "DLSS 5 add-on (renodx)",
              "nvngx_dlssnr.dll", "nvngx_dlss.dll"] \
        + ([DLSSD] if opt.dlssd else [])
    if opt.path == FEEDER and g.bitness == 32:
        steps.append("host64 helper process")
    if opt.mfg and mfg.applies(gpu.detect()[1], g.api, g.install_dir, g.folder)[0] \
            and (mfg.loader_name(g.exe,
                                 {refw.DINPUT8} if reengine.detected(g.install_dir) else set(),
                                 g.install_dir)
                 or mfg.existing_plugins(g.install_dir)):
        steps.append("RTX 40 multi-frame generation")
    steps.append("ReShade configuration")
    if opt.path == FEEDER:
        steps.append("dlss5-feed.cfg")
    elif opt.path == BRIDGE:
        steps.append("dlss5-bridge.cfg")
    return steps


# ---------------------------------------------------------------- preview

@dataclass
class Preview:
    """What an install would do to this folder, worked out without doing it.

    Every list holds relative paths (or one-line descriptions where a path
    does not exist, such as the Vulkan layer). `blockers` non-empty means
    install() would raise before writing anything.
    """
    steps: list[str] = field(default_factory=list)      # from plan()
    writes: list[str] = field(default_factory=list)     # created / overwritten
    backups: list[str] = field(default_factory=list)    # kept as *.backup first
    removes: list[str] = field(default_factory=list)    # cleaned up beforehand
    outside: list[str] = field(default_factory=list)    # written outside the folder
    blockers: list[str] = field(default_factory=list)   # would make install() raise
    warnings: list[str] = field(default_factory=list)   # anti-cheat, reliability


def _cached_zip_members(pattern: str) -> list[str] | None:
    """Member names of the newest download matching `pattern` in the cache,
    or None when nothing is cached. Read-only: the preview may look at what an
    earlier install fetched, but it never fetches anything itself."""
    try:
        zips = sorted(net.CACHE.glob(pattern), key=lambda p: p.stat().st_mtime)
        if not zips:
            return None
        import zipfile
        with zipfile.ZipFile(zips[-1]) as z:
            return [n for n in z.namelist() if not n.endswith("/")]
    except Exception:
        return None


def preview(g: games.Game, opt: Options) -> Preview:
    """Everything install() would write, back up, remove or touch outside
    the game folder - without a single network request or write.

    People want to know "did this delete my files?" BEFORE they press
    Install, so this mirrors install() step by step and reports its
    decisions on the folder as it is now. Anything whose exact file list
    only a download reveals (LumeniteFX shaders, the OptiScaler package) is
    read from the download cache when an earlier install left one there,
    and described by pattern otherwise.
    """
    pv = Preview()
    root = g.install_dir
    if g.exe_warning:
        pv.warnings.append(games.XBOX_EXE_CHOSEN)

    ok, why = check_supported(g)
    if not ok:
        pv.blockers.append(why)
    # Same hard rule install() enforces: a Remix game only ever takes the
    # remix route. Without this the preview would describe a plan (DXVK,
    # ReShade...) that install() actually refuses outright.
    if opt.path != ROUTE_REMIX and remix.is_remix_game(root):
        pv.blockers.append(
            "This game has an RTX Remix mod installed (its .trex runtime is "
            "in the folder). Only the remix route works here - every other "
            "route installs ReShade, which crashes a Remix game before it "
            "draws a frame, and on DirectX 9 it would write over the Remix "
            "runtime itself. Choose the remix route, or uninstall the "
            "Remix mod first.")
    pv.steps = plan(g, opt)
    x64 = g.bitness == 64
    if opt.path == FEEDER and g.api == "OpenGL" and opt.provider in (3, 4):
        opt = replace(opt, provider=2)       # as install() does: VORT on GL
    dxvk_from = g.api if uses_dxvk(g, opt) else ""
    g = via_dxvk(g, opt)
    proxy = _proxy_name(g.api, opt.reshade_proxy)

    def rel(*parts) -> str:
        return "/".join(str(p).replace("\\", "/") for p in parts if str(p))

    def add(lst: list[str], item: str) -> None:
        if item not in lst:
            lst.append(item)

    # Blockers: the checks preflight() and install() make, minus the write
    # probe - os.access instead, because a preview must not create files.
    if not games._isdir(root):
        pv.blockers.append(f"{root} does not exist.")
        return pv
    if not os.access(root, os.W_OK):
        pv.blockers.append(f"No permission to write into {root} - close the "
                           f"game if it is running, or run as administrator.")
    if g.exe and g.exe.name.lower() in _running_processes():
        pv.blockers.append(f"{g.exe.name} is running. Close the game first.")

    # Warnings, worded as install() records them.
    trex, flavour, swap = remix_state(g, opt) if opt.path == ROUTE_REMIX \
        else (None, "", False)
    level, why_rel = reliability(g, opt.path,
                                 "" if opt.native_dlss else opt.upscaler,
                                 remix_swap=swap)
    if level != STABLE:
        pv.warnings.append(f"{level}: {why_rel}")
    hw = hook_warning(root, opt.path)
    if hw:
        pv.warnings.append(hw)
    ac = anticheat.detect(root, g.folder)
    if ac.present:
        pv.warnings.append(
            f"{ac.summary} detected ({', '.join(ac.evidence)}). ReShade "
            f"add-ons and anti-cheat do not coexist: expect the game not to "
            f"start, or nothing to happen, or a ban. Do not use this online.")
    if opt.path != ROUTE_REMIX and reengine.detected(root):
        pv.warnings.append(reengine.message())

    preinstalled = _previously_ours(root)

    # What the previous-route uninstall takes away first. Worked out before
    # anything else: a file it removes will not be there to back up later.
    gone: set[str] = set()
    previous = _previous_route(root)
    if previous and previous != opt.path:
        data = _previous_manifest(root) or {}
        files = [str(f).replace("\\", "/")
                 for f in (data.get("files") or data.get("dosyalar") or [])]
        suffixes = (BACKUP_SUFFIX,) + LEGACY_BACKUP_SUFFIXES
        restored = set()
        for f in files:
            s = next((s for s in suffixes if f.endswith(s)), None)
            if s and (root / f).is_file():
                restored.add(f[:-len(s)])
                add(pv.removes, f"{f[:-len(s)]} (put back from its backup)")
                gone.add(f)
        for f in files:
            if any(f.endswith(s) for s in suffixes) or f in restored:
                continue
            if (root / f).is_file():
                add(pv.removes, f"{f} (previous {previous} install)")
                gone.add(f)
        if (root / HOST_DIR).is_dir():
            add(pv.removes, f"{HOST_DIR}/ (previous {previous} install)")
            gone.update(rel(p.relative_to(root))
                        for p in (root / HOST_DIR).rglob("*"))
        for name in RUNTIME_ARTIFACTS:
            if (root / name).is_file():
                add(pv.removes, f"{name} (log)")
                gone.add(name)

    def present(r: str) -> bool:
        """Will this file still be there when install() reaches it?"""
        return (root / r).is_file() and r not in gone

    def backup(r: str) -> None:
        """_backup's decision: the game's own file is kept, ours is not."""
        if present(r) and r not in preinstalled:
            add(pv.backups, r)

    def plain_backup(r: str) -> None:
        """dxvk keeps a copy whenever no backup exists yet."""
        if present(r) and not (root / (r + BACKUP_SUFFIX)).exists():
            add(pv.backups, r)

    def write(r: str, keep: bool = True) -> None:
        if keep:
            backup(r)
        add(pv.writes, r)

    # --- the RTX Remix route: everything goes inside the mod's .trex -------
    if opt.path == ROUTE_REMIX:
        if trex is None:
            pv.blockers.append(
                "No RTX Remix runtime (a '.trex' folder) was found in this "
                "game. This route is only for a game that already has an RTX "
                "Remix mod installed.")
            return pv
        try:
            tdir = rel(trex.relative_to(root))
        except ValueError:
            tdir = rel(trex)
        if not flavour and not swap:
            pv.blockers.append(
                "The RTX Remix runtime installed here has no DLSS 5 neural "
                "pass (NVIDIA's own runtime has none). Tick 'swap the Remix "
                "runtime' to replace it with a community build that does - "
                "experimental: it also replaces any game-specific fixes the "
                "mod's own runtime carries.")
            return pv
        # A ReShade proxy left in the folder kills the game outright: a
        # 32-bit ReShade dxgi.dll took GTAIV.exe down with 0xc0000005 before
        # the window appeared. Every one goes, whatever name it is under.
        for name in RESHADE_PROXIES:
            if not present(name) or not _is_reshade(root / name):
                continue
            if name in preinstalled:
                add(pv.writes, name + ORPHAN_SUFFIX)
                add(pv.removes, f"{name} (our ReShade - it crashes a Remix game)")
            else:
                backup(name)
                add(pv.removes, f"{name} (a ReShade proxy - it crashes a "
                                f"Remix game)")
        if swap:
            write(rel(tdir, REMIX_RUNTIME))
            write(rel(tdir, REMIX_NVNGX))
            pv.warnings.append(
                "the mod's own Remix runtime is being replaced; it is backed "
                "up and 'Uninstall' puts it back")
        write(rel(tdir, DLSSNR))
        try:
            crel = rel(remix.conf_path(root, trex).relative_to(root))
        except ValueError:
            crel = rel(remix.conf_path(root, trex))
        key = remix.enable_key(flavour or remix.NEURAL)
        add(pv.writes, f"{crel} ({key} = True - this one line, nothing else "
                       f"in the file is touched)")
        for r in sorted(preinstalled):
            if present(r):
                add(pv.writes, r)
        write(MANIFEST, keep=False)
        return pv

    # Another injector already under the proxy name?
    if opt.path != OPTI and proxy != VULKAN_LAYER:
        existing = root / proxy
        if existing.is_file() and not _is_reshade(existing):
            if optiscaler.is_optiscaler(existing):
                backup(proxy)
                add(pv.removes, f"{proxy} (an OptiScaler installed by hand)")
                for extra in (optiscaler.FORWARDER, optiscaler.INI):
                    if (root / extra).is_file():
                        backup(extra)
                        add(pv.removes, f"{extra} (OptiScaler, installed by hand)")
            else:
                pv.blockers.append(
                    f"{proxy} already exists but is not ReShade (DXVK, Special "
                    f"K or another injector?). Remove it first, then try again.")

    # A ReShade under another name.
    if opt.path != OPTI:
        for name in RESHADE_PROXIES:
            if name == proxy or not present(name) or not _is_reshade(root / name):
                continue
            if name in preinstalled:
                add(pv.writes, name + ORPHAN_SUFFIX)
                add(pv.removes, f"{name} (our ReShade under another name)")
            else:
                backup(name)
                add(pv.removes, f"{name} (a second ReShade copy)")

    # Add-ons of another route.
    for route, name in _foreign_addons(opt.path):
        if not present(name):
            continue
        if name in preinstalled:
            add(pv.writes, name + ORPHAN_SUFFIX)
        else:
            backup(name)
        add(pv.removes, f"{name} ({route} add-on)")

    # The game's too-old compiler goes aside; uninstall brings it back.
    if opt.path != OPTI:
        try:
            for f in root.iterdir():
                if f.is_file() and f.name.lower() in SIDELINE:
                    add(pv.backups, f"{f.name} -> {f.name}{SIDELINE_SUFFIX}")
        except OSError:
            pass

    # 0) REFramework (RE Engine games only - see reengine.py/refw.py)
    if reengine.detected(root):
        plain_backup(refw.DINPUT8)
        write(refw.DINPUT8, keep=False)

    # 0b) DXVK
    if dxvk_from:
        for name in dxvk.files_for(dxvk_from) or dxvk.FILES:
            plain_backup(name)
            write(name, keep=False)

    # OptiScaler: the whole route in one go.
    if opt.path == OPTI:
        oproxy = opt.opti_proxy or optiscaler.suggest_proxy(root)
        for other in optiscaler.find_existing(root, ignore=oproxy):
            backup(other.name)
            add(pv.removes, f"{other.name} (another OptiScaler copy)")
        for old in optiscaler.find_legacy(root):
            backup(old.name)
            add(pv.removes, f"{old.name} (pre-0.9 OptiScaler leftover)")
        # Which archive this build installs, so the preview lists that one
        # and not whichever OptiScaler zip happens to be in the cache: with
        # two forks cached, the glob alone described the wrong package
        # (their file lists differ - docs, redistributables, weights).
        # A .7z cannot be listed at all, and neither can a build nobody has
        # downloaded yet; both fall through to naming the folders instead.
        arch = optiscaler.archive_name(opt.opti_build)
        members = (_cached_zip_members(arch)
                   if arch.lower().endswith(".zip") else None)
        if members:
            for m in members:
                if Path(m).name in optiscaler.SKIP:
                    continue
                write(oproxy if m == optiscaler.MAIN_DLL else m)
        else:
            write(oproxy)
            write(optiscaler.FORWARDER)
            write(optiscaler.INI)
            add(pv.writes, "OptiScaler/* and Licenses/* (the rest of the package)")
        write(DLSSNR)
        if _opti_needs_dlss(opt) and not (present(DLSS) and opt.keep_game_dlss
                                          and DLSS not in preinstalled):
            write(DLSS)
        for r in sorted(preinstalled):
            if present(r):
                add(pv.writes, r)
        write(MANIFEST, keep=False)
        return pv

    if getattr(g, "emu", None) is not None:
        pv.outside.append(f"{g.emu.name}: its own config is switched to the "
                          f"render backend ReShade can reach (backed up beside "
                          f"it; 'Uninstall' restores it)")
    # 1) ReShade
    if g.api == "Vulkan":
        found = vulkan.existing_registration()
        if found is not None and not vulkan.is_ours(found):
            pv.outside.append(f"reuses the existing ReShade Vulkan layer ({found})")
        else:
            pv.outside.append(
                f"Vulkan layer: {vulkan.LAYER_NAME} registered for this user "
                f"(files in {vulkan.layer_dir()}) - it loads into EVERY Vulkan "
                f"application until 'Uninstall' removes it")
            pv.warnings.append("the Vulkan layer is global; 'Uninstall' "
                               "removes it again")
    else:
        write(proxy)
    if opt.vr and x64:
        from . import openxr
        xr_found = openxr.existing_registration()
        if xr_found is not None and not openxr.is_ours(xr_found):
            pv.outside.append(f"reuses the existing ReShade OpenXR layer ({xr_found})")
        else:
            pv.outside.append(
                f"OpenXR layer (VR): {openxr.LAYER_NAME} registered for this "
                f"user (files in {openxr.layer_dir()}) - it loads into EVERY "
                f"OpenXR application until the last VR install is removed")
        pv.warnings.append("VR through OpenXR has not been tried with a "
                           "headset by the author; OpenXR games only")
    elif opt.vr:
        pv.warnings.append("VR: the OpenXR layer is 64-bit only - nothing is "
                           "registered for this 32-bit game")
    host = HOST_DIR
    if not x64 and opt.path == FEEDER:
        add(pv.writes, rel(host, "dxgi.dll"))

    # 2) the path-specific middle
    if opt.path == BRIDGE:
        write(BRIDGE_ADDON)
        if present("dlss5-dx11-bridge.addon64"):
            add(pv.removes, "dlss5-dx11-bridge.addon64 (older bridge, conflicts)")
    if opt.path == FEEDER:
        for h in sources.RESHADE_HEADERS:
            write(rel(SHADERS, h))
        write(FEEDER_ADDON64 if x64 else FEEDER_ADDON32)
        write(rel(SHADERS, FEEDER_FX))
        if not x64:
            add(pv.writes, rel(host, FEEDER_HOST))
        if opt.provider in (3, 4) and foreign_lumenite(root, _previously_ours(root)):
            pv.warnings.append("LumeniteFX is already installed here - your "
                               "copy is used, nothing duplicated")
        elif opt.provider in (3, 4):
            listed = False
            for m in _cached_zip_members("LumeniteFX-mainline.zip") or []:
                parts = m.split("/")[1:]        # drop the archive root
                if len(parts) not in (2, 3):
                    continue
                tail = parts[-1].lower()
                where = "/".join(parts[:-1]).lower()
                if where == "shaders" and tail.endswith(".fx"):
                    if tail != LUMENITE_PROVIDER_FX.get(opt.provider, "lumenite_kernel.fx").lower():
                        continue
                    add(pv.writes, rel(SHADERS, parts[-1]))
                elif where == "shaders/include" and tail.endswith(".fxh"):
                    add(pv.writes, rel(INCLUDE, parts[-1]))
                elif where == "textures" and tail.endswith(".png"):
                    add(pv.writes, rel(TEXTURES, parts[-1]))
                else:
                    continue
                listed = True
            if not listed:
                add(pv.writes, rel(SHADERS, LUMENITE_PROVIDER_FX.get(opt.provider, "lumenite_Kernel.fx")))
                add(pv.writes, rel(INCLUDE, "lumenite_*.fxh"))
                add(pv.writes, rel(TEXTURES, "lumenite_*.png"))
        elif opt.provider == 2 or g.api == "OpenGL":
            if not foreign_lumenite(root, preinstalled, marker=VORT_FX):
                add(pv.writes, rel(SHADERS, VORT_FX))
                add(pv.writes, rel(VORT_INCLUDE, "vort_*.fxh"))
                add(pv.writes, rel(TEXTURES, VORT_TEXTURE))

    # 5/6/7) DLSS parts: in host64/ on the 32-bit feeder path
    dlss_dir = "" if (x64 or opt.path != FEEDER) else host
    if opt.path == UPSTREAM:
        write(UPSTREAM_ADDON)
    elif opt.path == STANDALONE:
        for h in sources.RESHADE_HEADERS:
            write(rel(SHADERS, h))
        write(STANDALONE_ADDON)
        write(STANDALONE_BRIDGE)
        write(rel(SHADERS, STANDALONE_FX))
        if foreign_lumenite(root, preinstalled, marker=VORT_FX):
            pv.warnings.append("VORT is already installed here - your copy "
                               "is used, nothing duplicated")
        else:
            write(rel(SHADERS, VORT_FX))
            listed = False
            for m in _cached_zip_members(sources.VORT_ZIP_NAME) or []:
                parts = m.split("/")[1:]        # drop the archive root
                if (len(parts) == 3 and parts[0].lower() == "shaders"
                        and parts[1].lower() == "includes"
                        and parts[2].lower().endswith(".fxh")):
                    add(pv.writes, rel(VORT_INCLUDE, parts[2]))
                    listed = True
            if not listed:
                add(pv.writes, rel(VORT_INCLUDE, "vort_*.fxh"))
            write(rel(TEXTURES, VORT_TEXTURE))
    else:
        write(rel(dlss_dir, RENODX_SF if opt.path == ROUTE_RENODX else RENODX))
    write(rel(dlss_dir, DLSSNR))
    game_has = present(DLSS)
    if opt.path != UPSTREAM and not (x64 and game_has and opt.keep_game_dlss):
        write(rel(dlss_dir, DLSS))
    if opt.path == STANDALONE and not (present(DLSSG) and opt.keep_game_dlss):
        write(DLSSG)
    if opt.dlssd:
        rr = _find_runtime(root, DLSSD, g.folder)
        if rr is not None:
            try:
                write(str(rr.relative_to(root)))
            except ValueError:
                # The game keeps it above the install folder, which is what
                # pv.outside exists to say out loud.
                write(str(rr))
                pv.outside.append(
                    f"replaces the game's own {DLSSD} at {rr} - outside the "
                    f"install folder, backed up beside itself")

    # 8b) RTX 40 multi-frame generation, when it applies here
    if opt.mfg and mfg.applies(gpu.detect()[1], g.api, root, g.folder)[0]:
        lname = mfg.loader_name(g.exe,
                                {refw.DINPUT8} if reengine.detected(root) else set(),
                                root)
        plug = mfg.existing_plugins(root) if lname is None else None
        if lname is None and plug is None:
            pv.warnings.append(
                f"multi-frame generation will be skipped: {g.exe.name if g.exe else 'the executable'} "
                f"imports none of {', '.join(mfg.LOADER_NAMES)} that is free here, so the ASI "
                f"loader has no name it would be loaded under")
        elif lname is None:
            # Another mod's loader is already here and its name is not
            # ours to take: the unlock goes into its plugins folder (#137).
            for n in mfg.PLUGIN_FILES:
                write(f"{mfg.PLUGINS_DIR}/{n}", keep=False)
            write(mfg.UI_ADDON, keep=False)
        else:
            for n in mfg.FILES:
                write(n, keep=False)
            plain_backup(lname)
            write(lname, keep=False)
            write(lname[:-4] + ".ini")
    elif not opt.mfg:
        # The same list remove_leftovers and the manifest-less uninstall use:
        # an unlock that went into another loader's plugins folder is removed
        # from there too, and the preview has to say so.
        _pre_low = {p.lower() for p in preinstalled}
        # remove_leftovers returns early unless one of the unlock's own files
        # is on the record, so the loader only comes out with them. The
        # preview must promise exactly that and no more.
        _unlock_here = any(
            n.lower() in _pre_low for n in
            [*mfg.FILES] + [f"{mfg.PLUGINS_DIR}/{x}" for x in mfg.PLUGIN_FILES])
        for n in [*mfg.FILES] + [f"{mfg.PLUGINS_DIR}/{x}"
                                 for x in mfg.PLUGIN_FILES]:
            if n in preinstalled or n.lower() in _pre_low:
                add(pv.removes, f"{n} (multi-frame generation is off now)")
        # ...and the loader itself, which remove_leftovers also takes out -
        # with the file of theirs that was under that name going back. That
        # is the line a person reads the preview for.
        for n in mfg.LOADER_NAMES if _unlock_here else []:
            if n.lower() not in _pre_low:
                continue
            # One line, not two: the same name listed twice - once as a
            # removal and once as a restore - reads as two files.
            _back = (root / (n + mfg.BACKUP_SUFFIX)).is_file()
            add(pv.removes,
                f"{n} (the ASI loader, multi-frame generation is off now"
                + ("; your own file goes back in its place)" if _back else ")"))
            # remove_leftovers unlinks the loader's ini as well, and the
            # preview's ON branch writes it - so the OFF branch names it.
            _lini = n[:-4] + ".ini"
            if _lini.lower() in _pre_low:
                add(pv.removes, f"{_lini} (the loader's settings)")
    # A pinned feeder build older than the D3D10 relay is a blocker, and
    # needs no network to say so.
    if opt.path == FEEDER and g.api == "DX10" and opt.feeder_tag and \
            sources.feeder_key(opt.feeder_tag) < sources.feeder_key(sources.FEEDER_DX10_MIN):
        pv.blockers.append(f"DLSS5-Feeder {opt.feeder_tag} refuses Direct3D 10 games - "
                           f"pick {sources.FEEDER_DX10_MIN} or newer in 'feeder build'")

    # 8/9/10) host64, ReShade configuration, the cfg
    if not x64 and opt.path == FEEDER:
        add(pv.writes, rel(host, "ReShade.ini"))
    write("ReShade.ini")
    if opt.path == FEEDER:
        write("ReShadePreset.ini")
        write(feedcfg.NAME)
    elif opt.path == BRIDGE:
        write(feedcfg.BRIDGE_NAME)

    # The manifest carries forward what an earlier install of ours left and
    # this one does not touch, so those are recorded as well.
    for r in sorted(preinstalled):
        if present(r):
            add(pv.writes, r)
    write(MANIFEST, keep=False)
    return pv


def preview_lines(pv: Preview) -> list[str]:
    """The preview as short lines for the log widget."""
    out: list[str] = []
    for b in pv.blockers:
        out.append(f"cannot install: {b}")
    if pv.blockers:
        return out
    for w in pv.warnings:
        out.append(f"warning: {w}")
    if pv.removes:
        out.append(f"will clean up first: {', '.join(pv.removes)}")
    n = len(pv.writes)
    shown = ", ".join(pv.writes[:3]) + (", ..." if n > 3 else "")
    out.append(f"will write {n} file{'s' if n != 1 else ''} ({shown})")
    if pv.backups:
        out.append(f"will back up: {', '.join(pv.backups)}")
    else:
        out.append("nothing of yours is overwritten - no backups needed")
    if pv.outside:
        for o in pv.outside:
            out.append(f"outside: {o}")
    else:
        out.append("nothing is written outside this folder")
    return out


def _install_feeder_parts(g, opt, root: Path, host: Path, x64: bool,
                          rep: "Report", begin, dl, log) -> None:
    """Shader headers, the feeder add-on and the motion-vector provider.

    Only the feeder path needs any of this: it is the only one that builds a
    DLSS contract out of ReShade shaders. The native and bridge paths hook the
    game's real NGX calls instead.
    """
    begin("ReShade shader headers")
    for h in sources.RESHADE_HEADERS:
        dest = root / SHADERS / h
        dest.parent.mkdir(parents=True, exist_ok=True)
        _backup(dest, rep, root)
        dest.write_bytes(net.fetch_text(sources.RESHADE_HEADERS_BASE + h))
        rep.written.append(str(Path(SHADERS) / h))
    log(f"      {', '.join(sources.RESHADE_HEADERS)}")

    begin("DLSS5-Feeder")
    tag, assets = sources.resolve_feeder(prerelease=opt.feeder_prerelease,
                                         tag=opt.feeder_tag)
    log(f"      DLSS5-Feeder {tag}"
        + ("  (this exact build, as requested)" if opt.feeder_tag
           else "  (pre-release, as requested)" if opt.feeder_prerelease else ""))
    rep.components["feeder"] = tag
    if g.api == "DX10" and sources.feeder_key(tag) < sources.feeder_key(sources.FEEDER_DX10_MIN):
        raise InstallError(
            f"DLSS5-Feeder {tag} refuses Direct3D 10 games. Pick "
            f"{sources.FEEDER_DX10_MIN} or newer in 'feeder build' - that is "
            f"the first build with the D3D10 relay - and install again.")
    addon = FEEDER_ADDON64 if x64 else FEEDER_ADDON32
    needed = (addon, FEEDER_FX) + ((FEEDER_HOST,) if not x64 else ())
    # From 0.10.0 the feeder ships one zip instead of loose files.
    zurl = next((u for n, u in assets.items()
                 if n.lower().endswith(".zip") and "feeder" in n.lower()), None)
    zpath = None
    if zurl and any(n not in assets for n in needed):
        zpath = dl(zurl, f"{tag}-{zurl.rsplit('/', 1)[-1]}")
    for name in needed:
        dest = (root / SHADERS / name) if name.endswith(".fx") else \
               (host / name if name == FEEDER_HOST else root / name)
        if name in assets:
            f = dl(assets[name], f"{tag}-{name}")
            _copy(f, dest, rep, root)
        elif zpath is not None:
            _extract(zpath, name, dest, rep, root)
            rep.written.append(str(dest.relative_to(root)))
        else:
            raise InstallError(f"The DLSS5-Feeder release has no {name}.")
        log(f"      {dest.relative_to(root)}")

    if opt.provider in (3, 4):
        begin("LumeniteFX (motion vectors)")
        theirs = foreign_lumenite(root, rep.preinstalled)
        if theirs:
            # ReShade loads every .fx under reshade-shaders, whatever the
            # subfolder; a second lumenite_Kernel.fx beside theirs is two
            # copies of the same technique and a red error in the overlay.
            # Theirs wins - it is their install, and the technique name
            # in ReShade.ini matches by file name, not by path.
            rel_t = theirs.relative_to(root)
            rep.notes.append(f"LumeniteFX is already installed at {rel_t.parent} "
                             f"- your copy is used, nothing duplicated")
            rep.skipped.append("LumeniteFX (already installed)")
            log(f"      already installed at {rel_t.parent} - using your copy, "
                f"no duplicate")
            return
        z = dl(sources.LUMENITE_ZIP, "LumeniteFX-mainline.zip")
        want_fx = LUMENITE_PROVIDER_FX.get(opt.provider, "lumenite_Kernel.fx")
        w = net.extract_tree(z, "Shaders", str(SHADERS), root, only_ext=(".fx",),
                             only_names=(want_fx,))
        w += net.extract_tree(z, "Shaders/include", str(INCLUDE), root,
                              only_ext=(".fxh",))
        w += net.extract_tree(z, "Textures", str(TEXTURES), root,
                              only_ext=(".png",))
        # Effects an earlier install of ours dropped in are taken back out:
        # they were compiling for nothing.
        for old in sorted((root / SHADERS).glob("lumenite_*.fx")):
            rel_old = str(old.relative_to(root))
            if old.name.lower() != want_fx.lower() and rel_old.replace("\\", "/") in {
                    p.replace("\\", "/") for p in rep.preinstalled}:
                try:
                    old.unlink()
                    rep.notes.append(f"removed {old.name} - an effect the feed does "
                                     f"not use, left by an earlier install")
                except OSError:
                    pass
        for p_ in w:
            rep.written.append(str(p_.relative_to(root)))
        log(f"      {want_fx} + includes + texture ({len(w)} files) - only the "
            f"provider the feed reads, not the whole pack")
    elif opt.provider == 2:
        _install_vort(root, rep, dl, log, begin)


def _install_vort(root: Path, rep: "Report", dl, log, begin) -> None:
    """VORT Motion beside the game: the .fx, its includes and its texture.

    A copy the person installed themselves is used as it is - a second
    vort_Motion.fx under reshade-shaders is two techniques of one name and
    a red error in the overlay.
    """
    begin("VORT Motion (motion vectors)")
    theirs = foreign_lumenite(root, rep.preinstalled, marker=VORT_FX)
    if theirs:
        rel_t = theirs.relative_to(root)
        rep.notes.append(f"VORT is already installed at {rel_t.parent} "
                         f"- your copy is used, nothing duplicated")
        rep.skipped.append("VORT (already installed)")
        log(f"      already installed at {rel_t.parent} - using your "
            f"copy, no duplicate")
        return
    z = dl(sources.VORT_ZIP, sources.VORT_ZIP_NAME)
    _extract(z, "Shaders/" + VORT_FX, root / SHADERS / VORT_FX, rep, root)
    rep.written.append(str(SHADERS / VORT_FX))
    w = net.extract_tree(z, "Shaders/Includes", str(VORT_INCLUDE),
                         root, only_ext=(".fxh",))
    for p_ in w:
        rep.written.append(str(p_.relative_to(root)))
    _extract(z, "Textures/" + VORT_TEXTURE,
             root / TEXTURES / VORT_TEXTURE, rep, root)
    rep.written.append(str(TEXTURES / VORT_TEXTURE))
    log(f"      {VORT_FX} + {len(w)} includes + {VORT_TEXTURE} "
        f"(Vortigern, MIT)")


# ---------------------------------------------------------------- install

LUMENITE_MARKER = "lumenite_Kernel.fx"

# Only the motion-vector provider the feed will read, never the whole pack.
# LumeniteFX ships eight heavy effects (RTAO, SSSR, TRAA, bloom...) that the
# feed does not use; ReShade compiled them all at start-up, and on a 32-bit
# game behind a translation layer the compile stall was long enough for the game to
# crash on its own (Bayonetta, issue #2, confirmed from the dump by the
# feeder's author). The provider's own includes are small and all needed.
LUMENITE_PROVIDER_FX = {3: "lumenite_Kernel.fx", 4: "lumenite_QuantMotion.fx"}
LUMENITE_INCLUDES = ("lumenite_Projections.fxh", "lumenite_Helpers.fxh",
                     "lumenite_Compute.fxh", "lumenite_ColorManagement.fxh")
LUMENITE_TEXTURES = ("lumenite_bluenoise256.png",)


def foreign_lumenite(root: Path, preinstalled=(),
                     marker: str = LUMENITE_MARKER) -> Path | None:
    """A LumeniteFX the person installed themselves, anywhere under
    reshade-shaders - or None. A copy an earlier install of OURS wrote (in
    the manifest) does not count: that one is overwritten as usual.

    `marker` makes the same question askable of VORT (vort_Motion.fx): the
    standalone route ships it, and a second copy beside the person's own is
    two techniques of the same name and a red error in the overlay."""
    base = root / "reshade-shaders"
    try:
        hits = sorted(base.rglob(marker))
    except OSError:
        return None
    ours = {str(Path(p)).replace("\\", "/").lower() for p in preinstalled}
    for h in hits:
        try:
            rel = str(h.relative_to(root)).replace("\\", "/").lower()
        except ValueError:
            continue
        if rel in ours:
            continue
        if h.parent == root / SHADERS and not preinstalled:
            # Same place we would write to, and no record of ours: theirs.
            return h
        if h.parent != root / SHADERS:
            return h
    return None


def _previous_manifest(root: Path) -> dict | None:
    """The install record already in this folder, ours or an older release's."""
    for name in (MANIFEST,) + LEGACY_MANIFESTS:
        p = root / name
        if not p.is_file():
            continue
        try:
            return json.loads(p.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _previous_route(root: Path) -> str | None:
    """Which route is recorded as installed here, if any."""
    data = _previous_manifest(root)
    if data is None:
        return None
    # v1.0-v1.2 wrote no route at all; everything then was the feeder.
    return data.get("path") or FEEDER


def _previously_ours(root: Path) -> set:
    """Relative paths an earlier install of ours wrote here.

    Read before anything is touched, because these must be overwritten rather
    than "preserved" - preserving one turns uninstall into a reinstall.
    """
    data = _previous_manifest(root) or {}
    files = data.get("files") or data.get("dosyalar") or []
    out = set()
    for f in files:
        f = str(f).replace("\\", "/")
        if any(f.endswith(s) for s in (BACKUP_SUFFIX,) + LEGACY_BACKUP_SUFFIXES):
            continue
        out.add(f)
    return out


# Every add-on this tool ever installs, and the route each belongs to.
# ReShade loads EVERY .addon64 in the folder, so two of these present at once
# means two of them try to establish a DLSS contract in the same process.
# Only the add-ons themselves: a stray .cfg conflicts with nothing, and
# removing one would be taking away a file that may well be the user's.
# The standalone route's nvngx.dll is not an add-on, but NGX loads it from
# the game folder ahead of the driver's own - as much a hook as any of these.
ROUTE_ADDONS = {
    FEEDER: (FEEDER_ADDON64, FEEDER_ADDON32),
    BRIDGE: (BRIDGE_ADDON,),
    ROUTE_RENODX: (RENODX_SF,),
    UPSTREAM: (UPSTREAM_ADDON,),
    STANDALONE: (STANDALONE_ADDON, STANDALONE_BRIDGE),
}


def _foreign_addons(keep: str) -> list[tuple[str, str]]:
    """(route, filename) of every add-on that must not sit beside `keep`."""
    out = [(r, n) for r, names in ROUTE_ADDONS.items() if r != keep for n in names]
    # renodx-dlss5 is shared by the native, bridge and feeder routes, so it is
    # not in the table - but it, ShortFuse's build and neural-upstream all
    # hook NGX, and two loaded together fight over the same entry points.
    # The standalone add-on runs the network itself; renodx-dlss5 beside it
    # would process the frame twice.
    if keep in (ROUTE_RENODX, UPSTREAM, STANDALONE, ROUTE_REMIX):
        out.append((NATIVE, RENODX))
    return out


def _clear_stale_reshade(root: Path, keep: str, rep: Report, log) -> None:
    """Move every ReShade proxy that is not the one being installed out of
    ReShade's reach. Ours go aside as orphans (uninstall deletes them); one we
    did not record is backed up first, so uninstall puts it back.

    `keep` = "" takes ALL of them out, which is what the REMIX route needs:
    a ReShade proxy of any name crashes a Remix game before it draws.

    d3d9.dll is in RESHADE_PROXIES and, in a Remix game, d3d9.dll IS the
    Remix runtime stub. _is_reshade() is consulted first for exactly that
    reason and the stub fails it (no "ReShade" string, and under a megabyte),
    so the runtime is never mistaken for a stale ReShade copy - but the check
    is spelled out here too, because getting it wrong deletes the mod.
    """
    for name in RESHADE_PROXIES:
        if name == keep:
            continue
        p = root / name
        if not _is_reshade(p):
            continue
        if name.lower() == "d3d9.dll" and remix.is_remix_game(root):
            continue
        try:
            if name in rep.preinstalled:
                aside = p.with_name(name + ORPHAN_SUFFIX)
                if aside.exists():
                    p.unlink()
                else:
                    p.rename(aside)
                    rep.written.append(aside.name)
            else:
                _backup(p, rep, root)
                p.unlink()
        except OSError as e:
            log(f"      could not move {name} aside ({e}) - if the game will "
                f"not start, remove it by hand")
            continue
        rep.notes.append(f"moved aside a second ReShade copy: {name}")
        log(f"      moved {name} out of the way - a second ReShade under "
            f"another name would stop the game from starting")


def _purge_foreign_addons(root: Path, keep: str, rep: Report, log) -> None:
    """Remove add-ons belonging to a route we are not installing.

    Uninstalling the recorded route handles the ordinary case, but only when
    the manifest is accurate. An install interrupted half way, a manifest
    written by a release that did not record the route, or a folder set up
    twice can all leave an add-on behind that nothing knows about - and
    ReShade will still load it.

    Seen in the wild: MGS V had dlss5-bridge.addon64 recorded and
    dlss5-feed.addon64 orphaned beside it. Both registered, both tried to
    build a contract, and the game exited before it ever created a swapchain.
    """
    for route, name in _foreign_addons(keep):
        if True:
            p = root / name
            if not p.is_file():
                continue
            ours = name in rep.preinstalled
            try:
                if ours:
                    # Ours, from an install we recorded: just take it away.
                    # A backup would make uninstall restore the conflict.
                    aside = p.with_name(p.name + ORPHAN_SUFFIX)
                    if aside.exists():
                        p.unlink()
                    else:
                        p.rename(aside)
                        rep.written.append(aside.name)
                else:
                    # Might be the user's own build. Preserve it the normal
                    # way so uninstall puts it back, then move it out of
                    # ReShade's reach for now.
                    _backup(p, rep, root)
                    p.unlink()
                rep.notes.append(f"moved aside an orphaned {route} add-on: {name}")
                log(f"      moved {name} out of the way - it is a {route} "
                    f"add-on and ReShade would load it alongside this one")
            except OSError:
                log(f"      WARNING: {name} belongs to the {route} route and "
                    f"could not be moved; the two will conflict")


def _write_manifest(root: Path, g: games.Game, opt: Options, rep: Report,
                    proxy: str, level: str, complete: bool) -> None:
    """Record what was written.

    Also written when an install FAILS part way: without it the orphaned files
    could not be cleaned up afterwards.
    """
    # Carry forward what an earlier install left that this one did not touch.
    # Without this the record only covers the LAST install, so a file written
    # the first time and merely left alone the second - nvngx_dlss.dll, say -
    # was orphaned and no uninstall could ever remove it.
    # Compared with one separator: _copy records "a\b", the old record
    # read back as "a/b", and the same file went into the record twice.
    have = {w.replace("\\", "/") for w in rep.written}
    for rel in sorted(rep.preinstalled):
        if rel.replace("\\", "/") in have:
            continue
        if (root / rel).exists():
            rep.written.append(rel)
    # And the game's original beside any file on the record: whichever path
    # skipped _backup this time (a file of ours from last time, a step not
    # taken), the backup an earlier install made stays on the record, so
    # uninstall puts the original back. Losing it here is how a reinstall
    # left the swapped Remix runtime unrestored.
    have = {w.replace("\\", "/") for w in rep.written}
    for w in list(rep.written):
        if any(w.endswith(s) for s in (BACKUP_SUFFIX,) + LEGACY_BACKUP_SUFFIXES):
            continue
        b = w + BACKUP_SUFFIX
        if b.replace("\\", "/") not in have and (root / b).is_file():
            rep.written.append(b)
            have.add(b.replace("\\", "/"))

    try:
        _write_atomic(root / MANIFEST, json.dumps({
            "version": 1,
            "complete": complete,
            "exe": g.exe.name if g.exe else None,
            "bitness": g.bitness,
            "api": g.api,
            "proxy": proxy,
            "opti_build": opt.opti_build if opt.path == OPTI else "",
            "provider": opt.provider,
            "path": opt.path,
            "reliability": level,
            "files": rep.written,
            "skipped": rep.skipped,
            "notes": rep.notes,
            "warnings": rep.warnings,
            "feed_cfg": opt.feed,
            "nr": opt.nr,
            "fg": opt.fg,
            "mfg": opt.mfg,
            "vr": bool(opt.vr) and opt.path not in (OPTI, ROUTE_REMIX),
            "native_dlss": opt.native_dlss,
            "upscaler": opt.upscaler,
            "keep_game_dlss": opt.keep_game_dlss,
            "feeder_prerelease": opt.feeder_prerelease,
            "feeder_tag": opt.feeder_tag,
            "dxvk": rep.components.get("dxvk"),
            "components": rep.components,
            "kind": g.kind,
            "sidelined": rep.sidelined,
            "remix": rep.remix,
        }, ensure_ascii=False, indent=2))
    except OSError:
        pass


def _write_atomic(path: Path, text: str) -> None:
    """Write beside, then replace: a failed write leaves the old file whole.

    write_text() empties the file first. On a full drive that lost the
    previous install's record - the list uninstall reads - and the note
    saying the drive was full was never written either.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf8")
        # An antivirus or the indexer holding the old file for a moment
        # makes the replace fail; a moment later it goes through.
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def options_from_manifest(root: Path) -> Options | None:
    """Rebuild the choices an earlier install was made with.

    This is what "update" means: the same route, provider, dials and
    add-on family, with every component fetched fresh. Versions are NOT
    pinned to what was installed - that is the point.
    """
    data = _previous_manifest(root)
    if not data:
        return None
    path = data.get("path") or FEEDER
    if path not in (NATIVE, BRIDGE, FEEDER, OPTI, ROUTE_RENODX, UPSTREAM,
                    STANDALONE, ROUTE_REMIX):
        return None
    upscaler = str(data.get("upscaler") or "")
    return Options(
        upscaler=upscaler,
        provider=int(data.get("provider") or 3),
        feed=dict(data.get("feed_cfg") or {}),
        nr=dict(data.get("nr") or {}),
        fg=bool(data.get("fg", False)),
        mfg=bool(data.get("mfg", False)),
        vr=bool(data.get("vr", False)),
        path=path,
        keep_game_dlss=bool(data.get("keep_game_dlss", True)),
        feeder_prerelease=bool(data.get("feeder_prerelease", False)),
        feeder_tag=str(data.get("feeder_tag") or ""),
        # OptiScaler used to imply the game's own DLSS; with an upscaler
        # recorded it is OptiScaler's DLSS in place of the game's FSR/XeSS.
        native_dlss=(path in (NATIVE, UPSTREAM) or (path == OPTI and not upscaler)
                     or bool(data.get("native_dlss", False))),
        opti_proxy=(data.get("proxy") or "") if path == OPTI else "",
        opti_build=str(data.get("opti_build") or "") if path == OPTI else "",
        # A runtime we swapped last time must be swapped again on an update,
        # or the update would put the mod's neural-pass-less runtime back.
        remix_swap=bool((data.get("components") or {}).get("remix_runtime")),
        dlssd=str((data.get("components") or {}).get("dlssd") or ""),
    )


def _running_processes() -> set[str]:
    """Lower-cased names of running executables, best effort."""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=0x08000000).stdout
        return {line.split('","')[0].lstrip('"').lower()
                for line in out.splitlines() if line.startswith('"')}
    except Exception:
        return set()


def preflight(g: games.Game) -> None:
    """Fail early and clearly instead of part way through with a traceback.

    A half-written install leaves the game in a worse state than not starting,
    so the two things that actually stop us - the folder not being writable
    and the game holding its files open - are checked up front.
    """
    root = g.install_dir
    if not games._isdir(root):
        raise InstallError(f"{root} does not exist.")

    try:
        with tempfile.NamedTemporaryFile(prefix=".dlss5-autopilot-write-test-",
                                         dir=root) as probe:
            probe.write(b"x")
    except PermissionError as e:
        if games.is_locked_store_path(root):
            # Xbox / Game Pass: the folder is owned by the system, and
            # elevation does not help - the Xbox app has the switch for it.
            from . import log
            log.write(f"preflight: cannot write into {root}: {e}", "warn")
            raise InstallError(
                f"No permission to write into:\n{root}\n\n"
                f"{games.XBOX_HINT}") from None
        raise InstallError(
            f"No permission to write into:\n{root}\n\n"
            f"Close the game if it is running, then try again. If that is not "
            f"it, right-click dlss5-autopilot.exe and choose 'Run as "
            f"administrator' - some games installed outside Steam or Epic sit "
            f"in folders only an administrator can write to.") from None
    except OSError as e:
        if net.is_disk_full(e):
            raise InstallError(net.disk_full_message(e, root, net.CACHE)) from None
        raise InstallError(f"Cannot write into {root}: {e}") from None

    if g.exe and g.exe.name.lower() in _running_processes():
        raise InstallError(
            f"{g.exe.name} is running. Close the game first - Windows will not "
            f"let anything replace files a running program has open, and a "
            f"half-finished install is worse than none.")

def _sideline(root: Path, rep: Report, log) -> None:
    """Move a game-shipped file that breaks the neural pass out of the way.

    Only when the file is really the game's: one a previous install of ours
    already moved has the suffix and is left alone. The rename is recorded
    under its own manifest key, never in `files`, because uninstall deletes
    everything in `files`.
    """
    try:
        present = {f.name.lower(): f for f in root.iterdir() if f.is_file()}
    except OSError:
        return
    for name in SIDELINE:
        f = present.get(name)
        if f is None:
            continue
        moved = f.with_name(f.name + SIDELINE_SUFFIX)
        try:
            if moved.exists():
                f.unlink()          # an earlier run already kept a copy
            else:
                f.rename(moved)
        except OSError as e:
            rep.warnings.append(f"{f.name} could not be moved aside ({e}); "
                                f"if it is too old for the neural pass, "
                                f"nothing will visibly happen in game")
            continue
        rep.sidelined.append(f.name)
        rep.notes.append(f"{f.name} renamed to {moved.name}: the game's copy "
                         f"is older than Windows' and the neural pass will not "
                         f"compile against it; uninstall puts it back")
        log(f"      {f.name} moved aside ({moved.name}) - the game's copy "
            f"is too old for the neural pass; Windows' own is used instead")


def _restore_sidelined(root: Path, names, log) -> list[str]:
    """Undo _sideline: the game's file goes back under its own name."""
    back: list[str] = []
    cands = list(names or [])
    try:
        for f in root.iterdir():
            if f.is_file() and f.name.lower().endswith(SIDELINE_SUFFIX):
                n = f.name[:-len(SIDELINE_SUFFIX)]
                if n not in cands:
                    cands.append(n)
    except OSError:
        pass
    for name in cands:
        moved = root / (name + SIDELINE_SUFFIX)
        orig = root / name
        if not moved.is_file():
            continue
        try:
            if orig.exists():
                orig.unlink()
            moved.rename(orig)
            back.append(name)
            log(f"restored: {name} (the game's own file, moved aside)")
        except OSError as e:
            log(f"could not restore: {name} ({e})")
    return back


def install(g: games.Game, opt: Options, on_step=None, on_prog=None, on_log=None) -> Report:
    # "the version list is 14h old" is a sentence about one install. It was
    # module state that nothing ever cleared, so the next install in the
    # same session repeated it whether or not it had fallen back to
    # anything - and now that every component can fall back, it would be
    # repeated far more often.
    sources.last_fallback = None
    # The detection walk is remembered per folder; writing into it makes
    # that memory wrong.
    dlss.forget_walk(g.folder)
    dlss.forget_walk(g.install_dir)
    ok, why = check_supported(g)
    if not ok:
        raise InstallError(why)
    # Before a single byte is written: a Remix game takes the remix route and
    # nothing else. A ReShade proxy crashes it before it draws (seen on GTA
    # IV), and on DX9 our own d3d9.dll would land on top of the Remix
    # runtime itself.
    if opt.path != ROUTE_REMIX and remix.is_remix_game(g.install_dir):
        raise InstallError(
            "This game has an RTX Remix mod installed (its .trex runtime is "
            "in the folder).\n\nOnly the remix route works here: it puts DLSS 5 "
            "inside the Remix runtime. Every other route installs ReShade, "
            "which crashes a Remix game before it draws a frame - and on "
            "DirectX 9 it would write over the Remix runtime itself.\n\n"
            "Choose the remix route, or uninstall the Remix mod first.")
    preflight(g)

    log = on_log or (lambda *_: None)
    step = on_step or (lambda *_: None)
    prog = on_prog or (lambda *_: None)

    root = g.install_dir
    rep = Report()
    x64 = g.bitness == 64
    if opt.path == FEEDER and g.api == "OpenGL" and opt.provider in (3, 4):
        # LumeniteFX reads 0% motion under OpenGL (perseval-BLR, six GL
        # games); VORT's optical flow is what works there.
        opt = replace(opt, provider=2)
        log("      OpenGL game: motion vectors from VORT (LumeniteFX gives "
            "none on GL)")
    # Through DXVK the game is a Vulkan game from here on: no proxy DLL, the
    # Vulkan layer instead. DXVK itself goes in at step 0, below.
    dxvk_from = g.api if uses_dxvk(g, opt) else ""
    steps = plan(g, opt)          # counted before the switch: DXVK is a step
    g = via_dxvk(g, opt)
    proxy = _proxy_name(g.api, opt.reshade_proxy)
    host = root / HOST_DIR
    trex, flavour, swap = remix_state(g, opt) if opt.path == ROUTE_REMIX \
        else (None, "", False)
    if opt.path == ROUTE_REMIX:
        # Nothing of ours is a proxy DLL on this route, and d3d9.dll here
        # belongs to Remix - recording it as "our proxy" would make the
        # diagnosis and uninstall reach for the mod's own file.
        proxy = ""

    level, why_rel = reliability(g, opt.path,
                                 "" if opt.native_dlss else opt.upscaler,
                                 remix_swap=swap)
    if level != STABLE:
        rep.warnings.append(f"{level}: {why_rel}")

    # Two executables in one folder (Medieval II and its Kingdoms expansion,
    # a game and its launcher) share one install. Say so, or uninstalling
    # "the other one" looks like it broke this one.
    other = games._recorded_exe(root)
    if other and g.exe and other.lower() != g.exe.name.lower():
        rep.notes.append(f"this folder was already set up for {other}; both "
                         f"executables share these files, and uninstalling "
                         f"either removes them for both")
        log(f"      note: {other} in this folder uses the same files")

    # Unreal and CryEngine games run from a subfolder; the executable in the
    # root is a launcher stub. Everything goes beside the real one, and the
    # store still starts the game the normal way - say so, because "I put
    # the files in the game folder" is the classic mistake here.
    try:
        if root.resolve() != g.folder.resolve():
            rel_dir = root.relative_to(g.folder)
            log(f"      installing into {rel_dir} - the game runs from there "
                f"(the exe in the root is a launcher). Start it from the store "
                f"as usual.")
            rep.notes.append(f"files are in {rel_dir}, beside the executable "
                             f"the game actually runs; start it from the store "
                             f"as usual")
    except (OSError, ValueError):
        pass

    hw = hook_warning(root, opt.path)
    if hw:
        rep.warnings.append(hw)
        log(f"      !! {hw.split('.')[0]}")
    ac = anticheat.detect(root, g.folder)
    if ac.present:
        # Not refused: single-player-only users sometimes want this anyway,
        # and it is their machine. But it is stated plainly, kept in the
        # manifest, and repeated in the finished-install notes.
        rep.warnings.append(
            f"{ac.summary} detected ({', '.join(ac.evidence)}). ReShade "
            f"add-ons and anti-cheat do not coexist: expect the game not to "
            f"start, or nothing to happen, or a ban. Do not use this online.")
        log(f"      !! {ac.summary} detected - see the warning above")
    if opt.path != ROUTE_REMIX and reengine.detected(root):
        rep.warnings.append(reengine.message())
        log("      !! RE Engine game detected - ReShade's add-on support is "
            "documented to crash this engine; see the warning above")

    # Is another injector already in place?
    existing = root / proxy if proxy else root
    if opt.path not in (OPTI, ROUTE_REMIX) and existing.is_file() \
            and not _is_reshade(existing):
        if optiscaler.is_optiscaler(existing):
            # A hand-installed OptiScaler (no record of ours) under the name
            # ReShade needs. Two injectors under one name cannot coexist, and
            # refusing sends people to delete files by hand - so it is backed
            # up (uninstall puts it back) and moved out of the way, with the
            # other OptiScaler files it came with.
            _backup(existing, rep, root)
            existing.unlink()
            for extra in (optiscaler.FORWARDER, optiscaler.INI):
                p_ = root / extra
                if p_.is_file():
                    _backup(p_, rep, root)
                    p_.unlink()
            rep.notes.append(f"an OptiScaler installed by hand as {proxy} was "
                             f"backed up and moved aside - two injectors "
                             f"cannot share the name")
            log(f"      {proxy} was OptiScaler (not installed by this tool) - "
                f"backed up and moved aside")
        else:
            raise InstallError(
                f"{proxy} already exists but is not ReShade (DXVK, Special K or "
                f"another injector?). Remove it first, then try again.")

    # Read before anything is written: what is here that we put here.
    rep.preinstalled = _previously_ours(root)

    # A ReShade left under ANOTHER name would be loaded as a second copy. It
    # aborts itself ("Another ReShade instance was already loaded"), and the
    # game may not start at all - MGS V did not. It happens when the name
    # ReShade loads under is changed between installs, and when an uninstall
    # that knew only the recorded name left the other one behind. Through
    # DXVK or on a Vulkan game there must be none at all.
    if opt.path != OPTI:
        # On the Remix route there is no proxy to keep: every ReShade in the
        # folder has to go, whatever name it is under.
        _clear_stale_reshade(root, "" if opt.path == ROUTE_REMIX else proxy,
                             rep, log)

    # Switching routes must not leave the previous one behind. The routes put
    # very different things in the folder - the feeder alone drops 28 files,
    # including a ReShade.ini that would sit next to OptiScaler and confuse
    # everything - and the new manifest would not list them, so a later
    # uninstall could never clean them up either.

    if opt.path == FEEDER and g.api == "DX10":
        # Only feeder 0.13.1+ reaches D3D10. Checked here, before a single
        # file is written: raised from inside the feeder step it left a
        # bare ReShade behind (review, 1.7.0).
        tag0, _ = sources.resolve_feeder(prerelease=opt.feeder_prerelease,
                                         tag=opt.feeder_tag)
        if sources.feeder_key(tag0) < sources.feeder_key(sources.FEEDER_DX10_MIN):
            raise InstallError(
                f"DLSS5-Feeder {tag0} refuses Direct3D 10 games. Pick "
                f"{sources.FEEDER_DX10_MIN} or newer in 'feeder build' - that is "
                f"the first build with the D3D10 relay - and install again.")

    previous = _previous_route(root)
    if previous and previous != opt.path:
        log(f"[0] removing the previous {previous} install first")
        for line in uninstall(g, on_log=lambda s: None):
            pass
        log(f"    the {previous} route was removed; installing {opt.path}")
        rep.notes.append(f"replaced a previous {previous} install")

    # Belt and braces: whatever the manifest said, no add-on from another
    # route may be left in the folder. ReShade loads them all.
    _purge_foreign_addons(root, opt.path, rep, log)
    if opt.path not in (OPTI, ROUTE_REMIX):
        _sideline(root, rep, log)
    # An emulator on the wrong render backend never gets a DXGI swap chain,
    # and ReShade then attaches to nothing. Switch it for them, say so, and
    # let uninstall put the config back.
    if getattr(g, "emu", None) is not None and g.exe:
        try:
            for line in emulators.set_backend(g.emu, g.exe):
                rep.notes.append(line)
                log(f"      {line}")
        except Exception as e:      # never let a config quirk stop the install
            rep.warnings.append(f"could not set the emulator's render backend "
                                f"({e}); {g.emu.renderer_hint}")

    n = len(steps)
    i = 0
    done = False

    def begin(name: str) -> None:
        nonlocal i
        step(i, n, name)
        log(f"[{i + 1}/{n}] {name}")
        i += 1

    def dl(url: str, fname: str) -> Path:
        def p(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            prog(pct, f"{fname} - {net.human(done)}"
                      + (f" / {net.human(total)}" if total else ""))
        return net.download(url, fname, progress=p)

    # Every step below can fail (network, rate limit, permissions). If it
    # does, we still record the files already written - otherwise they would
    # be orphaned in the game folder with no way to clean them up.
    try:
        # --- 0) REFramework first, on an RE Engine game (see reengine.py) ---
        # Never on the remix route: Remix has already replaced the renderer
        # by the time this runs, and RE Engine's own tamper checks do not
        # apply to it - plan() already leaves this route out for the same
        # reason.
        if opt.path != ROUTE_REMIX and reengine.detected(root):
            begin("REFramework (RE Engine - so ReShade survives)")
            for f in refw.install(root, log):
                rep.written.append(f)
            rep.notes.append(
                "REFramework installed: it loads before the game's own "
                "tamper checks and patches around them, so ReShade (below) "
                "does not get killed the way it would on its own.")

        # --- 0b) DXVK: the game renders on Vulkan, ReShade stays outside -----
        # DirectX 9 arrives here too, and never on the remix route: Remix IS
        # the D3D9 implementation there, so a d3d9.dll of ours would land
        # straight on top of the Remix bridge client. Caught on the real
        # GTA IV install; uses_dxvk() excludes that route for the same reason.
        if dxvk_from:
            begin(f"DXVK ({dxvk_from} -> Vulkan)")
            ver, files = dxvk.install(root, x64, log, api=dxvk_from)
            rep.written += files
            rep.components["dxvk"] = ver
            rep.notes.append(f"DXVK {ver} installed ({dxvk_from} -> Vulkan): the "
                             f"game renders on Vulkan and ReShade loads as a "
                             f"Vulkan layer, so nothing hooks the game itself. "
                             f"Use a borderless window, not exclusive fullscreen.")

        # --- the RTX Remix route ---------------------------------------
        if opt.path == ROUTE_REMIX:
            if trex is None:
                raise InstallError(
                    "No RTX Remix runtime (a '.trex' folder) was found in "
                    "this game. This route is only for a game that already "
                    "has an RTX Remix mod installed.")
            if not flavour and not swap:
                raise InstallError(
                    "The RTX Remix runtime installed here has no DLSS 5 "
                    "neural pass - NVIDIA's own runtime has none, and only "
                    "some community forks do.\n\nTick 'swap the Remix "
                    "runtime' to replace it with a build that has the pass. "
                    "That is experimental: a mod's runtime is often a fork "
                    "carrying fixes for this exact game, and replacing it "
                    "can break them. The old one is backed up either way.")
            conf = remix.conf_path(root, trex)

            def _rel(p: Path) -> str:
                try:
                    return str(p.relative_to(root))
                except ValueError:
                    return str(p)

            rec = _remix_swap_record(g)
            if not swap and rec:
                rt0 = trex / remix.RUNTIME_DLL
                try:
                    key0 = str(rt0.relative_to(root)).replace("\\", "/")
                except ValueError:
                    key0 = str(rt0).replace("\\", "/")
                if _remix_runtime_replaced(g, trex):
                    # The mod updated its own runtime, with the pass, since
                    # our swap: it is the mod's again, and so is whatever it
                    # brought beside it. Not ours to delete on uninstall.
                    # The backup of the older runtime would only put that
                    # one back - moved aside under a name uninstall does not
                    # restore, rather than deleted, in case this is wrong.
                    for name in sources.REMIX_RUNTIME_ASSETS:
                        p = trex / name
                        try:
                            k = str(p.relative_to(root)).replace("\\", "/")
                        except ValueError:
                            k = str(p).replace("\\", "/")
                        rep.preinstalled.discard(k)
                    stale = rt0.with_name(rt0.name + BACKUP_SUFFIX)
                    aside = stale.with_name(rt0.name + SUPERSEDED_SUFFIX)
                    if stale.is_file():
                        try:
                            os.replace(stale, aside)
                            rep.notes.append(
                                f"{_rel(aside)} is the runtime this mod had "
                                f"before it updated - delete it if you do not "
                                f"need it")
                        except OSError:
                            # Left under the backup name, uninstall would
                            # put the older runtime back over the mod's.
                            try:
                                stale.unlink()
                            except OSError as e:
                                rep.warnings.append(
                                    f"{_rel(stale)} could not be moved aside "
                                    f"({e}) - rename or delete it before "
                                    f"uninstalling, or uninstall puts an older "
                                    f"runtime back")
                    log("      the mod has its own DLSS 5 runtime now - "
                        "left as it is")
                else:
                    # Still the runtime we swapped in: the record keeps
                    # saying so, or the next update would not renew it.
                    rep.components["remix_runtime"] = rec["remix_runtime"]
                    if rec.get("remix_runtime_stamp"):
                        rep.components["remix_runtime_stamp"] = rec["remix_runtime_stamp"]

            if swap:
                begin("RTX Remix runtime (DLSS 5 build)")
                rtag, rurls = sources.resolve_remix_runtime()
                rt = trex / remix.RUNTIME_DLL
                if _remix_runtime_replaced(g, trex) and rt.is_file():
                    # The mod put its own runtime there after our swap. That
                    # one is now the game's own: it replaces the backup of
                    # the older one - written beside first and then moved
                    # over it, so a full drive never leaves neither. Only the
                    # runtime itself: a mod whose own runtime has no pass
                    # ships no remix_nvngx.dll, so ours stays ours here.
                    old_bak = rt.with_name(rt.name + BACKUP_SUFFIX)
                    tmp_bak = old_bak.with_name(old_bak.name + ".tmp")
                    try:
                        shutil.copy2(rt, tmp_bak)
                        os.replace(tmp_bak, old_bak)
                    except OSError as e:
                        try:
                            tmp_bak.unlink()
                        except OSError:
                            pass
                        raise InstallError(
                            f"Could not back up the mod's own runtime before "
                            f"replacing it ({e}). Nothing was replaced.") from e
                    log("      the mod updated its own runtime since the last "
                        "swap - that one is kept as the backup")
                for name in sources.REMIX_RUNTIME_ASSETS:
                    f_ = dl(rurls[name], f"remix-runtime-{rtag}-{name}")
                    _copy(f_, trex / name, rep, root)
                    log(f"      {_rel(trex / name)}")
                log(f"      dxvk-remix-plus-dlssnr {rtag}")
                if rtag == "latest":
                    log("      (GitHub's API was out of reach; took the "
                        "newest release by its download redirect)")
                rep.components["remix_runtime"] = rtag
                stamp = _runtime_stamp(rt)
                if stamp:
                    rep.components["remix_runtime_stamp"] = stamp
                rep.notes.append(f"remix runtime version: {rtag}")
                rep.notes.append(
                    "the mod's own Remix runtime was replaced with a "
                    "community build that has the neural pass; the original "
                    "is backed up beside it and 'Uninstall' puts it back. If "
                    "the mod misbehaves after this, that swap is the reason.")
                flavour = remix.runtime_flavour(trex) or remix.NEURAL

            begin(DLSSNR)
            card, sm = gpu.detect()
            if card:
                log(f"      graphics card: {card} ({gpu.label(sm)})")
            catalog = sources.rhi_catalog()
            if sources.last_fallback:
                log(f"      {sources.last_fallback}")
                if sources.last_fallback not in rep.warnings:
                    rep.warnings.append(sources.last_fallback)
            tried: list[str] = []
            chosen = None
            candidates = ([sources.pick(catalog["dlssnr"], opt.dlssnr)]
                          if opt.dlssnr
                          else gpu.order_dlssnr(catalog["dlssnr"], sm))
            for e in candidates:
                f_ = dl(e["url"], f"dlssnr-{e['label']}.zip")
                _extract(f_, DLSSNR, trex / DLSSNR, rep, root)
                compat, why_gpu = gpu.check(trex / DLSSNR, sm)
                if compat is False and not opt.ignore_gpu_mismatch:
                    if opt.dlssnr:
                        raise InstallError(
                            f"Build {e['label']} will not run on "
                            f"{card or 'your card'}.\n\n{why_gpu}")
                    tried.append(e["label"])
                    log(f"      skipped {e['label']} - {why_gpu}")
                    continue
                chosen = (e, compat, why_gpu)
                break
            if chosen is None:
                raise InstallError(
                    f"No suitable nvngx_dlssnr build found for "
                    f"{card or 'your card'}.\n\nTried: {', '.join(tried)}")
            e, compat, why_gpu = chosen
            rep.written.append(_rel(trex / DLSSNR))
            log(f"      {_rel(trex / DLSSNR)}  (nvngx_dlssnr {e['label']})")
            log(f"      GPU check: {why_gpu}")
            rep.notes.append(f"dlssnr version: {e['label']}")
            rep.components["dlssnr"] = e["label"]
            tier = gpu.tier_note(sm, e["label"])
            if tier:
                log(f"      {tier}")
                rep.notes.append(tier)
            if compat is False:
                rep.warnings.append(f"dlssnr {e['label']} does not match your "
                                    f"card - installed anyway")

            begin(remix.CONF)
            key = remix.enable_key(flavour)
            # Whether it ended with a newline decides what uninstall has to
            # put back: set_option adds one when it is missing.
            had_nl = remix.ends_with_newline(conf)
            if not remix.set_option(conf, key, "True"):
                raise InstallError(f"Could not write {conf}. Close the game "
                                   f"and the Remix toolkit, then try again.")
            rep.remix = {"conf": _rel(conf), "key": key,
                         "trex": _rel(trex), "flavour": flavour,
                         "conf_final_newline": had_nl}
            log(f"      {_rel(conf)}: {key} = True")
            log("      (that one line; every other setting in rtx.conf is "
                "left exactly as it was)")
            rep.notes.append(
                f"{key} = True was set in {_rel(conf)}; 'Uninstall' takes "
                f"that line back out and touches nothing else in the file")
            rep.notes.append(
                "In game: Alt+X opens the Remix menu -> Developer Settings "
                "Menu -> Post-Processing, where the neural pass can be "
                "toggled. It runs inside the Remix runtime, after DLSS, so "
                "the game's own DLSS/RR settings still apply.")
            rep.notes.append(
                "No ReShade, no feeder and no add-on go into a Remix game. A "
                "ReShade proxy DLL left in this folder crashes it before it "
                "draws a frame.")
            _write_manifest(root, g, opt, rep, proxy, level, complete=True)
            prefs.add_install(root)
            prog(100, "Done")
            return rep

        if opt.path == OPTI:
            begin("OptiScaler (DLSS-NR build)")
            # A game that ships its own dxgi.dll (an ENB, DXVK, its own
            # wrapper) gets a different proxy name rather than having that
            # file replaced, unless the user picked one explicitly.
            oproxy = opt.opti_proxy or optiscaler.suggest_proxy(root)
            if oproxy != optiscaler.DEFAULT_PROXY and not opt.opti_proxy:
                log(f"      {optiscaler.DEFAULT_PROXY} is already taken here, "
                    f"installing as {oproxy} instead")
            orel = optiscaler.resolve(opt.opti_build)
            rep.components["optiscaler"] = orel[0]
            if opt.opti_build == optiscaler.FORK:
                log(f"      y4my4my4m's fork, {orel[0]}")
                rep.notes.append("OptiScaler is y4my4my4m's fork of the DLSS-NR "
                                 "build, with multi-frame generation on RTX 40 "
                                 "(OptiScaler.ini: [DLSSG] AdaMfgUnlock=true) and "
                                 "its own neural-pass changes. The rest is on the "
                                 "overlay. Development builds - if a "
                                 "game misbehaves, install again with the "
                                 "DLSS-NR build.")
            elif opt.opti_build == optiscaler.PRESR:
                log(f"      wilsjo2's fork, {orel[0]}")
                rep.notes.append("OptiScaler is wilsjo2's fork of the DLSS-NR "
                                 "build: the neural pass runs before super "
                                 "resolution rather than after it, over one to "
                                 "three passes (OptiScaler.ini: Passes=). Not "
                                 "run in a game here - if it misbehaves, "
                                 "install again with the DLSS-NR build.")
            for f in optiscaler.install(root, proxy=oproxy, dl=dl, log=log,
                                        backup=lambda p: _backup(p, rep, root),
                                        release=orel):
                rep.written.append(f)
            _, sm_ = gpu.detect()
            note = optiscaler.requirements_note(sm_)
            if note:
                rep.warnings.append(note)
                log(f"      !! {note}")

            begin("nvngx_dlssnr.dll")
            catalog_ = sources.rhi_catalog()
            e_ = sources.pick(gpu.order_dlssnr(catalog_["dlssnr"], sm_), opt.dlssnr)
            f_ = dl(e_["url"], f"dlssnr-{e_['label']}.zip")
            _extract(f_, DLSSNR, root / DLSSNR, rep, root)
            rep.written.append(DLSSNR)
            compat_, why_ = gpu.check(root / DLSSNR, sm_)
            log(f"      nvngx_dlssnr {e_['label']}")
            log(f"      GPU check: {why_}")
            rep.notes.append(f"dlssnr version: {e_['label']}")
            rep.components["dlssnr"] = e_["label"]
            tier_ = gpu.tier_note(sm_, e_["label"])
            if tier_:
                log(f"      {tier_}")
                rep.notes.append(tier_)
            if compat_ is False and not opt.ignore_gpu_mismatch:
                raise InstallError(
                    f"Build {e_['label']} will not run on your card.\n\n{why_}")

            if _opti_needs_dlss(opt):
                # The game has FSR/XeSS and no DLSS: OptiScaler will call
                # DLSS in their place, so the runtime has to be here. Same
                # pick as the ReShade routes - newest unless pinned - and a
                # copy someone put here by hand is kept, as everywhere else.
                begin("nvngx_dlss.dll")
                game_has = (root / DLSS).is_file() and DLSS not in rep.written \
                    and DLSS not in rep.preinstalled
                if game_has and opt.keep_game_dlss:
                    log("      a nvngx_dlss.dll is already here, left untouched")
                    rep.skipped.append(DLSS)
                else:
                    e_ = _place_family(catalog_["dlss"], opt.dlss,
                                       root / DLSS, rep, root, dl, DLSS,
                                       "dlss", log)
                    log(f"      nvngx_dlss {e_['label']} (the game has none: "
                        f"OptiScaler runs DLSS in place of its "
                        f"{'FSR' if opt.upscaler == 'fsr' else 'XeSS'})")
                    rep.notes.append(f"dlss version: {e_['label']}")
                    rep.components["dlss"] = e_["label"]

            begin("OptiScaler configuration")
            nr_settings = dict(opt.nr)
            if opt.opti_build == optiscaler.PRESR:
                # The build is offered for this placement; the placement is
                # a setting, and its default is off (#81).
                for k, v in optiscaler.PRESR_BEFORE_SR.items():
                    nr_settings.setdefault(k, v)
            optiscaler.enable_nr(root, log, settings=nr_settings)
            # A keyboard without an Insert key has no way into the overlay,
            # which is where neural rendering is switched on (#88).
            optiscaler.set_overlay_key(root, _overlay_key_pref(), log)
            for line in optiscaler.describe_nr(nr_settings):
                rep.notes.append(line)
            if opt.fg and g.api == "DX12":
                if optiscaler.enable_fg(root, log):
                    rep.notes.append("frame generation: FSR 3.1 through "
                                     "OptiScaler, one generated frame per "
                                     "rendered one, on any RTX card. Turn the "
                                     "game's own frame generation OFF; expect "
                                     "added latency and check the HUD")
                    rep.components["fg"] = "fsr31"
            elif opt.fg:
                log("      frame generation needs a D3D12 game - not on this one")
                rep.warnings.append("frame generation was requested but the "
                                    "game is not D3D12; not enabled")
            if _opti_needs_dlss(opt):
                optiscaler.enable_inputs(root, opt.upscaler, g.api, log)
                rep.notes.append(
                    f"no DLSS in this game: OptiScaler hooks its "
                    f"{'FSR 2/3' if opt.upscaler == 'fsr' else 'XeSS'} calls "
                    f"and runs DLSS in their place, then neural rendering. "
                    f"Pick the upscaler you would normally pick in the game's "
                    f"own menu; if nothing changes in game, the game probably "
                    f"links its upscaler statically - use the feeder route.")
            if g.api == "DX11":
                # The model refuses to run on a D3D11 device. OptiScaler gets
                # around it by running the upscaler on D3D12 underneath -
                # which means DLSS cannot be the upscaler here, FSR is.
                optiscaler.set_dx11_bridged_upscaler(root, log)
                rep.notes.append("D3D11 game: OptiScaler's upscaler set to FSR "
                                 "2.2 on D3D12 (the model does not run on "
                                 "D3D11 directly; DLSS cannot be the upscaler "
                                 "on this route)")
            rep.notes.append(
                f"OptiScaler is installed INSTEAD of ReShade. Press "
                f"{reshade_ini.overlay_key_name(optiscaler.OVERLAY_KEY)} "
                f"in game to open its overlay, then "
                f"turn on Neural Rendering - it is off by default. If it "
                f"refuses, the overlay says why under the checkbox.")
            rep.notes.append(f"OptiScaler proxy: {oproxy}")
            # Record the name OptiScaler actually went in under, not the
            # ReShade proxy this route never installs.
            _write_manifest(root, g, opt, rep, oproxy, level, complete=True)
            prog(100, "Done")
            return rep

        # --- 1) ReShade -------------------------------------------------------
        begin("ReShade")
        ver, url = sources.resolve_reshade()

        setup = dl(url, f"ReShade_Setup_{ver}_Addon.exe")
        log(f"      ReShade {ver}")
        rep.components["reshade"] = ver
        if opt.vr and not x64:
            log("      VR: the OpenXR layer is 64-bit only - skipped for this "
                "32-bit game")
        elif opt.vr:
            from . import openxr
            xr_manifest, xr_fresh = openxr.install_layer(setup, log)
            prefs.add_openxr_game(root)
            if xr_fresh:
                rep.notes.append("registered ReShade as an OpenXR layer for this "
                                 "user, so the pass reaches the headset's image; "
                                 "it loads into EVERY OpenXR application until "
                                 "'Uninstall' removes it")
            else:
                rep.notes.append(f"reused the existing ReShade OpenXR layer "
                                 f"({xr_manifest})")
            rep.warnings.append("VR through OpenXR has not been tried with a "
                                "headset by the author, and only games that "
                                "run on OpenXR are reached (OpenVR/SteamVR "
                                "titles are not): if the headset shows nothing "
                                "new, or the game refuses to start, untick "
                                "'VR headset' and install again - and say what "
                                "happened in a report")
        # The installer exe has a zip appended: both ReShade32.dll and ReShade64.dll.
        if g.api == "Vulkan":
            # A Vulkan game never loads dxgi.dll. ReShade reaches it as an
            # implicit Vulkan layer instead - a registry value the loader reads.
            manifest, fresh = vulkan.install_layer(setup, log, also32=not x64)
            prefs.add_vulkan_game(root)
            if fresh:
                rep.notes.append("registered ReShade as a Vulkan layer for this "
                                 "user - it now loads into EVERY Vulkan "
                                 "application, not just this game")
                rep.warnings.append("the Vulkan layer is global; 'Uninstall' "
                                    "removes it again")
            else:
                rep.notes.append(f"reused the existing ReShade Vulkan layer "
                                 f"({manifest})")
        else:
            _backup(root / proxy, rep, root)
            net.extract_one(setup, "ReShade64.dll" if x64 else "ReShade32.dll",
                            root / proxy)
            rep.written.append(proxy)
            log(f"      {proxy} <- ReShade{'64' if x64 else '32'}.dll")
        if not x64 and opt.path == FEEDER:
            net.extract_one(setup, "ReShade64.dll", host / "dxgi.dll")
            rep.written.append(f"{HOST_DIR}/dxgi.dll")
            log(f"      {HOST_DIR}/dxgi.dll <- ReShade64.dll (for the helper process)")

        # --- 2) the path-specific middle -------------------------------------
        if opt.path == BRIDGE:
            begin("dlss5-bridge")
            btag, burl = sources.resolve_bridge()
            bf = dl(burl, f"dlss5-bridge-{btag}.addon64")
            _copy(bf, root / BRIDGE_ADDON, rep, root)
            log(f"      dlss5-bridge {btag}")
            rep.notes.append(f"bridge version: {btag}")
            rep.components["bridge"] = btag
            # An older 1.0.x build under its previous name would be loaded too
            # and fight with this one; ReShade loads every add-on it finds.
            legacy = root / "dlss5-dx11-bridge.addon64"
            if legacy.is_file():
                try:
                    legacy.unlink()
                    log("      removed the older dlss5-dx11-bridge.addon64 "
                        "(both loading at once conflict)")
                    rep.notes.append("removed a legacy dlss5-dx11-bridge.addon64")
                except OSError:
                    rep.warnings.append("could not remove the older "
                                        "dlss5-dx11-bridge.addon64 - delete it "
                                        "by hand, it conflicts")

        if opt.path == FEEDER:
            _install_feeder_parts(g, opt, root, host, x64, rep, begin, dl, log)

        # --- 5/6/7) DLSS parts ------------------------------------------------
        # On the 32-bit path these live in host64/, otherwise next to the game.
        dlss_dir = root if (x64 or opt.path != FEEDER) else host
        catalog = sources.rhi_catalog()
        if sources.last_fallback:
            log(f"      {sources.last_fallback}")
            if sources.last_fallback not in rep.warnings:
                rep.warnings.append(sources.last_fallback)

        if opt.path == UPSTREAM:
            begin("neural-upstream")
            utag, uurl = sources.resolve_upstream()
            uf = dl(uurl, f"neural-upstream-{utag}.addon64")
            _copy(uf, dlss_dir / UPSTREAM_ADDON, rep, root)
            log(f"      neural-upstream {utag} -> {UPSTREAM_ADDON}")
            if utag == "latest":
                log("      (GitHub's API was out of reach; took the newest "
                    "release by its download redirect)")
            rep.notes.append(f"upstream version: {utag}")
            rep.components["upstream"] = utag
            rep.notes.append("neural-upstream runs the network itself, before "
                             "the game's DLSS: no renodx-dlss5 add-on on this "
                             "route, and the game's nvngx_dlss.dll is left alone")
        elif opt.path == STANDALONE:
            # VORT and the companion shader include ReShade.fxh, so the
            # headers go in exactly as on the feeder route.
            begin("ReShade shader headers")
            for h in sources.RESHADE_HEADERS:
                dest = root / SHADERS / h
                dest.parent.mkdir(parents=True, exist_ok=True)
                _backup(dest, rep, root)
                dest.write_bytes(net.fetch_text(sources.RESHADE_HEADERS_BASE + h))
                rep.written.append(str(Path(SHADERS) / h))
            log(f"      {', '.join(sources.RESHADE_HEADERS)}")

            begin("standalone-dlssnr")
            stag, surls = sources.resolve_standalone()
            if sources.STANDALONE_ZIP in surls:
                # 2.1.0 and later: one 64-bit archive laid out like a game folder.
                zf = dl(surls[sources.STANDALONE_ZIP], f"standalone-{stag}-64-bit.zip")
                for name in sources.STANDALONE_ASSETS + sources.STANDALONE_ZIP_EXTRA:
                    dest = (root / SHADERS / name) if name.endswith(".fx") else root / name
                    try:
                        _extract(zf, name, dest, rep, root)
                    except RuntimeError:        # net.extract_one: not in the archive
                        if name in sources.STANDALONE_ASSETS:
                            raise RuntimeError(f"The DLSS5-Reshade-AIO archive has no {name}.")
                        continue
                    rep.written.append(str(dest.relative_to(root)))
                    log(f"      {dest.relative_to(root)}")
            else:
                for name in sources.STANDALONE_ASSETS:
                    f = dl(surls[name], f"standalone-{stag}-{name}")
                    dest = (root / SHADERS / name) if name.endswith(".fx") else root / name
                    _copy(f, dest, rep, root)
                    log(f"      {dest.relative_to(root)}")
            log(f"      standalone-dlssnr {stag}")
            if stag == "latest":
                log("      (GitHub's API was out of reach; took the newest "
                    "release by its download redirect)")
            rep.notes.append(f"standalone version: {stag}")
            rep.components["standalone"] = stag
            rep.notes.append("standalone-dlssnr runs its own feed and the network "
                             "itself: no renodx add-on on this route. The "
                             "nvngx.dll beside it is the add-on's caller bridge, "
                             "not a driver file - uninstall removes it")

            _install_vort(root, rep, dl, log, begin)
        else:
            sf = opt.path == ROUTE_RENODX
            addon_name = RENODX_SF if sf else RENODX
            begin("DLSS 5 add-on (renodx-dlss SF)" if sf else "DLSS 5 add-on (renodx)")
            # Even without an explicit choice, prefer a local build if one exists:
            # Discord releases are not on the mirror. Only a build of the right
            # family, though - the two add-ons are not interchangeable.
            if not opt.renodx_local and not opt.renodx:
                found, _ = prefs.find_renodx(sf=sf)
                if found:
                    opt.renodx_local = found
                    log(f"      found a local renodx build: {found.name}")
            if opt.renodx_local:
                src = Path(opt.renodx_local)
                if not src.is_file():
                    raise InstallError(f"Selected renodx file not found: {src}")
                try:
                    if pe.exe_bitness(src) != 64:
                        raise InstallError("The selected renodx file is not 64-bit.")
                except pe.PEError as e:
                    raise InstallError(f"The selected renodx file is not valid: {e}") from e
                _copy(src, dlss_dir / addon_name, rep, root)
                log(f"      {src.name} (your local file) -> {addon_name}")
                rep.notes.append(f"renodx: local file used ({src.name})")
            elif sf:
                fam = catalog.get("renodx_sf") or []
                if not fam:
                    raise InstallError("The mirror lists no renodx-dlss (SF) build. "
                                       "Pick 'use my file' with the add-on from the "
                                       "RenoDX Discord, or choose another route.")
                e = sources.pick(fam, opt.renodx)
                f = dl(e["url"], f"renodx-sf-{e['label']}.zip")
                _extract(f, ".addon64", dlss_dir / RENODX_SF, rep, root)
                rep.written.append(str((dlss_dir / RENODX_SF).relative_to(root)))
                log(f"      renodx-dlss SF {e['label']}")
                rep.notes.append(f"renodx-dlss SF version: {e['label']}")
                rep.components["renodx_sf"] = e["label"]
            else:
                want = opt.renodx
                if not want and gpu.driver_at_least(sources.DRIVER_FAULT_MIN):
                    # 616.64+: 4.6/4.7 fault on every evaluate (sources.py has
                    # the measurement). 4.55 is the classic engine that still
                    # runs there - and it also satisfies the OpenGL and stable-
                    # feeder pins below, so it wins outright.
                    want = sources.DRIVER_FAULT_RENODX_PIN
                    log(f"      driver {gpu.driver_version()}: renodx-dlss5 pinned to "
                        f"{want} - 4.6/4.7 fault inside the NGX runtime on 616.64 "
                        f"and newer (every evaluate, no neural frame)")
                    rep.notes.append(f"renodx-dlss5 pinned to {want}: on driver "
                                     f"616.64+ the 4.6/4.7 builds fault in the "
                                     f"driver's NGX runtime on every evaluate")
                if not want and g.api == "OpenGL":
                    # 4.70's fenced workset pool never recycles under GL and
                    # the pass stalls after four frames; 4.60 is the last
                    # build that runs there (verified on six GL games by
                    # perseval-BLR/dlss5-classic-games).
                    want = sources.OPENGL_RENODX_PIN
                    log(f"      OpenGL game: renodx-dlss5 pinned to {want} "
                        f"(4.70 stalls on GL after a few frames)")
                    rep.notes.append(f"renodx-dlss5 pinned to {want}: newer "
                                     f"builds stall on OpenGL")
                if not want and opt.path == FEEDER:
                    # The feeder's stable release only accepts 4.55; anything
                    # newer overlaps it and the DLSS feature dies in CreateFeature.
                    want = sources.renodx_for_feeder(rep.components.get("feeder", ""))
                    if want:
                        log(f"      DLSS5-Feeder {rep.components.get('feeder')} accepts "
                            f"renodx-dlss5 up to {want} - pinning to it (newer builds "
                            f"conflict; pick 'newest pre-release' under 'feeder build' "
                            f"to use them)")
                        rep.notes.append(f"renodx-dlss5 pinned to {want} for this "
                                         f"feeder release - newer builds conflict "
                                         f"with it")
                e = sources.pick(catalog["renodx"], want)
                # pick() falls back to the newest build when the wanted one
                # is not on the list, and every pin above exists because the
                # newest is the build that breaks. Silence there would read
                # as "pinned to 4.55" in the log and install 5.2.1, so the
                # miss is said out loud and carried into the report.
                if want and e["label"] != want:
                    miss = (f"renodx-dlss5 {want} is not on the build "
                            f"list - installing {e['label']} instead, which "
                            f"is the kind of build that pin exists to avoid. "
                            f"If the neural pass faults, pick {want} under "
                            f"'dlss5 add-on' on the install page.")
                    log(f"      {miss}")
                    rep.warnings.append(miss)
                f = dl(e["url"], f"renodx-{e['label']}.zip")
                _extract(f, ".addon64", dlss_dir / RENODX, rep, root)
                rep.written.append(str((dlss_dir / RENODX).relative_to(root)))
                log(f"      renodx-dlss5 {e['label']}")
                rep.notes.append(f"renodx version: {e['label']}")
                rep.components["renodx"] = e["label"]

        begin("nvngx_dlssnr.dll")
        card, sm = gpu.detect()
        if card:
            log(f"      graphics card: {card} ({gpu.label(sm)})")
        else:
            log("      no NVIDIA card detected")

        # Some builds of the leaked library are compiled for one architecture only
        # (310.8.0 is RTX 50 only, for instance). When the user has not pinned a
        # version we find the newest build that actually supports this card:
        # download, inspect, and move down the list if it does not match.
        tried: list[str] = []
        chosen = None
        candidates = ([sources.pick(catalog["dlssnr"], opt.dlssnr)] if opt.dlssnr
                      else gpu.order_dlssnr(catalog["dlssnr"], sm))
        for e in candidates:
            f = dl(e["url"], f"dlssnr-{e['label']}.zip")
            _extract(f, DLSSNR, dlss_dir / DLSSNR, rep, root)
            compat, why_gpu = gpu.check(dlss_dir / DLSSNR, sm)
            if compat is False and not opt.ignore_gpu_mismatch:
                if opt.dlssnr:
                    raise InstallError(
                        f"Build {e['label']} will not run on {card or 'your card'}.\n\n"
                        f"{why_gpu}\n\nLeave the version on Auto and the tool picks "
                        f"the newest build that supports your card.")
                tried.append(e["label"])
                log(f"      skipped {e['label']} - {why_gpu}")
                continue
            chosen = (e, compat, why_gpu)
            break

        if chosen is None:
            raise InstallError(
                f"No suitable nvngx_dlssnr build found for {card or 'your card'}.\n\n"
                f"Tried: {', '.join(tried)}\n\n"
                f"DLSS 5 currently runs on NVIDIA RTX 20 series and newer.")

        e, compat, why_gpu = chosen
        rep.written.append(str((dlss_dir / DLSSNR).relative_to(root)))
        log(f"      nvngx_dlssnr {e['label']}")
        rep.notes.append(f"dlssnr version: {e['label']}")
        rep.components["dlssnr"] = e["label"]
        if tried:
            rep.notes.append(f"skipped as incompatible: {', '.join(tried)}")
        tier = gpu.tier_note(sm, e["label"])
        if tier:
            log(f"      {tier}")
            rep.notes.append(tier)
        if compat is True:
            log(f"      GPU check: {why_gpu}")
        elif compat is False:
            log(f"      GPU check: {why_gpu}")
            rep.warnings.append(f"dlssnr {e['label']} does not match your card - installed anyway")
        else:
            rep.warnings.append(f"could not verify GPU compatibility ({why_gpu})")

        if opt.path == UPSTREAM:
            rep.skipped.append(DLSS)
        else:
            begin("nvngx_dlss.dll")
            game_has = (root / DLSS).is_file() and str(Path(DLSS)) not in rep.written
            if x64 and game_has and opt.keep_game_dlss:
                log("      the game ships its own nvngx_dlss.dll, left untouched")
                rep.skipped.append(DLSS)
            else:
                e = _place_family(catalog["dlss"], opt.dlss,
                                  dlss_dir / DLSS, rep, root, dl, DLSS,
                                  "dlss", log)
                log(f"      nvngx_dlss {e['label']}")
                rep.notes.append(f"dlss version: {e['label']}")
                rep.components["dlss"] = e["label"]

        # Ray reconstruction. A swap, never an addition: the game asks for
        # this feature or it does not, and a runtime nothing calls is dead
        # weight in the folder. NVIDIA publishes it, so the person can move
        # a game off an old build the way they already can with DLSS itself.
        if opt.dlssd:
            begin(DLSSD)
            # Its own name: `e` above still holds the nvngx_dlss entry, and
            # reusing it here would make a skipped swap look like a done one.
            rr_entry = None
            have = _find_runtime(root, DLSSD, g.folder)
            fam = catalog.get("dlssd") or []
            if have is None:
                log("      this game does not ship nvngx_dlssd.dll - ray "
                    "reconstruction is not something it asks for, so nothing "
                    "was written")
                rep.skipped.append(DLSSD)
            elif not fam:
                log("      no nvngx_dlssd build could be listed")
                rep.skipped.append(DLSSD)
            else:
                # The publisher is the only source for this one - the mirror
                # publishes no dlssd builds - so a download that does not
                # arrive has nothing behind it. The game runs perfectly well
                # on the runtime it shipped, and everything else in this
                # install is already done: skip the swap, say so, and carry
                # on rather than ending the install over an extra.
                try:
                    rr_entry = _place_family(fam, opt.dlssd, have, rep, root,
                                             dl, DLSSD, "dlssd", log)
                except PermissionError:
                    raise      # the game is running: that answer is better
                except (sources.RateLimited, sources.Unavailable):
                    raise      # a server outage has words of its own
                except Exception as ex:
                    log(f"      nvngx_dlssd could not be fetched ({ex}) - "
                        f"the game keeps the one it shipped")
                    rep.warnings.append(
                        "ray reconstruction was not swapped: the download did "
                        "not arrive. The game keeps the runtime it shipped, "
                        "and everything else installed normally.")
                    rep.skipped.append(DLSSD)
                    rr_entry = None
            if rr_entry is not None:
                try:
                    where = have.relative_to(root)
                except ValueError:
                    where = have
                log(f"      nvngx_dlssd {rr_entry['label']} -> {where}")
                rep.notes.append(f"ray reconstruction: {rr_entry['label']} "
                                 f"(the game's own is backed up and comes "
                                 f"back on uninstall)")
                rep.notes.append("a launcher that verifies its files will put "
                                 "its own nvngx_dlssd.dll back, and an online "
                                 "game's anti-cheat can treat a changed file "
                                 "as tampering - this is a single-player "
                                 "swap")
                rep.components["dlssd"] = rr_entry["label"]

        if opt.path == STANDALONE:
            # Frame generation is optional to the add-on: with no
            # nvngx_dlssg.dll it says so in its log and runs NR + DLAA/SR.
            begin("nvngx_dlssg.dll")
            fam = catalog.get("dlssg") or []
            game_has_g = (root / DLSSG).is_file() and DLSSG not in rep.written
            if game_has_g and opt.keep_game_dlss:
                log("      a nvngx_dlssg.dll is already here, left untouched")
                rep.skipped.append(DLSSG)
            elif not fam:
                log("      no nvngx_dlssg build could be listed - frame "
                    "generation stays off")
                rep.notes.append("frame generation needs nvngx_dlssg.dll, not "
                                 "fetched (no source listed one); neural "
                                 "rendering and DLAA/DLSS SR still run")
                rep.skipped.append(DLSSG)
            else:
                e = _place_family(fam, None, root / DLSSG, rep, root, dl,
                                  DLSSG, "dlssg", log)
                log(f"      nvngx_dlssg {e['label']} (frame generation)")
                rep.notes.append(f"dlssg version: {e['label']}")
                rep.components["dlssg"] = e["label"]
                if sm is not None and sm < 89:
                    fg = ("frame generation needs an RTX 40 or 50 card; on "
                          "this one the add-on falls back to real frames")
                    log(f"      {fg}")
                    rep.notes.append(fg)

        # --- 8) host64 --------------------------------------------------------
        if not x64 and opt.path == FEEDER:
            begin("host64 helper process")
            reshade_ini.write_addon_only_ini(host)
            rep.written.append(f"{HOST_DIR}/ReShade.ini")
            log(f"      {HOST_DIR}/ ready (ReShade + DLSS parts inside)")

        # --- 8b) RTX 40 multi-frame generation (opt-in - see mfg.py) --------
        if opt.mfg:
            ok_mfg, why_mfg = mfg.applies(gpu.detect()[1], g.api, root, g.folder)
            if ok_mfg:
                begin("RTX 40 multi-frame generation")
                taken = {refw.DINPUT8} if reengine.detected(root) else set()
                try:
                    mtag, mfiles = mfg.install(root, g.exe, log, taken=taken,
                                               preinstalled=rep.preinstalled)
                # An opt-in extra: whatever stops it - a project that changed
                # shape (#141), GitHub's rate limit, a proxy page - is a
                # warning, not the end of an install that is otherwise done.
                # mfg.install checks both archives before it writes a file.
                except (mfg.NoLoaderName, mfg.ShapeChanged, sources.RateLimited,
                        sources.Unavailable, net.WrongContent) as e:
                    log(f"      multi-frame generation skipped: {e}")
                    rep.warnings.append(f"multi-frame generation not enabled: {e}")
                    mtag, mfiles = "", []
            else:
                mtag, mfiles = "", []
            if mfiles:
                rep.written += mfiles
                rep.components["mfg"] = mtag
                rep.notes.append(
                    f"RTX40MFG-Unlock {mtag}: multi-frame generation on this "
                    f"RTX 40 - pick the multiplier in ReShade's DLSS MFG tab "
                    f"(Follow game / fixed / Dynamic). The game's own DLSS "
                    f"Frame Generation must be ON; the unlock raises its "
                    f"multiplier, it does not add frame generation")
                rep.warnings.append(
                    "multi-frame generation on RTX 40 is research software: "
                    "artifacts, a frozen picture or a crash are possible at "
                    "the higher multipliers, and on Vulkan")
            elif not ok_mfg:
                log(f"      multi-frame generation skipped: {why_mfg}")
                rep.warnings.append(f"multi-frame generation not enabled: {why_mfg}")
        else:
            # The box is off: an unlock an earlier install of ours placed
            # here comes out, or it would keep hooking Streamline while the
            # new manifest says the option is off.
            for gone in mfg.remove_leftovers(root, rep.preinstalled, log):
                rep.preinstalled.discard(gone)
                rep.preinstalled.discard(gone.replace("\\", "/"))

        # --- 9) ReShade configuration ----------------------------------------
        begin("ReShade configuration")
        if opt.path == FEEDER:
            _backup(root / "ReShade.ini", rep, root)
            _backup(root / "ReShadePreset.ini", rep, root)
            reshade_ini.write_reshade_ini(root, opt.provider)
            reshade_ini.write_preset(root, opt.provider)
            src = reshade_ini.carry_over(root, [Path(x) for x in prefs.installs()])
            if src is not None:
                log(f"      your ReShade keys and overlay settings carried over "
                    f"from {src.parent.name}")
                rep.notes.append(f"ReShade key bindings and overlay settings "
                                 f"carried over from {src.parent.name}")
            rep.written += ["ReShade.ini", "ReShadePreset.ini"]
            label, tech, _ = reshade_ini.PROVIDERS[opt.provider]
            log(f"      DLSS5_MV_PROVIDER={opt.provider} ({label})")
            if tech:
                log(f"      technique order: {tech} -> {reshade_ini.FEED_TECHNIQUE}")
            else:
                rep.notes.append("You must install your chosen provider's shader "
                                 "yourself, and place its technique ABOVE DLSS 5 "
                                 "Feed in ReShade.")
        else:
            # Native and bridge hook the game's real NGX calls, so there is no
            # effect to compile and no technique order to get right. ReShade
            # only has to load the add-ons sitting next to the executable.
            _backup(root / "ReShade.ini", rep, root)
            reshade_ini.write_addon_only_ini(root)
            if opt.path == ROUTE_RENODX:
                reshade_ini.enable_renodx_dlss_nr(root)
                log("      [RENODX-DLSS] NeuralRenderingEnabled=1")
            if opt.path == UPSTREAM:
                rep.notes.append("neural-upstream is configured from its 'NR "
                                 "Pre-Upscale' tab in the ReShade overlay. With "
                                 "DLSS Frame Generation on, set its cadence to "
                                 "Quality (every frame) or expect stutter.")
                rep.notes.append("If the picture only gets darker, the add-on "
                                 "is not reading the game's exposure buffer "
                                 "(it normalises the frame against it, and "
                                 "some games do not expose one). There is no "
                                 "setting for that: switch the route to "
                                 "native, which runs after the game's own "
                                 "tone mapping.")
            rep.written.append("ReShade.ini")
            if opt.path == STANDALONE:
                # Search paths so ReShade can compile the two shaders; no
                # technique in any preset - the add-on schedules them itself
                # inside its Present callback, in the order it needs.
                reshade_ini.write_shader_paths(root)
                log("      shader search paths set, no technique enabled - the "
                    "add-on runs DLSS5_AIO_Feed and VORT itself")
                rep.notes.append("standalone-dlssnr: turn the game's own DLSS, "
                                 "frame generation and anti-aliasing OFF. Game "
                                 "resolution = the monitor's gives DLAA, a lower "
                                 "one gives DLSS Super Resolution. F10 compares, "
                                 "Home opens ReShade; leave DLSS5_AIO_Feed and "
                                 "vort_MotionEffects unticked, the add-on runs "
                                 "them. Its log is outside the game folder: "
                                 "%LOCALAPPDATA%\\RHI\\Logs\\standalone-dlssnr.log")
            else:
                log("      add-on loading enabled (no shaders needed on this path)")
                rep.notes.append("ReShade's overlay will report 'no .fx files found' "
                                 "on this route - normal, no shaders are used; the "
                                 "add-on tab is what matters")

        # The overlay key, last: carry_over above copies the [INPUT] section
        # from another game and would otherwise put the old binding back.
        # ReShade opens on Home, and a keyboard without one - or without the
        # Insert key OptiScaler uses - has no way in at all (#88).
        _key = _overlay_key_pref()
        if _key and (root / "ReShade.ini").is_file():
            written = True
            try:
                reshade_ini.set_overlay_key(root, _key)
            except OSError as e:
                # This runs after everything is written and before the
                # manifest: an unwritable ReShade.ini here would have left a
                # fully set-up folder with no record of it. And having said
                # it could not be written, it must not then announce a key.
                log(f"      could not write the overlay key ({e}) - "
                    f"ReShade keeps its own")
                written = False
            name = next((k for k, v in reshade_ini.OVERLAY_KEYS.items()
                         if v == _key), f"0x{_key:02X}")
            if written:
                log(f"      ReShade overlay opens on {name}")
                rep.notes.append(
                    f"ReShade's overlay opens on {name}"
                    + ("" if name == "Home" else ", not Home"))

        # --- 10) dlss5-feed.cfg ----------------------------------------------
        if opt.path == FEEDER:
            begin("dlss5-feed.cfg")
            _backup(root / feedcfg.NAME, rep, root)
            feedcfg.write(root, opt.feed, host_window=None if x64 else True)
            rep.written.append(feedcfg.NAME)
            summary = feedcfg.describe(opt.feed) if opt.feed else []
            if summary:
                for s in summary:
                    log(f"      {s}")
                rep.notes += summary
            else:
                log("      defaults (work_resolution=100, preset=0)")
        elif opt.path == BRIDGE:
            begin("dlss5-bridge.cfg")
            cfg = feedcfg.bridge_defaults(opt.native_dlss)
            cfg.update(opt.feed)          # user overrides (ofa_grid, ofa_perf)
            if (root / feedcfg.BRIDGE_NAME).is_file():
                log("      merging into the existing dlss5-bridge.cfg")
            _backup(root / feedcfg.BRIDGE_NAME, rep, root)
            feedcfg.write_bridge(root, cfg)
            rep.written.append(feedcfg.BRIDGE_NAME)
            for line in feedcfg.describe_bridge(cfg):
                log(f"      {line}")
                rep.notes.append(line)
            if not opt.native_dlss:
                log("      the bridge will build a synthetic contract from the "
                    "driver's optical flow engine")

    except PermissionError as e:
        _write_manifest(root, g, opt, rep, proxy, level, complete=False)
        raise InstallError(
            f"Windows refused to write a file:\n{e}\n\n"
            f"Almost always this means the game (or its launcher) is running "
            f"and holding the file open. Close it and run the install again - "
            f"what was written so far has been recorded, so 'Uninstall' can "
            f"clean up if you would rather start fresh.") from e
    except (sources.RateLimited, sources.Unavailable) as e:
        _write_manifest(root, g, opt, rep, proxy, level, complete=False)
        log("")
        log(str(e))
        raise InstallError(str(e)) from e
    except Exception as e:
        if net.is_disk_full(e):
            # Said in words, and recorded: the diagnosis reads this note
            # instead of telling someone with a full drive to install again.
            rep.notes.append(net.DISK_FULL_NOTE)
            _write_manifest(root, g, opt, rep, proxy, level, complete=False)
            raise InstallError(net.disk_full_message(e, root, net.CACHE)) from e
        if isinstance(e, InstallError):
            # The install's own refusal, raised part way through (the Remix
            # runtime without the neural pass, #148). Recorded as the reason,
            # or the diagnosis reads an unfinished install and says to
            # install again - which is refused again for the same reason.
            # All of it, not the first line: the Remix refusal says what is
            # wrong in its first paragraph and what to tick in its second.
            said = " ".join(ln.strip() for ln in str(e).splitlines() if ln.strip())
            if said:
                rep.notes.append(net.STOP_NOTE + said[:400])
        _write_manifest(root, g, opt, rep, proxy, level, complete=False)
        log("")
        log(f"Install did not finish. {len(rep.written)} files were already "
            f"written and have been recorded, so 'Uninstall' can remove them.")
        raise

    # --- did everything survive? -------------------------------------------
    # The DLSS 5 add-on and the neural-rendering runtime are unsigned, freshly
    # built and rare, which is exactly what machine-learning antivirus
    # heuristics flag - Defender has called renodx builds Trojan:Win32/
    # Ulthar.A!ml and OptiScaler Trojan:Win32/Fonzi.A!ml. A quarantine removes
    # the file after we wrote it, so the install reports success and the game
    # then does nothing. Say so instead of leaving it a mystery.
    missing = []
    for rel in rep.written:
        if rel.endswith(BACKUP_SUFFIX):
            continue
        if not (root / rel).exists():
            missing.append(rel)
    if missing:
        names = ", ".join(missing[:4]) + ("..." if len(missing) > 4 else "")
        rep.warnings.append(
            f"{len(missing)} file(s) were written and are no longer there: "
            f"{names}. Almost always this is antivirus quarantining them. "
            f"These components are unsigned and uncommon, so heuristic "
            f"scanners flag them; the detections are false positives on "
            f"software this tool downloads from its publishers, not on the "
            f"tool. Restore them from your antivirus quarantine and add this "
            f"game folder to its exclusions, then install again.")
        log("")
        log(f"      !! {len(missing)} files vanished after being written "
            f"- check your antivirus quarantine")
        for m in missing[:8]:
            log(f"         {m}")

    # --- record -----------------------------------------------------------
    _write_manifest(root, g, opt, rep, proxy, level, complete=True)
    prefs.add_install(root)
    prog(100, "Done")
    return rep


# ---------------------------------------------------------------- uninstall

def uninstall(g: games.Game, on_log=None) -> list[str]:
    """Remove only what this tool wrote; never touch the game's own files."""
    dlss.forget_walk(g.folder)
    dlss.forget_walk(g.install_dir)
    log = on_log or (lambda *_: None)
    root = g.install_dir
    man = root / MANIFEST
    removed: list[str] = []

    # Read ours, or an older release's, whichever is there.
    sources_ = [man] + [root / n for n in LEGACY_MANIFESTS]
    found_man = next((m for m in sources_ if m.is_file()), None)
    data: dict = {}
    if found_man is not None:
        try:
            data = json.loads(found_man.read_text(encoding="utf8"))
            # v1.0/v1.1 used Turkish keys
            files = data.get("files") or data.get("dosyalar") or []
            if found_man != man:
                log(f"found an install recorded by an older version "
                    f"({found_man.name})")
        except (OSError, json.JSONDecodeError):
            files = []
            data = {}
    else:
        files = [FEEDER_ADDON64, FEEDER_ADDON32, RENODX, RENODX_SF, UPSTREAM_ADDON,
                 STANDALONE_ADDON, DLSSNR, DLSS, DLSSG,
                 BRIDGE_ADDON, BRIDGE_CFG, feedcfg.NAME,
                 # dgVoodoo2 was the DX9 translation until 1.6.0. Its files
                 # stay on this list so an install made by an older release
                 # still cleans up completely.
                 "dgVoodoo.conf", "dgVoodooCpl.exe",
                 "ReShade.ini", "ReShadePreset.ini",
                 str(SHADERS / FEEDER_FX), str(SHADERS / STANDALONE_FX),
                 str(SHADERS / VORT_FX), str(TEXTURES / VORT_TEXTURE),
                 *mfg.FILES,
                 *(f"{mfg.PLUGINS_DIR}/{n}" for n in mfg.PLUGIN_FILES)]
        # A d3d9.dll here is ours (DXVK, or dgVoodoo2 from an older release)
        # UNLESS the folder is a Remix game, where that name belongs to the
        # Remix bridge client. With no manifest to tell them apart the Remix
        # install wins: a file left behind is harmless, a deleted runtime is
        # not.
        if not remix.is_remix_game(root):
            files.append("D3D9.dll")
        else:
            log("RTX Remix is installed here: d3d9.dll is its runtime, left alone.")
        # A plain nvngx.dll is the standalone add-on's bridge only when the
        # add-on is there too; on its own it could be somebody's OptiScaler.
        if (root / STANDALONE_ADDON).is_file():
            files.append(STANDALONE_BRIDGE)
        try:
            files += [str(VORT_INCLUDE / f.name)
                      for f in (root / VORT_INCLUDE).glob("vort_*.fxh")]
        except OSError:
            pass
        files += [str(SHADERS / h) for h in sources.RESHADE_HEADERS]
        # LumeniteFX shaders/includes/texture we may have dropped in
        for d_ in (SHADERS, INCLUDE):
            try:
                files += [str(Path(d_) / f.name)
                          for f in (root / d_).glob("lumenite_*")]
            except OSError:
                pass
        try:
            files += [str(Path(TEXTURES) / f.name)
                      for f in (root / TEXTURES).glob("lumenite_*")]
        except OSError:
            pass
        # ReShade under any of the other names it can load as - only when
        # the file really is ReShade, a game's own d3d11.dll stays.
        files += [n for n in RESHADE_PROXIES
                  if n not in files and _is_reshade(root / n)]
        # REFramework's dinput8.dll, same reasoning: a real one the person
        # put there themselves - or is still using for something else -
        # stays untouched unless it is confirmed to be ours by content.
        if refw.DINPUT8 not in files and refw.is_reframework(root / refw.DINPUT8):
            files.append(refw.DINPUT8)
        log("No install record found; cleaning up by known filenames.")

    # The manifest is a file in the game folder, and anything can write it -
    # a broken install, a tool that "cleans" mods, a hand edit. Every name in
    # it is turned into a path below and then deleted or overwritten, so a
    # `../` entry would take a file outside the folder with it (#79). Drop
    # those here, once, rather than at each of the three places that build a
    # path from `files`.
    # The manifest can legitimately name a path outside the INSTALL folder -
    # _backup() and _copy() write an absolute one when a file is not under it,
    # and a Remix .trex sits at the game root while the install folder is the
    # executable's subfolder. It can never legitimately name a path outside
    # the GAME, so that is the boundary.
    roots = [root]
    game_root = getattr(g, "folder", None)
    if game_root is not None and Path(game_root) != root:
        roots.append(Path(game_root))

    def _permitted(entry: str) -> bool:
        for r in roots:
            try:
                net.inside(r, entry, absolute_ok=True)
                return True
            except net.OutsideError:
                continue
        return False

    safe: list[str] = []
    for rel in files:
        try:
            if not _permitted(rel):
                raise net.OutsideError(rel)
        except net.OutsideError:
            log(f"ignored an install record entry that points outside the "
                f"game folder: {rel}")
            continue
        safe.append(rel)
    files = safe

    # Restore backups first, then delete the rest
    # A safety net beyond the manifest: restore every backup sitting in the
    # folder, even one an interrupted install left unrecorded.
    try:
        for bak in list(root.rglob("*" + BACKUP_SUFFIX)):
            rel = str(bak.relative_to(root))
            if rel not in files:
                files.append(rel)
    except OSError:
        pass

    all_suffixes = (BACKUP_SUFFIX,) + LEGACY_BACKUP_SUFFIXES
    for rel in list(files):
        suffix = next((s for s in all_suffixes if rel.endswith(s)), None)
        if suffix is None:
            continue
        bak = root / rel
        orig = bak.with_name(bak.name[:-len(suffix)])
        try:
            if bak.is_file():
                shutil.copy2(bak, orig)
                bak.unlink()
                removed.append(rel)
                log(f"restored: {orig.name} (the game's own file)")
        except OSError as e:
            log(f"could not restore: {orig.name} ({e})")

    # The Remix route's one edit outside its own files: a single line in the
    # user's rtx.conf. It is never in `files` - that list gets deleted, and
    # deleting somebody's rtx.conf would take their whole mod configuration
    # with it - so the key is removed by name instead.
    rx = data.get("remix") or {}
    if rx.get("conf") and rx.get("key"):
        # Recorded relative to the game folder by the install (`_rel(conf)`),
        # so anything that resolves outside it came from a tampered record.
        try:
            # ...and the Remix route records an absolute rtx.conf whenever
            # the .trex is outside the install folder, which is every game
            # whose executable sits in a subfolder.
            conf_p = None
            for _r in roots:
                try:
                    conf_p = net.inside(_r, str(rx["conf"]), absolute_ok=True)
                    break
                except net.OutsideError:
                    continue
            if conf_p is None:
                raise net.OutsideError(str(rx["conf"]))
        except net.OutsideError:
            log(f"ignored a Remix configuration path outside the game "
                f"folder: {rx['conf']}")
            conf_p = None
        try:
            if conf_p is not None and remix.remove_option(
                    conf_p, rx["key"],
                    bool(rx.get("conf_final_newline", True))):
                removed.append(f"{rx['conf']} ({rx['key']})")
                log(f"removed: {rx['key']} from {rx['conf']} "
                    f"(nothing else in the file was touched)")
        except OSError as e:
            log(f"could not edit {rx['conf']}: {e}")

    if getattr(g, "emu", None) is not None and g.exe:
        try:
            for line in emulators.restore_backend(g.emu, g.exe):
                log(line)
        except Exception as e:
            log(f"could not restore the emulator's render backend: {e}")
    for name in _restore_sidelined(root, data.get("sidelined"), log):
        removed.append(name + SIDELINE_SUFFIX)

    restored = set()
    for rel in files:
        s = next((s for s in all_suffixes if rel.endswith(s)), None)
        if s:
            restored.add(rel[:-len(s)])
    stuck: list[str] = []
    for rel in files:
        if any(rel.endswith(s) for s in all_suffixes) or rel in restored:
            continue
        p = root / rel
        if not p.is_file():
            continue
        if _delete(p):
            removed.append(rel)
            log(f"removed: {rel}")
        else:
            stuck.append(rel)
            log(f"could not remove: {rel} (locked - is the game or its "
                f"launcher still running?)")

    hostdir = root / HOST_DIR
    if hostdir.is_dir():
        shutil.rmtree(hostdir, ignore_errors=True)
        removed.append(HOST_DIR + "/")
        log(f"removed: {HOST_DIR}/")

    # Logs the components write while the game runs. They appear after the
    # install, so the manifest has never heard of them, and every uninstall
    # used to leave the lot behind.
    for name in RUNTIME_ARTIFACTS:
        p = root / name
        try:
            if p.is_file():
                p.unlink()
                removed.append(name)
                log(f"removed: {name} (log)")
        except OSError:
            pass
    # DXVK names its logs after the executable; only when the DXVK was ours.
    if data.get("dxvk"):
        exe_rec = data.get("exe") or (g.exe.name if g.exe else "")
        for name in dxvk.logs_for(Path(exe_rec)) if exe_rec else ():
            p = root / name
            try:
                if p.is_file():
                    p.unlink()
                    removed.append(name)
                    log(f"removed: {name} (log)")
            except OSError:
                pass
    # OptiScaler's own log folder: take out its files, and the folder only if
    # that leaves it empty - a game could have a folder of the same name.
    logs = root / OPTI_LOG_DIR
    try:
        if logs.is_dir():
            for f in list(logs.glob("*")):
                if f.is_file() and (f.name.lower().startswith("optiscaler")
                                    or f.suffix.lower() == ".log"):
                    f.unlink()
                    removed.append(f"{OPTI_LOG_DIR}/{f.name}")
            if not any(logs.iterdir()):
                logs.rmdir()
                removed.append(OPTI_LOG_DIR + "/")
                log(f"removed: {OPTI_LOG_DIR}/")
    except OSError:
        pass

    try:
        _prov = int(data.get("provider")) if data.get("provider") is not None else None
    except (TypeError, ValueError):
        _prov = None
    reshade_ini.remove_our_techniques(root, _prov)
    try:
        prefs.drop_install(root)
    except Exception:
        pass
    # The frame-rate measurements were about this install; with it gone they
    # are about nothing, and they would otherwise sit in the settings file
    # for every folder the tool has ever touched.
    try:
        from . import autotune
        autotune.forget(root)
    except Exception:
        pass

    # The Vulkan layer is registered once for the whole user, so it may only be
    # removed when the LAST game that needs it goes. Removing it while another
    # Vulkan install still relies on it would silently break that game.
    try:
        if str(root) in prefs.openxr_games():
            from . import openxr
            still_xr = prefs.drop_openxr_game(root)
            if still_xr:
                log(f"kept the OpenXR layer: {len(still_xr)} other VR "
                    f"install(s) still use it")
            elif openxr.unregister():
                removed.append("OpenXR layer registration")
                log("removed: our ReShade OpenXR layer registration "
                    "(no VR games left)")
    except Exception:
        pass
    try:
        was_vulkan = str(root) in prefs.vulkan_games()
        if was_vulkan:
            still = prefs.drop_vulkan_game(root)
            if still:
                log(f"kept the Vulkan layer: {len(still)} other Vulkan "
                    f"install(s) still use it")
            elif vulkan.unregister():
                removed.append("Vulkan layer registration")
                log("removed: our ReShade Vulkan layer registration "
                    "(no Vulkan games left)")
    except Exception:
        pass

    # Every folder a removed file lived in, deepest first, if it is empty now.
    # OptiScaler's zip alone brings D3D12_Optiscaler, DlssOverrides and
    # Licenses; deleting the files and leaving the folders looked like an
    # uninstall that "does not remove everything".
    dirs: set[Path] = {root / INCLUDE, root / VORT_INCLUDE, root / SHADERS,
                       root / TEXTURES, root / "reshade-shaders"}
    for rel in files + removed:
        p = root / rel
        for parent in p.parents:
            if parent == root or root not in parent.parents:
                break
            dirs.add(parent)
    for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
        try:
            if d != root and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    if stuck:
        # A locked proxy DLL is the usual case: the game or its launcher is
        # still up. Keep the record so the next uninstall can finish the job,
        # and say so plainly instead of reporting success.
        try:
            data = json.loads(man.read_text(encoding="utf8")) if man.is_file() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data["files"] = stuck
        data["complete"] = False
        data["notes"] = [f"uninstall left {len(stuck)} locked file(s); run it "
                         f"again with the game closed"]
        try:
            man.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf8")
        except OSError:
            pass
        log(f"Removed {len(removed)} items; {len(stuck)} could not be removed. "
            f"Close the game and its launcher, then uninstall again.")
        return removed
    man.unlink(missing_ok=True)
    for n in LEGACY_MANIFESTS:
        (root / n).unlink(missing_ok=True)
    log(f"Removed {len(removed)} items.")
    return removed


def _delete(p: Path, attempts: int = 4) -> bool:
    """Delete a file, retrying briefly when something still holds it.

    Antivirus scanners and launchers hold files open for a moment after the
    game exits; a single failed unlink used to count as "cannot delete".
    """
    import time
    for i in range(attempts):
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if i == attempts - 1:
                break
            time.sleep(0.4 * (i + 1))
        except OSError:
            break
    # Read-only attribute set by a game updater? Clear it and try once more.
    try:
        import stat
        p.chmod(p.stat().st_mode | stat.S_IWRITE)
        p.unlink()
        return True
    except OSError:
        return False
