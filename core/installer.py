r"""Install engine: 64-bit, 32-bit and DX9 paths.

64-bit layout (next to the game executable):
    <proxy>.dll                 ReShade64.dll  (dxgi.dll or opengl32.dll)
    dlss5-feed.addon64          (feeder route)
    renodx-dlss5.addon64        (feeder / native / bridge routes)
    renodx-dlss.addon64         (renodx route - ShortFuse's SF build)
    nvngx_dlssnr.dll
    nvngx_dlss.dll
    ReShade.ini / ReShadePreset.ini / dlss5-feed.cfg
    reshade-shaders/Shaders/{headers, DLSS5_Feed.fx, lumenite_*.fx}
    reshade-shaders/Shaders/include/lumenite_*.fxh
    reshade-shaders/Textures/lumenite_bluenoise256.png

32-bit: a 32-bit process cannot load 64-bit NGX, so a helper process is
needed. The game gets 32-bit ReShade + addon32; host64/ holds its own 64-bit
ReShade and all the DLSS parts.

DX9: dgVoodoo2 translates to D3D11 first, then the 32-bit path applies.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import (anticheat, dgvoodoo, dlss, feedcfg, games, gpu, net,
               optiscaler, pe, prefs, reshade_ini, sources, vulkan)
# Imported by name as well: inside the Options class body the field
# `dlss: str | None` shadows the module, so `dlss.FEEDER` would read the
# field's default (None) instead of the module attribute.
from .dlss import BRIDGE, FEEDER, NATIVE, OPTI
from .dlss import RENODX as ROUTE_RENODX   # the route; RENODX below is a file name

MANIFEST = "dlss5-autopilot.json"

FEEDER_ADDON64 = "dlss5-feed.addon64"
FEEDER_ADDON32 = "dlss5-feed.addon32"
FEEDER_HOST = "dlss5-feed-host64.exe"
FEEDER_FX = "DLSS5_Feed.fx"
RENODX = "renodx-dlss5.addon64"
# ShortFuse's add-on - the "SF" build - which hooks D3D9/D3D11/D3D12 itself.
RENODX_SF = "renodx-dlss.addon64"
DLSSNR = "nvngx_dlssnr.dll"
DLSS = "nvngx_dlss.dll"
HOST_DIR = "host64"

SHADERS = Path("reshade-shaders") / "Shaders"
INCLUDE = SHADERS / "include"
TEXTURES = Path("reshade-shaders") / "Textures"

BRIDGE_ADDON = "dlss5-bridge.addon64"
BRIDGE_CFG = "dlss5-bridge.cfg"

BACKUP_SUFFIX = ".dlss5-autopilot-backup"
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
    keep_game_dlss: bool = True             # leave the game's own nvngx_dlss.dll alone
    feed: dict = field(default_factory=dict)   # dlss5-feed.cfg settings
    ignore_gpu_mismatch: bool = False
    path: str = FEEDER                      # native / bridge / feeder
    opti_proxy: str = ""                    # "" = pick a free name for this game
    reshade_proxy: str = ""                 # "" = choose from the API
    native_dlss: bool = False               # game ships its own DLSS
    # The feeder's pre-releases are where support for the newer DLSS 5 add-on
    # generations lives; the stable release only accepts renodx-dlss5 4.55.
    feeder_prerelease: bool = False
    nr: dict = field(default_factory=dict)  # OptiScaler [DlssNr] settings


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


# ---------------------------------------------------------------- reliability

# Measured on real games, not guessed. DLSS 5 feeding was designed around
# DXGI; everything else is a bolt-on and fails far more often.
STABLE, BETA, EXPERIMENTAL = "stable", "beta", "experimental"

def reliability(g: games.Game, path: str = FEEDER) -> tuple[str, str]:
    """(level, explanation) - how likely this route is to actually work."""
    if g.api == "DX10":
        return EXPERIMENTAL, ("DirectX 10 is not supported by any DLSS 5 "
                              "component.")
    if path == NATIVE:
        return STABLE, ("The game's own DLSS is hooked directly - no synthetic "
                        "contract, no motion-vector shaders, and your in-game "
                        "DLSS quality setting still applies.")
    if path == ROUTE_RENODX:
        if g.api == "DX9":
            return BETA, ("64-bit DirectX 9 through the renodx-dlss add-on: it "
                          "evaluates the presentation backbuffer with no "
                          "motion vectors, so expect a softer result.")
        return BETA, ("The renodx-dlss add-on hooks the game in-process. It is "
                      "the build the other authors now point to, but it is "
                      "only days old (September 2026).")
    if path == OPTI:
        return BETA, ("OptiScaler replaces the upscaler and runs the model over "
                      "its output. RTX 50 only; the game must already use DLSS."
                      + (" On D3D11 it needs a bridged upscaler (FSR on D3D12) "
                         "in place of DLSS." if g.api == "DX11" else ""))
    if path == BRIDGE:
        if g.api == "Vulkan":
            return BETA, ("The bridge mirrors the game's DLSS contract onto a "
                          "private D3D12 session. This is the only route for "
                          "Vulkan and it is newer than the rest.")
        return BETA, ("The bridge reproduces the DLSS contract on a private "
                      "D3D12 session. Fewer moving parts than the feeder, but "
                      "less proven.")
    if g.api == "DX9":
        return EXPERIMENTAL, (
            "DirectX 9 is the least reliable path. The game runs through "
            "dgVoodoo2 translation and then the 32-bit helper process; the "
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
        return EXPERIMENTAL, (
            "OpenGL needs interop extensions the driver may not expose to "
            "this game, and the game must render on the NVIDIA card. Upstream "
            "has verified it on one 32-bit title; frequently does not work.")
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
    "d3d9.dll": "DirectX 9, after dgVoodoo2 translation",
    "opengl32.dll": "the only option for OpenGL",
}


def _proxy_name(api: str, chosen: str = "") -> str:
    if chosen in RESHADE_PROXIES:
        return chosen
    return "opengl32.dll" if api == "OpenGL" else "dxgi.dll"


def check_supported(g: games.Game) -> tuple[bool, str]:
    """Can this game be set up automatically?"""
    if not g.exe:
        return False, "No game executable found."
    if g.bitness not in (32, 64):
        return False, "Could not read the architecture."
    if g.api == "Vulkan":
        # Reachable since the bridge landed: it mirrors the game's DLSS
        # contract onto a private D3D12 session. ReShade still has to be
        # attached to the Vulkan runtime, which its own installer does.
        return True, ""
    if g.api == "DX10":
        return False, ("DirectX 10 is not supported by any DLSS 5 component - "
                       "the feeder dropped it and nothing else hooks D3D10.")
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
    try:
        if str(dst.relative_to(root)).replace("\\", "/") in rep.preinstalled:
            return
    except ValueError:
        pass
    bak = dst.with_name(dst.name + BACKUP_SUFFIX)
    try:
        rel = str(bak.relative_to(root))
    except ValueError:
        rel = str(bak)
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

def plan(g: games.Game, opt: Options) -> list[str]:
    """The steps for this game on the selected path.

    The three paths need very different things. Only the feeder builds a
    synthetic contract out of ReShade shaders, so only it needs the shader
    headers, the .fx and a motion-vector provider.
    """
    steps: list[str] = []
    if g.api == "DX9":
        steps.append("dgVoodoo2 (DX9 -> D3D11)")
    if opt.path == OPTI:
        # OptiScaler replaces ReShade entirely - it is the proxy DLL itself.
        return ["OptiScaler (DLSS-NR build)", "nvngx_dlssnr.dll",
                "OptiScaler configuration"]
    steps.append("ReShade (Vulkan layer)" if g.api == "Vulkan" else "ReShade")

    if opt.path == FEEDER:
        steps.append("ReShade shader headers")
        steps.append("DLSS5-Feeder")
        if opt.provider in (3, 4):
            steps.append("LumeniteFX (motion vectors)")
    elif opt.path == BRIDGE:
        steps.append("dlss5-bridge")

    steps += ["DLSS 5 add-on (renodx-dlss SF)" if opt.path == ROUTE_RENODX
              else "DLSS 5 add-on (renodx)",
              "nvngx_dlssnr.dll", "nvngx_dlss.dll"]
    if opt.path == FEEDER and g.bitness == 32:
        steps.append("host64 helper process")
    steps.append("ReShade configuration")
    if opt.path == FEEDER:
        steps.append("dlss5-feed.cfg")
    elif opt.path == BRIDGE:
        steps.append("dlss5-bridge.cfg")
    return steps


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
    tag, assets = sources.resolve_feeder(prerelease=opt.feeder_prerelease)
    log(f"      DLSS5-Feeder {tag}"
        + ("  (pre-release, as requested)" if opt.feeder_prerelease else ""))
    rep.components["feeder"] = tag
    addon = FEEDER_ADDON64 if x64 else FEEDER_ADDON32
    for name in (addon, FEEDER_FX) + ((FEEDER_HOST,) if not x64 else ()):
        if name not in assets:
            raise InstallError(f"The DLSS5-Feeder release has no {name}.")
        f = dl(assets[name], f"{tag}-{name}")
        dest = (root / SHADERS / name) if name.endswith(".fx") else                (host / name if name == FEEDER_HOST else root / name)
        _copy(f, dest, rep, root)
        log(f"      {dest.relative_to(root)}")

    if opt.provider in (3, 4):
        begin("LumeniteFX (motion vectors)")
        z = dl(sources.LUMENITE_ZIP, "LumeniteFX-mainline.zip")
        w = net.extract_tree(z, "Shaders", str(SHADERS), root, only_ext=(".fx",))
        w += net.extract_tree(z, "Shaders/include", str(INCLUDE), root,
                              only_ext=(".fxh",))
        w += net.extract_tree(z, "Textures", str(TEXTURES), root,
                              only_ext=(".png",))
        for p_ in w:
            rep.written.append(str(p_.relative_to(root)))
        log(f"      {len(w)} files (shaders + includes + texture)")


# ---------------------------------------------------------------- install

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
ROUTE_ADDONS = {
    FEEDER: (FEEDER_ADDON64, FEEDER_ADDON32),
    BRIDGE: (BRIDGE_ADDON,),
    ROUTE_RENODX: (RENODX_SF,),
}


def _foreign_addons(keep: str) -> list[tuple[str, str]]:
    """(route, filename) of every add-on that must not sit beside `keep`."""
    out = [(r, n) for r, names in ROUTE_ADDONS.items() if r != keep for n in names]
    # renodx-dlss5 is shared by the native, bridge and feeder routes, so it is
    # not in the table - but it and ShortFuse's build both hook NGX, and the
    # two loaded together fight over the same entry points.
    if keep == ROUTE_RENODX:
        out.append((NATIVE, RENODX))
    return out


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
    for rel in sorted(rep.preinstalled):
        if rel in rep.written:
            continue
        if (root / rel).exists():
            rep.written.append(rel)

    try:
        (root / MANIFEST).write_text(json.dumps({
            "version": 1,
            "complete": complete,
            "exe": g.exe.name if g.exe else None,
            "bitness": g.bitness,
            "api": g.api,
            "proxy": proxy,
            "provider": opt.provider,
            "path": opt.path,
            "reliability": level,
            "files": rep.written,
            "skipped": rep.skipped,
            "notes": rep.notes,
            "warnings": rep.warnings,
            "feed_cfg": opt.feed,
            "nr": opt.nr,
            "keep_game_dlss": opt.keep_game_dlss,
            "feeder_prerelease": opt.feeder_prerelease,
            "components": rep.components,
        }, ensure_ascii=False, indent=2), encoding="utf8")
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
    if path not in (NATIVE, BRIDGE, FEEDER, OPTI, ROUTE_RENODX):
        return None
    return Options(
        provider=int(data.get("provider") or 3),
        feed=dict(data.get("feed_cfg") or {}),
        nr=dict(data.get("nr") or {}),
        path=path,
        keep_game_dlss=bool(data.get("keep_game_dlss", True)),
        feeder_prerelease=bool(data.get("feeder_prerelease", False)),
        native_dlss=path in (NATIVE, OPTI) or bool(data.get("native_dlss", False)),
        opti_proxy=(data.get("proxy") or "") if path == OPTI else "",
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
    if not root.is_dir():
        raise InstallError(f"{root} does not exist.")

    probe = root / ".dlss5-autopilot-write-test"
    try:
        probe.write_bytes(b"x")
        probe.unlink()
    except PermissionError:
        raise InstallError(
            f"No permission to write into:\n{root}\n\n"
            f"Close the game if it is running, then try again. If that is not "
            f"it, right-click dlss5-autopilot.exe and choose 'Run as "
            f"administrator' - some games installed outside Steam or Epic sit "
            f"in folders only an administrator can write to.") from None
    except OSError as e:
        raise InstallError(f"Cannot write into {root}: {e}") from None

    if g.exe and g.exe.name.lower() in _running_processes():
        raise InstallError(
            f"{g.exe.name} is running. Close the game first - Windows will not "
            f"let anything replace files a running program has open, and a "
            f"half-finished install is worse than none.")

def install(g: games.Game, opt: Options, on_step=None, on_prog=None, on_log=None) -> Report:
    ok, why = check_supported(g)
    if not ok:
        raise InstallError(why)
    preflight(g)

    log = on_log or (lambda *_: None)
    step = on_step or (lambda *_: None)
    prog = on_prog or (lambda *_: None)

    root = g.install_dir
    rep = Report()
    x64 = g.bitness == 64
    proxy = _proxy_name(g.api, opt.reshade_proxy)
    host = root / HOST_DIR

    level, why_rel = reliability(g, opt.path)
    if level != STABLE:
        rep.warnings.append(f"{level}: {why_rel}")

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

    # Is another injector already in place?
    existing = root / proxy
    if opt.path != OPTI and existing.is_file() and not _is_reshade(existing):
        raise InstallError(
            f"{proxy} already exists but is not ReShade (DXVK, Special K or "
            f"another injector?). Remove it first, then try again.")

    # Switching routes must not leave the previous one behind. The routes put
    # very different things in the folder - the feeder alone drops 28 files,
    # including a ReShade.ini that would sit next to OptiScaler and confuse
    # everything - and the new manifest would not list them, so a later
    # uninstall could never clean them up either.
    # Read before anything is written: what is here that we put here.
    rep.preinstalled = _previously_ours(root)

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

    steps = plan(g, opt)
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
        # --- 0) DX9 needs dgVoodoo2 first -------------------------------------
        if g.api == "DX9":
            begin("dgVoodoo2 (DX9 -> D3D11)")
            for f in dgvoodoo.install(root, log):
                rep.written.append(f)
            rep.notes.append("dgVoodoo2 installed (DX9 -> D3D11). If the game will "
                             "not start, raise VRAM with dgVoodooCpl.exe.")

        if opt.path == OPTI:
            begin("OptiScaler (DLSS-NR build)")
            # A game that ships its own dxgi.dll (an ENB, DXVK, its own
            # wrapper) gets a different proxy name rather than having that
            # file replaced, unless the user picked one explicitly.
            oproxy = opt.opti_proxy or optiscaler.suggest_proxy(root)
            if oproxy != optiscaler.DEFAULT_PROXY and not opt.opti_proxy:
                log(f"      {optiscaler.DEFAULT_PROXY} is already taken here, "
                    f"installing as {oproxy} instead")
            orel = optiscaler.resolve()
            rep.components["optiscaler"] = orel[0]
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

            begin("OptiScaler configuration")
            optiscaler.enable_nr(root, log, settings=opt.nr)
            for line in optiscaler.describe_nr(opt.nr):
                rep.notes.append(line)
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
                f"{optiscaler.OVERLAY_KEY} in game to open its overlay, then "
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
        # The installer exe has a zip appended: both ReShade32.dll and ReShade64.dll.
        if g.api == "Vulkan":
            # A Vulkan game never loads dxgi.dll. ReShade reaches it as an
            # implicit Vulkan layer instead - a registry value the loader reads.
            manifest, fresh = vulkan.install_layer(setup, log)
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
            if not want and opt.path == FEEDER:
                # The feeder's stable release only accepts 4.55; anything
                # newer overlaps it and the DLSS feature dies in CreateFeature.
                want = sources.renodx_for_feeder(rep.components.get("feeder", ""))
                if want:
                    log(f"      DLSS5-Feeder {rep.components.get('feeder')} accepts "
                        f"renodx-dlss5 up to {want} - pinning to it (newer builds "
                        f"conflict; tick 'feeder pre-release' to use them)")
                    rep.notes.append(f"renodx-dlss5 pinned to {want} for this "
                                     f"feeder release - newer builds conflict "
                                     f"with it")
            e = sources.pick(catalog["renodx"], want)
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

        begin("nvngx_dlss.dll")
        game_has = (root / DLSS).is_file() and str(Path(DLSS)) not in rep.written
        if x64 and game_has and opt.keep_game_dlss:
            log("      the game ships its own nvngx_dlss.dll, left untouched")
            rep.skipped.append(DLSS)
        else:
            e = sources.pick(catalog["dlss"], opt.dlss)
            f = dl(e["url"], f"dlss-{e['label']}.zip")
            _backup(dlss_dir / DLSS, rep, root)
            _extract(f, DLSS, dlss_dir / DLSS, rep, root)
            rep.written.append(str((dlss_dir / DLSS).relative_to(root)))
            log(f"      nvngx_dlss {e['label']}")
            rep.notes.append(f"dlss version: {e['label']}")
            rep.components["dlss"] = e["label"]

        # --- 8) host64 --------------------------------------------------------
        if not x64 and opt.path == FEEDER:
            begin("host64 helper process")
            reshade_ini.write_addon_only_ini(host)
            rep.written.append(f"{HOST_DIR}/ReShade.ini")
            log(f"      {HOST_DIR}/ ready (ReShade + DLSS parts inside)")

        # --- 9) ReShade configuration ----------------------------------------
        begin("ReShade configuration")
        if opt.path == FEEDER:
            _backup(root / "ReShade.ini", rep, root)
            _backup(root / "ReShadePreset.ini", rep, root)
            reshade_ini.write_reshade_ini(root, opt.provider)
            reshade_ini.write_preset(root, opt.provider)
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
            rep.written.append("ReShade.ini")
            log("      add-on loading enabled (no shaders needed on this path)")

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
    except sources.RateLimited as e:
        _write_manifest(root, g, opt, rep, proxy, level, complete=False)
        log("")
        log(str(e))
        raise InstallError(str(e)) from e
    except Exception:
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
    prog(100, "Done")
    return rep


# ---------------------------------------------------------------- uninstall

def uninstall(g: games.Game, on_log=None) -> list[str]:
    """Remove only what this tool wrote; never touch the game's own files."""
    log = on_log or (lambda *_: None)
    root = g.install_dir
    man = root / MANIFEST
    removed: list[str] = []

    # Read ours, or an older release's, whichever is there.
    sources_ = [man] + [root / n for n in LEGACY_MANIFESTS]
    found_man = next((m for m in sources_ if m.is_file()), None)
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
    else:
        files = [FEEDER_ADDON64, FEEDER_ADDON32, RENODX, RENODX_SF, DLSSNR, DLSS,
                 BRIDGE_ADDON, BRIDGE_CFG, feedcfg.NAME, "dxgi.dll", "opengl32.dll",
                 "D3D9.dll", "dgVoodoo.conf", "dgVoodooCpl.exe",
                 "ReShade.ini", "ReShadePreset.ini",
                 str(SHADERS / FEEDER_FX)]
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
        log("No install record found; cleaning up by known filenames.")

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

    reshade_ini.remove_our_techniques(root)

    # The Vulkan layer is registered once for the whole user, so it may only be
    # removed when the LAST game that needs it goes. Removing it while another
    # Vulkan install still relies on it would silently break that game.
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

    for d in (root / INCLUDE, root / SHADERS, root / TEXTURES,
              root / "reshade-shaders"):
        try:
            if d.is_dir() and not any(d.iterdir()):
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
