r"""Install engine: 64-bit, 32-bit and DX9 paths.

64-bit layout (next to the game executable):
    <proxy>.dll                 ReShade64.dll  (dxgi.dll or opengl32.dll)
    dlss5-feed.addon64
    renodx-dlss5.addon64
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

from . import (dgvoodoo, dlss, feedcfg, games, gpu, net, pe, prefs,
               reshade_ini, sources, vulkan)
# Imported by name as well: inside the Options class body the field
# `dlss: str | None` shadows the module, so `dlss.FEEDER` would read the
# field's default (None) instead of the module attribute.
from .dlss import BRIDGE, FEEDER, NATIVE

MANIFEST = "dlss5-autopilot.json"

FEEDER_ADDON64 = "dlss5-feed.addon64"
FEEDER_ADDON32 = "dlss5-feed.addon32"
FEEDER_HOST = "dlss5-feed-host64.exe"
FEEDER_FX = "DLSS5_Feed.fx"
RENODX = "renodx-dlss5.addon64"
DLSSNR = "nvngx_dlssnr.dll"
DLSS = "nvngx_dlss.dll"
HOST_DIR = "host64"

SHADERS = Path("reshade-shaders") / "Shaders"
INCLUDE = SHADERS / "include"
TEXTURES = Path("reshade-shaders") / "Textures"

BRIDGE_ADDON = "dlss5-bridge.addon64"
BRIDGE_CFG = "dlss5-bridge.cfg"

BACKUP_SUFFIX = ".dlss5-autopilot-backup"

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
    native_dlss: bool = False               # game ships its own DLSS


@dataclass
class Report:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- reliability

# Measured on real games, not guessed. DLSS 5 feeding was designed around
# DXGI; everything else is a bolt-on and fails far more often.
STABLE, BETA, EXPERIMENTAL = "stable", "beta", "experimental"

def reliability(g: games.Game, path: str = FEEDER) -> tuple[str, str]:
    """(level, explanation) - how likely this route is to actually work."""
    if path == NATIVE:
        return STABLE, ("The game's own DLSS is hooked directly - no synthetic "
                        "contract, no motion-vector shaders, and your in-game "
                        "DLSS quality setting still applies.")
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
    if g.bitness == 32:
        return EXPERIMENTAL, (
            "32-bit games go through a cross-process helper (host64). "
            "Upstream marks this beta and it often fails to start the DLSS "
            "feature.")
    if g.api == "OpenGL":
        return EXPERIMENTAL, (
            "OpenGL needs interop extensions the driver may not expose to "
            "this game, and the GPU must be forced to NVIDIA. Frequently "
            "does not work.")
    if g.api in ("DX11", "DX12", "Unknown"):
        return STABLE, "DirectX 10/11/12 is the path DLSS 5 feeding is built around."
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


def _proxy_name(api: str) -> str:
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
    if g.api == "DX9":
        if g.bitness != 32:
            return False, "64-bit DirectX 9 games are not supported (very rare)."
        return True, ""
    return True, ""


def _backup(dst: Path, rep: Report, root: Path) -> None:
    """Preserve the game's own file before overwriting it.

    If the game ships its own nvngx_dlss.dll and we replace it, uninstalling
    must be able to put it back - otherwise the game loses its DLSS for good.
    """
    if not dst.is_file():
        return
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


def _copy(src: Path, dst: Path, rep: Report, root: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
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
    steps.append("ReShade (Vulkan layer)" if g.api == "Vulkan" else "ReShade")

    if opt.path == FEEDER:
        steps.append("ReShade shader headers")
        steps.append("DLSS5-Feeder")
        if opt.provider in (3, 4):
            steps.append("LumeniteFX (motion vectors)")
    elif opt.path == BRIDGE:
        steps.append("dlss5-bridge")

    steps += ["DLSS 5 add-on (renodx)", "nvngx_dlssnr.dll", "nvngx_dlss.dll"]
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
        dest.write_bytes(net.fetch_text(sources.RESHADE_HEADERS_BASE + h))
        rep.written.append(str(Path(SHADERS) / h))
    log(f"      {', '.join(sources.RESHADE_HEADERS)}")

    begin("DLSS5-Feeder")
    tag, assets = sources.resolve_feeder()
    log(f"      DLSS5-Feeder {tag}")
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

def _write_manifest(root: Path, g: games.Game, opt: Options, rep: Report,
                    proxy: str, level: str, complete: bool) -> None:
    """Record what was written.

    Also written when an install FAILS part way: without it the orphaned files
    could not be cleaned up afterwards.
    """
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
        }, ensure_ascii=False, indent=2), encoding="utf8")
    except OSError:
        pass


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
    proxy = _proxy_name(g.api)
    host = root / HOST_DIR

    level, why_rel = reliability(g, opt.path)
    if level != STABLE:
        rep.warnings.append(f"{level}: {why_rel}")

    # Is another injector already in place?
    existing = root / proxy
    if existing.is_file() and not _is_reshade(existing):
        raise InstallError(
            f"{proxy} already exists but is not ReShade (DXVK, Special K or "
            f"another injector?). Remove it first, then try again.")

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

        # --- 1) ReShade -------------------------------------------------------
        begin("ReShade")
        ver, url = sources.resolve_reshade()

        setup = dl(url, f"ReShade_Setup_{ver}_Addon.exe")
        log(f"      ReShade {ver}")
        # The installer exe has a zip appended: both ReShade32.dll and ReShade64.dll.
        if g.api == "Vulkan":
            # A Vulkan game never loads dxgi.dll. ReShade reaches it as an
            # implicit Vulkan layer instead - a registry value the loader reads.
            manifest, fresh = vulkan.install_layer(setup, log)
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

        begin("DLSS 5 add-on (renodx)")
        # Even without an explicit choice, prefer a local build if one exists:
        # Discord releases are not on the mirror.
        if not opt.renodx_local and not opt.renodx:
            found, _ = prefs.find_renodx()
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
            _copy(src, dlss_dir / RENODX, rep, root)
            log(f"      {src.name} (your local file) -> {RENODX}")
            rep.notes.append(f"renodx: local file used ({src.name})")
        else:
            e = sources.pick(catalog["renodx"], opt.renodx)
            f = dl(e["url"], f"renodx-{e['label']}.zip")
            net.extract_one(f, ".addon64", dlss_dir / RENODX)
            rep.written.append(str((dlss_dir / RENODX).relative_to(root)))
            log(f"      renodx-dlss5 {e['label']}")
            rep.notes.append(f"renodx version: {e['label']}")

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
                      else catalog["dlssnr"])
        for e in candidates:
            f = dl(e["url"], f"dlssnr-{e['label']}.zip")
            net.extract_one(f, DLSSNR, dlss_dir / DLSSNR)
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
        if tried:
            rep.notes.append(f"skipped as incompatible: {', '.join(tried)}")
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
            net.extract_one(f, DLSS, dlss_dir / DLSS)
            rep.written.append(str((dlss_dir / DLSS).relative_to(root)))
            log(f"      nvngx_dlss {e['label']}")
            rep.notes.append(f"dlss version: {e['label']}")

        # --- 8) host64 --------------------------------------------------------
        if not x64 and opt.path == FEEDER:
            begin("host64 helper process")
            reshade_ini.write_addon_only_ini(host)
            rep.written.append(f"{HOST_DIR}/ReShade.ini")
            log(f"      {HOST_DIR}/ ready (ReShade + DLSS parts inside)")

        # --- 9) ReShade configuration ----------------------------------------
        begin("ReShade configuration")
        if opt.path == FEEDER:
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
            reshade_ini.write_addon_only_ini(root)
            rep.written.append("ReShade.ini")
            log("      add-on loading enabled (no shaders needed on this path)")

        # --- 10) dlss5-feed.cfg ----------------------------------------------
        if opt.path == FEEDER:
            begin("dlss5-feed.cfg")
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
            # The bridge writes its own defaults on first run; we only pre-set
            # the synthetic contract when the game has no DLSS of its own,
            # because it is off unless synth_after is set.
            lines = ["vk_mirror=1"]
            if not opt.native_dlss:
                lines.append("synth_after=3")
                log("      synth_after=3 (this game has no DLSS of its own, so "
                    "the bridge builds a synthetic contract)")
                rep.notes.append("bridge: synthetic contract armed after 3s")
            else:
                log("      mirroring the game's own DLSS contract")
            cfgp = root / BRIDGE_CFG
            if not cfgp.is_file():
                cfgp.write_text("\n".join(lines) + "\n", encoding="utf8")
                rep.written.append(BRIDGE_CFG)
            else:
                log("      existing dlss5-bridge.cfg left alone")

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
        files = [FEEDER_ADDON64, FEEDER_ADDON32, RENODX, DLSSNR, DLSS,
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
    for rel in files:
        if any(rel.endswith(s) for s in all_suffixes) or rel in restored:
            continue
        p = root / rel
        try:
            if p.is_file():
                p.unlink()
                removed.append(rel)
                log(f"removed: {rel}")
        except OSError as e:
            log(f"could not remove: {rel} ({e})")

    hostdir = root / HOST_DIR
    if hostdir.is_dir():
        shutil.rmtree(hostdir, ignore_errors=True)
        removed.append(HOST_DIR + "/")
        log(f"removed: {HOST_DIR}/")

    reshade_ini.remove_our_techniques(root)

    # A Vulkan layer we registered is global, so take it back out.
    try:
        if vulkan.unregister():
            removed.append("Vulkan layer registration")
            log("removed: our ReShade Vulkan layer registration")
    except Exception:
        pass

    for d in (root / INCLUDE, root / SHADERS, root / TEXTURES,
              root / "reshade-shaders"):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    man.unlink(missing_ok=True)
    for n in LEGACY_MANIFESTS:
        (root / n).unlink(missing_ok=True)
    log(f"Removed {len(removed)} items.")
    return removed
