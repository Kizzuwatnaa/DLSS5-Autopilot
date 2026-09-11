"""Windows PE (executable) inspection: bitness, import table, graphics API.

No third-party dependencies - the file is parsed by hand so you can audit
exactly what is read.
"""
from __future__ import annotations

import os
import re
import struct
import time
from pathlib import Path

from . import log

PE_X64 = 0x8664
PE_X86 = 0x014C

# Helper programs to skip when looking for the actual game executable.
_SKIP_PARTS = (
    "unins", "setup", "vcredist", "dxsetup", "dotnet", "prereq", "redist",
    "crashhandler", "crashreport", "crashpad", "easyanticheat", "battleye",
    "touchup", "installer", "activation", "cleanup", "helper", "webhelper",
    "unitycrashhandler", "ue4prereqsetup", "ue5prereqsetup", "epicwebhelper",
    # The feeder's 32-bit helper, which our own install puts under host64\.
    # Ranked above a 32-bit game executable it was taken for the game.
    "dlss5-feed-host",
)


class PEError(Exception):
    pass


def exe_bitness(path: Path) -> int:
    """Return 32 or 64, read from the PE COFF header's Machine field.

    Reads only the few bytes it needs - pulling entire executables into
    memory while scanning hundreds of games would be pointlessly slow.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                raise PEError("Not a Windows executable (no MZ signature).")
            (off,) = struct.unpack_from("<I", head, 0x3C)
            f.seek(off)
            sig = f.read(6)
            if len(sig) < 6 or sig[:4] != b"PE\0\0":
                raise PEError("No PE header found.")
            (machine,) = struct.unpack_from("<H", sig, 4)
    except OSError as e:
        raise PEError(f"Cannot read {path.name}: {e}") from e
    if machine == PE_X64:
        return 64
    if machine == PE_X86:
        return 32
    raise PEError(f"Unsupported machine type: 0x{machine:04x}")


def pe_imports(path: Path, delay: bool = False) -> list[str]:
    """Lower-cased DLL names from the executable's import table.

    `delay` reads the DELAY-LOAD table instead of the static one. A delay
    import is still a dependency the linker recorded - the loader just
    resolves it on first use - and some engines link d3d9.dll for something
    old while delay-loading the renderer they actually draw with (Grand
    Theft Auto V, issue #77: read as a DirectX 9 game and sent to a route
    that has nothing for it).

    Returns an empty list if anything cannot be parsed - that is not an
    error, just "unknown".
    """
    try:
        # Some game executables exceed 500 MB (e.g. Deathloop.exe at 486 MB)
        # and the import table can sit near the END of the file. Instead of
        # loading the file into memory we read only the few small regions we
        # need: PE header, section table, import descriptors, name strings.
        size = path.stat().st_size
        with open(path, "rb") as f:
            def at(offset: int, n: int) -> bytes:
                if offset < 0 or offset >= size:
                    return b""
                f.seek(offset)
                return f.read(n)

            head = at(0, 0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                return []
            (pe,) = struct.unpack_from("<I", head, 0x3C)
            if at(pe, 4) != b"PE\0\0":
                return []

            coff = at(pe + 4, 20)
            if len(coff) < 20:
                return []
            n_sections = struct.unpack_from("<H", coff, 2)[0]
            opt_size = struct.unpack_from("<H", coff, 16)[0]
            opt = pe + 24

            magic_b = at(opt, 2)
            if len(magic_b) < 2:
                return []
            magic = struct.unpack_from("<H", magic_b, 0)[0]
            if magic == 0x20B:      # PE32+
                dd = 112
            elif magic == 0x10B:    # PE32
                dd = 96
            else:
                return []

            # Data directory 1 is the import table, 13 the delay-load one.
            # NumberOfRvaAndSizes sits just before the directories and says
            # how many there are; an image with fewer would have this read
            # land in the section table instead.
            want = 13 if delay else 1
            n_dirs = struct.unpack_from("<I", at(opt + dd - 4, 4) or b"\0" * 4, 0)[0]
            if n_dirs <= want:
                return []
            imp = at(opt + dd + want * 8, 4)
            if len(imp) < 4:
                return []
            import_rva = struct.unpack_from("<I", imp, 0)[0]
            if import_rva == 0:
                return []

            sec_raw = at(opt + opt_size, n_sections * 40)
            sections = []
            for i in range(min(n_sections, len(sec_raw) // 40)):
                vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", sec_raw, i * 40 + 8)
                sections.append((vaddr, max(vsize, rawsize), rawptr))

            def to_off(rva: int) -> int | None:
                for vaddr, vlen, rawptr in sections:
                    if vaddr <= rva < vaddr + vlen:
                        return rawptr + (rva - vaddr)
                return None

            desc = to_off(import_rva)
            if desc is None:
                return []

            # Import descriptors are 20-byte records, delay-load ones 32,
            # with the DLL name at a different offset; grab them in one read.
            step, name_at = (32, 4) if delay else (20, 12)
            table = at(desc, step * 1024)
            names: list[str] = []
            for i in range(len(table) // step):
                if delay:
                    attrs, name_rva = struct.unpack_from("<II", table, i * step)
                    first_thunk = name_rva
                    # Attributes bit 0 clear means the old linkers wrote
                    # virtual addresses here, not RVAs; those cannot be
                    # resolved without the image base, so they are skipped.
                    if name_rva and not (attrs & 1):
                        continue
                else:
                    name_rva, first_thunk = struct.unpack_from(
                        "<II", table, i * step + name_at)
                if name_rva == 0 and first_thunk == 0:
                    break
                n_off = to_off(name_rva)
                if n_off is None:
                    continue
                blob = at(n_off, 256)          # a DLL name is a short string
                end = blob.find(b"\0")
                if end > 0:
                    names.append(blob[:end].decode("ascii", "ignore").lower())
            return names
    except Exception:
        return []


# API label -> the proxy DLL name ReShade is installed as
API_PROXY = {
    "DX10": "dxgi.dll",
    "DX11": "dxgi.dll",
    "DX12": "dxgi.dll",
    "OpenGL": "opengl32.dll",
    "Vulkan": None,   # needs a system-wide layer registration
    "DX9": None,      # needs DXVK first
}


def _has_d3d12_agility_sdk(folder: Path) -> bool:
    """A `D3D12/D3D12Core.dll` beside the exe: the Agility SDK, loaded at
    run time via SetD3D12SDKPath rather than a static import - so a game
    that only statically links d3d11.dll can still be a D3D12 title. Seen
    on Resident Evil Requiem, which ships DLSS Frame Generation and Ray
    Reconstruction (DX12-only NGX features) as further evidence."""
    try:
        if (folder / "D3D12" / "D3D12Core.dll").is_file():
            return True
        names = {f.name.lower() for f in folder.iterdir() if f.is_file()}
    except OSError:
        return False
    return "nvngx_dlssg.dll" in names or "nvngx_dlssd.dll" in names


def _ships_dlss(folder: Path) -> str:
    """The name of a DLSS runtime the GAME shipped, or "".

    Evidence that a title is not what its imports say: NGX runs on D3D11,
    D3D12 and Vulkan and never on DirectX 9. Files our own install put there
    (nvngx_dlssnr.dll always; nvngx_dlss.dll on a 64-bit game, per the
    manifest) are excluded, or every DX9 game would be re-labelled the
    moment it was set up once.
    """
    try:
        names = {f.name.lower() for f in folder.iterdir() if f.is_file()}
    except OSError:
        return ""
    # Every ReShade route places nvngx_dlss.dll beside a 64-bit game too, so
    # what our own install manifest lists as written is not the game's.
    ours: set[str] = set()
    if "dlss5-autopilot.json" in names:
        try:
            import json
            man = json.loads((folder / "dlss5-autopilot.json")
                             .read_text(encoding="utf8"))
            ours = {str(f).replace("\\", "/").rsplit("/", 1)[-1].lower()
                    for f in man.get("files") or [] if isinstance(f, str)}
        except (OSError, ValueError, AttributeError):
            ours = set()
    for n in ("nvngx_dlss.dll", "nvngx_dlssg.dll", "nvngx_dlssd.dll"):
        if n in names and n not in ours:
            return n
    if (folder / "D3D12" / "D3D12Core.dll").is_file():
        return "a D3D12 Agility SDK"
    return ""


def detect_api(path: Path) -> tuple[str, str]:
    """Return (api_label, reason).

    Order matters. Many Unreal games run on DX12 yet statically link
    opengl32.dll for unrelated reasons (e.g. Hell is Us, Fatekeeper - both
    import dxgi.dll AND opengl32.dll). Checking OpenGL first would pick the
    wrong proxy DLL and break the game, so DXGI presence decides.

    Confusing DX11 with DX12 does not change which proxy DLL ReShade goes
    in as - dxgi.dll either way - but it does change which route gets
    recommended (native/upstream/optiscaler need DX12), so a D3D12 Agility
    SDK folder or DLSS Frame Generation/Ray Reconstruction promotes the
    label even without a static d3d12.dll import.
    """
    imports = pe_imports(path)
    has = lambda d: any(d in i for i in imports)

    if has("d3d12.dll"):
        return "DX12", "imports d3d12.dll statically"
    if has("d3d11.dll"):
        if _has_d3d12_agility_sdk(path.parent):
            return ("DX12", "imports d3d11.dll, but ships a D3D12 Agility SDK "
                            "or DLSS Frame Generation/Ray Reconstruction - "
                            "the real renderer is D3D12")
        return "DX11", "imports d3d11.dll statically"
    if has("d3d10.dll") or has("d3d10_1.dll") or has("d3d10core.dll"):
        # DX10 is DXGI-based, so ReShade still installs as dxgi.dll. Only
        # the feeder reaches D3D10 (0.13.1+, through a private D3D11 relay
        # device) - and these games are rare.
        return "DX10", "imports d3d10.dll statically"
    # DXGI without d3d11/d3d12: API chosen at runtime, proxy is dxgi.dll anyway
    if has("dxgi.dll"):
        return "DX12", "imports dxgi.dll (DX11/DX12; proxy is dxgi.dll either way)"
    # Below here: no DXGI, so genuinely a non-DXGI API
    if has("vulkan-1.dll"):
        return "Vulkan", "imports vulkan-1.dll, no DXGI"
    if has("opengl32.dll"):
        engine = _engine_default(path)
        if engine:
            return engine
        return "OpenGL", "imports opengl32.dll, no DXGI"
    if has("d3d9.dll"):
        # A d3d9.dll in the import table is the weakest evidence in the whole
        # file. Engines keep it for a launcher, a video player, an old
        # settings dialog or a compatibility path long after they stopped
        # drawing with it: Red Dead Redemption 2 imports d3d9.dll and no DXGI
        # at all yet renders with D3D12 (issue #12), and Grand Theft Auto V
        # was read as a DirectX 9 game and sent to a route that has nothing
        # for it (issue #77 - which of the readings below rescues it has not
        # been checked on that executable). So every other kind of evidence
        # is asked first, and only a game with nothing else anywhere is
        # called DirectX 9.
        modern = _ships_dlss(path.parent)
        if modern:
            return ("DX12", f"imports d3d9.dll, but ships {modern} - the "
                            f"renderer is D3D12 or Vulkan, not DirectX 9. "
                            f"If the game is set to Vulkan, pick that in "
                            f"the settings before installing")
        if _has_d3d12_agility_sdk(path.parent):
            return ("DX12", "imports d3d9.dll, but ships a D3D12 Agility SDK "
                            "- the renderer is D3D12, not DirectX 9")
        # A delay-load is still a dependency the linker recorded; the loader
        # simply resolves it on first use. d3d11/d3d12 there means the game
        # draws with it. A bare dxgi.dll does not count: a DirectX 9 game
        # can use DXGI on its own just to enumerate displays.
        delayed = set(pe_imports(path, delay=True))
        for dll, api, label in (("d3d12.dll", "DX12", "Direct3D 12"),
                                ("d3d11.dll", "DX11", "Direct3D 11")):
            if dll in delayed:
                return (api, f"imports d3d9.dll, but delay-loads {dll} - the "
                             f"renderer is {label} and the d3d9 import is a "
                             f"leftover")
        # Last: what the file says about itself - but only the parts of it
        # that are records rather than text. _runtime_graphics ranks d3d11
        # above d3d9, and every executable here has "d3d9.dll" in its bytes
        # (the import table's own name string), so a game that merely
        # MENTIONS d3d11.dll - SDL2 carries that literal for its render
        # backend - would come back as D3D11 and be sent to a dxgi proxy it
        # never loads. A 32-bit game reaching here is DirectX 9 in every
        # case seen so far and has the most to lose from a wrong answer, so
        # it is left alone entirely.
        try:
            wide = exe_bitness(path) == 64
        except PEError:
            # Unreadable is not evidence of anything; a game that cannot be
            # parsed keeps the answer its import table already gave.
            wide = False
        if wide:
            name, where = _runtime_graphics(path)
            label = {"DX12": "Direct3D 12", "DX11": "Direct3D 11"}
            # "named in the exe" is a string; an engine's marker file or
            # another DLL's import table is a record. Only records count.
            strong = where != "named in the exe"
            if strong and name in ("d3d12.dll", "d3d11.dll"):
                api = _RUNTIME_API[name]
                return (api, f"imports d3d9.dll, but {name} is the one it "
                             f"loads at run time ({where}) - the renderer is "
                             f"{label[api]}, not DirectX 9")
            if strong and name in label:
                return (name, f"imports d3d9.dll, but {where} - the renderer "
                              f"is {label[name]}, not DirectX 9")
        return "DX9", "imports d3d9.dll, no DXGI"
    if _has_d3d12_agility_sdk(path.parent):
        return ("DX12", "no graphics DLL imported statically, but ships a "
                        "D3D12 Agility SDK or DLSS Frame Generation/Ray "
                        "Reconstruction - the real renderer is D3D12")
    # No static graphics import at all: the engine LoadLibrary()s its
    # renderer (Chrome Engine's Call of Juarez: Gunslinger names d3d9.dll in
    # the exe and imports nothing - issue #31). The name it will load is
    # still in the file, or in the engine DLL beside it.
    name, where = _runtime_graphics(path)
    if name:
        api = _RUNTIME_API.get(name, name)
        if name not in _RUNTIME_API:
            return api, where
        if api == "DX9" and _ships_dlss(path.parent):
            api = "DX12"
        return api, f"loads {name} at run time ({where}); no static graphics import"
    return "Unknown", "graphics DLL loaded at runtime; assuming DX11/DX12 via dxgi.dll"


# Same priority as the static table above: DXGI evidence beats everything, a
# lone d3d9.dll is DirectX 9.
_RUNTIME_API = {
    "d3d12.dll": "DX12", "d3d11.dll": "DX11", "d3d10.dll": "DX10",
    "dxgi.dll": "DX12", "vulkan-1.dll": "Vulkan", "opengl32.dll": "OpenGL",
    "d3d9.dll": "DX9",
}
_RUNTIME_SCAN_MAX = 512 * 1024 * 1024
# Files our own routes (or ReShade, DXVK, OptiScaler) drop beside a game;
# their imports say nothing about the game.
_NOT_THE_GAME = set(_RUNTIME_API) | {
    "reshade32.dll", "reshade64.dll", "reshade.dll", "d3d8.dll", "ddraw.dll",
    "dinput8.dll", "winmm.dll", "version.dll", "dbghelp.dll", "winhttp.dll",
    "wininet.dll", "d3dcompiler_47.dll",
    "nvngx_dlssnr.dll", "nvngx_dlss.dll", "nvngx_dlssd.dll", "nvngx_dlssg.dll",
    "_nvngx.dll", "nvngx-wrapper.dll", "remix_nvngx.dll",
    "optiscaler.dll", "dlss5-bridge.dll", "dlss-enabler.dll",
    "dlss-enabler-headless.dll", "rtx40mfgcore.dll",
    "sl.interposer.dll", "sl.common.dll", "sl.reflex.dll", "sl.dlss.dll",
    "sl.dlss_g.dll", "sl.dlss_d.dll", "sl.pcl.dll",
    "libxess.dll", "libxess_dx11.dll", "libxess_fg.dll", "libxell.dll",
    "amd_fidelityfx_dx12.dll", "amd_fidelityfx_vk.dll",
    "amd_fidelityfx_upscaler_dx12.dll", "amd_fidelityfx_framegeneration_dx12.dll",
    "amd_fidelityfx_loader_dx12.dll",
    "ffx_fsr2_api_x64.dll", "ffx_fsr2_api_dx12_x64.dll", "ffx_fsr2_api_vk_x64.dll",
    "ffx_fsr3upscaler_x64.dll", "ffx_backend_dx12_x64.dll", "ffx_backend_vk_x64.dll",
}
# Middleware that names every graphics API it instruments, whichever one the
# game uses. NVIDIA's Aftermath crash library names d3d12.dll and dxgi.dll in
# every build - Vulkan games and the Remix runtime included - and made
# Enshrouded, a Vulkan game, "DX12": OptiScaler went in as dxgi.dll and never
# loaded (#130). AMD's AGS and Epic's online services SDK do the same.
_NAMES_EVERY_API = {
    "gfsdk_aftermath_lib.x64.dll", "gfsdk_aftermath_lib.dll",
    "amd_ags_x64.dll", "amd_ags_x86.dll", "eossdk-win64-shipping.dll",
    "eossdk-win32-shipping.dll",
}


def _ours_in(folder: Path) -> set[str]:
    """Names the tool's own manifest in this folder says it wrote."""
    import json
    try:
        data = json.loads((folder / "dlss5-autopilot.json").read_text(encoding="utf8"))
        files = data.get("files") if isinstance(data, dict) else None
        return {str(f).replace("\\", "/").rsplit("/", 1)[-1].lower()
                for f in (files or []) if isinstance(f, str)}
    except (OSError, ValueError):
        return set()


def _names_in(path: Path) -> set[str]:
    """Which graphics DLL names the file mentions (ANSI or UTF-16)."""
    found: set[str] = set()
    needles = {n: (n.encode(), n.encode("utf-16-le")) for n in _RUNTIME_API}
    try:
        size = path.stat().st_size
        if size > _RUNTIME_SCAN_MAX:
            return found
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                low = (tail + chunk).lower()
                for n, (a, u) in needles.items():
                    if n not in found and (a in low or u in low):
                        found.add(n)
                tail = chunk[-64:]
    except OSError:
        pass
    return found


# An engine module beside the exe that settles the renderer by itself. Unity
# names every backend it can drive (d3d11, d3d12, opengl32, vulkan-1) in
# UnityPlayer.dll and in older players in the exe, and picks Direct3D 11 on
# Windows unless the game is started with -force-glcore/-force-vulkan/
# -force-d3d12. Taking opengl32.dll from those strings put Cities: Skylines
# II, House Party and every other Unity game on the OpenGL route, where the
# game never loads opengl32.dll and ReShade never appears (issues #46-#48).
_ENGINE_DEFAULT = {
    "unityplayer.dll": ("DX11", "Unity player beside the exe - Direct3D 11 "
                                "on Windows unless the game is started with "
                                "-force-d3d12, -force-vulkan or -force-glcore"),
}
# How many of the largest DLLs beside the exe get their strings read when
# their import tables name no renderer either. An engine DLL of a few MB
# that LoadLibrary()s its backend is the usual case.
_RUNTIME_SIBLING_STRINGS = 6
_RUNTIME_SIBLING_MIN = 512 * 1024
_RUNTIME_SIBLING_MAX = 64 * 1024 * 1024
# Named by engines that can drive several backends, used by few: a lone
# mention in the exe is not yet the renderer.
_AMBIGUOUS = {"opengl32.dll", "vulkan-1.dll"}


def _engine_default(exe: Path, names: dict[str, Path] | None = None) -> tuple[str, str] | None:
    """(api, reason) when an engine module beside the exe settles it."""
    if names is None:
        try:
            names = {p.name.lower(): p for p in exe.parent.iterdir()
                     if p.suffix.lower() == ".dll"}
        except OSError:
            return None
    for n, hit in _ENGINE_DEFAULT.items():
        if n in names and names[n].is_file():
            return hit
    return None


def _runtime_graphics(exe: Path) -> tuple[str, str]:
    """(dll name, where it was seen) for a renderer loaded at run time.

    A known engine module beside the exe decides first. Then the exe's own
    strings: a Direct3D name there is taken as it stands (Call of Juarez:
    Gunslinger names d3d9.dll and nothing else - #31), because the DLLs
    beside a game mention renderers of their own (Bink, CEF, SDL name
    d3d11.dll) and must not overrule the game. Only when the exe names
    nothing, or nothing but opengl32.dll / vulkan-1.dll - which engines
    that can drive several backends name without using - are the DLLs
    beside it consulted: their import tables, then the strings of the
    largest of them, ranked in the static table's order so a Direct3D
    name outranks OpenGL or Vulkan. Proxy DLLs and the files our routes
    place are not consulted.
    """
    try:
        names = {p.name.lower(): p for p in exe.parent.iterdir()
                 if p.suffix.lower() == ".dll"}
    except OSError:
        names = {}
    engine = _engine_default(exe, names)
    if engine:
        return engine
    found = _names_in(exe)
    own = next((n for n in _RUNTIME_API if n in found), "")
    if own and own not in _AMBIGUOUS:
        return own, "named in the exe"
    seen: dict[str, str] = {}
    if own:
        seen[own] = "named in the exe"
    ours = _NOT_THE_GAME | _NAMES_EVERY_API | _ours_in(exe.parent)
    sibs = sorted((p for n, p in names.items() if n not in ours
                   and p.is_file()), key=lambda p: p.name.lower())[:60]
    for dll in sibs:
        for imp in pe_imports(dll):
            base = imp.rsplit("/", 1)[-1]
            if base in _RUNTIME_API:
                seen.setdefault(base, f"{dll.name} beside the exe imports it")

    def _size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return 0
    big = sorted((p for p in sibs
                  if _RUNTIME_SIBLING_MIN <= _size(p) <= _RUNTIME_SIBLING_MAX),
                 key=lambda p: -_size(p))[:_RUNTIME_SIBLING_STRINGS]
    for dll in big:
        for n in _names_in(dll):
            seen.setdefault(n, f"named in {dll.name} beside the exe")
    for n in _RUNTIME_API:              # dict order is the priority order
        if n in seen:
            return n, seen[n]
    return "", ""


def looks_like_game(exe: Path) -> bool:
    low = exe.name.lower()
    return not any(p in low for p in _SKIP_PARTS)


# Directories never worth descending into: redistributables, anti-cheat,
# engine tooling.
_PRUNE_DIRS = {
    "_commonredist", "commonredist", "redist", "redistributable", "redistributables",
    "directx", "dotnet", "vcredist", "vc_redist", "easyanticheat", "easyanticheat_eos",
    "battleye", "punkbuster", "installers", "installer", "prerequisites", "prereq",
    "support", "docs", "manual", "soundtrack", "artbook", "extras", "dxsetup",
    "crashreportclient", "epicwebhelper", "thirdparty", "steamvr", "openvr",
    "__installer", "dotnetfx", "movies", "content", "data", "assets", "textures",
    "host64",
}
_MAX_DEPTH = 5


# A scan must finish. The depth and the 400-executable cap below are not
# enough on their own: a tree can hold hundreds of thousands of directories
# with no .exe in any of them, and then neither limit ever fires. Reported on
# C:\XboxGames (an empty GameSave folder, and the Minecraft Launcher's
# runtime tree) where the scan stopped for minutes and eventually died.
_WALK_DIRS = 6000
_WALK_SECONDS = 6.0


def _walk_exes(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Walk the folder to a bounded depth collecting .exe files."""
    found: list[Path] = []
    base_depth = len(folder.parts)
    seen = 0
    deadline = time.monotonic() + _WALK_SECONDS
    for root, dirs, files in os.walk(folder, topdown=True):
        rp = Path(root)
        depth = len(rp.parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.lower() not in _PRUNE_DIRS
                       and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".exe"):
                found.append(rp / f)
        seen += 1
        if len(found) > 400:      # don't get stuck in pathological trees
            break
        if seen >= _WALK_DIRS or (seen % 64 == 0
                                  and time.monotonic() > deadline):
            log.write(f"stopped looking for executables under {folder} after "
                      f"{seen} folders - too big to search", "warn")
            break
    return found


def _score(exe: Path, folder: Path) -> float:
    """Higher score = more likely to be the real game executable."""
    rel = str(exe.relative_to(folder)).lower().replace("\\", "/")
    stem = exe.stem.lower()
    s = 0.0

    # Unreal's "-Shipping" suffix is the strongest signal
    if stem.endswith("-shipping") or "shipping" in stem:
        s += 1000
    # Known binary directories
    if "/binaries/win64/" in rel or "/bin/win64/" in rel or rel.startswith("binaries/win64/"):
        s += 400
    elif "/binaries/win32/" in rel or "/bin/win32/" in rel:
        s += 300
    elif "/bin/" in rel or rel.startswith("bin/"):
        s += 200
    # Engine tooling is never the game itself
    if "/engine/binaries/" in rel or rel.startswith("engine/binaries/"):
        s -= 900
    # Executable name resembling the folder name
    fn = re.sub(r"[^a-z0-9]", "", folder.name.lower())
    sn = re.sub(r"[^a-z0-9]", "", stem)
    if fn and sn and (sn in fn or fn in sn):
        s += 350
    # An executable in the root is usually a good candidate
    if exe.parent == folder:
        s += 120
    # Helper program names
    if not looks_like_game(exe):
        s -= 1500
    # Size (log-ish, capped at a few hundred points)
    try:
        mb = exe.stat().st_size / (1024 * 1024)
        s += min(mb, 300) * 1.2
    except OSError:
        pass
    # Very deep = probably a helper
    s -= rel.count("/") * 15
    # Unity keeps the game's data in <exe name>_Data beside the player, and
    # nothing else has one. The player exe itself is small - under a
    # megabyte - so size alone ranked the launcher beside it higher (#152).
    try:
        if (exe.parent / f"{exe.stem}_Data").is_dir():
            s += 300
    except OSError:
        pass
    return s


_TRIAL = re.compile(r"[\s._-]*(trial|demo)$")
# "UBOAT Launcher.exe" beside "UBOAT.exe": the launcher is the settings box
# that starts the game, and ReShade in front of it hooks nothing (#152).
_LAUNCHER = re.compile(r"[\s._-]*launcher$")


def find_game_exes(folder: Path) -> list[Path]:
    """Candidate game executables, most likely first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    cands = _walk_exes(folder)
    if not cands:
        return []
    score = {p: _score(p, folder) for p in cands}
    # A trial or demo build beside the full game's executable matches the
    # folder name just as well and is often as large, so it won: Need for
    # Speed Heat opened on NeedForSpeedHeatTrial.exe (#131). Only when the
    # full one is right beside it - a demo-only install keeps its exe, and
    # the trial stays in the list for whoever plays it.
    stems = {(p.parent, p.stem.lower()) for p in cands}
    for p in cands:
        for pat in (_TRIAL, _LAUNCHER):
            base = pat.sub("", p.stem.lower())
            if base != p.stem.lower() and (p.parent, base) in stems:
                score[p] -= 600
    scored = sorted(cands, key=lambda p: score[p], reverse=True)
    # Drop obvious helpers, but never return nothing if that is all there is.
    good = [p for p in scored if score[p] > -500]
    return good or scored


def resolve_target(target: Path) -> tuple[Path, list[Path]]:
    """The user may pass an .exe or a folder. Returns (chosen, all candidates)."""
    target = Path(target)
    if target.is_file() and target.suffix.lower() == ".exe":
        return target, [target]
    if target.is_dir():
        cands = find_game_exes(target)
        if not cands:
            raise PEError(f"No .exe found in {target}.")
        return cands[0], cands
    raise PEError(f"{target} not found.")
