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
    try:
        data = json.loads((d / manifest).read_text(encoding="utf8"))
        data.setdefault("layer", {})["library_path"] = f".\\{dll}"
        (d / manifest).write_text(json.dumps(data, indent=2), encoding="utf8")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return d / manifest


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
    found = registered_for(x64=not also32)
    if found is not None and not is_ours(found):
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
