r"""VR: registering ReShade as an implicit OpenXR API layer.

A VR game that runs on OpenXR draws the headset's image through it, and
the desktop window is only a mirror of it (games on OpenVR/SteamVR are
not reached by this layer). A proxy DLL or the Vulkan layer puts ReShade - and
the neural pass - on that mirror; the headset never sees it (issue #33).
ReShade 6 ships an OpenXR layer (ReShade64_XR.json, the same ReShade64.dll)
that hooks the OpenXR swapchain, which is the picture the headset shows.

    HKCU\Software\Khronos\OpenXR\1\ApiLayers\Implicit
        <full path to ReShade64_XR.json>  =  (DWORD) 0

Like the Vulkan layer this is global for the user: once registered, ReShade
loads into every OpenXR application. Uninstalling the last VR install
removes the value. Whether the feeder and the DLSS 5 add-ons behave inside
an OpenXR swapchain has not been tried by the author - there is no headset
here - so the option is marked experimental and asks for a report.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import vulkan

LAYER_KEY = r"Software\Khronos\OpenXR\1\ApiLayers\Implicit"
LAYER_NAME = "XR_APILAYER_reshade"
DLL = vulkan.DLL                    # ReShade64.dll, shared with the Vulkan layer
MANIFEST = "ReShade64_XR.json"


def layer_dir() -> Path:
    return vulkan.layer_dir()


def registrations() -> list[tuple[Path, int]]:
    """Every registered ReShade OpenXR manifest with its registry value
    (0 = active)."""
    out: list[tuple[Path, int]] = []
    try:
        import winreg
    except ImportError:
        return out
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, LAYER_KEY) as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if "reshade" in os.path.basename(name).lower() \
                            and name.lower().endswith(".json"):
                        out.append((Path(name), int(value) if isinstance(value, int) else 1))
        except OSError:
            continue
    return out


def existing_registration() -> Path | None:
    for path, value in registrations():
        if value == 0 and path.is_file():
            return path
    return None


def is_ours(path: Path) -> bool:
    try:
        return path.parent.resolve() == layer_dir().resolve()
    except OSError:
        return False


def _register(manifest: Path) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, LAYER_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, str(manifest), 0, winreg.REG_DWORD, 0)


def install_layer(setup_exe: Path, log=None) -> tuple[Path, bool]:
    """Extract ReShade's OpenXR layer beside our Vulkan layer files and
    register it for this user. Returns (manifest, newly_registered). A
    registration from ReShade's own installer is reused untouched."""
    log = log or (lambda *_: None)
    found = existing_registration()
    if found is not None and not is_ours(found):
        log(f"      ReShade's OpenXR layer is already registered ({found}); "
            f"reusing it")
        return found, False
    from . import net
    d = layer_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        net.extract_one(setup_exe, DLL, d / DLL)
        net.extract_one(setup_exe, MANIFEST, d / MANIFEST)
    except OSError as e:
        # The DLL is shared with the Vulkan layer: a running Vulkan or
        # OpenXR program holds it open and the write is refused.
        raise RuntimeError(f"{DLL} is in use by a running Vulkan or OpenXR "
                           f"program - close it and install again ({e})") from e
    manifest = d / MANIFEST
    try:
        data = json.loads(manifest.read_text(encoding="utf8"))
        data.setdefault("api_layer", {})["library_path"] = f".\\{DLL}"
        manifest.write_text(json.dumps(data, indent=2), encoding="utf8")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    if found is not None:
        # Ours already: the files are refreshed, the registration stands.
        log(f"      ReShade's OpenXR layer is registered for this user ({manifest})")
        return manifest, False
    try:
        _register(manifest)
    except OSError as e:
        raise RuntimeError(f"Could not register the OpenXR layer: {e}") from e
    log(f"      registered {LAYER_NAME} for this user")
    log(f"      {manifest}")
    return manifest, True


def unregister() -> bool:
    """Remove only a registration we created. True when one was removed."""
    try:
        import winreg
    except ImportError:
        return False
    target = str(layer_dir() / MANIFEST)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYER_KEY, 0,
                            winreg.KEY_ALL_ACCESS) as k:
            winreg.DeleteValue(k, target)
            return True
    except OSError:
        return False
