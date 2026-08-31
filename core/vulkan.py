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


def layer_dir() -> Path:
    """Where we keep our own copy of the layer files."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "dlss5-autopilot" / "reshade-vulkan"


def _hives():
    import winreg
    return ((winreg.HKEY_CURRENT_USER, LAYER_KEY),
            (winreg.HKEY_LOCAL_MACHINE, LAYER_KEY))


def existing_registration() -> Path | None:
    """A ReShade Vulkan layer already registered by anything, if there is one."""
    try:
        import winreg
    except ImportError:
        return None
    for hive, key in _hives():
        try:
            with winreg.OpenKey(hive, key) as k:
                i = 0
                while True:
                    try:
                        name, _val, _type = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if "reshade" in name.lower() and name.lower().endswith(".json"):
                        p = Path(name)
                        if p.is_file():
                            return p
        except OSError:
            continue
    return None


def is_ours(path: Path) -> bool:
    try:
        return path.resolve().parent == layer_dir().resolve()
    except OSError:
        return False


def install_layer(setup_exe: Path, log=None) -> tuple[Path, bool]:
    """Extract the layer next to our data and register it.

    Returns (manifest path, newly_registered). An existing registration from
    ReShade's own installer is reused untouched.
    """
    from . import net
    log = log or (lambda *_: None)

    found = existing_registration()
    if found is not None and not is_ours(found):
        log(f"      ReShade's Vulkan layer is already registered "
            f"({found}); reusing it")
        return found, False

    d = layer_dir()
    d.mkdir(parents=True, exist_ok=True)
    net.extract_one(setup_exe, DLL, d / DLL)
    net.extract_one(setup_exe, MANIFEST, d / MANIFEST)

    # The manifest points at the DLL relative to itself, which is what we want,
    # but rewrite it anyway so a moved folder cannot leave a dangling path.
    try:
        data = json.loads((d / MANIFEST).read_text(encoding="utf8"))
        data.setdefault("layer", {})["library_path"] = f".\\{DLL}"
        (d / MANIFEST).write_text(json.dumps(data, indent=2), encoding="utf8")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    manifest = d / MANIFEST
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, LAYER_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, str(manifest), 0, winreg.REG_DWORD, 0)
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
    target = str(layer_dir() / MANIFEST)
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
