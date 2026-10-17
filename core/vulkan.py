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
rather than duplicated. When this tool itself runs as administrator it adds
a value under HKLM as well, pointing at a copy of the layer under Program
Files, because that is the only list a program started as administrator
reads (see machine_dir).

IMPORTANT, and the tool says so before doing it: an implicit layer is GLOBAL.
Once registered, ReShade loads into every Vulkan application on this account
- on every account, for the machine's registration - not just the game being
set up. Uninstalling the last Vulkan install removes the per-user value; the
machine's one needs administrator rights to remove (see cleanup_machine).
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


def _view(x64: bool) -> int:
    """The registry view a game of that architecture reads HKLM through.

    HKLM\\Software is redirected per architecture: a 32-bit loader reads the
    WOW6432Node copy and never the other one, whatever this process is.
    """
    import winreg
    return getattr(winreg, "KEY_WOW64_64KEY" if x64 else "KEY_WOW64_32KEY", 0)


def _hives():
    """(hive, key, view) for the lists registrations() answers from.

    The machine's 32-bit view is left out on purpose: a foreign 32-bit
    ReShade layer found there would be reused instead of ours, and it calls
    itself VK_LAYER_reshade - the name clash described above, back again
    for every 32-bit game on a PC that also has our 64-bit layer.
    """
    import winreg
    return ((winreg.HKEY_CURRENT_USER, LAYER_KEY, 0),
            (winreg.HKEY_LOCAL_MACHINE, LAYER_KEY, _view(True)))


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
    seen: set[tuple[str, int]] = set()
    for hive, key, view in _hives():
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                i = 0
                while True:
                    try:
                        name, val, _type = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if "reshade" in name.lower() and name.lower().endswith(".json"):
                        p = Path(name)
                        val = val if isinstance(val, int) else 1
                        # The same manifest may be on the per-user list and
                        # the machine's; one row per answer is enough.
                        if p.is_file() and (name.lower(), val) not in seen:
                            seen.add((name.lower(), val))
                            out.append((p, val))
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
        return path.resolve().parent in (layer_dir().resolve(),
                                         machine_dir().resolve())
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


# A program started as administrator reads NONE of the per-user layers: the
# loader skips HKEY_CURRENT_USER under a high-integrity token and reads the
# machine's list only. Its own trace from such a process (#562) checks
# HKEY_LOCAL_MACHINE\...\ImplicitLayers and nothing else, so DXVK ran and
# ReShade was never asked for. Starting the game "normally" is not always
# on offer - with UAC off every program is elevated - so when this tool can
# write the machine's list, it writes ours there as well.
#
# What that list points at must not be a file an ordinary program can
# replace: the loader ignores the per-user list for elevated processes
# precisely so that nothing unelevated can choose a DLL for them. So the
# machine's registration gets its own copy of the layer under Program Files,
# which only an administrator can write - never the copy in the user's own
# folder (gate 2.0.9).
def machine_dir() -> Path:
    """Where the copy the machine's list points at is kept.

    Program Files as WINDOWS records it, not as the environment says: a
    variable of that name can be set per user, and a folder a user chose is
    exactly what this copy exists to avoid."""
    base = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion", 0,
                            winreg.KEY_READ | _view(True)) as k:
            base = str(winreg.QueryValueEx(k, "ProgramFilesDir")[0] or "")
    except (ImportError, OSError):
        base = ""
    return Path(base or r"C:\Program Files") / "dlss5-autopilot" / "reshade-vulkan"


def _machine_value(x64: bool):
    """The machine's registry value for our manifest of that architecture,
    or None when there is none (or it cannot be read)."""
    try:
        import winreg
        target = str(machine_dir() / (MANIFEST if x64 else MANIFEST32))
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LAYER_KEY, 0,
                            winreg.KEY_READ | _view(x64)) as k:
            return winreg.QueryValueEx(k, target)[0]
    except (ImportError, OSError):
        return None


def _register_machine(manifest: Path, x64: bool) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, LAYER_KEY, 0,
                            winreg.KEY_SET_VALUE | _view(x64)) as k:
        winreg.SetValueEx(k, str(manifest), 0, winreg.REG_DWORD, 0)


def machine_registered(x64: bool) -> bool:
    """Is OUR layer for that architecture on the machine's list, active,
    with its files there? A value whose files are gone loads nothing."""
    name = MANIFEST if x64 else MANIFEST32
    try:
        return (_machine_value(x64) == 0
                and (machine_dir() / name).is_file()
                and (machine_dir() / (DLL if x64 else DLL32)).is_file())
    except OSError:
        return False


def reaches_elevated(x64: bool) -> bool:
    """Would a game of that architecture, started as administrator, get a
    ReShade layer - ours or anybody's - from the machine's list?"""
    return machine_registered(x64) or _foreign_machine(x64)


def _foreign_machine(x64: bool) -> bool:
    """Somebody else's active ReShade layer for that architecture on the
    machine's list - ReShade's own setup writes one. Where there is one,
    ours is NOT added beside it: the 32-bit layers carry different names,
    so both would load into every 32-bit Vulkan program (gate 2.0.9)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LAYER_KEY, 0,
                            winreg.KEY_READ | _view(x64)) as k:
            i = 0
            while True:
                try:
                    name, val, _type = winreg.EnumValue(k, i)
                except OSError:
                    return False
                i += 1
                if ("reshade" in name.lower() and name.lower().endswith(".json")
                        and val == 0 and Path(name).is_file()
                        and not is_ours(Path(name))
                        and manifest_x64(Path(name)) in (None, x64)):
                    return True
    except (ImportError, OSError):
        return False


def _for_elevated(setup_exe: Path, manifest: Path, x64: bool, log) -> bool:
    """Extract the layer under Program Files and put THAT on the machine's
    list, when this process may.

    Extracted from ReShade's setup again, not copied from the per-user
    folder: a copy made by an elevated process out of a folder any program
    can write would carry whatever was put there.

    Never the reason an install fails: without the rights it is skipped, and
    the per-user registration still serves every normally started game.
    """
    try:
        from . import wincrash
        if not wincrash.elevated():
            return False
        d = machine_dir()
        d.mkdir(parents=True, exist_ok=True)
        _place(setup_exe, d, DLL if x64 else DLL32, manifest.name)
        _register_machine(d / manifest.name, x64)
    except Exception as e:
        log(f"      could not add the layer to the machine's list ({e}); "
            f"programs started as administrator will not get it")
        return False
    log(f"      also on the machine's list ({d / manifest.name}), which is "
        f"the only one a program started as administrator reads")
    return True


def will_do(x64: bool) -> tuple[Path | None, bool]:
    """What install_layer is about to do for a game of that architecture:
    (the foreign layer it will reuse, or None when it registers ours;
    whether a copy of ours goes on the machine's list as well).

    For the preview, which has to announce exactly the writes the install
    makes. The same questions in the same order as install_layer - keep the
    two together (gate 2.0.9: the preview promised a Program Files copy the
    install skipped beside ReShade's own machine-wide layer).
    """
    also32 = not x64
    try:
        from . import wincrash
        elevated = wincrash.elevated()
    except Exception:
        elevated = False
    found = registered_for(x64)
    stale = name_clash() if also32 else None
    unreachable = not also32 and elevated and not reaches_elevated(True)
    if found is not None and not is_ours(found) and stale is None \
            and not unreachable:
        return found, False
    machine = elevated and (not _foreign_machine(True)
                            or (also32 and not _foreign_machine(False)))
    return None, machine


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
    # A foreign layer is reused - unless this tool is elevated and that
    # layer is only on the per-user list, where an elevated game never
    # looks: "install again" would then promise the machine's list and
    # reuse the same registration every time (gate 2.0.9).
    try:
        from . import wincrash
        # 64-bit only: a foreign 32-bit layer is called VK_LAYER_reshade
        # and ours VK_LAYER_reshade32, so adding ours beside it would load
        # two ReShades into one 32-bit process.
        unreachable = (not also32 and wincrash.elevated()
                       and not reaches_elevated(True))
    except Exception:
        unreachable = False
    if found is not None and not is_ours(found) and stale is None \
            and not unreachable:
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
        m32 = None
        if also32:
            m32 = _place(setup_exe, d, DLL32, MANIFEST32)
            _register(m32)
            log(f"      registered the 32-bit layer as well ({m32.name})")
    except OSError as e:
        raise RuntimeError(
            f"Could not register the Vulkan layer: {e}") from e
    log(f"      registered {LAYER_NAME} for this user")
    log(f"      {manifest}")
    if not _foreign_machine(True):
        _for_elevated(setup_exe, manifest, True, log)
    if m32 is not None and not _foreign_machine(False):
        _for_elevated(setup_exe, m32, False, log)
    return manifest, True


def unregister_machine() -> bool:
    """Take our layer off the machine's list and delete its copy under
    Program Files. True when a value was removed; needs administrator
    rights, and without them changes nothing."""
    try:
        import winreg
    except ImportError:
        return False
    removed = False
    for m, dll, x64 in ((MANIFEST, DLL, True), (MANIFEST32, DLL32, False)):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LAYER_KEY, 0,
                                winreg.KEY_SET_VALUE | _view(x64)) as k:
                winreg.DeleteValue(k, str(machine_dir() / m))
                removed = True
        except OSError:
            continue
        for name in (m, dll):
            try:
                (machine_dir() / name).unlink()
            except OSError:
                pass
    if removed:
        for d in (machine_dir(), machine_dir().parent):
            try:
                d.rmdir()               # only when empty
            except OSError:
                break
    return removed


def unregister() -> bool:
    """Remove only a registration we created. True when one was removed."""
    try:
        import winreg
    except ImportError:
        return False
    removed = False
    for m in (MANIFEST, MANIFEST32):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYER_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, str(layer_dir() / m))
                removed = True
        except OSError:
            pass
    return unregister_machine() or removed


def machine_leftover() -> list[str]:
    """Our manifests still on the machine's list, whether or not their
    files are - after unregister(), the ones this process had no right to
    take off."""
    return [m for m, x64 in ((MANIFEST, True), (MANIFEST32, False))
            if _machine_value(x64) is not None]


def cleanup_machine(vulkan_games) -> bool:
    """At start-up, as administrator, with no Vulkan install left: take off
    the machine's registration an earlier uninstall could not. This is what
    makes "start this tool once as administrator" something a person can
    do - after the last uninstall there is no game left to press a button
    on (gate 2.0.9)."""
    try:
        from . import wincrash
        # None is "the settings could not be read", which is not "no Vulkan
        # install left": nothing is removed on a guess.
        if vulkan_games is None or vulkan_games or not machine_leftover() \
                or not wincrash.elevated():
            return False
        return unregister_machine()
    except Exception:
        return False
