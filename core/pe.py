"""Windows PE (executable) inspection: bitness, import table, graphics API.

No third-party dependencies - the file is parsed by hand so you can audit
exactly what is read.
"""
from __future__ import annotations

import os
import re
import struct
from pathlib import Path

PE_X64 = 0x8664
PE_X86 = 0x014C

# Helper programs to skip when looking for the actual game executable.
_SKIP_PARTS = (
    "unins", "setup", "vcredist", "dxsetup", "dotnet", "prereq", "redist",
    "crashhandler", "crashreport", "crashpad", "easyanticheat", "battleye",
    "touchup", "installer", "activation", "cleanup", "helper", "webhelper",
    "unitycrashhandler", "ue4prereqsetup", "ue5prereqsetup", "epicwebhelper",
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


def pe_imports(path: Path) -> list[str]:
    """Lower-cased DLL names from the executable's static import table.

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

            imp = at(opt + dd + 8, 4)
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

            # Import descriptors are 20-byte records; grab them in one read.
            table = at(desc, 20 * 1024)
            names: list[str] = []
            for i in range(len(table) // 20):
                name_rva, first_thunk = struct.unpack_from("<II", table, i * 20 + 12)
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
    "DX9": None,      # needs dgVoodoo2 first
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
        # DX10 is DXGI-based, so ReShade still installs as dxgi.dll. Neither
        # the add-on nor the bridge hooks D3D10 itself, so only the feeder's
        # synthetic contract can reach these - and they are rare.
        return "DX10", "imports d3d10.dll statically"
    # DXGI without d3d11/d3d12: API chosen at runtime, proxy is dxgi.dll anyway
    if has("dxgi.dll"):
        return "DX12", "imports dxgi.dll (DX11/DX12; proxy is dxgi.dll either way)"
    # Below here: no DXGI, so genuinely a non-DXGI API
    if has("vulkan-1.dll"):
        return "Vulkan", "imports vulkan-1.dll, no DXGI"
    if has("opengl32.dll"):
        return "OpenGL", "imports opengl32.dll, no DXGI"
    if has("d3d9.dll"):
        return "DX9", "imports d3d9.dll, no DXGI"
    if _has_d3d12_agility_sdk(path.parent):
        return ("DX12", "no graphics DLL imported statically, but ships a "
                        "D3D12 Agility SDK or DLSS Frame Generation/Ray "
                        "Reconstruction - the real renderer is D3D12")
    return "Unknown", "graphics DLL loaded at runtime; assuming DX11/DX12 via dxgi.dll"


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
}
_MAX_DEPTH = 5


def _walk_exes(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Walk the folder to a bounded depth collecting .exe files."""
    found: list[Path] = []
    base_depth = len(folder.parts)
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
        if len(found) > 400:      # don't get stuck in pathological trees
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
    return s


def find_game_exes(folder: Path) -> list[Path]:
    """Candidate game executables, most likely first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    cands = _walk_exes(folder)
    if not cands:
        return []
    scored = sorted(cands, key=lambda p: _score(p, folder), reverse=True)
    # Drop obvious helpers, but never return nothing if that is all there is.
    good = [p for p in scored if _score(p, folder) > -500]
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
