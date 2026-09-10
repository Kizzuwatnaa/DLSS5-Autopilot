r"""Vulkan support: registering ReShade as an implicit Vulkan layer.

A Vulkan game never loads dxgi.dll, so the proxy-DLL trick used everywhere
else does not apply. ReShade reaches Vulkan as an *implicit layer*: a JSON
manifest on disk, referenced by a registry value the Vulkan loader reads at
application start.

    HKCU\Software\Khronos\Vulkan\ImplicitLayers
        <full path to ReShade64.json>  =  (DWORD) 0

We use HKEY_CURRENT_USER on purpose: it needs no administrator rights and
only affects this user. ReShade's own installer writes the same value under
HKLM when run elevated, and an existing registration of either kind is reused
rather than duplicated.

IMPORTANT, and the tool says so before doing it: an implicit layer is GLOBAL.
Once registered, ReShade loads into every Vulkan application on this account,
not just the game being set up. Uninstalling removes the value again.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

LAYER_KEY = r"Software\Khronos\Vulkan\ImplicitLayers"
LAYER_NAME = "VK_LAYER_reshade"
DLL = "ReShade64.dll"
MANIFEST = "ReShade64.json"
# A 32-bit game needs the 32-bit layer; the loader picks by the manifest's
# DLL. Registered alongside the 64-bit one only when a 32-bit game asks.
DLL32 = "ReShade32.dll"
MANIFEST32 = "ReShade32.json"
# ...under a DIFFERENT layer name, and this is load-bearing. Both of
# ReShade's manifests call themselves VK_LAYER_reshade, and the key we
# register under - HKCU\Software - is NOT redirected per architecture the
# way HKLM\Software is, so a 32-bit game's loader sees both of ours at
# once. The loader then de-duplicates implicit layers BY NAME, keeps the
# first (the 64-bit one) and throws the 32-bit one away - and then refuses
# the survivor. Its own words, from a 32-bit process:
#
#   Removing layer VK_LAYER_reshade (...ReShade32.json) because it is a
#     duplicate of VK_LAYER_reshade (...ReShade64.json)
#   Requested layer "VK_LAYER_reshade" was wrong bit-type.
#
# Nothing loads, no ReShade.log is written, and the game looks untouched -
# Call of Juarez: Gunslinger (#31, four rounds of wrong answers), Bayonetta
# (#2) and every other 32-bit game that goes through DXVK. Giving the
# 32-bit manifest its own name makes both survive and each process load the
# one it can. Verified with the Vulkan loader's own trace, 32-bit and
# 64-bit, before and after.
LAYER_NAME32 = "VK_LAYER_reshade32"


def layer_dir() -> Path:
    """Where we keep our own copy of the layer files."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "dlss5-autopilot" / "reshade-vulkan"


def _hives():
    import winreg
    return ((winreg.HKEY_CURRENT_USER, LAYER_KEY),
            (winreg.HKEY_LOCAL_MACHINE, LAYER_KEY))


def registrations() -> list[tuple[Path, int]]:
    """Every registered ReShade layer manifest, with its registry value.

    The value is what the Vulkan loader reads: 0 means the implicit layer is
    active, anything else means it is registered but DISABLED. Reusing a
    disabled registration is how an install could finish, report success and
    still leave the game without ReShade.
    """
    out: list[tuple[Path, int]] = []
    try:
        import winreg
    except ImportError:
        return out
    for hive, key in _hives():
        try:
            with winreg.OpenKey(hive, key) as k:
                i = 0
                while True:
                    try:
                        name, val, _type = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if "reshade" in name.lower() and name.lower().endswith(".json"):
                        p = Path(name)
                        if p.is_file():
                            out.append((p, val if isinstance(val, int) else 1))
        except OSError:
            continue
    return out


def existing_registration() -> Path | None:
    """An ACTIVE ReShade Vulkan layer registered by anything, if there is one."""
    for path, val in registrations():
        if val == 0:
            return path
    return None


def manifest_x64(path: Path) -> bool | None:
    """True for a 64-bit layer manifest, False for 32-bit, None if unclear.

    A 64-bit game cannot load ReShade32.dll and a 32-bit game cannot load
    ReShade64.dll, so "a ReShade layer is registered" is not the question -
    "is one registered for THIS game's architecture" is.
    """
    lib = ""
    try:
        data = json.loads(path.read_text(encoding="utf8"))
        lib = str(data.get("layer", {}).get("library_path", ""))
    except (OSError, ValueError, AttributeError, TypeError):
        lib = ""
    name = (lib.replace("/", "\\").rsplit("\\", 1)[-1] or path.name).lower()
    if "32" in name:
        return False
    if "64" in name:
        return True
    return None


def registered_for(x64: bool) -> Path | None:
    """An active layer this game's architecture can actually load."""
    for path, val in registrations():
        if val != 0:
            continue
        arch = manifest_x64(path)
        if arch is None or arch is x64:
            return path
    return None


def is_ours(path: Path) -> bool:
    try:
        return path.resolve().parent == layer_dir().resolve()
    except OSError:
        return False


def _place(setup_exe: Path, d: Path, dll: str, manifest: str) -> Path:
    from . import net
    net.extract_one(setup_exe, dll, d / dll)
    net.extract_one(setup_exe, manifest, d / manifest)
    # The manifest points at the DLL relative to itself, which is what we want,
    # but rewrite it anyway so a moved folder cannot leave a dangling path.
    # The name is rewritten for the 32-bit layer at the same time: see
    # LAYER_NAME32 above - sharing one name is what stopped it loading.
    try:
        data = json.loads((d / manifest).read_text(encoding="utf8"))
        layer = data.setdefault("layer", {})
        layer["library_path"] = f".\\{dll}"
        if manifest == MANIFEST32:
            layer["name"] = LAYER_NAME32
        (d / manifest).write_text(json.dumps(data, indent=2), encoding="utf8")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return d / manifest


def layer_name(path: Path) -> str:
    """The name a manifest declares - what the loader de-duplicates on."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf8"))
        return str(data.get("layer", {}).get("name", ""))
    except (OSError, ValueError, AttributeError, TypeError):
        return ""


def name_clash() -> Path | None:
    """Our own 32-bit manifest, when it still shares the 64-bit one's name.

    Installs made before this was understood left a ReShade32.json calling
    itself VK_LAYER_reshade, and it stays broken until it is rewritten -
    reinstalling reused it rather than replacing it.
    """
    m32 = layer_dir() / MANIFEST32
    m64 = layer_dir() / MANIFEST
    if not m32.is_file():
        return None
    n32 = layer_name(m32)
    if n32 and n32 != LAYER_NAME32 and (not m64.is_file()
                                        or n32 == layer_name(m64)):
        return m32
    return None


def _register(manifest: Path) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, LAYER_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, str(manifest), 0, winreg.REG_DWORD, 0)


def install_layer(setup_exe: Path, log=None, also32: bool = False) -> tuple[Path, bool]:
    """Extract the layer next to our data and register it.

    Returns (manifest path, newly_registered). An existing registration from
    ReShade's own installer is reused untouched. `also32` adds the 32-bit
    layer for a 32-bit game (it is registered under its own manifest).
    """
    log = log or (lambda *_: None)

    # A 32-bit game needs a 32-bit layer; a registration that only covers the
    # other architecture is no use to it. Reported twice on 32-bit DX9 games
    # (Bayonetta, GTA IV) after 1.6.0 sent DirectX 9 through DXVK: the install
    # said "reusing the Vulkan layer that is already registered", the game ran
    # on Vulkan, and ReShade was never in it.
    # Ours, but written before the name clash was understood: it has to be
    # rewritten, not reused, or the 32-bit game gets nothing again.
    stale = name_clash() if also32 else None
    if stale is not None:
        log(f"      the 32-bit layer registered here shares the 64-bit "
            f"layer's name, which is why it never loaded - rewriting it")

    found = registered_for(x64=not also32)
    if found is not None and not is_ours(found) and stale is None:
        log(f"      ReShade's Vulkan layer is already registered "
            f"({found}); reusing it")
        return found, False

    if found is None and existing_registration() is not None:
        log("      a ReShade Vulkan layer is registered, but not one this "
            "game's architecture can load - adding ours")

    d = layer_dir()
    d.mkdir(parents=True, exist_ok=True)
    manifest = _place(setup_exe, d, DLL, MANIFEST)
    try:
        _register(manifest)
        if also32:
            m32 = _place(setup_exe, d, DLL32, MANIFEST32)
            _register(m32)
            log(f"      registered the 32-bit layer as well ({m32.name})")
    except OSError as e:
        raise RuntimeError(
            f"Could not register the Vulkan layer: {e}") from e
    log(f"      registered {LAYER_NAME} for this user")
    log(f"      {manifest}")
    return manifest, True


def unregister() -> bool:
    """Remove only a registration we created. True when one was removed."""
    try:
        import winreg
    except ImportError:
        return False
    removed = False
    for m in (MANIFEST, MANIFEST32):
        target = str(layer_dir() / m)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYER_KEY, 0,
                                winreg.KEY_ALL_ACCESS) as k:
                try:
                    winreg.DeleteValue(k, target)
                    removed = True
                except OSError:
                    pass
        except OSError:
            pass
    return removed
