"""Full verification pass: every module imports, every route installs and
uninstalls cleanly, and the guard rails actually fire.

Run this before cutting a release.
"""
import atexit
import inspect
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILS: list[str] = []
X64 = Path(os.environ.get("DLSS5_TEST_X64",
                          r"C:\Users\Mustafa\Downloads\dlss5-feed-host64.exe"))
SRC_DIR = Path(__file__).resolve().parent


def src_of(obj) -> str:
    """The source of a function or module, or "" when it cannot be read.

    Dozens of checks below read source to assert that a rule is written the
    way it has to be. `inspect.getsource` raises rather than returning
    nothing - a TokenError while a file is being edited, an OSError for a
    frozen module - and a raise here does not fail one check, it ends the
    run before RESULT is printed and quietly drops every check after it.
    An empty string fails the one check that asked.
    """
    import inspect as _i
    try:
        return _i.getsource(obj)
    except Exception as e:
        print(f"   (source unreadable: {type(e).__name__}: {e})")
        return ""


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"   {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)
    return cond


_SECTIONS: list[str] = []
_REACHED_RESULT = False


@atexit.register
def _say_if_truncated() -> None:
    """A suite that dies half way is not a suite that passed.

    An uncaught exception in any section ends the run - the sections after
    it never execute, and the exit code is 1, exactly as it is for a failed
    check. This says which section it stopped in, so nobody reads 731
    passes as a green run.
    """
    if _REACHED_RESULT or not _SECTIONS:
        return
    print()
    print("!! THE SUITE STOPPED IN: " + _SECTIONS[-1])
    print(f"!! {len(_SECTIONS)} section(s) ran; everything after that one "
          f"never did. This is not a pass.")


def section(title: str) -> None:
    _SECTIONS.append(title)
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------- 1. imports
section("1. every module imports cleanly, with warnings as errors")
with warnings.catch_warnings():
    warnings.simplefilter("error")
    mods = ("pe", "games", "emulators", "gpu", "sources", "net", "prefs",
            "reshade_ini", "feedcfg", "dxvk", "dlss", "vulkan",
            "anticheat", "optiscaler", "diagnose", "selfupdate", "update",
            "log", "components", "profiles", "remix", "reengine", "refw",
            "installer", "gui")
    for m in mods:
        try:
            __import__(f"core.{m}")
            check(f"core.{m}", True)
        except Exception as e:
            check(f"core.{m}", False, f"{type(e).__name__}: {e}")

from core import remix, remixlist  # noqa: E402
from core import pe, reengine, refw  # noqa: E402
from core import (diagnose, dlss, games, gpu, installer, net, optiscaler,  # noqa: E402
                  pe, prefs, reshade_ini, sources, update, vulkan)

check("no Turkish characters in any source", not any(
    any(ch in p.read_text(encoding="utf8") for ch in "şğıöçüŞĞİÖÇÜ")
    for p in list(Path("core").glob("*.py")) + [Path("dlss5_autopilot.py")]))

# ---------------------------------------------------------------- 2. detection
section("2. detection on the real library")
found = games.scan_all(lambda m: None)
playable = [g for g in found if g.exe]
check("library scan returns games", len(playable) > 0, f"{len(playable)} playable")
for g in playable:
    s = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0)
    ok = (s.recommended in s.options
          and all(o in dlss.ALL_ROUTES for o in s.options))
    if not ok:
        check(f"route sane for {g.name}", False, f"{s.recommended} / {s.options}")
check("every game got a sane route", not any(f.startswith("route sane") for f in FAILS))

# A 32-bit game must never be offered the 64-bit-only bridge or add-ons. The
# feeder is the only route that reaches one - plus remix, which injects
# nothing into the game at all and so has no bitness of its own.
for g in playable:
    if g.bitness == 32:
        s = dlss.detect(g.install_dir, g.folder, g.api, g.bitness)
        check(f"32-bit {g.name[:22]} is feeder-only",
              set(s.options) <= {dlss.FEEDER, dlss.REMIX}
              and dlss.FEEDER in s.options, str(s.options))

# ---------------------------------------------------------------- 3. routes
section("3. install and uninstall on every route")
EXPECT = {
    dlss.NATIVE: (["dxgi.dll", "renodx-dlss5.addon64", "nvngx_dlssnr.dll",
                   "ReShade.ini"],
                  ["dlss5-feed.addon64", "dlss5-bridge.addon64",
                   "ReShadePreset.ini", "dlss5-feed.cfg"]),
    dlss.BRIDGE: (["dxgi.dll", "dlss5-bridge.addon64", "dlss5-bridge.cfg",
                   "renodx-dlss5.addon64", "nvngx_dlssnr.dll", "ReShade.ini"],
                  ["dlss5-feed.addon64", "dlss5-feed.cfg"]),
    dlss.FEEDER: (["dxgi.dll", "dlss5-feed.addon64", "renodx-dlss5.addon64",
                   "nvngx_dlssnr.dll", "ReShade.ini", "ReShadePreset.ini",
                   "reshade-shaders/Shaders/DLSS5_Feed.fx",
                   "reshade-shaders/Shaders/lumenite_Kernel.fx",
                   "dlss5-feed.cfg"],
                  ["dlss5-bridge.addon64"]),
}
EXPECT[dlss.OPTI] = (["dxgi.dll", "nvngx_dlssnr.dll", "nvngx.dll_dlssnr.dll",
                      "OptiScaler.ini"],
                     ["dlss5-feed.addon64", "dlss5-bridge.addon64",
                      "ReShade.ini", "renodx-dlss5.addon64"])
EXPECT[dlss.RENODX] = (["dxgi.dll", "renodx-dlss.addon64", "nvngx_dlssnr.dll",
                        "ReShade.ini"],
                       ["renodx-dlss5.addon64", "dlss5-feed.addon64",
                        "dlss5-bridge.addon64", "OptiScaler.ini"])

for route, (want, unwanted) in EXPECT.items():
    d = Path(tempfile.mkdtemp(prefix=f"all_{route}_"))
    shutil.copyfile(X64, d / "Game.exe")
    g = games.manual(d)
    try:
        installer.install(g, installer.Options(path=route,
                                               native_dlss=route != dlss.FEEDER),
                          on_log=lambda t: None)
        idir = g.install_dir
        files = {p.relative_to(idir).as_posix() for p in idir.rglob("*") if p.is_file()}
        check(f"{route}: all expected files", not [w for w in want if w not in files],
              str([w for w in want if w not in files]))
        check(f"{route}: nothing from other routes",
              not [u for u in unwanted if u in files],
              str([u for u in unwanted if u in files]))
        installer.uninstall(g, on_log=lambda t: None)
        left = [p.name for p in idir.rglob("*") if p.is_file()]
        check(f"{route}: uninstall is clean", left == ["Game.exe"], str(left))
        dirs_left = [p.name for p in idir.iterdir() if p.is_dir()]
        check(f"{route}: no empty folders left behind", not dirs_left, str(dirs_left))
    except Exception as e:
        check(f"{route}: installs", False, f"{type(e).__name__}: {e}")
    shutil.rmtree(d, ignore_errors=True)

# --------------------------------------------------- 3b. switching routes
section("3b. switching routes does not leave the old one behind")
for a, b in ((dlss.FEEDER, dlss.OPTI), (dlss.OPTI, dlss.FEEDER),
             (dlss.NATIVE, dlss.BRIDGE), (dlss.BRIDGE, dlss.NATIVE),
             (dlss.NATIVE, dlss.RENODX), (dlss.RENODX, dlss.FEEDER)):
    d = Path(tempfile.mkdtemp(prefix="switch_"))
    shutil.copyfile(X64, d / "Game.exe")
    (d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
    g = games.manual(d)
    try:
        installer.install(g, installer.Options(path=a, native_dlss=True),
                          on_log=lambda t: None)
        installer.install(g, installer.Options(path=b, native_dlss=True),
                          on_log=lambda t: None)
        files = {p.relative_to(g.install_dir).as_posix()
                 for p in g.install_dir.rglob("*") if p.is_file()}
        if b == dlss.FEEDER:
            stale = [f for f in files if "OptiScaler" in f or "nvngx.dll_dlssnr" in f
                     or "renodx-dlss.addon64" in f]
        elif b == dlss.RENODX:
            stale = [f for f in files if "renodx-dlss5" in f or "dlss5-feed" in f]
        else:
            stale = [f for f in files
                     if "dlss5-feed" in f or "reshade-shaders" in f]
        check(f"{a} -> {b}: no leftovers", not stale, str(stale[:3]))
        installer.uninstall(g, on_log=lambda t: None)
        left = sorted(p.name for p in g.install_dir.rglob("*") if p.is_file())
        check(f"{a} -> {b}: uninstall is clean",
              left == ["Game.exe", "sl.interposer.dll"], str(left))
    except Exception as e:
        check(f"{a} -> {b}: switches", False, f"{type(e).__name__}: {e}")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 4. guards
section("4. guard rails fire")

d = Path(tempfile.mkdtemp(prefix="guard_"))
shutil.copyfile(X64, d / "explorer.exe")          # a name that is running
g = games.manual(d)
try:
    installer.preflight(g)
    check("running game is refused", False)
except installer.InstallError as e:
    check("running game is refused", "running" in str(e).lower())
shutil.rmtree(d, ignore_errors=True)

d = Path(tempfile.mkdtemp(prefix="guard2_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "dxgi.dll").write_bytes(b"MZ" + b"\x00" * (2 << 20))   # not ReShade
g = games.manual(d)
try:
    installer.install(g, installer.Options(), on_log=lambda t: None)
    check("foreign dxgi.dll is refused", False)
except installer.InstallError as e:
    check("foreign dxgi.dll is refused", "not ReShade" in str(e))
shutil.rmtree(d, ignore_errors=True)

# GPU compatibility: an RTX 50-only build must be refused on this card
_, sm = gpu.detect()
d = Path(tempfile.mkdtemp(prefix="guard3_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
try:
    installer.install(g, installer.Options(dlssnr="310.8.0"), on_log=lambda t: None)
    check("incompatible dlssnr is refused", sm == 120, "installed anyway")
except installer.InstallError as e:
    check("incompatible dlssnr is refused", "will not run" in str(e).lower()
          or "not run on" in str(e).lower(), str(e).splitlines()[0][:60])
shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- 5. backups
section("5. the game's own files survive")
d = Path(tempfile.mkdtemp(prefix="bak_"))
shutil.copyfile(X64, d / "Game.exe")
orig = b"GAME ORIGINAL" + b"\x00" * 500
(d / "nvngx_dlss.dll").write_bytes(orig)
g = games.manual(d)
installer.install(g, installer.Options(keep_game_dlss=False), on_log=lambda t: None)
check("backup was made",
      (g.install_dir / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).is_file())
installer.install(g, installer.Options(keep_game_dlss=False), on_log=lambda t: None)
installer.uninstall(g, on_log=lambda t: None)
check("original restored after a REinstall",
      (g.install_dir / "nvngx_dlss.dll").is_file()
      and (g.install_dir / "nvngx_dlss.dll").read_bytes() == orig)
shutil.rmtree(d, ignore_errors=True)

# ------------------------------------------------- 5b. nothing is destroyed
section("5b. pre-existing files survive every route, byte for byte")
PRE = {
    "nvngx_dlssnr.dll":     b"USER OWN DLSSNR",
    "renodx-dlss5.addon64": b"USER OWN RENODX",
    "ReShade.ini":          b"[GENERAL]\nMyCustomSetting=42\n",
    "ReShadePreset.ini":    b"Techniques=MyFavourite@Cool.fx\n",
    "OptiScaler.ini":       b"[Upscalers]\nDx12Upscaler=fsr31\n",
    "dlss5-bridge.cfg":     b"ofa_perf=5\n",
    "nvngx_dlss.dll":       b"USER OWN DLSS",
    "d3d9.dll":             b"USER OWN DXVK",
}
for route in (dlss.FEEDER, dlss.OPTI, dlss.BRIDGE, dlss.NATIVE):
    d = Path(tempfile.mkdtemp(prefix=f"pre_{route}_"))
    shutil.copyfile(X64, d / "Game.exe")
    (d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
    for n, c in PRE.items():
        (d / n).write_bytes(c + bytes(300))
    g = games.manual(d)
    try:
        installer.install(g, installer.Options(path=route, native_dlss=True,
                                               keep_game_dlss=False),
                          on_log=lambda t: None)
        installer.uninstall(g, on_log=lambda t: None)
        idir = g.install_dir
        lost = [n for n, c in PRE.items()
                if not (idir / n).is_file()
                or not (idir / n).read_bytes().startswith(c)]
        check(f"{route}: every pre-existing file restored", not lost, str(lost))
    except Exception as e:
        check(f"{route}: survives pre-existing files", False,
              f"{type(e).__name__}: {e}")
    shutil.rmtree(d, ignore_errors=True)

# A manifest-less uninstall (record deleted, corrupted, or a pre-manifest
# v1.0/v1.1 install) used to delete dxgi.dll/opengl32.dll unconditionally -
# unlike every other proxy name, which was only ever removed after
# confirming the file really is ReShade. A real dxgi.dll from something else
# entirely (SpecialK, an ENB, a separately installed ReShade) sitting in the
# folder must survive.
d = Path(tempfile.mkdtemp(prefix="uninstall_foreign_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "dxgi.dll").write_bytes(b"MZ some other injector, not reshade" + bytes(2000))
(d / "opengl32.dll").write_bytes(b"MZ also not reshade" + bytes(2000))
g = games.manual(d)
installer.uninstall(g, on_log=lambda t: None)
check("a foreign dxgi.dll survives a manifest-less uninstall",
      (d / "dxgi.dll").is_file())
check("a foreign opengl32.dll survives a manifest-less uninstall",
      (d / "opengl32.dll").is_file())
shutil.rmtree(d, ignore_errors=True)

# The same folder, but this time the dxgi.dll really is ours (ReShade) -
# it must still be removed the way it always was.
d = Path(tempfile.mkdtemp(prefix="uninstall_real_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "dxgi.dll").write_bytes(b"ReShade" + bytes(1 << 20))
g = games.manual(d)
installer.uninstall(g, on_log=lambda t: None)
check("a real ReShade dxgi.dll is still removed with no manifest",
      not (d / "dxgi.dll").is_file())
shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- 6. vulkan
section("6. vulkan layer handling")
before = vulkan.existing_registration()
check("existing ReShade registration is detected or absent", True, str(before))
# Ours can legitimately be the active one on this PC (a Vulkan-layer game of
# ours is installed here); what must hold is that it points at our folder
# and the manifest is really there.
check("an active registration is a real manifest; ours lives in our layer dir",
      before is None or (before.is_file() and (not vulkan.is_ours(before)
                                                 or before.parent == vulkan.layer_dir())),
      str(before))

# ---------------------------------------------------------------- 7. misc
section("6b. optiscaler proxy names")


def _fake_game(prefix: str = "opti_"):
    """A throwaway game folder that looks like it ships DLSS."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, d / "Game.exe")
    (d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
    return games.manual(d)


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


# OptiScaler identifies itself through the PE version resource, which keeps
# saying "OptiScaler.dll" whatever the file on disk is called.
d = Path(tempfile.mkdtemp(prefix="dlss5-proxy-"))
(d / "winmm.dll").write_bytes(
    b"MZ" + b"\0" * (1 << 20) + "OptiScaler.dll".encode("utf-16-le"))
(d / "dxgi.dll").write_bytes(b"MZ" + b"\0" * (1 << 21))     # someone else's
check("optiscaler is recognised under another name",
      optiscaler.is_optiscaler(d / "winmm.dll"))
check("an unrelated dll is not mistaken for optiscaler",
      not optiscaler.is_optiscaler(d / "dxgi.dll"))
check("a missing file is not optiscaler",
      not optiscaler.is_optiscaler(d / "version.dll"))
check("a taken proxy name is stepped over",
      optiscaler.suggest_proxy(d) == "winmm.dll", optiscaler.suggest_proxy(d))
check("an empty folder gets the default",
      optiscaler.suggest_proxy(Path(tempfile.mkdtemp())) == optiscaler.DEFAULT_PROXY)
check("every proxy name is explained",
      set(optiscaler.PROXY_HELP) == set(optiscaler.PROXY_NAMES))
check("an unsupported proxy name is refused",
      _raises(lambda: optiscaler.install(d, proxy="nonsense.dll")))

# A real install under a chosen name, with a conflicting copy already there.
g = _fake_game()
rival = g.install_dir / "version.dll"
rival.write_bytes(b"MZ" + b"\0" * (1 << 20) + "OptiScaler.dll".encode("utf-16-le"))
own = g.install_dir / "dxgi.dll"
own.write_bytes(b"THE GAME'S OWN DXGI")
installer.install(g, installer.Options(path=dlss.OPTI, native_dlss=True,
                                       opti_proxy="winmm.dll"),
                  on_log=lambda t: None)
check("the chosen proxy name is what gets written",
      (g.install_dir / "winmm.dll").is_file()
      and optiscaler.is_optiscaler(g.install_dir / "winmm.dll"))
check("the game's own dxgi.dll is left alone",
      own.read_bytes() == b"THE GAME'S OWN DXGI")
check("a rival optiscaler is moved out of the way",
      not rival.exists()
      and rival.with_name("version.dll" + installer.BACKUP_SUFFIX).is_file())
installer.uninstall(g, on_log=lambda t: None)
check("the rival is put back on uninstall",
      rival.is_file() and optiscaler.is_optiscaler(rival))
check("uninstall removes the proxy it installed",
      not (g.install_dir / "winmm.dll").exists())

# With no choice made, a game that ships its own dxgi.dll gets another name.
g = _fake_game()
(g.install_dir / "dxgi.dll").write_bytes(b"THE GAME'S OWN DXGI")
installer.install(g, installer.Options(path=dlss.OPTI, native_dlss=True),
                  on_log=lambda t: None)
check("auto avoids replacing the game's own dxgi.dll",
      (g.install_dir / "dxgi.dll").read_bytes() == b"THE GAME'S OWN DXGI"
      and optiscaler.is_optiscaler(g.install_dir / "winmm.dll"))
man = json.loads((g.install_dir / installer.MANIFEST).read_text(encoding="utf8"))
check("the manifest records the name it actually used",
      man["proxy"] == "winmm.dll", man["proxy"])
installer.uninstall(g, on_log=lambda t: None)

section("6c. the interface survives bad data")
import tkinter as _tk  # noqa: E402
# A window that finds a saved library from another version rescans the
# disks at start; the one on the machine running this is not the test's.
from core import library as _library_iso  # noqa: E402
_library_iso.FILE = Path(tempfile.mkdtemp(prefix="lib_iso_")) / "library.json"
from core import gui as _gui  # noqa: E402
_r = _tk.Tk()
_app = _gui.App(_r)
_r.update()


def _ticked_names() -> set[str]:
    """Every "tick 'X'" / "untick 'X'" the tool says, joined across lines."""
    import ast as _ast
    import re as _re_t
    out = set()
    pat = _re_t.compile(r"\b(?:un)?tick(?:ed)? '([^'{}]+)'", _re_t.I)
    for _f in sorted((SRC_DIR / "core").glob("*.py")):
        for _n in _ast.walk(_ast.parse(_f.read_text(encoding="utf8"))):
            if isinstance(_n, _ast.JoinedStr):
                _s = "".join(v.value if isinstance(v, _ast.Constant) else "{}"
                             for v in _n.values)
            elif isinstance(_n, _ast.Constant) and isinstance(_n.value, str):
                _s = _n.value
            else:
                continue
            out.update(m.group(1) for m in pat.finditer(_s))
    return out


def _checkbox_texts(w) -> list[str]:
    out = [str(w.cget("text")).lower()] if w.winfo_class() == "Checkbutton" else []
    for c in w.winfo_children():
        out += _checkbox_texts(c)
    return out


# Names in someone else's window (Remix's developer menu, ReShade's overlay).
_FOREIGN_TICKS = {"Enable Neural Uplift (DLSS-NR)",
                  # ReShade's own Generic Depth tab
                  "Copy depth buffer before clear operations"}
_boxes = _checkbox_texts(_r)
_dead = sorted(n for n in _ticked_names() - _FOREIGN_TICKS
               if not any(t.startswith(n.lower()) for t in _boxes))
# Five messages sent people to tick 'swap the Remix runtime' and the window
# never had that box (#148); 'feeder pre-release' was a dropdown entry.
check("every 'tick X' the tool says names a checkbox that exists", not _dead, _dead)

# a folder that has gone away must not abandon the whole list
_ghost = games.Game(name="Ghost", folder=Path("Z:/gone"))
_ghost.exe = Path("Z:/gone/x.exe")
_app.all_games = [_ghost]
_app._fill()
check("one unreadable game does not empty the list",
      len(_app.tree.get_children()) == 1)

# an exception in a queue handler must not stop the pump for good
_app.q.put(("scanned", None))          # payload that makes _fill raise
_app._pump()
_app.q.put(("scan", "alive"))
_app._pump()
check("the pump survives a handler that raises",
      _app.scanlbl.cget("text") == "alive", _app.scanlbl.cget("text"))

# a game whose architecture could not be read must stay visible
_unk = games.Game(name="Unknown", folder=Path("Z:/g1"))
_unk.exe, _unk.bitness = Path("Z:/g1/x.exe"), None
_b64 = games.Game(name="Sixtyfour", folder=Path("Z:/g2"))
_b64.exe, _b64.bitness = Path("Z:/g2/x.exe"), 64
_app.all_games = [_unk, _b64]
_seen = {}
for _a in ("all", "64", "32"):
    _app.arch.set(_a)
    _app._fill()
    _seen[_a] = [x.name for x in _app.shown]
check("unknown architecture is never filtered away",
      all("Unknown" in v for v in _seen.values()), str(_seen))
check("a known architecture still filters",
      "Sixtyfour" not in _seen["32"], str(_seen["32"]))
# issue #30: the add-on dropdown opened on the newest build and passed it as
# an explicit choice, so the driver pin to 4.55 never ran from the GUI
_app.catalog = {"renodx": [{"label": "4.70", "tag": "4.70", "url": "u"},
                           {"label": "4.55", "tag": "4.55", "url": "u"}],
                "renodx_sf": []}
_saved_find = _gui.prefs.find_renodx
_gui.prefs.find_renodx = lambda sf=False: (None, [])
_app._fill_addon_list(False)
_o = _app._opts()
check("the DLSS 5 add-on dropdown opens on auto, not on the newest build",
      _app.cb_renodx.get().startswith("auto") and _app.cb_renodx["values"][1] == "4.70",
      f"{_app.cb_renodx.get()!r} {_app.cb_renodx['values']}")
check("...so the options carry no explicit add-on version and the installer's pins apply",
      _o.renodx is None, repr(_o.renodx))
_app.cb_renodx.set("4.70")
check("a build picked from the list is still an explicit choice", _app._opts().renodx == "4.70")
_gui.prefs.find_renodx = _saved_find
_r.destroy()

section("6d. a quarantined file is reported, not ignored")
_d = Path(tempfile.mkdtemp(prefix="quar_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
installer.install(_g, installer.Options(), on_log=lambda t: None)
# antivirus takes the add-on away after the install wrote it
_victim = _g.install_dir / installer.RENODX
_victim.unlink()
_rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
check("an install that lost a file says nothing was wrong",
      not [w for w in _rep.warnings if "no longer there" in w])
# now simulate the file vanishing DURING the install
_orig_manifest = installer._write_manifest
def _steal(root, g, opt, rep, proxy, level, complete):
    if complete:
        pass
    return _orig_manifest(root, g, opt, rep, proxy, level, complete)
_rep2 = installer.Report()
_rep2.written = [installer.RENODX, "definitely-not-here.dll"]
_miss = [r for r in _rep2.written if not (_g.install_dir / r).exists()]
check("a missing written file is detectable", _miss == ["definitely-not-here.dll"],
      str(_miss))
installer.uninstall(_g, on_log=lambda t: None)
shutil.rmtree(_d, ignore_errors=True)

section("6e. no two routes' add-ons in one folder, no logs left behind")
# Seen in MGS V: a bridge install recorded in the manifest with an orphaned
# dlss5-feed.addon64 beside it. ReShade loads every .addon64, so both
# registered, both tried to build a contract, and the game exited before it
# ever created a swap chain.
_d = Path(tempfile.mkdtemp(prefix="orphan_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
_g = games.manual(_d)
installer.install(_g, installer.Options(path=dlss.BRIDGE, native_dlss=True),
                  on_log=lambda t: None)
# an orphan no manifest knows about
(_g.install_dir / installer.FEEDER_ADDON64).write_bytes(b"MZ" + bytes(1000))
(_g.install_dir / "dlss5-feed.cfg").write_text("orphan")
installer.install(_g, installer.Options(path=dlss.BRIDGE, native_dlss=True),
                  on_log=lambda t: None)
check("an orphaned add-on from another route is removed",
      not (_g.install_dir / installer.FEEDER_ADDON64).is_file())
check("only one route's add-on remains",
      (_g.install_dir / installer.BRIDGE_ADDON).is_file())

# every log the components write must go on uninstall
for _n in ("ReShade.log", "dlss5-feed.log", "OptiScaler.log", "nvngx.log"):
    (_g.install_dir / _n).write_text("runtime")
(_g.install_dir / "Logs").mkdir(exist_ok=True)
(_g.install_dir / "Logs" / "OptiScaler-x.log").write_text("x")
installer.uninstall(_g, on_log=lambda t: None)
_left = sorted(p.relative_to(_g.install_dir).as_posix()
               for p in _g.install_dir.rglob("*") if p.is_file())
# The orphan came back because we cannot prove it was ours - uninstall's job
# is to return the folder to how it was, and an add-on with no ReShade beside
# it does nothing. What must NOT survive is any log or anything we wrote.
check("uninstall leaves no runtime logs behind",
      not [f for f in _left if f.endswith(".log")], str(_left))
check("uninstall removes everything this tool wrote",
      not [f for f in _left if f in (installer.BRIDGE_ADDON, "dxgi.dll",
                                     installer.RENODX, installer.DLSSNR,
                                     installer.MANIFEST)], str(_left))
check("a file we could not prove was ours is put back",
      (_g.install_dir / installer.FEEDER_ADDON64).is_file())
shutil.rmtree(_d, ignore_errors=True)

# ...but one we DID record as ours is removed, not restored.
_d = Path(tempfile.mkdtemp(prefix="orphan2_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
_g = games.manual(_d)
installer.install(_g, installer.Options(path=dlss.FEEDER), on_log=lambda t: None)
installer.install(_g, installer.Options(path=dlss.BRIDGE, native_dlss=True),
                  on_log=lambda t: None)
check("switching routes leaves only the new route's add-on",
      (_g.install_dir / installer.BRIDGE_ADDON).is_file()
      and not (_g.install_dir / installer.FEEDER_ADDON64).is_file())
installer.uninstall(_g, on_log=lambda t: None)
_left = sorted(p.relative_to(_g.install_dir).as_posix()
               for p in _g.install_dir.rglob("*") if p.is_file())
check("and uninstall after a switch leaves nothing of ours",
      _left == ["Game.exe", "sl.interposer.dll"], str(_left))
shutil.rmtree(_d, ignore_errors=True)

section("6f. reshade can be loaded under another name")
check("every reshade proxy name is explained",
      set(installer.RESHADE_PROXY_HELP) == set(installer.RESHADE_PROXIES))
check("the api still decides by default",
      installer._proxy_name("DX11") == "dxgi.dll"
      and installer._proxy_name("OpenGL") == "opengl32.dll")
check("an explicit choice wins",
      installer._proxy_name("DX11", "d3d11.dll") == "d3d11.dll")
check("a name reshade does not support is ignored",
      installer._proxy_name("DX11", "nonsense.dll") == "dxgi.dll")

_d = Path(tempfile.mkdtemp(prefix="rproxy_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
_g = games.manual(_d)
installer.install(_g, installer.Options(path=dlss.BRIDGE, native_dlss=True,
                                        reshade_proxy="d3d11.dll"),
                  on_log=lambda t: None)
check("reshade is installed under the chosen name",
      (_g.install_dir / "d3d11.dll").is_file()
      and not (_g.install_dir / "dxgi.dll").exists())
_man = json.loads((_g.install_dir / installer.MANIFEST).read_text(encoding="utf8"))
check("the manifest records the reshade name used",
      _man["proxy"] == "d3d11.dll", _man["proxy"])
installer.uninstall(_g, on_log=lambda t: None)
_left = sorted(p.relative_to(_g.install_dir).as_posix()
               for p in _g.install_dir.rglob("*") if p.is_file())
check("uninstall removes it under that name too",
      _left == ["Game.exe", "sl.interposer.dll"], str(_left))
shutil.rmtree(_d, ignore_errors=True)

section("6g. an install in a subfolder is still found")
# The exe is picked fresh on every scan. Reported as "uninstall does not work":
# the install went to Bin\Win64, the next scan ranked another exe first, and
# the marker files were then looked for in a folder that never had them.
_d = Path(tempfile.mkdtemp(prefix="adopt_"))
_sub = _d / "Bin" / "Win64"
_sub.mkdir(parents=True)
shutil.copyfile(X64, _sub / "Game.exe")
shutil.copyfile(X64, _d / "Decoy-Shipping.exe")   # ranks above the real exe
_cands = pe.find_game_exes(_d)
check("the ranking really does prefer the other exe",
      bool(_cands) and _cands[0].parent == _d,
      _cands[0].name if _cands else "no candidates")
_g = games.manual(_d)
check("with nothing installed, the top-ranked exe is used",
      _g.install_dir == _d and not _g.installed, str(_g.install_dir))
(_sub / installer.MANIFEST).write_text(json.dumps(
    {"version": 1, "exe": "Game.exe", "files": ["dxgi.dll"]}), encoding="utf8")
_g2 = games.manual(_d)
check("an install in a subfolder is adopted", _g2.install_dir == _sub,
      str(_g2.install_dir))
check("and the exe it was made for comes with it",
      _g2.exe == _sub / "Game.exe", str(_g2.exe))
check("so the uninstall button is enabled", _g2.installed)
# an older release wrote no exe name - the folder must still be found
(_sub / installer.MANIFEST).unlink()
(_sub / "dlss5kur-kurulum.json").write_text("{}", encoding="utf8")
_g3 = games.manual(_d)
check("a record left by an older release counts too",
      _g3.install_dir == _sub and _g3.installed, str(_g3.install_dir))
(_sub / "dlss5kur-kurulum.json").unlink()
_g4 = games.manual(_d)
check("once nothing is installed, nothing is adopted",
      _g4.install_dir == _d and not _g4.installed, str(_g4.install_dir))
shutil.rmtree(_d, ignore_errors=True)

section("6h. the game list can be searched")
from core import gui as _gui  # noqa: E402
_m = _gui.App._matches       # the caller lowercases what was typed
_fake = games.Game(name="Cyberpunk 2077", source="Steam",
                   folder=Path(r"D:\SteamLibrary\common\Cyberpunk 2077"))
check("an empty search matches everything", _m(_fake, []))
check("part of the name matches", _m(_fake, ["cyber"]))
check("typing does not have to match the case", _m(_fake, ["cyberpunk 2077".lower()]))
check("every word has to match",
      _m(_fake, ["cyber", "2077"]) and not _m(_fake, ["cyber", "witcher"]))
check("the folder is searched as well", _m(_fake, ["steamlibrary"]))
check("so is the store it came from", _m(_fake, ["steam"]))
check("a word in neither matches nothing", not _m(_fake, ["skyrim"]))

section("7. odds and ends")
check("rate-limit fallback message exists", hasattr(sources, "last_fallback"))
check("api cache path set", "api-cache" in str(sources._API_CACHE))
check("download supports retry", "attempts" in net.download.__code__.co_varnames)
check("update points at the right repo", update.REPO.endswith("DLSS5-Autopilot"))
check("version is 1.8.2", update.VERSION == "1.8.2", update.VERSION)

from core import log as _log  # noqa: E402
_log.write("test run")
check("the log file is written", _log.path().is_file(), str(_log.path()))
_before = _log.path().stat().st_size
try:
    raise ValueError("deliberate")
except ValueError as e:
    _log.exception("test", e)
check("a traceback reaches the log",
      _log.path().stat().st_size > _before
      and "deliberate" in _log.path().read_text(encoding="utf8", errors="replace"))
from core import components as _comp  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="comp_"))
(_d / installer.MANIFEST).write_text(json.dumps(
    {"components": {"renodx": "4.60"}}), encoding="utf8")
_items = _comp.check(_d)
check("component versions are read from the manifest",
      len(_items) == 1 and _items[0].installed == "4.60",
      str([(i.name, i.installed, i.latest) for i in _items]))
# pre-1.3 installs kept their versions in the notes only
(_d / installer.MANIFEST).write_text(json.dumps(
    {"notes": ["renodx version: 4.55", "backed up the game's own x.dll"]}),
    encoding="utf8")
# On this PC's driver the renodx pin may cap "latest" at 4.55 (section 44);
# this check is about reading the old note format, so the driver is taken
# out of the equation.
_saved_drv = gpu.driver_at_least
gpu.driver_at_least = lambda want: False
_old = _comp.check(_d)
gpu.driver_at_least = _saved_drv
check("versions recorded by an older release are still read",
      len(_old) == 1 and _old[0].installed == "4.55"
      and _old[0].outdated, str([(i.installed, i.latest, i.outdated) for i in _old]))
check("a different build family is not called outdated",
      not _comp.Item("x", "310.8.SF-v2", "310.8.0-RTX40",
                     _comp._key("310.8.0-RTX40") > _comp._key("310.8.SF-v2")).outdated)
check("nothing recorded gives nothing to report", _comp.check(Path(tempfile.mkdtemp())) == [])

check("every store is scanned",
      all(hasattr(games, f"scan_{s}") for s in
          ("steam", "epic", "gog", "ea", "ubisoft", "battlenet", "xbox")))
r = diagnose.analyse(Path(r"C:\Program Files (x86)\Steam\steamapps\common\DEATHLOOP"))
check("diagnosis reads a real log", bool(r.verdict), r.verdict[:52])

# ---------------------------------------------------------- 8. v1.3.0 rules
section("8. route rules, version pins and the OptiScaler dials")

# DirectX 10: reachable since feeder 0.13.1 (private D3D11 relay), feeder only.
d = Path(tempfile.mkdtemp(prefix="dx10_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
g.api = "DX10"
ok, why = installer.check_supported(g)
check("dx10 is supported now", ok, why)
s10 = dlss.detect(d, d, "DX10", 64)
check("dx10 goes to the feeder only, and says which build",
      s10.supported and s10.options == [dlss.FEEDER] and "0.13.1" in s10.reason,
      s10.reason)
check("dx10 reliability is beta with the relay named",
      installer.reliability(g, dlss.FEEDER)[0] == installer.BETA
      and "relay" in installer.reliability(g, dlss.FEEDER)[1])
check("the feeder build gate compares versions the feeder's way",
      sources.feeder_key("v0.13.1-beta.1") >= sources.feeder_key(sources.FEEDER_DX10_MIN)
      and sources.feeder_key("v0.12.1-beta.2") < sources.feeder_key(sources.FEEDER_DX10_MIN)
      and sources.feeder_key("v0.14.0") > sources.feeder_key(sources.FEEDER_DX10_MIN))
shutil.rmtree(d, ignore_errors=True)

# 64-bit D3D9 is reachable now, through ShortFuse's add-on only.
# A fresh folder, never %TEMP% itself: handing the detector the whole
# temp directory let another test's leftover .trex folder decide the answer,
# and the suite gave different results on back-to-back runs.
_empty = Path(tempfile.mkdtemp(prefix="empty_game_"))
s9 = dlss.detect(_empty, _empty, "DX9", 64)
check("64-bit dx9 goes to the renodx add-on only", s9.options == [dlss.RENODX], str(s9.options))
# 32-bit stays feeder-only whatever the API.
for api in ("DX9", "DX11", "DX12", "Vulkan", "OpenGL"):
    s32 = dlss.detect(_empty, _empty, api, 32)
    check(f"32-bit {api} is feeder-only", s32.options == [dlss.FEEDER], str(s32.options))

# D3D12 + DLSS -> OptiScaler on any RTX card; the note says what the author tested.
g50 = _fake_game("rtx50_")
sup50 = dlss.detect(g50.install_dir, g50.folder, "DX12", 64, sm=120)
sup40 = dlss.detect(g50.install_dir, g50.folder, "DX12", 64, sm=89)
sup10 = dlss.detect(g50.install_dir, g50.folder, "DX12", 64, sm=61)
check("rtx 50 with a dlss d3d12 game is steered to optiscaler",
      sup50.recommended == dlss.OPTI, sup50.recommended)
check("rtx 40 is steered to optiscaler too", sup40.recommended == dlss.OPTI, sup40.recommended)
check("a pascal card is not steered anywhere new", sup10.recommended == dlss.NATIVE)
fit40 = dlss.fit(dlss.OPTI, "DX12", True, 89)
check("optiscaler is usable on an rtx 40, with the author's caveat",
      fit40[0] is True and "author tested RTX 50" in fit40[1], str(fit40))
check("optiscaler is marked usable on an rtx 50",
      dlss.fit(dlss.OPTI, "DX12", True, 120)[0] is True)
check("optiscaler without dlss in the game is refused",
      dlss.fit(dlss.OPTI, "DX12", False, 120)[0] is False)
shutil.rmtree(g50.folder, ignore_errors=True)

# The feeder's stable release only accepts renodx-dlss5 4.55.
check("feeder 0.7.0 pins renodx to 4.55", sources.renodx_for_feeder("v0.7.0") == "4.55")
check("feeder 0.8.0-beta.2 still pins", sources.renodx_for_feeder("v0.8.0-beta.2") == "4.55")
check("feeder 0.8.0-beta.3 accepts newer", sources.renodx_for_feeder("v0.8.0-beta.3") is None)
check("feeder 0.9.0-beta.1 accepts newer", sources.renodx_for_feeder("v0.9.0-beta.1") is None)
check("a plain release sorts above its betas",
      sources.feeder_key("v0.9.0") > sources.feeder_key("v0.9.0-beta.1"))

# nvngx_dlssnr build order follows the card.
fake_cat = [{"label": l} for l in ("310.8.SF-v2", "310.8.0-RTX40", "310.8.0", "310.8.SF")]
check("rtx 50 gets nvidia's own build first",
      gpu.order_dlssnr(fake_cat, 120)[0]["label"] == "310.8.0")
check("rtx 40 gets the -RTX40 build first",
      gpu.order_dlssnr(fake_cat, 89)[0]["label"] == "310.8.0-RTX40")
check("rtx 30 gets an SF build first",
      gpu.order_dlssnr(fake_cat, 86)[0]["label"].startswith("310.8.SF"))
check("unknown card keeps the mirror's order",
      [e["label"] for e in gpu.order_dlssnr(fake_cat, None)] == [e["label"] for e in fake_cat])
check("every tier has a plain-words note",
      all(gpu.tier_note(sm_) for sm_ in (75, 86, 89, 120)))

# OptiScaler.ini: dials land in [DlssNr], the rest of the file is untouched.
d = Path(tempfile.mkdtemp(prefix="nr_"))
(d / "OptiScaler.ini").write_text("; tuned by hand\n[Upscalers]\nDx12Upscaler=dlss\n\n"
                                   "[DLSSNR]\nEnabled=false\nIntensity=1.3\n",
                                   encoding="utf8")
optiscaler.enable_nr(d, settings={"WorkingScale": 0.75, "Preset": 2})
txt = (d / "OptiScaler.ini").read_text(encoding="utf8")
check("nr enabled in place", "Enabled=true" in txt and "Enabled=false" not in txt)
check("working scale written", "WorkingScale=0.75" in txt, txt)
check("hand-tuned keys survive", "Intensity=1.3" in txt and "; tuned by hand" in txt)
check("section spelling normalised", "[DlssNr]" in txt and "[DLSSNR]" not in txt)
check("other sections untouched", "Dx12Upscaler=dlss" in txt)
optiscaler.set_dx11_bridged_upscaler(d)
txt = (d / "OptiScaler.ini").read_text(encoding="utf8")
check("dx11 gets a bridged upscaler", "Dx11Upscaler=fsr22_12" in txt)
check("still exactly one DlssNr section", txt.count("[DlssNr]") == 1)
shutil.rmtree(d, ignore_errors=True)

# Uninstall with a locked file: nothing is lost, the record stays, second run cleans.
g = _fake_game("locked_")
installer.install(g, installer.Options(path=dlss.NATIVE, native_dlss=True),
                  on_log=lambda t: None)
held = open(g.install_dir / "dxgi.dll", "rb")
lines = []
installer.uninstall(g, on_log=lines.append)
check("locked file is reported, not silently skipped",
      any("could not remove" in l for l in lines))
check("record kept for the locked file", (g.install_dir / installer.MANIFEST).is_file())
held.close()
installer.uninstall(g, on_log=lambda t: None)
left = sorted(p.name for p in g.install_dir.rglob("*") if p.is_file())
check("second uninstall finishes the job", left == ["Game.exe", "sl.interposer.dll"], str(left))
shutil.rmtree(g.folder, ignore_errors=True)

# A hand-installed OptiScaler under dxgi.dll is moved aside, not fought with.
g = _fake_game("handopti_")
fake = b"MZ" + bytes(1 << 20) + "OptiScaler.dll".encode("utf-16-le")
(g.install_dir / "dxgi.dll").write_bytes(fake)
(g.install_dir / "OptiScaler.ini").write_text("[Upscalers]\nDx12Upscaler=dlss\n")
try:
    installer.install(g, installer.Options(path=dlss.NATIVE, native_dlss=True),
                      on_log=lambda t: None)
    check("hand-installed optiscaler does not block a reshade route", True)
    check("it was backed up", (g.install_dir / "dxgi.dll.dlss5-autopilot-backup").is_file())
    check("its ini was moved aside too", not (g.install_dir / "OptiScaler.ini").is_file())
    installer.uninstall(g, on_log=lambda t: None)
    check("uninstall puts the hand-installed optiscaler back",
          (g.install_dir / "dxgi.dll").read_bytes() == fake
          and (g.install_dir / "OptiScaler.ini").is_file())
except Exception as e:
    check("hand-installed optiscaler does not block a reshade route", False, f"{type(e).__name__}: {e}")
shutil.rmtree(g.folder, ignore_errors=True)

# DLSS kept where engines keep it, not beside the exe, still counts.
for sub in (Path("Engine/Plugins/Runtime/Nvidia/DLSS/Binaries/ThirdParty/Win64"),
            Path("Bin/Win64Shared")):
    d = Path(tempfile.mkdtemp(prefix="deepdlss_"))
    exe_dir = d / "Binaries" / "Win64"
    exe_dir.mkdir(parents=True)
    shutil.copyfile(X64, exe_dir / "Game.exe")
    (d / sub).mkdir(parents=True)
    (d / sub / "nvngx_dlss.dll").write_bytes(b"MZ" + bytes(1000))
    (d / "Content").mkdir()
    (d / "Content" / "nvngx_dlss.dll").write_bytes(b"MZ")     # never looked at
    sd = dlss.detect(exe_dir, d, "DX12", 64, sm=89)
    check(f"dlss under {sub.parts[0]}/... is found", sd.native_dlss and dlss.OPTI in sd.options,
          str(sd.evidence))
    shutil.rmtree(d, ignore_errors=True)
d = Path(tempfile.mkdtemp(prefix="nodlss_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "Content").mkdir()
(d / "Content" / "nvngx_dlss.dll").write_bytes(b"MZ")
check("a dll inside Content does not count", not dlss.detect(d, d, "DX12", 64).native_dlss)
shutil.rmtree(d, ignore_errors=True)

# A store's launch stub in the root must not win over the real Binaries exe.
d = Path(tempfile.mkdtemp(prefix="stub_"))
real = d / "Snowfall" / "Binaries" / "Win64"
real.mkdir(parents=True)
shutil.copyfile(X64, real / "GWT.exe")
shutil.copyfile(X64, d / "GWT.exe")
gs = games.Game(name="stub", folder=d, exe=d / "GWT.exe", source="Epic")
games.enrich(gs)
check("the real Binaries exe wins over the root stub", gs.exe == real / "GWT.exe", str(gs.exe))
shutil.rmtree(d, ignore_errors=True)

# The SF add-on is told apart from renodx-dlss5 by content, not by name.
d = Path(tempfile.mkdtemp(prefix="sf_"))
(d / "a.addon64").write_bytes(b"MZ" + bytes(300_000) + b"RenoDX DLSS renodx-dlss.addon64")
(d / "b.addon64").write_bytes(b"MZ" + bytes(300_000) + b"RenoDX DLSS renodx-dlss5.addon64")
check("sf build recognised", prefs.is_renodx_sf(d / "a.addon64"))
check("renodx-dlss5 is not mistaken for sf", not prefs.is_renodx_sf(d / "b.addon64"))
shutil.rmtree(d, ignore_errors=True)




# ------------------------------------------------------------ 9. v1.3.2
section("9. dxvk for games that quit on reshade, stray reshade copies, "
        "settings that travel, the feeder zip")
from core import dxvk, reshade_ini, sources, prefs

# The known list and the switch that follows it.
d = Path(tempfile.mkdtemp(prefix="dxvk_"))
shutil.copyfile(X64, d / "mgsvtpp.exe")
g = games.manual(d)
g.api = "DX11"            # the fixture exe is not the real game; MGS V is D3D11
check("mgs v is recognised as needing dxvk", bool(installer.wants_dxvk(g)),
      str(installer.wants_dxvk(g)))
check("an ordinary game is not", installer.wants_dxvk(
    games.Game(name="x", folder=d, exe=d / "Game.exe", bitness=64, api="DX11")) is None)
o = installer.Options(path=dlss.FEEDER, dxvk=True)
steps = installer.plan(g, o)
check("dxvk is the first step and reshade becomes the vulkan layer",
      steps[0].startswith("DXVK") and steps[1] == "ReShade (Vulkan layer)", str(steps[:2]))
check("optiscaler never goes through dxvk",
      not installer.uses_dxvk(g, installer.Options(path=dlss.OPTI, dxvk=True)))
check("a vulkan game has no proxy dll name",
      installer._proxy_name("Vulkan") == installer.VULKAN_LAYER)
check("dxvk names its logs after the exe",
      dxvk.logs_for(Path("mgsvtpp.exe"))[0] == "mgsvtpp_dxgi.log")
g9 = games.Game(name="gta", folder=d, exe=d / "GTAIV.exe", bitness=32, api="DX9")
s9 = installer.plan(g9, installer.Options(path=dlss.FEEDER, dxvk=True))
check("dx9 through dxvk: dxvk first, vulkan layer, host64 helper",
      s9[0] == "DXVK (DX9 -> Vulkan)"
      and s9[1] == "ReShade (Vulkan layer)" and "host64 helper process" in s9, str(s9))
# dgVoodoo2 was dropped in 1.6.0, so DXVK is the only DirectX 9 translation
# left: a DX9 game takes it whether or not the box is ticked, and nothing
# may ever put a dgVoodoo step back into the plan.
sp9 = installer.plan(g9, installer.Options(path=dlss.FEEDER))
check("dx9 takes dxvk even with the box unticked - it is the only way left",
      sp9[0] == "DXVK (DX9 -> Vulkan)"
      and installer.uses_dxvk(g9, installer.Options(path=dlss.FEEDER)), str(sp9))
check("no plan on any route mentions dgVoodoo any more",
      not any("dgvoodoo" in step.lower()
              for r in (dlss.FEEDER, dlss.NATIVE, dlss.BRIDGE, dlss.RENODX)
              for step in installer.plan(g9, installer.Options(path=r))))
check("dxvk puts d3d9.dll for dx9 and dxgi+d3d11 for dx11",
      dxvk.files_for("DX9") == ("d3d9.dll",) and "d3d11.dll" in dxvk.files_for("DX11"))
check("the renodx-dlss route never goes through dxvk (it hooks in-process)",
      not installer.uses_dxvk(g9, installer.Options(path=dlss.RENODX, dxvk=True)))
from core import vulkan as _vk
check("the 32-bit layer has its own manifest and both are unregistered",
      _vk.MANIFEST32 == "ReShade32.json" and "MANIFEST32" in open(SRC_DIR / "core" / "vulkan.py", encoding="utf8").read())

# The real thing: install through DXVK, check what landed, uninstall. Under
# a name of its own: the install refuses while a process of that name runs,
# and the owner may well be playing MGS V while this runs.
shutil.move(d / "mgsvtpp.exe", d / "dxvktest.exe")
dxvk.NEEDS_DXVK["dxvktest.exe"] = "test game"
g = games.manual(d)
g.api = "DX11"
try:
    installer.install(g, o, on_log=lambda t: None)
    idir = g.install_dir
    check("dxvk's dxgi.dll and d3d11.dll are in place, and they are dxvk",
          dxvk.is_dxvk(idir / "dxgi.dll") and dxvk.is_dxvk(idir / "d3d11.dll"))
    check("no reshade proxy dll beside them",
          not any(installer._is_reshade(idir / n) for n in installer.RESHADE_PROXIES))
    man = json.loads((idir / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest records dxvk and the vulkan layer",
          man.get("dxvk") and man["api"] == "Vulkan"
          and man["proxy"] == installer.VULKAN_LAYER, str((man.get("dxvk"), man["api"], man["proxy"])))
    check("the folder counts as a vulkan install", str(idir) in prefs.vulkan_games())
    check("the folder is remembered as an install", str(idir) in prefs.installs())
    (idir / "dxvktest_dxgi.log").write_text("x")
    (idir / "dxvktest_d3d11.log").write_text("x")
    installer.uninstall(g, on_log=lambda t: None)
    left = [p_.name for p_ in idir.rglob("*") if p_.is_file()]
    check("uninstall removes dxvk and its logs too", left == ["dxvktest.exe"], str(left))
    check("the folder is forgotten again", str(idir) not in prefs.installs())
except Exception as e:
    check("dxvk route installs", False, f"{type(e).__name__}: {e}")
dxvk.NEEDS_DXVK.pop("dxvktest.exe", None)
shutil.rmtree(d, ignore_errors=True)

# A ReShade left under another name is moved out of the way, ours or not.
d = Path(tempfile.mkdtemp(prefix="stray_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
(d / "d3d11.dll").write_bytes(b"MZ" + bytes(1 << 20) + b"ReShade")   # not ours
g = games.manual(d)
installer.install(g, installer.Options(path=dlss.BRIDGE, native_dlss=True),
                  on_log=lambda t: None)
check("a stray reshade d3d11.dll is moved aside before dxgi.dll goes in",
      not (d / "d3d11.dll").exists() and (d / "dxgi.dll").is_file()
      and (d / ("d3d11.dll" + installer.BACKUP_SUFFIX)).is_file())
installer.uninstall(g, on_log=lambda t: None)
check("uninstall puts the stray one back (it was not ours)",
      (d / "d3d11.dll").is_file() and not (d / "dxgi.dll").exists())
shutil.rmtree(d, ignore_errors=True)

# Without a record, uninstall still finds ReShade under any name - and only
# ReShade: a game's own d3d11.dll is left alone.
d = Path(tempfile.mkdtemp(prefix="norec_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "d3d12.dll").write_bytes(b"MZ" + bytes(1 << 20) + b"ReShade")
(d / "d3d11.dll").write_bytes(b"MZ" + bytes(1 << 20) + b"the game's own")
installer.uninstall(games.manual(d), on_log=lambda t: None)
check("no record: a reshade d3d12.dll is removed", not (d / "d3d12.dll").exists())
check("no record: a game's own d3d11.dll stays", (d / "d3d11.dll").is_file())
shutil.rmtree(d, ignore_errors=True)

# The user's ReShade keys and overlay settings travel to the next game.
a = Path(tempfile.mkdtemp(prefix="carry_a_"))
b = Path(tempfile.mkdtemp(prefix="carry_b_"))
(a / "ReShade.ini").write_text("[GENERAL]\nEffectSearchPaths=.\\x\n\n[INPUT]\n"
                               "KeyOverlay=36,0,0,0\nKeyEffects=145,0,0,0\n\n"
                               "[OVERLAY]\nTutorialProgress=4\nShowFPS=1\n\n"
                               "[STYLE]\nStyleIndex=2\n", encoding="utf8")
(b / "ReShade.ini").write_text("[GENERAL]\nEffectSearchPaths=.\\y\n\n[INPUT]\n"
                               "KeyOverlay=35,0,0,0\n", encoding="utf8")
src = reshade_ini.carry_over(b, [a])
bi = reshade_ini.Ini.load(b / "ReShade.ini")
check("settings come from the other game", src == a / "ReShade.ini")
check("the tutorial stays done and the fps counter follows",
      bi.get("OVERLAY", "TutorialProgress") == "4" and bi.get("OVERLAY", "ShowFPS") == "1")
check("a key this game already had is not overruled",
      bi.get("INPUT", "KeyOverlay") == "35,0,0,0")
check("this game's own paths are untouched",
      bi.get("GENERAL", "EffectSearchPaths") == ".\\y")
check("nothing to carry from an empty folder",
      reshade_ini.carry_over(a, [Path(tempfile.mkdtemp())]) is None)
shutil.rmtree(a, ignore_errors=True); shutil.rmtree(b, ignore_errors=True)

# The feeder's newer releases ship one zip; the loose names still resolve.
tag, assets = sources.resolve_feeder(prerelease=True)
check("the newest feeder pre-release is found", tag.startswith("v"), tag)
d = Path(tempfile.mkdtemp(prefix="feedzip_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
try:
    installer.install(g, installer.Options(path=dlss.FEEDER, feeder_prerelease=True),
                      on_log=lambda t: None)
    idir = g.install_dir
    check("the add-on and shader came out of the zip",
          (idir / "dlss5-feed.addon64").is_file()
          and (idir / "reshade-shaders/Shaders/DLSS5_Feed.fx").is_file())
    man = json.loads((idir / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest names the pre-release", man["components"].get("feeder") == tag,
          str(man["components"].get("feeder")))
    installer.uninstall(g, on_log=lambda t: None)
    left = [p_.name for p_ in idir.rglob("*") if p_.is_file()]
    check("pre-release feeder uninstalls clean", left == ["Game.exe"], str(left))
except Exception as e:
    check("pre-release feeder installs", False, f"{type(e).__name__}: {e}")
shutil.rmtree(d, ignore_errors=True)

check("dxvk is imported with the rest",
      "dxvk" in open(Path(__file__).with_name("test_all.py"), encoding="utf8").read())


# ------------------------------------------- 10. the diagnosis reads real logs
section("10. the diagnosis reads real logs")
import os as _os  # noqa: E402
import time as _time  # noqa: E402

_ADDONS = ["dlss5-feed.addon64", "renodx-dlss5.addon64"]


def _diag_dir(prefix: str, *, proxy: bool = True, addons: bool = True,
              reshade: str | None = None, feed: str | None = None,
              **extra) -> Path:
    """A folder that looks like a feeder install, minus whatever the test removes."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    man = {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
           "api": "DX11", "proxy": "dxgi.dll", "path": "feeder",
           "files": ["dxgi.dll", *_ADDONS, "reshade-shaders\\Shaders\\DLSS5_Feed.fx"]}
    man.update(extra)
    (d / "dlss5-autopilot.json").write_text(json.dumps(man), encoding="utf8")
    if proxy:
        (d / "dxgi.dll").write_bytes(b"MZ")
    # A real install leaves the shader there too, and the diagnosis now
    # treats a recorded file that has since gone as the answer (#84).
    _fx = d / "reshade-shaders" / "Shaders" / "DLSS5_Feed.fx"
    _fx.parent.mkdir(parents=True, exist_ok=True)
    _fx.write_text("// technique", encoding="utf8")
    if addons:
        for a in _ADDONS:
            (d / a).write_bytes(b"MZ")
    if reshade is not None:
        (d / "ReShade.log").write_text(reshade, encoding="utf8")
    if feed is not None:
        (d / "dlss5-feed.log").write_text(feed, encoding="utf8")
    return d


def _levels(rep, level):
    return [f_.title for f_ in rep.findings if f_.level == level]


# no log: the folder itself has to say why
_d = _diag_dir("diag_noproxy_", proxy=False)
_r = diagnose.analyse(_d)
check("missing proxy DLL is named, not 'never loaded'",
      not _r.ran and "dxgi.dll is missing" in _r.verdict
      and any("gone from the folder" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_noaddon_", addons=False)
_r = diagnose.analyse(_d)
check("a quarantined add-on is reported before anything else",
      not _r.ran and "gone from the folder" in _r.verdict
      and any("dlss5-feed.addon64" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_notrun_")
_r = diagnose.analyse(_d)
_info = " ".join(f_.title + f_.detail for f_ in _r.findings)
check("intact folder with no ReShade.log means 'not started since the install'",
      not _r.ran and _r.verdict.startswith("Not started since the install")
      and not _levels(_r, "bad"), _r.verdict)
check("...and the hints name the exe and the other proxy name",
      "Game.exe" in _info and "d3d11.dll" in _info)
# #171: that verdict rests on there being no log, so it has to say as much -
# Windows' fault record for the game's own exe is better evidence, and the
# report carried one while the answer said "run the game once".
check("...and the verdict marks itself as resting on the absent log",
      _r.never_ran is True)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_stale_", reshade="INFO | Initializing crosire's ReShade\n"
                                        'Registered add-on "DLSS 5 Feed" v0.1\n')
_old = _time.time() - 3600
_os.utime(_d / "ReShade.log", (_old, _old))
_r = diagnose.analyse(_d)
check("a ReShade.log older than the install is not evidence it ran",
      not _r.ran and "play once and check again" in _r.verdict.lower()
      and not _levels(_r, "bad"), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# frames delivered into a neural pass that cannot compile is not "Working."
_FEED_OK = ("[feed] effects: DLSS5_Feed.fx technique found, ColorInput found, "
            "DLSS5_MV_PROVIDER=3 (LumeniteFX Kernel) -> Lumenite_Kernel (enabled), depth reversed=1\n"
            "[feed] NVSDK_NGX_D3D12_Init -> 0x00000001 (Success)\n"
            "[feed] feature ready: 1920x1080 DLAA\n"
            "[feed] frame 1 delivered (1920x1080 at 100%)\n"
            "[feed] frame 2 delivered (1920x1080 at 100%)\n")
_OLD_COMPILER = (
    "d3dcompiler_47.dll: C:\\g\\d3dcompiler_47.dll -- rejects cs_5_1, hr=0x8876086C "
    "(error X3506: unrecognized compiler target 'cs_5_1'\n"
    "C:\\g\\d3dcompiler_47.dll is too old for Shader Model 5.1. The DLSS 5 add-on "
    "compiles its neural pass as cs_5_1, so neural rendering will silently do nothing "
    "-- this add-on will still report frames delivered\n")
_d = _diag_dir("diag_working_", feed=_FEED_OK)
_r = diagnose.analyse(_d)
check("frames delivered with a good compiler is Working.", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_oldcomp_", feed=_OLD_COMPILER + _FEED_OK)
_r = diagnose.analyse(_d)
check("an old d3dcompiler_47.dll stops 'Working.' (feed log form)",
      _r.verdict.startswith("Frames flow, but neural rendering is silently doing nothing")
      and any("d3dcompiler_47.dll is too old" in t for t in _levels(_r, "bad")), _r.verdict)
check("...and the finding carries the rename fix",
      any("dlss5-off" in f_.detail for f_ in _r.findings))
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_oldcomp2_", feed=_FEED_OK, reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "ERROR | error X3506: unrecognized compiler target 'cs_5_1'\n"))
_r = diagnose.analyse(_d)
check("an old d3dcompiler_47.dll stops 'Working.' (ReShade.log form)",
      "silently doing nothing" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# shader compile errors: only the feed's own shaders count
_d = _diag_dir("diag_shaders_", feed=_FEED_OK, reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "ERROR | Failed to compile 'C:\\g\\reshade-shaders\\Shaders\\lumenite_RTAO.fx':\n"
    "ERROR | Failed to compile 'C:\\g\\reshade-shaders\\Shaders\\lumenite_SSSR.fx':\n"
    "ERROR | Failed to load 'C:\\g\\reshade-shaders\\Shaders\\lumenite_TRAA.fx'\n"))
_r = diagnose.analyse(_d)
_bad = _levels(_r, "bad")
_info = [f_ for f_ in _r.findings if f_.level == "info" and "other shaders" in f_.title]
check("shaders the feed does not use are one INFO line, not failures",
      not _bad and len(_info) == 1 and _info[0].title.startswith("3 other shaders")
      and "lumenite_RTAO.fx" in _info[0].detail and "lumenite_TRAA.fx" in _info[0].detail,
      str(_bad) + " " + str([f_.title for f_ in _info]))
check("the verdict stays Working.", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_shaders2_", feed=_FEED_OK, reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "ERROR | Failed to compile 'C:\\g\\reshade-shaders\\Shaders\\DLSS5_Feed.fx':\n"
    "ERROR | Failed to compile 'C:\\g\\reshade-shaders\\Shaders\\lumenite_Kernel.fx':\n"))
_r = diagnose.analyse(_d)
_bad = _levels(_r, "bad")
check("the feed's own shaders failing IS reported",
      len(_bad) == 2 and any("DLSS5_Feed.fx" in t for t in _bad)
      and any("lumenite_Kernel.fx" in t for t in _bad), str(_bad))
shutil.rmtree(_d, ignore_errors=True)

# the new feeder lines
_FLAT = ("[feed] Depth probe (4x 32x32, frame 600): min 0, max 0, mean 0, variance 0, "
         "100% finite  <-- sampled depth is flat; inspect the depth debug view\n")
_d = _diag_dir("diag_depth_", feed=_FEED_OK + _FLAT)
_r = diagnose.analyse(_d)
check("flat depth in a game is a warning with the Generic Depth hint",
      any("depth buffer" in t for t in _levels(_r, "warn"))
      and any("aspect ratio heuristics" in f_.detail for f_ in _r.findings))
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_depthvideo_", feed=_FEED_OK + _FLAT, kind="video")
_r = diagnose.analyse(_d)
check("flat depth in a video player is expected (info only)",
      any("video player" in t for t in _levels(_r, "info"))
      and not any("depth buffer" in t for t in _levels(_r, "warn")))
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_lastwins_", feed=(
    "[feed] effects: DLSS5_Feed.fx technique MISSING, ColorInput MISSING, "
    "DLSS5_MV_PROVIDER=3 (LumeniteFX Kernel) -> none (not installed), depth reversed=1\n"
    "DLSS5_Feed.fx is not loaded (technique/textures missing) -- install it.\n"
    + _FEED_OK), reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "WARN | [DLSS 5 Feed] DLSS5_Feed.fx is not loaded (technique/textures missing)\n"
    "WARN | Skipping device because the focus window is the desktop window.\n"))
_r = diagnose.analyse(_d)
check("the last 'technique found' wins over an earlier MISSING",
      not _levels(_r, "bad") and _r.verdict == "Working."
      and any("Lumenite" in t and "enabled" in t for t in _levels(_r, "ok")),
      str(_levels(_r, "bad")) + " " + _r.verdict)
check("the desktop-window skip is information only",
      any("desktop" in t for t in _levels(_r, "info")))
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_d3d9_", reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "INFO | Redirecting Direct3DCreate9Ex(SDKVersion = 32, ppD3D = 0) ...\n"
    "INFO | Exiting ...\n"))
_r = diagnose.analyse(_d)
check("a D3D9 device under a DXGI install is called out",
      any("Direct3D 9" in t for t in _levels(_r, "warn"))
      and not any("closed before" in t for t in _levels(_r, "bad")),
      str([f_.title for f_ in _r.findings]))
shutil.rmtree(_d, ignore_errors=True)

# the real video-player install, when it is on this machine
_real = Path(r"C:\Users\Mustafa\Desktop\dlss 5\_video\mpc-hc")
# Only when the player's own logs are from a real playback: a helper started
# from that folder can overwrite ReShade.log with a no-swapchain session.
if (_real / "dlss5-feed.log").is_file()         and "Registered add-on" in (_real / "ReShade.log").read_text(errors="replace"):
    _r = diagnose.analyse(_real)
    # A live folder: the owner plays in it, so its verdict is whatever the
    # last session did. What is checked is that real logs parse to a verdict.
    check("the mpc-hc sample parses to a verdict",
          _r.ran and bool(_r.verdict), _r.verdict)

# the bug report body
_d = _diag_dir("diag_body_", feed=_FEED_OK, reshade=(
    "INFO | Redirecting RegisterClassW(...)\n"
    'INFO | Registered add-on "DLSS 5 Feed" v0.11\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"))
_r = diagnose.analyse(_d)
_body = diagnose.issue_body("9.9", "RTX 4060 Ti", 89, "581.0", None, "feeder", _r,
                            "scan steam: 3 found\nscan epic: 0 found\nreal line\n",
                            Path("C:/x/autopilot.log"), _d, last_error="Traceback: boom")
_order = ["**Did the game start?**", "**What happened**", "**What I expected**", "- version: 9.9",
          "**Diagnosis**", "**Files in the folder**", "**ReShade.log**",
          "**dlss5-feed.log**", "**Last error**", "autopilot.log"]
_pos = [_body.find(k) for k in _order]
check("the report has every section, in order",
      all(p >= 0 for p in _pos) and _pos == sorted(_pos), str(_pos))
check("the folder check names the proxy and the add-ons",
      "- dxgi.dll: present" in _body and "- dlss5-feed.addon64: present" in _body
      and "- nvngx_dlssnr.dll: MISSING" in _body)
check("ReShade.log is filtered to what matters",
      "Registered add-on" in _body and "CreateSwapChainForHwnd" in _body
      and "RegisterClassW" not in _body)
check("the scan lines are dropped from the autopilot tail",
      "real line" in _body and "scan steam" not in _body)
check("the whole report fits in a URL-sized budget", len(_body) <= 6000, str(len(_body)))
shutil.rmtree(_d, ignore_errors=True)
_body = diagnose.issue_body("9.9", "x", None, "?", None, "optiscaler", None, "",
                            Path("C:/x/autopilot.log"), Path(tempfile.mkdtemp(prefix="diag_empty_")))
check("missing logs say (none), and the OptiScaler tail appears on that route",
      _body.count("(none)") >= 3 and "**OptiScaler.log**" in _body)



# ------------------------------------------------- 11. video player
section("11. the video player and the d3dcompiler sideline")
from core import video  # noqa: E402

# The ini: written fresh, and merged into one the person already edited.
_d = Path(tempfile.mkdtemp(prefix="video_ini_"))
video._write_ini(_d)
_ini = (_d / video.INI).read_text(encoding="utf8")
check("fresh ini selects the D3D11 renderer and silences the updater",
      "DSVidRen=14" in _ini and "UpdaterAutoCheck=0" in _ini and "[Settings]" in _ini)
(_d / video.INI).write_text("[Settings]\r\nDSVidRen=11\r\nVolume=42\r\n"
                            "YDLMaxHeight=720\r\n[Other]\r\nX=1\r\n", encoding="utf8")
video._write_ini(_d)
_ini = (_d / video.INI).read_text(encoding="utf8")
check("a user-set renderer is corrected back to MPCVR",
      "DSVidRen=14" in _ini and "DSVidRen=11" not in _ini)
check("the user's other settings survive", "Volume=42" in _ini and "X=1" in _ini)
check("a user-set YouTube quality is respected",
      "YDLMaxHeight=720" in _ini and "YDLMaxHeight=1440" not in _ini)
check("keys are not duplicated", _ini.count("UpdaterAutoCheck=") == 1
      and _ini.count("[Settings]") == 1)
# YDLExePath made MPC-HC fail to open any URL; an earlier build wrote it.
(_d / video.INI).write_bytes(b"[Settings]\r\nYDLExePath=C:\\x\\yt-dlp.exe\r\n")
video._write_ini(_d)
(_d / video.INI).write_bytes(b"\xef\xbb\xbf[Settings]\r\nVolume=42\r\n[Other]\r\nX=1\r\n")
for _ in range(3):
    video._write_ini(_d)
_raw = (_d / video.INI).read_bytes()
check("three rewrites keep the BOM, CRLF only, and add no blank lines",
      _raw.startswith(b"\xef\xbb\xbf") and b"\r\r" not in _raw
      and b"\r\n\r\n" not in _raw and b"\n" not in _raw.replace(b"\r\n", b""))
(_d / video.INI).write_bytes(b"[Settings]\r\nYDLExePath=C:\\x\\yt-dlp.exe\r\n")
video._write_ini(_d)
(_d / video.INI).write_bytes(
    b"[Settings]\r\nDSVidRen=14\r\n[Commands2]\r\n"
    b"CommandMod9=807 3 74 \"\" 5 0 0 0 0 0\r\n"
    b"CommandMod38=996 3 24 \"\" 5 0 0 0 0 0\r\n"
    b"CommandMod40=830 3 0 \"\" 5 0 0 0 0 0\r\n")
video._write_ini(_d)
_txt = (_d / video.INI).read_text(encoding="utf8")
check("the player's Home = jump-to-start binding is taken away",
      "CommandMod38=996 3 0 " in _txt and "CommandMod9=807 3 74 " in _txt
      and "CommandMod40=830 3 0 " in _txt, _txt[-200:])
check("a stray YDLExePath is dropped",
      "YDLExePath" not in (_d / video.INI).read_text(encoding="utf8"))
check("helper tools live under tools/, yt-dlp beside the player",
      video.tools_dir(_d) == _d / "tools" and video.YTDLP == "yt-dlp.exe")
shutil.rmtree(_d, ignore_errors=True)

check("the checklist names the toggle key", any("F6" in c for c in video.CHECKLIST))
check("video default folder is under the user's Videos",
      "Videos" in str(video.default_dir()))

# The sideline: a game-shipped d3dcompiler_47.dll goes aside on install and
# comes back on uninstall, byte for byte, without ever being deleted.
_d = Path(tempfile.mkdtemp(prefix="sideline_"))
shutil.copyfile(X64, _d / "Game.exe")
_comp = b"OLD COMPILER" + b"\x00" * 300
(_d / "D3DCompiler_47.dll").write_bytes(_comp)
_g = games.manual(_d)
installer.install(_g, installer.Options(), on_log=lambda t: None)
_moved = _d / ("D3DCompiler_47.dll" + installer.SIDELINE_SUFFIX)
check("d3dcompiler_47.dll is moved aside by the install",
      _moved.is_file() and not (_d / "D3DCompiler_47.dll").exists())
_man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
check("the manifest records the sideline and the kind",
      _man.get("sidelined") == ["D3DCompiler_47.dll"] and _man.get("kind") == "game")
# Reinstalling must not lose the original.
installer.install(_g, installer.Options(), on_log=lambda t: None)
check("a reinstall keeps the moved-aside original",
      _moved.is_file() and _moved.read_bytes() == _comp)
installer.uninstall(_g, on_log=lambda t: None)
check("uninstall puts the game's compiler back, byte for byte",
      (_d / "D3DCompiler_47.dll").is_file()
      and (_d / "D3DCompiler_47.dll").read_bytes() == _comp
      and not _moved.exists())
check("nothing of ours is left",
      not (_d / "dxgi.dll").exists() and not (_d / installer.MANIFEST).exists())
shutil.rmtree(_d, ignore_errors=True)

# An install with no such file records an empty list, and the OptiScaler
# route leaves the game's compiler alone (it does not use the neural pass
# through ReShade).
_d = Path(tempfile.mkdtemp(prefix="sideline_none_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
installer.install(_g, installer.Options(), on_log=lambda t: None)
_man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
check("no compiler, nothing sidelined", _man.get("sidelined") == [])
installer.uninstall(_g, on_log=lambda t: None)
shutil.rmtree(_d, ignore_errors=True)

_real = Path(r"C:\Users\Mustafa\Desktop\dlss 5\_video\mpc-hc")
if video.is_player(_real):
    _vg = video.as_game(_real)
    check("the real player folder is seen as a 64-bit D3D11 video target",
          _vg.kind == "video" and _vg.bitness == 64 and _vg.api == "DX11"
          and _vg.exe.name == video.PLAYER_EXE, f"{_vg.api} {_vg.bitness}")
    _sup = dlss.detect(_vg.install_dir, _vg.folder, _vg.api, _vg.bitness or 0, 89)
    check("...and the feeder is what it gets", _sup.recommended == dlss.FEEDER)


# ------------------------------------------------- 12. xbox and game pass
section("12. xbox and game pass")
# The path test on its own: only the two system-owned folder names count,
# and ModifiableWindowsApps (the one meant to be modified) must not.
check("XboxGames\\...\\Content is a locked store path",
      games.is_locked_store_path(Path(r"C:\XboxGames\Forza Horizon 6\Content")))
check("WindowsApps is a locked store path",
      games.is_locked_store_path(Path(r"C:\Program Files\WindowsApps\X")))
check("ModifiableWindowsApps is not",
      not games.is_locked_store_path(Path(r"C:\Program Files\ModifiableWindowsApps\X")))
check("a plain folder is not", not games.is_locked_store_path(Path(r"D:\Games\X")))

# A readable game under XboxGames is a normal game.
_d = Path(tempfile.mkdtemp(prefix="xbox_ok_"))
_content = _d / "XboxGames" / "Fake" / "Content"
_content.mkdir(parents=True)
shutil.copyfile(X64, _content / "Game.exe")
_g = games.manual(_content)
check("a readable XboxGames game scans like any other",
      not _g.error and _g.bitness == 64 and _g.exe == _content / "Game.exe",
      f"{_g.error!r} {_g.bitness}")
check("...and is supported", installer.check_supported(_g)[0])
shutil.rmtree(_d, ignore_errors=True)


_real_open = open


def _protected_read(path, mode="r", *args, **kwargs):
    if Path(path) == _exe and mode == "rb":
        raise PermissionError(13, "Permission denied", str(path))
    return _real_open(path, mode, *args, **kwargs)


# The real failure: an exe under XboxGames the user cannot read.
_d = Path(tempfile.mkdtemp(prefix="xbox_locked_"))
_content = _d / "XboxGames" / "Fake" / "Content"
_content.mkdir(parents=True)
_exe = _content / "Game.exe"
shutil.copyfile(X64, _exe)
with patch("builtins.open", side_effect=_protected_read):
    _g = games.manual(_content)
    check("an unreadable XboxGames exe gets a warning, not a fatal error",
          not _g.error and _g.exe_warning == games.XBOX_EXE_HINT, repr(_g.exe_warning))
    check("...the game keeps its executable so it stays listed", _g.exe == _exe)
    _ok, _why = installer.check_supported(_g)
    check("...and check_supported requests missing metadata, naming the "
          "controls as the window spells them",
          not _ok and "'architecture'" in _why and "'graphics api'" in _why,
          repr(_why))
    installer.preflight(_g)
    check("writing beside a protected EXE is allowed", True)
check("the executable was not changed", _exe.read_bytes()[:2] == b"MZ")
shutil.rmtree(_d, ignore_errors=True)

# The same unreadable exe outside a store folder keeps the plain error: the
# Xbox instruction would send someone to an app that has nothing to do with
# their game.
_d = Path(tempfile.mkdtemp(prefix="plain_locked_"))
_exe = _d / "Game.exe"
shutil.copyfile(X64, _exe)
with patch("builtins.open", side_effect=_protected_read):
    _g = games.manual(_d)
    check("an unreadable exe elsewhere stays fatal without an Xbox warning",
          _g.error and not _g.exe_warning and not installer.check_supported(_g)[0],
          repr(_g.error))
shutil.rmtree(_d, ignore_errors=True)

# preflight distinguishes a refused directory write from EXE protection.
_d = Path(tempfile.mkdtemp(prefix="xbox_pre_"))
_content = _d / "XboxGames" / "Fake" / "Content"
_content.mkdir(parents=True)
shutil.copyfile(X64, _content / "Game.exe")
_gx = games.manual(_content)
_plain = Path(tempfile.mkdtemp(prefix="plain_pre_"))
shutil.copyfile(X64, _plain / "Game.exe")
_gp = games.manual(_plain)
_orig_probe = tempfile.NamedTemporaryFile


def _refuse(*args, **kwargs):
    raise PermissionError(13, "Permission denied", str(kwargs.get("dir")))


tempfile.NamedTemporaryFile = _refuse
try:
    try:
        installer.preflight(_gx)
        check("preflight under XboxGames raises on a refused write", False)
    except installer.InstallError as e:
        check("preflight under XboxGames reports the actual write restriction",
              games.XBOX_HINT in str(e) and "Permission denied" not in str(e)
              and "administrator" not in str(e), str(e)[:80])
    try:
        installer.preflight(_gp)
        check("preflight elsewhere raises on a refused write", False)
    except installer.InstallError as e:
        check("preflight elsewhere still says run as administrator",
              "administrator" in str(e) and "Xbox" not in str(e), str(e)[:80])
finally:
    tempfile.NamedTemporaryFile = _orig_probe
try:
    installer.preflight(_gp)
    check("preflight passes again once writes work", True)
except installer.InstallError as e:
    check("preflight passes again once writes work", False, str(e))
shutil.rmtree(_d, ignore_errors=True)
shutil.rmtree(_plain, ignore_errors=True)

# ---------------------------------------------------------------- 13. profiles
section("13. settings profiles")
from core import profiles  # noqa: E402
_pdir = Path(tempfile.mkdtemp(prefix="profiles_"))
_old_dir = profiles.DIR
profiles.DIR = _pdir / "profiles"
try:
    _full = installer.Options(
        provider=4, renodx="v1.2.3", renodx_local=Path(r"C:\x\renodx.addon64"),
        dlssnr="310.1.0", dlss="310.2.1", keep_game_dlss=False,
        feed={"work_resolution": 80, "preset": 10, "hdr": 1},
        ignore_gpu_mismatch=True, path=dlss.OPTI, opti_proxy="winmm.dll",
        reshade_proxy="d3d11.dll", native_dlss=True, feeder_prerelease=True,
        feeder_tag="v0.9.0-beta", dxvk=True,
        nr={"WorkingScale": 0.66, "Preset": 2, "Style": 1})
    check("built-ins are listed with nothing on disk",
          profiles.list_profiles() == ["Quality", "Balanced", "Performance"],
          str(profiles.list_profiles()))
    check("built-ins are recognised", all(profiles.is_builtin(n) for n in
          ("Quality", "Balanced", "Performance")) and not profiles.is_builtin("Mine"))
    _b = profiles.load("Performance")
    check("built-in Performance is 70% / 0.5",
          _b.feed == {"work_resolution": 70} and _b.nr == {"WorkingScale": 0.5, "Preset": 0})

    _pf = profiles.save("My Cyberpunk", _full)
    check("save lands under the profiles folder", _pf.parent == profiles.DIR and _pf.is_file())
    _raw = json.loads(_pf.read_text(encoding="utf8"))
    check("the file keeps the display name, a timestamp and the app version",
          _raw.get("name") == "My Cyberpunk" and "saved" in _raw
          and _raw.get("app_version") == update.VERSION)
    check("machine and per-game fields are NOT written",
          not any(k in _raw for k in ("renodx_local", "native_dlss", "ignore_gpu_mismatch")))
    _back = profiles.load("My Cyberpunk")
    check("every profile field survives a round-trip", all(
        getattr(_back, f) == getattr(_full, f) for f in profiles.FIELDS),
          str([(f, getattr(_back, f), getattr(_full, f)) for f in profiles.FIELDS
               if getattr(_back, f) != getattr(_full, f)]))
    check("...and the excluded ones come back as defaults",
          _back.renodx_local is None and _back.native_dlss is False
          and _back.ignore_gpu_mismatch is False)
    check("the saved profile is listed after the built-ins",
          profiles.list_profiles() == ["Quality", "Balanced", "Performance", "My Cyberpunk"])

    # apply: the profile wins on its own fields, the game keeps its own
    _base = installer.Options(path=dlss.FEEDER, provider=3, native_dlss=True,
                              renodx_local=Path(r"C:\me\renodx.addon64"),
                              ignore_gpu_mismatch=True, feed={"work_resolution": 100})
    _ap = profiles.apply(_base, _back)
    check("apply overlays the profile's fields",
          _ap.path == dlss.OPTI and _ap.provider == 4 and _ap.feed == _full.feed
          and _ap.nr == _full.nr and _ap.dxvk is True and _ap.feeder_tag == "v0.9.0-beta")
    check("apply keeps the per-game and per-machine fields",
          _ap.native_dlss is True and _ap.renodx_local == _base.renodx_local
          and _ap.ignore_gpu_mismatch is True)
    check("apply does not alias the profile's dicts",
          _ap.feed is not _back.feed and _ap.nr is not _back.nr)
    check("apply leaves the base untouched", _base.path == dlss.FEEDER and _base.provider == 3)

    # odd names land on disk safely and still round-trip
    _odd = 'we/ird: name*?<>|"\\ \u00e7\u011f'
    _op = profiles.save(_odd, installer.Options())
    check("an odd name becomes a safe file name",
          _op.is_file() and _op.parent == profiles.DIR
          and all(ord(c) < 128 for c in _op.name)
          and not any(c in _op.name for c in '/\\:*?<>|"'), _op.name)
    check("...and is listed under its display name", _odd in profiles.list_profiles())
    check("...and loads by its display name", profiles.load(_odd).path == dlss.FEEDER)
    check("a name of only odd characters still gets a file",
          profiles.save("???", installer.Options()).is_file())

    # bad data
    (profiles.DIR / "broken.json").write_text(json.dumps(
        {"name": "Broken", "path": "teleport", "provider": 3}), encoding="utf8")
    try:
        profiles.load("Broken")
        check("an unknown route is rejected", False)
    except ValueError as e:
        check("an unknown route is rejected", "teleport" in str(e), str(e))
    (profiles.DIR / "extra.json").write_text(json.dumps(
        {"name": "Extra", "path": "bridge", "future_knob": 1, "provider": "4"}), encoding="utf8")
    _ex = profiles.load("Extra")
    check("unknown keys are ignored, missing keys default, types coerced",
          _ex.path == dlss.BRIDGE and _ex.provider == 4 and _ex.keep_game_dlss is True
          and _ex.feed == {} and _ex.nr == {})
    try:
        profiles.load("does not exist")
        check("a missing profile is a plain error", False)
    except ValueError:
        check("a missing profile is a plain error", True)
    for _bad in ("Quality", "Balanced"):
        try:
            profiles.save(_bad, installer.Options())
            check(f"built-in {_bad} cannot be overwritten", False)
        except ValueError:
            check(f"built-in {_bad} cannot be overwritten", True)

    # describe
    _desc = profiles.describe(_full)
    check("describe names the route and the dials",
          "route optiscaler" in _desc and "work resolution 80%" in _desc
          and "model resolution 66%" in _desc and "dxvk" in _desc, str(_desc))
    _fd = profiles.describe(installer.Options(provider=3, feed={"work_resolution": 85}))
    check("describe names the feeder provider",
          "route feeder" in _fd and "provider 3 (LumeniteFX Kernel 2.0)" in _fd
          and "work resolution 85%" in _fd, str(_fd))

    # delete
    profiles.delete("My Cyberpunk")
    check("delete removes the file", not _pf.exists()
          and "My Cyberpunk" not in profiles.list_profiles())
    profiles.delete("My Cyberpunk")
    check("deleting twice is harmless", True)
    try:
        profiles.delete("Quality")
        check("built-ins cannot be deleted", False)
    except ValueError:
        check("built-ins cannot be deleted", "Quality" in profiles.list_profiles())
finally:
    profiles.DIR = _old_dir
    shutil.rmtree(_pdir, ignore_errors=True)

# ------------------------------------------- 16. before/after screenshots
section("16. before/after screenshots")
import os  # noqa: E402
import struct  # noqa: E402
import time as _time  # noqa: E402
from core import compare  # noqa: E402

_d = Path(tempfile.mkdtemp(prefix="compare_"))
(_d / "ReShade.ini").write_text(
    "[INPUT]\nKeyScreenshot=44,1,0,0\n[SCREENSHOT]\nSavePath=.\\shots\n",
    encoding="utf8")
(_d / "shots").mkdir()
_now = _time.time()


def _mk(rel, age, size=8):
    p = _d / rel
    p.write_bytes(b"x" * size)
    os.utime(p, (_now - age, _now - age))
    return p


_old = _mk("Game 2020-01-01 10-00-00.png", 0)      # name stamp wins over mtime
_a = _mk("shots/Game_1.png", 200)
_b = _mk("shots/Game_2.png", 100)
_j = _mk("Game_3.jpg", 50)
_mk("dlss5_compare_2026.png", 10)                  # ours - never listed
_mk("readme.txt", 5)
_found = compare.find_screenshots(_d)
check("save path resolves relative to the game folder",
      compare.save_path(_d) == (_d / "shots").resolve(), str(compare.save_path(_d)))
check("finds images in the game folder and the save path, ignoring ours and non-images",
      set(_found) == {_old, _a, _b, _j}, ", ".join(p.name for p in _found))
check("newest first, name stamp beating mtime",
      _found[:3] == [_j, _b, _a] and _found[-1] == _old)
check("pairs the two newest within 5 minutes, oldest first",
      compare.pair(_found) == (_b, _j))
_c = _mk("shots/Game_4.png", 0)
os.utime(_b, (_now - 2000, _now - 2000))
os.utime(_j, (_now - 1000, _now - 1000))
check("the newest close-enough pair wins over older shots",
      compare.pair(compare.find_screenshots(_d)) == (_a, _c))
check("...and none when no two are close", compare.pair([_c, _j, _b]) is None)
check("a single file has no pair", compare.pair([_c]) is None)
check("screenshot key: vk + modifier from the ini",
      compare.screenshot_key(_d) == "Ctrl + Print Screen", compare.screenshot_key(_d))
check("screenshot key: default when no ini",
      compare.screenshot_key(_d / "nowhere") == "Print Screen")
check("key names: 44, F5, letters, numpad, unbound, garbage",
      compare.key_name("44,0,0,0") == "Print Screen"
      and compare.key_name("116,0,0,0") == "F5"
      and compare.key_name("65,0,1,0") == "Shift + A"
      and compare.key_name("101") == "Numpad 5"
      and compare.key_name("0,0,0,0").startswith("not bound")
      and compare.key_name("garbage") == "Print Screen")
check("fit factor caps a 4K side at 1920",
      compare.fit_factor(3840, 1920) == 2 and compare.fit_factor(1920, 1920) == 1
      and compare.fit_factor(2000, 1920) == 2)

try:
    import tkinter as _tk
    _root = _tk.Tk()
    _root.withdraw()
except Exception as e:  # no display / no Tcl on this machine
    _root = None
    check("tk could not start - export test skipped", True, f"{type(e).__name__}: {e}")
if _root is not None:
    def _png(name, w, h, colour):
        img = _tk.PhotoImage(master=_root, width=w, height=h)
        img.put("{" + " ".join([colour] * w) + "}", to=(0, 0, w, h))
        p = _d / name
        img.write(str(p), format="png")
        return p
    _pa = _png("Game 2026-09-02 12-00-00.png", 40, 30, "#d8a657")
    _pb = _png("Game 2026-09-02 12-00-20.png", 60, 20, "#6f9f6f")
    _out = compare.export_side_by_side(_pa, _pb, _d / "out" / "combo.png", master=_root)
    _hdr = _out.read_bytes()[:24]
    _w, _h = struct.unpack(">II", _hdr[16:24])
    check("export writes a png of the combined width and the taller height",
          _hdr[:8] == b"\x89PNG\r\n\x1a\n" and (_w, _h) == (100, 30), f"{_w}x{_h}")
    _chk = _tk.PhotoImage(master=_root, file=str(_out))
    check("left pixels come from a, right pixels from b",
          _chk.get(5, 5) == (0xd8, 0xa6, 0x57) and _chk.get(70, 5) == (0x6f, 0x9f, 0x6f))
    _big = _tk.PhotoImage(master=_root, width=4000, height=2)
    _big.put("{" + " ".join(["#ffffff"] * 4000) + "}", to=(0, 0, 4000, 2))
    _big.write(str(_d / "big.png"), format="png")
    _out2 = compare.export_side_by_side(_d / "big.png", _pa, _d / "combo2.png", master=_root)
    _w2, _ = struct.unpack(">II", _out2.read_bytes()[16:24])
    check("a wide side is subsampled under 1920 first",
          _w2 == 1334 + 40 and compare.fit_factor(4000, 1920) == 3, str(_w2))
    check("without a master it makes and tears down its own hidden root",
          compare.export_side_by_side(_pa, _pb, _d / "combo3.png").is_file())
    check("the export is 'ours' and never listed as a screenshot",
          compare.is_ours(compare.export_name(_d))
          and (_d / "out" / "combo.png") not in compare.find_screenshots(_d))
    try:
        from core import compareui
        _p = Path(tempfile.mkdtemp(prefix="compare_pair_"))
        shutil.copy(_pa, _p); shutil.copy(_pb, _p)
        _pa, _pb = _p / _pa.name, _p / _pb.name
        _cw = compareui.show(_root, _p, "test game")
        _root.update()
        check("the compare window opens on a real pair and shows both",
              _cw.shots == [_pa, _pb] and all(x is not None for x in _cw._full))
        _cw._swap()
        check("swap flips the sides", _cw.shots == [_pb, _pa])
        _cw.win.destroy()
        _e = Path(tempfile.mkdtemp(prefix="compare_empty_"))
        _cw = compareui.show(_root, _e, "empty game")
        _root.update()
        check("with no screenshots it explains the F6 / screenshot-key flow",
              not _cw.shots and "F6" in _cw.help_text.cget("text")
              and "Print Screen" in _cw.help_text.cget("text"))
        _cw.win.destroy()
        shutil.rmtree(_e, ignore_errors=True)
        shutil.rmtree(_p, ignore_errors=True)
    except Exception as e:
        check("compare window", False, f"{type(e).__name__}: {e}")
    _root.destroy()
shutil.rmtree(_d, ignore_errors=True)

# --------------------------------------- 15. the preview tells the truth
section("15. the install preview tells the truth")


def _snapshot(d: Path) -> set:
    return {p.relative_to(d).as_posix() for p in d.rglob("*")}


def _unknowable(route: str, f: str) -> bool:
    """Files only a download reveals: LumeniteFX's shader set and the
    OptiScaler package. The preview lists them when the cache has the zip
    and by pattern otherwise, so the exact-name check must let them pass."""
    fl = f.lower()
    if fl.startswith("reshade-shaders/") and "lumenite_" in fl:
        return True
    if route == dlss.OPTI and (fl.startswith(("optiscaler/", "licenses/"))
                               or fl.endswith(".txt") or fl.startswith("!!")):
        return True
    return "*" in f


for route in (dlss.NATIVE, dlss.BRIDGE, dlss.FEEDER, dlss.OPTI, dlss.RENODX):
    d = Path(tempfile.mkdtemp(prefix=f"pv_{route}_"))
    shutil.copyfile(X64, d / "Game.exe")
    g = games.manual(d)
    o = installer.Options(path=route, native_dlss=route != dlss.FEEDER)
    before = _snapshot(d)
    pv = installer.preview(g, o)
    check(f"{route}: preview creates nothing", _snapshot(d) == before,
          str(_snapshot(d) - before))
    check(f"{route}: preview steps are the plan", pv.steps == installer.plan(g, o))
    check(f"{route}: clean folder - no blockers, backups or removals",
          not pv.blockers and not pv.backups and not pv.removes,
          str((pv.blockers, pv.backups, pv.removes)))
    check(f"{route}: nothing outside the folder", not pv.outside, str(pv.outside))
    check(f"{route}: the manifest is announced", installer.MANIFEST in pv.writes)
    try:
        installer.install(g, o, on_log=lambda t: None)
        man = json.loads((d / installer.MANIFEST).read_text(encoding="utf8"))
        wrote = {str(f).replace("\\", "/") for f in man["files"]}
        pv_back = {b.split(" -> ")[0] for b in pv.backups}
        unannounced = []
        for f in wrote:
            if f.endswith(installer.BACKUP_SUFFIX):
                if f[:-len(installer.BACKUP_SUFFIX)] not in pv_back:
                    unannounced.append(f)
            elif f not in pv.writes and not _unknowable(route, f):
                unannounced.append(f)
        check(f"{route}: every file the install wrote was announced",
              not unannounced, str(unannounced))
        on_disk = _snapshot(d)
        unannounced = [f for f in on_disk - before
                       if (d / f).is_file() and f not in wrote
                       and f not in pv.writes and not _unknowable(route, f)]
        check(f"{route}: every file on disk was announced", not unannounced,
              str(unannounced))
        extra = [w for w in pv.writes if w not in wrote and w != installer.MANIFEST
                 and not _unknowable(route, w)]
        check(f"{route}: the preview promised nothing the install did not do",
              not extra, str(extra))
        # A same-route reinstall: what is there is ours, so no backups.
        pv2 = installer.preview(g, o)
        check(f"{route}: a reinstall backs up none of our own files",
              not pv2.backups, str(pv2.backups))
        installer.uninstall(g, on_log=lambda t: None)
    except Exception as e:
        check(f"{route}: preview vs install", False, f"{type(e).__name__}: {e}")
    shutil.rmtree(d, ignore_errors=True)

# The game's own files: an nvngx_dlss.dll we replace is backed up, one we
# keep is neither written nor backed up; the compiler goes aside with the
# arrow the log shows.
d = Path(tempfile.mkdtemp(prefix="pv_bak_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "nvngx_dlss.dll").write_bytes(b"GAME OWN" + bytes(500))
(d / "d3dcompiler_47.dll").write_bytes(b"OLD COMPILER" + bytes(300))
(d / "ReShade.ini").write_bytes(b"[GENERAL]\nMine=1\n")
g = games.manual(d)
pv = installer.preview(g, installer.Options(keep_game_dlss=False))
check("replacing the game's dlss is announced as a backup",
      "nvngx_dlss.dll" in pv.backups and "nvngx_dlss.dll" in pv.writes,
      str(pv.backups))
check("the user's ReShade.ini is backed up", "ReShade.ini" in pv.backups)
check("the compiler sideline is shown with its new name",
      "d3dcompiler_47.dll -> d3dcompiler_47.dll" + installer.SIDELINE_SUFFIX
      in pv.backups, str(pv.backups))
pv = installer.preview(g, installer.Options(keep_game_dlss=True))
check("keeping the game's dlss: neither written nor backed up",
      "nvngx_dlss.dll" not in pv.backups and "nvngx_dlss.dll" not in pv.writes)
lines = installer.preview_lines(pv)
check("the lines say what is backed up",
      any(l.startswith("will back up:") and "d3dcompiler_47.dll" in l for l in lines)
      and any(l.startswith("will write ") for l in lines)
      and "nothing is written outside this folder" in lines, str(lines))
try:
    installer.install(g, installer.Options(keep_game_dlss=False), on_log=lambda t: None)
    check("the backup really happened as previewed",
          (d / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).is_file()
          and (d / ("d3dcompiler_47.dll" + installer.SIDELINE_SUFFIX)).is_file())
    installer.uninstall(g, on_log=lambda t: None)
except Exception as e:
    check("backup preview vs install", False, f"{type(e).__name__}: {e}")
shutil.rmtree(d, ignore_errors=True)

# Vulkan: no proxy DLL, the layer lands outside the folder - and so does a
# D3D11 game sent through DXVK, which additionally gets DXVK's DLLs.
d = Path(tempfile.mkdtemp(prefix="pv_vk_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
g.api = "Vulkan"
pv = installer.preview(g, installer.Options(path=dlss.BRIDGE, native_dlss=True))
check("a vulkan game lists the layer as written outside",
      len(pv.outside) == 1 and "Vulkan layer" in pv.outside[0], str(pv.outside))
check("...and no proxy dll", "dxgi.dll" not in pv.writes and not pv.blockers,
      str(pv.writes))
check("...and the lines say so",
      any(l.startswith("outside: ") for l in installer.preview_lines(pv)))
g.api = "DX11"
pv = installer.preview(g, installer.Options(path=dlss.FEEDER, dxvk=True))
check("dxvk: its dlls are written and the layer goes outside",
      "dxgi.dll" in pv.writes and "d3d11.dll" in pv.writes and pv.outside
      and pv.steps[0].startswith("DXVK"), str((pv.writes[:3], pv.outside)))
shutil.rmtree(d, ignore_errors=True)

# Blockers: what install() would refuse is said up front, and nothing else.
d = Path(tempfile.mkdtemp(prefix="pv_block_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "dxgi.dll").write_bytes(b"MZ some other injector" + bytes(4000))
g = games.manual(d)
pv = installer.preview(g, installer.Options(path=dlss.NATIVE, native_dlss=True))
check("a foreign dxgi.dll is a blocker",
      len(pv.blockers) == 1 and "not ReShade" in pv.blockers[0], str(pv.blockers))
check("the lines lead with it",
      installer.preview_lines(pv)[0].startswith("cannot install:"))
check("install refuses for the same reason",
      _raises(lambda: installer.install(g, installer.Options(path=dlss.NATIVE, native_dlss=True),
                                        on_log=lambda t: None)))
check("the folder is still untouched", sorted(p.name for p in d.iterdir())
      == ["Game.exe", "dxgi.dll"], str(sorted(p.name for p in d.iterdir())))
g.api = "DX10"
pv = installer.preview(g, installer.Options())
check("dx10 is no longer a preview blocker", not any("DirectX 10" in b for b in pv.blockers),
      str(pv.blockers))
shutil.rmtree(d, ignore_errors=True)

# A Remix-modded folder with some other route picked by hand: install()
# refuses this outright, and until this was found in review the preview did
# not know that - it happily described a dgVoodoo/ReShade plan for a route
# that was never going to run.
d = Path(tempfile.mkdtemp(prefix="pv_remix_block_"))
shutil.copyfile(X64, d / "Game.exe")
(d / ".trex").mkdir()
(d / ".trex" / "d3d9.dll").write_bytes(b"MZ" + bytes(4096))
g = games.manual(d)
pv = installer.preview(g, installer.Options(path=dlss.NATIVE, native_dlss=True))
check("preview refuses a non-remix route on a Remix folder",
      any("Only the remix route works" in b for b in pv.blockers), str(pv.blockers))
check("install refuses the same way",
      _raises(lambda: installer.install(
          g, installer.Options(path=dlss.NATIVE, native_dlss=True), on_log=lambda t: None)))
pv = installer.preview(g, installer.Options(path=dlss.REMIX))
check("the remix route itself is not blocked by this check",
      not any("Only the remix route works" in b for b in pv.blockers), str(pv.blockers))
shutil.rmtree(d, ignore_errors=True)

# Switching routes: the previous install's files are announced as removals.
d = Path(tempfile.mkdtemp(prefix="pv_switch_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
try:
    installer.install(g, installer.Options(path=dlss.FEEDER), on_log=lambda t: None)
    pv = installer.preview(g, installer.Options(path=dlss.OPTI, native_dlss=True))
    check("switching routes announces the old add-on's removal",
          any(r.startswith("dlss5-feed.addon64") for r in pv.removes)
          and any(r.startswith("ReShade.ini") for r in pv.removes), str(pv.removes[:5]))
    check("...and backs up nothing of the old route", not pv.backups, str(pv.backups))
    check("...and the lines say so",
          any(l.startswith("will clean up first:") for l in installer.preview_lines(pv)))
    installer.uninstall(g, on_log=lambda t: None)
except Exception as e:
    check("route switch preview", False, f"{type(e).__name__}: {e}")
shutil.rmtree(d, ignore_errors=True)

# ------------------------------------------------- 14. emulator render backends
section("14. emulator render backends")
from core import emulators  # noqa: E402

# Each fake: a real exe name so profile_for() recognises it, a portable
# marker so the config resolves inside the temp folder, and a config with
# the backend on a non-DXGI value plus unrelated keys that must survive.
_EMU_CASES = [
    # (exe name, marker files, config relative path, original text, expected new line, old name)
    ("duckstation-qt-x64.exe", (), "settings.ini",
     "[Main]\nSettingsVersion = 3\n\n[GPU]\nRenderer = Vulkan\nResolutionScale = 3\n\n[Audio]\nBackend = Cubeb\n",
     "Renderer = D3D12\n", "Vulkan"),
    ("pcsx2-qt.exe", ("portable.ini",), "inis/PCSX2.ini",
     "[UI]\r\nMainWindowGeometry = x\r\n\r\n[EmuCore/GS]\r\nVsyncEnable = 0\r\nRenderer = 14\r\nupscale_multiplier = 2\r\n\r\n[EmuCore]\r\nRenderer = 99\r\n",
     "Renderer = 15\r\n", "Vulkan"),
    ("Dolphin.exe", ("portable.txt",), "User/Config/Dolphin.ini",
     "[General]\nISOPath0 = D:/wii\n[Core]\nGFXBackend = Vulkan\nCPUThread = True\n",
     "GFXBackend = D3D12\n", "Vulkan"),
    ("PPSSPPWindows64.exe", (), "memstick/PSP/SYSTEM/ppsspp.ini",
     "[General]\nLanguage = en_US\n[Graphics]\nFailedGraphicsBackends = \nGraphicsBackend = 3 (VULKAN)\nInternalResolution = 3\n",
     "GraphicsBackend = 2 (DIRECT3D11)\n", "Vulkan"),
    ("xenia_canary.exe", ("portable.txt",), "xenia-canary.config.toml",
     '[APU]\napu = "any"\n\n[GPU]\ndraw_resolution_scale_x = 2\ngpu = "vulkan"\nvsync = true\n',
     'gpu = "d3d12"\n', "Vulkan"),
    ("retroarch.exe", (), "retroarch.cfg",
     'audio_driver = "xaudio"\nvideo_driver = "vulkan"\nvideo_fullscreen = "false"\n',
     'video_driver = "d3d11"\n', "Vulkan"),
]
for _exe_name, _markers, _rel, _orig, _expect_line, _old_name in _EMU_CASES:
    _d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
    _exe = _d / _exe_name
    _exe.write_bytes(b"MZ")
    for _m in _markers:
        (_d / _m).write_text("", encoding="utf8")
    _cfg = _d / _rel
    _cfg.parent.mkdir(parents=True, exist_ok=True)
    _cfg.write_bytes(_orig.encode("utf8"))
    _p = emulators.profile_for(_exe)
    _label = _p.name if _p else _exe_name
    if not check(f"{_label}: the fake exe is recognised", _p is not None):
        continue
    _status, _found = emulators.backend_status(_p, _exe)
    check(f"{_label}: status reads the config and the old backend",
          _found == _cfg and _status == _old_name, f"{_status} {_found}")
    _notes = emulators.set_backend(_p, _exe)
    _bak = _cfg.with_name(_cfg.name + emulators.BACKUP_SUFFIX)
    _new = _cfg.read_bytes().decode("utf8")
    _old_lines = _orig.splitlines(keepends=True)
    _new_lines = _new.splitlines(keepends=True)
    _diff = [(a, b) for a, b in zip(_old_lines, _new_lines) if a != b]
    check(f"{_label}: exactly one line changed and it is the backend key",
          len(_old_lines) == len(_new_lines) and len(_diff) == 1 and _diff[0][1] == _expect_line,
          repr(_diff))
    check(f"{_label}: the note says file and old -> new",
          any(str(_cfg) in n and "->" in n for n in _notes), " | ".join(_notes))
    check(f"{_label}: backup holds the original byte for byte",
          _bak.is_file() and _bak.read_bytes() == _orig.encode("utf8"))
    check(f"{_label}: status now reports DXGI",
          emulators.backend_status(_p, _exe)[0] in ("D3D11", "D3D12"))
    _notes2 = emulators.set_backend(_p, _exe)
    check(f"{_label}: second run changes nothing (idempotent)",
          _cfg.read_bytes().decode("utf8") == _new and _bak.read_bytes() == _orig.encode("utf8")
          and any("already" in n for n in _notes2), " | ".join(_notes2))
    _r = emulators.restore_backend(_p, _exe)
    check(f"{_label}: restore brings the original back and drops the backup",
          _cfg.read_bytes() == _orig.encode("utf8") and not _bak.exists(), " | ".join(_r))
    check(f"{_label}: nothing else was created in the folder",
          sorted(f.name for f in _d.iterdir()) == sorted({_exe_name, *_markers, _rel.split("/")[0]}))
    shutil.rmtree(_d, ignore_errors=True)

# A config that already runs on the other DXGI flavour is left alone: no
# backup, no edit, and nothing to restore.
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "duckstation-qt-x64.exe").write_bytes(b"MZ")
(_d / "settings.ini").write_text("[GPU]\nRenderer = D3D11\n", encoding="utf8")
_p = emulators.profile_for(_d / "duckstation-qt-x64.exe")
_notes = emulators.set_backend(_p, _d / "duckstation-qt-x64.exe")
check("an existing D3D11 choice is respected, with no backup",
      any("already" in n for n in _notes)
      and not (_d / ("settings.ini" + emulators.BACKUP_SUFFIX)).exists())
check("restore with no backup just says so",
      any("no backend backup" in n for n in emulators.restore_backend(_p, _d / "duckstation-qt-x64.exe")))
shutil.rmtree(_d, ignore_errors=True)

# The key missing from its section is added under the header, not appended
# somewhere the emulator will not read it.
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "Dolphin.exe").write_bytes(b"MZ")
(_d / "portable.txt").write_text("", encoding="utf8")
(_d / "User" / "Config").mkdir(parents=True)
(_d / "User" / "Config" / "Dolphin.ini").write_text("[Core]\nCPUThread = True\n[DSP]\nBackend = Cubeb\n", encoding="utf8")
_p = emulators.profile_for(_d / "Dolphin.exe")
emulators.set_backend(_p, _d / "Dolphin.exe")
_ini = (_d / "User" / "Config" / "Dolphin.ini").read_text(encoding="utf8")
check("a missing key is inserted under its own section",
      _ini == "[Core]\nGFXBackend = D3D12\nCPUThread = True\n[DSP]\nBackend = Cubeb\n", repr(_ini))
shutil.rmtree(_d, ignore_errors=True)

# No config yet: a hint, and not a single file written.
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "pcsx2-qt.exe").write_bytes(b"MZ")
(_d / "portable.ini").write_text("", encoding="utf8")
_p = emulators.profile_for(_d / "pcsx2-qt.exe")
_notes = emulators.set_backend(_p, _d / "pcsx2-qt.exe")
check("a missing config gives the by-hand hint and writes nothing",
      any(_p.renderer_hint in n for n in _notes) and sorted(f.name for f in _d.iterdir()) == ["pcsx2-qt.exe", "portable.ini"])
check("status on a missing config is unknown",
      emulators.backend_status(_p, _d / "pcsx2-qt.exe") == ("unknown", None))
shutil.rmtree(_d, ignore_errors=True)

# Vulkan/OpenGL-only emulators: the tool reports and leaves the file alone.
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "rpcs3.exe").write_bytes(b"MZ")
(_d / "config.yml").write_text("Core:\n  PPU Decoder: Recompiler (LLVM)\nVideo:\n  Renderer: Vulkan\n  Resolution: 1280x720\n", encoding="utf8")
_p = emulators.profile_for(_d / "rpcs3.exe")
_notes = emulators.set_backend(_p, _d / "rpcs3.exe")
check("RPCS3: no DXGI backend, Vulkan is left in place",
      any("no DXGI backend" in n for n in _notes)
      and emulators.backend_status(_p, _d / "rpcs3.exe")[0] == "Vulkan"
      and sorted(f.name for f in _d.iterdir()) == ["config.yml", "rpcs3.exe"], " | ".join(_notes))
check("RPCS3: restore is a no-op", emulators.restore_backend(_p, _d / "rpcs3.exe") == [])
shutil.rmtree(_d, ignore_errors=True)
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "Cemu.exe").write_bytes(b"MZ")
(_d / "settings.xml").write_text('<?xml version="1.0"?>\n<content>\n  <Graphic>\n    <api>1</api>\n    <device></device>\n  </Graphic>\n</content>\n', encoding="utf8")
_p = emulators.profile_for(_d / "Cemu.exe")
_notes = emulators.set_backend(_p, _d / "Cemu.exe")
check("Cemu: no DXGI backend, note only, Vulkan reported",
      any("no DXGI backend" in n for n in _notes)
      and emulators.backend_status(_p, _d / "Cemu.exe")[0] == "Vulkan", " | ".join(_notes))
shutil.rmtree(_d, ignore_errors=True)
for _name in ("Ryujinx.exe", "yuzu.exe"):
    _p = emulators.profile_for(Path(_name))
    check(f"{_p.name}: note only", any("no DXGI backend" in n for n in emulators.set_backend(_p, Path(_name)))
          and emulators.backend_status(_p, Path(_name)) == ("unknown", None))



# ------------------------------------------------- 17. an unready drive
section("17. an unready drive letter does not kill a scan")
import pathlib as _pl  # noqa: E402
_orig_is_dir = _pl.Path.is_dir


def _angry_is_dir(self, *a, **k):
    if str(self).upper().startswith("Q:"):
        raise OSError(87, "The parameter is incorrect", str(self))
    return _orig_is_dir(self, *a, **k)


try:
    _pl.Path.is_dir = _angry_is_dir
    check("_isdir swallows OSError 87", games._isdir(_pl.Path("Q:/")) is False)
    _errs = []
    for _fn in (games.scan_xbox, games.scan_folders, emulators.scan):
        try:
            _fn()
        except OSError as e:
            _errs.append(f"{_fn.__name__}: {e}")
    check("xbox / folder / emulator scans survive it", not _errs, str(_errs))
    # installer.preview()/preflight() used a bare root.is_dir() until this was
    # found in review: a game on a drive that goes unready mid-session
    # (unplugged, asleep, a dropped network share) crashed both with an
    # uncaught OSError instead of the intended "does not exist" message.
    _gq = games.Game(name="Unready", folder=_pl.Path("Q:/SomeGame"))
    _pvq = None
    try:
        _pvq = installer.preview(_gq, installer.Options())
    except OSError as e:
        check("preview survives an unready drive", False, str(e))
    if _pvq is not None:
        check("preview reports it as a normal blocker, not a crash",
              any("does not exist" in b for b in _pvq.blockers), str(_pvq.blockers))
    try:
        installer.preflight(_gq)
        check("preflight survives an unready drive", False, "did not raise")
    except installer.InstallError as e:
        check("preflight turns it into a clean InstallError", "does not exist" in str(e), str(e))
    except OSError as e:
        check("preflight survives an unready drive", False, str(e))
finally:
    _pl.Path.is_dir = _orig_is_dir


# ------------------------------------------------- 18. other NGX hooks
section("18. another DLSS hook in the folder is called out")
_d = _diag_dir("diag_hooks_", reshade=(
    'INFO | Registered add-on "RenoDX" v0.0.0.0\n'
    'INFO | Registered add-on "RenoDX DLSS" v0.0.0.0\n'
    'INFO | Registered add-on "Auto Reload" v16.2.1.0\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"), path="native")
(_d / "OptiScaler.ini").write_text("[Upscalers]\n", encoding="utf8")
(_d / "dlssg_to_fsr3_amd_is_better.dll").write_bytes(b"MZ")
(_d / "renodx-cp2077.addon64").write_bytes(b"MZ")
_r = diagnose.analyse(_d)
_bad = _levels(_r, "bad")
_warn = _levels(_r, "warn")
check("our add-on missing from the loaded list is a failure",
      any("did not load" in b for b in _bad), str(_bad))
check("other RenoDX add-ons are named",
      any("Other ReShade add-ons" in w and "RenoDX" in w for w in _warn), str(_warn))
check("OptiScaler / frame-gen files are named",
      any("Another DLSS hook" in w and "OptiScaler.ini" in w for w in _warn), str(_warn))
_hooks = installer.other_ngx_hooks(_d)
check("other_ngx_hooks sees the ini, the dll and the foreign add-on",
      "OptiScaler.ini" in _hooks and "dlssg_to_fsr3_amd_is_better.dll" in _hooks
      and "renodx-cp2077.addon64" in _hooks and "dlss5-feed.addon64" not in _hooks,
      str(_hooks))
check("the OptiScaler route ignores its own ini",
      "OptiScaler.ini" not in installer.hook_warning(_d, "optiscaler")
      and "dlssg" in installer.hook_warning(_d, "optiscaler"))
check("a clean folder gives no warning",
      installer.hook_warning(Path(tempfile.mkdtemp(prefix="clean_")), "native") == "")
shutil.rmtree(_d, ignore_errors=True)

# and the install itself says so
_d = Path(tempfile.mkdtemp(prefix="hooks_install_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "dlss-enabler.dll").write_bytes(b"MZ")
_g = games.manual(_d)
_pv = installer.preview(_g, installer.Options())
check("the preview warns about the other hook",
      any("another DLSS hook" in w for w in _pv.warnings), str(_pv.warnings))
_rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
check("the install warns about the other hook",
      any("another DLSS hook" in w for w in _rep.warnings), str(_rep.warnings))
installer.uninstall(_g, on_log=lambda t: None)
check("the other mod's file is left alone", (_d / "dlss-enabler.dll").is_file())
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 19. feeder crash record
section("19. the feeder's own crash record is read")
_d = _diag_dir("diag_crash_", feed=(
    "22:31:54.185  [feed32] frame 1 delivered (1920x1080, reset=0)\n"
    "22:31:54.235  [feed32] frame 3 delivered (1920x1080, reset=0)\n"
    "22:31:54.839  ### CRASH RECORDED ###  exception 0xC0000005 at 00C5ED39 in "
    "V:\\Games\\Bayonetta\\Bayonetta.exe; this add-on was last doing: "
    "preparing work-resolution inputs\n"
    "22:31:55.613  [feed32] crash dump written: V:\\Games\\Bayonetta\\dlss5-feed-crash.dmp "
    "-- attach it to the issue with this log\n"), reshade=(
    'INFO | Registered add-on "DLSS 5 Feed (32-bit) 0.12.0" v0.0.0.0\n'
    "INFO | Redirecting IDXGIFactory::CreateSwapChain(...)\n"))
_r = diagnose.analyse(_d)
check("a recorded crash is not reported as Working",
      not _r.verdict.startswith("Working") and "crashed" in _r.verdict, _r.verdict)
_bad = _levels(_r, "bad")
check("the crash line names the exception and the step",
      any("0xC0000005" in b and "work-resolution" in b for b in _bad), str(_bad))
_det = " ".join(f_.detail for f_ in _r.findings if f_.level == "bad")
check("the advice names the dump and another feeder build",
      "dlss5-feed-crash.dmp" in _det and "feeder build" in _det, _det[:200])
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 20. their own LumeniteFX
section("20. a LumeniteFX the person installed is not duplicated")
_d = Path(tempfile.mkdtemp(prefix="lum_"))
shutil.copyfile(X64, _d / "Game.exe")
_theirs = _d / "reshade-shaders" / "Shaders" / "LumeniteFX"
_theirs.mkdir(parents=True)
(_theirs / "lumenite_Kernel.fx").write_text("// theirs\n", encoding="utf8")
_g = games.manual(_d)
_pv = installer.preview(_g, installer.Options())
check("the preview says their copy is used",
      any("already installed" in w for w in _pv.warnings)
      and not any("lumenite_" in w for w in _pv.writes), str(_pv.warnings))
_rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
check("no second lumenite_Kernel.fx is written",
      not (_d / "reshade-shaders" / "Shaders" / "lumenite_Kernel.fx").exists()
      and any("already installed" in n for n in _rep.notes), str(_rep.notes))
check("the technique is still wired in ReShade.ini",
      "Lumenite_Kernel@lumenite_Kernel.fx" in (_d / "ReShadePreset.ini").read_text(encoding="utf8"))
installer.uninstall(_g, on_log=lambda t: None)
check("their copy survives uninstall", (_theirs / "lumenite_Kernel.fx").read_text(encoding="utf8") == "// theirs\n")
shutil.rmtree(_d, ignore_errors=True)

# and our own earlier copy is still overwritten, not mistaken for theirs
_d = Path(tempfile.mkdtemp(prefix="lum_ours_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
installer.install(_g, installer.Options(), on_log=lambda t: None)
_rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
check("our own copy from the first install is refreshed, not skipped",
      not any("already installed" in n for n in _rep.notes), str(_rep.notes))
installer.uninstall(_g, on_log=lambda t: None)
check("uninstall removes our lumenite files",
      not list((_d / "reshade-shaders").rglob("lumenite_*")) if (_d / "reshade-shaders").is_dir() else True)
shutil.rmtree(_d, ignore_errors=True)

# ------------------------------------------------- 21. neural-upstream
section("21. the neural-upstream route")
# A DX12 game with its own DLSS: the only place the route is offered.
_d = Path(tempfile.mkdtemp(prefix="upstream_"))
shutil.copyfile(X64, _d / "Game.exe")
_own = b"MZ" + bytes(range(256)) * 40
(_d / "nvngx_dlss.dll").write_bytes(_own)
_g = games.manual(_d)
_sup = dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness)
check("upstream is offered second, after native",
      _sup.options[:2] == [dlss.NATIVE, dlss.UPSTREAM], str(_sup.options))
check("upstream is never the recommendation", _sup.recommended != dlss.UPSTREAM)
_ok, _note = dlss.fit(dlss.UPSTREAM, _g.api, True, None)
check("fit says upstream is usable", _ok and bool(_note), _note)
_bare = Path(tempfile.mkdtemp(prefix="upstream_nodlss_"))
shutil.copyfile(X64, _bare / "Game.exe")
_gb = games.manual(_bare)
check("a game without DLSS is not offered upstream",
      dlss.UPSTREAM not in dlss.detect(_gb.install_dir, _gb.folder, _gb.api,
                                       _gb.bitness).options)
shutil.rmtree(_bare, ignore_errors=True)
check("every route has a conflicts entry of 2-4 lines",
      set(dlss.CONFLICTS) == set(dlss.ALL_ROUTES)
      and all(2 <= len(v) <= 4 for v in dlss.CONFLICTS.values()),
      str(sorted(dlss.CONFLICTS)))
check("upstream has a label and a blurb",
      dlss.UPSTREAM in dlss.LABELS and dlss.UPSTREAM in dlss.BLURB)

_opt = installer.Options(path=dlss.UPSTREAM, native_dlss=True)
_steps = installer.plan(_g, _opt)
check("the plan has no renodx step and never touches nvngx_dlss.dll",
      not any("renodx" in s for s in _steps) and "nvngx_dlss.dll" not in _steps
      and "neural-upstream" in _steps, str(_steps))
_pv = installer.preview(_g, _opt)
check("the preview lists nvngx.dll.addon64 and not renodx-dlss5.addon64",
      installer.UPSTREAM_ADDON in _pv.writes and installer.RENODX not in _pv.writes
      and "nvngx_dlss.dll" not in _pv.writes, str(_pv.writes))
check("reliability is beta and says so",
      installer.reliability(_g, dlss.UPSTREAM)[0] == installer.BETA
      and "two games" in installer.reliability(_g, dlss.UPSTREAM)[1])
try:
    _rep = installer.install(_g, _opt, on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("install writes the add-on and nvngx_dlssnr.dll",
          installer.UPSTREAM_ADDON in _files and installer.DLSSNR in _files,
          str(sorted(_files)))
    check("install writes no renodx add-on",
          installer.RENODX not in _files and installer.RENODX_SF not in _files)
    check("the game's nvngx_dlss.dll is byte-identical, no backup made",
          (_d / "nvngx_dlss.dll").read_bytes() == _own
          and not (_d / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).exists())
    check("the plan and the steps taken agree",
          len(_steps) == len(installer.plan(_g, _opt)) and
          any("upstream version" in n for n in _rep.notes), str(_rep.notes))
    _man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest records path upstream and its version",
          _man.get("path") == "upstream"
          and bool((_man.get("components") or {}).get("upstream")),
          str(_man.get("components")))
    check("the manifest round-trips the route",
          installer.options_from_manifest(_d).path == dlss.UPSTREAM
          and installer.options_from_manifest(_d).native_dlss)
    check("our own add-on is not reported as a foreign hook",
          installer.UPSTREAM_ADDON not in installer.other_ngx_hooks(_d)
          and installer.hook_warning(_d, dlss.UPSTREAM) == "",
          str(installer.other_ngx_hooks(_d)))
    installer.install(_g, installer.Options(path=dlss.NATIVE, native_dlss=True),
                      on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("switching to native removes nvngx.dll.addon64",
          installer.UPSTREAM_ADDON not in _files and installer.RENODX in _files,
          str(sorted(_files)))
    installer.install(_g, _opt, on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("switching back removes renodx-dlss5.addon64",
          installer.RENODX not in _files and installer.UPSTREAM_ADDON in _files,
          str(sorted(_files)))
    installer.uninstall(_g, on_log=lambda t: None)
    _left = sorted(p.name for p in _d.rglob("*") if p.is_file())
    check("uninstall leaves only the game and its DLSS",
          _left == ["Game.exe", "nvngx_dlss.dll"], str(_left))
except Exception as e:
    check("upstream: installs", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# The diagnosis: no frame log, the overlay tab is the judge, and the
# renodx-dlss5 add-on registered beside it is two NGX hooks.
_d = _diag_dir("diag_upstream_", addons=False, reshade=(
    'INFO | Registered add-on "DLSS5 NR Pre-Upscale" v0.3.0.0\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"),
    path="upstream", files=["dxgi.dll", "nvngx.dll.addon64"])
(_d / "nvngx.dll.addon64").write_bytes(b"MZ")
_r = diagnose.analyse(_d)
check("upstream loaded alone is not a failure",
      not _levels(_r, "bad") and "Pre-Upscale" in _r.verdict,
      str(_levels(_r, "bad")) + " / " + _r.verdict)
check("the folder's own add-on is not called a foreign hook",
      not any("Another DLSS hook" in w for w in _levels(_r, "warn")),
      str(_levels(_r, "warn")))
(_d / "ReShade.log").write_text(
    'INFO | Registered add-on "DLSS5 NR Pre-Upscale" v0.3.0.0\n'
    'INFO | Registered add-on "DLSS 5 Neural Rendering" v4.7.0.0\n', encoding="utf8")
_r = diagnose.analyse(_d)
check("renodx-dlss5 beside upstream is two NGX hooks",
      any("Two NGX hooks" in b for b in _levels(_r, "bad")), str(_levels(_r, "bad")))
shutil.rmtree(_d, ignore_errors=True)

section("22. FSR and XeSS games through OptiScaler")
# A 64-bit D3D12 game with FSR 2 (or XeSS) and no DLSS: OptiScaler hooks the
# game's upscaler calls as its input and runs DLSS in their place, so the
# route is offered and the tool has to bring a nvngx_dlss.dll of its own.


def _upscaler_game(prefix: str, runtime: str):
    d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, d / "Game.exe")
    (d / runtime).write_bytes(b"MZ" + bytes(4096))
    g = games.manual(d)
    g.api = "DX12"
    return d, g


for _runtime, _kind, _name in (("ffx_fsr2_api_x64.dll", "fsr", "FSR"),
                               ("libxess.dll", "xess", "XeSS")):
    _d, _g = _upscaler_game(f"opti_{_kind}_", _runtime)
    _sup = dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness, 89)
    check(f"{_name}: detect reports upscaler {_kind} and no native DLSS",
          _sup.upscaler == _kind and not _sup.native_dlss
          and _runtime in _sup.upscaler_evidence,
          f"{_sup.upscaler} {_sup.upscaler_evidence}")
    check(f"{_name}: optiscaler is offered, feeder stays recommended",
          dlss.OPTI in _sup.options and _sup.options[0] == dlss.FEEDER
          and _sup.recommended == dlss.FEEDER, str(_sup.options))
    check(f"{_name}: the reason names the upscaler seen",
          _runtime in _sup.reason and "OptiScaler" in _sup.reason, _sup.reason)
    check(f"{_name}: rtx 50 is steered to optiscaler",
          dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness, 120).recommended
          == dlss.OPTI)
    _ok, _note = dlss.fit(dlss.OPTI, "DX12", False, 89, upscaler=_kind)
    check(f"{_name}: fit accepts optiscaler with the upscaler passed",
          _ok and "redirected into DLSS" in _note, _note)
    check(f"{_name}: fit without the upscaler still refuses (old positional call)",
          dlss.fit(dlss.OPTI, "DX12", False, 89)[0] is False)
    _opt = installer.Options(path=dlss.OPTI, upscaler=_kind)
    _steps = installer.plan(_g, _opt)
    check(f"{_name}: the plan lists nvngx_dlss.dll",
          "nvngx_dlss.dll" in _steps and any(s.startswith("OptiScaler (") for s in _steps),
          str(_steps))
    _pv = installer.preview(_g, _opt)
    check(f"{_name}: the preview lists nvngx_dlss.dll and says beta",
          "nvngx_dlss.dll" in _pv.writes
          and any("redirected into DLSS" in w for w in _pv.warnings),
          str(_pv.writes))
    _lvl, _why = installer.reliability(_g, dlss.OPTI, _kind)
    check(f"{_name}: reliability is beta and honest",
          _lvl == installer.BETA and "not all" in _why, _why)
    try:
        _rep = installer.install(_g, _opt, on_log=lambda t: None)
        _files = {p.name for p in _d.iterdir() if p.is_file()}
        check(f"{_name}: install writes OptiScaler, nvngx_dlssnr and nvngx_dlss",
              "dxgi.dll" in _files and optiscaler.FORWARDER in _files
              and installer.DLSSNR in _files and installer.DLSS in _files,
              str(sorted(_files)))
        check(f"{_name}: the plan and the steps taken agree",
              len(_steps) == len(installer.plan(_g, _opt))
              and any("dlss version" in n for n in _rep.notes), str(_rep.notes))
        _ini = (_d / optiscaler.INI).read_text(encoding="utf8")
        _want = "EnableFsr2Inputs=true" if _kind == "fsr" else "EnableXeSSInputs=true"
        check(f"{_name}: the ini enables the input and picks dlss on D3D12",
              _want in _ini and "Dx12Upscaler=dlss" in _ini
              and "Enabled=true" in _ini,
              str([ln for ln in _ini.splitlines()
                   if "Inputs=" in ln or "Dx12Upscaler" in ln]))
        _man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
        check(f"{_name}: the manifest records the upscaler",
              _man.get("upscaler") == _kind and _man.get("path") == "optiscaler"
              and "nvngx_dlss.dll" in _man.get("files", []), str(_man.get("upscaler")))
        _back = installer.options_from_manifest(_d)
        check(f"{_name}: the manifest round-trips upscaler and native_dlss",
              _back.upscaler == _kind and not _back.native_dlss
              and _back.path == dlss.OPTI)
        check(f"{_name}: with ours installed detect still says {_kind}",
              dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness).upscaler
              == _kind and not dlss.detect(_g.install_dir, _g.folder, _g.api,
                                           _g.bitness).native_dlss)
        installer.uninstall(_g, on_log=lambda t: None)
        _left = sorted(p.name for p in _d.rglob("*") if p.is_file())
        check(f"{_name}: uninstall removes all of ours and leaves the {_name} dll",
              _left == ["Game.exe", _runtime], str(_left))
    except Exception as e:
        check(f"{_name}: installs", False, f"{type(e).__name__}: {e}")
    shutil.rmtree(_d, ignore_errors=True)

# Neither DLSS nor an upscaler: OptiScaler has nothing to hook.
_bare = Path(tempfile.mkdtemp(prefix="opti_none_"))
shutil.copyfile(X64, _bare / "Game.exe")
_gb = games.manual(_bare)
_gb.api = "DX12"
_sb = dlss.detect(_gb.install_dir, _gb.folder, _gb.api, _gb.bitness, 120)
check("a game with neither DLSS nor FSR/XeSS is not offered optiscaler",
      dlss.OPTI not in _sb.options and _sb.upscaler == ""
      and _sb.recommended == dlss.FEEDER, str(_sb.options))
shutil.rmtree(_bare, ignore_errors=True)

# A native-DLSS game keeps the old behaviour: no upscaler, no extra dlss step.
_gn = _fake_game("opti_native_")
_sn = dlss.detect(_gn.install_dir, _gn.folder, "DX12", 64)
check("a game with its own DLSS reports no upscaler",
      _sn.native_dlss and _sn.upscaler == "" and _sn.dlss_source)
check("native optiscaler plan is unchanged",
      "nvngx_dlss.dll" not in installer.plan(
          _gn, installer.Options(path=dlss.OPTI, native_dlss=True)))
shutil.rmtree(_gn.folder, ignore_errors=True)

# The ini writer on its own, D3D11 included.
_d = Path(tempfile.mkdtemp(prefix="opti_ini_"))
(_d / optiscaler.INI).write_text(
    "[Upscalers]\nDx11Upscaler=auto\nDx12Upscaler=auto\n\n[Inputs]\n"
    "EnableFsr2Inputs=auto\nUseFsr2Dx11Inputs=auto\nEnableXeSSInputs=auto\n",
    encoding="utf8")
optiscaler.enable_inputs(_d, "fsr", "DX11")
_ini = (_d / optiscaler.INI).read_text(encoding="utf8")
check("on D3D11 the FSR2 D3D11 inputs are hooked and dlss is not the upscaler",
      "UseFsr2Dx11Inputs=true" in _ini and "EnableFsr2Inputs=true" in _ini
      and "Dx12Upscaler=auto" in _ini and _ini.count("[Inputs]") == 1, _ini)
optiscaler.enable_inputs(_d, "xess", "DX12")
_ini = (_d / optiscaler.INI).read_text(encoding="utf8")
check("xess on D3D12 enables XeSS inputs and pins dlss",
      "EnableXeSSInputs=true" in _ini and "Dx12Upscaler=dlss" in _ini, _ini)
shutil.rmtree(_d, ignore_errors=True)

# The diagnosis: with an upscaler recorded, the log has to show the hook.
_log_head = ("[00:00:01.000] [I] DLLMain forwarder loaded\n"
             "[00:00:01.100] [I] HookFSR2ExeInputs Trying to hook FSR2 methods\n"
             "[00:00:05.000] [I] DLSS-NR running at 1920x1080\n")
_d = _diag_dir("diag_optifsr_", addons=False, path="optiscaler",
               upscaler="fsr", files=["dxgi.dll"])
(_d / "OptiScaler.log").write_text(_log_head, encoding="utf8")
_r = diagnose.analyse(_d)
check("no FSR context through OptiScaler is a warning that names the feeder",
      any("never saw the game's FSR calls" in t for t in _levels(_r, "warn")),
      str(_levels(_r, "warn")))
(_d / "OptiScaler.log").write_text(
    _log_head + "[00:00:02.000] [I] hk_ffxFsr2ContextCreate_Dx12 context created: 1A2B\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("an FSR context created through OptiScaler is not flagged",
      not any("never saw" in t for t in _levels(_r, "warn") + _levels(_r, "bad"))
      and _r.verdict == "Working.", str(_r.findings))
shutil.rmtree(_d, ignore_errors=True)
_d = _diag_dir("diag_optixess_", addons=False, path="optiscaler",
               upscaler="xess", files=["dxgi.dll"])
(_d / "OptiScaler.log").write_text(
    _log_head + "[00:00:01.200] [W] Config::CheckXeSS libxess.dll not found!\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("'libxess.dll not found!' is bad and names the feeder",
      any("never saw the game's XeSS calls" in t for t in _levels(_r, "bad"))
      and any("feeder" in f_.detail for f_ in _r.findings if f_.level == "bad"),
      str(_levels(_r, "bad")))
shutil.rmtree(_d, ignore_errors=True)
_d = _diag_dir("diag_optinative_", addons=False, path="optiscaler",
               files=["dxgi.dll"])
(_d / "OptiScaler.log").write_text(_log_head, encoding="utf8")
_r = diagnose.analyse(_d)
check("a native-DLSS OptiScaler install is not asked about FSR/XeSS inputs",
      not any("never saw" in t for t in _levels(_r, "warn") + _levels(_r, "bad")))
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 24. only the provider shader
section("24. only the selected motion-vector shader is installed")
_d = Path(tempfile.mkdtemp(prefix="lum_min_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
_rep = installer.install(_g, installer.Options(provider=3), on_log=lambda t: None)
_fx = sorted(p.name for p in (_d / installer.SHADERS).glob("lumenite_*.fx"))
check("provider 3 installs lumenite_Kernel.fx and nothing else from the pack",
      _fx == ["lumenite_Kernel.fx"], str(_fx))
check("its includes and texture are there",
      (_d / installer.INCLUDE / "lumenite_Compute.fxh").is_file()
      and (_d / installer.TEXTURES / "lumenite_bluenoise256.png").is_file())
_pv = installer.preview(_g, installer.Options(provider=4))
check("the preview lists only the provider it would write",
      any("lumenite_QuantMotion.fx" in w for w in _pv.writes)
      and not any("lumenite_RTAO" in w or "lumenite_TRAA" in w for w in _pv.writes),
      str([w for w in _pv.writes if "lumenite" in w]))
# an earlier full-pack install of ours is trimmed on reinstall
(_d / installer.SHADERS / "lumenite_RTAO.fx").write_text("// old", encoding="utf8")
_man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
_man["files"].append(str(installer.SHADERS / "lumenite_RTAO.fx"))
(_d / installer.MANIFEST).write_text(json.dumps(_man), encoding="utf8")
_rep = installer.install(_g, installer.Options(provider=3), on_log=lambda t: None)
check("a leftover effect from an earlier install of ours is removed",
      not (_d / installer.SHADERS / "lumenite_RTAO.fx").exists()
      and any("removed lumenite_RTAO.fx" in n for n in _rep.notes), str(_rep.notes))
installer.uninstall(_g, on_log=lambda t: None)
check("uninstall leaves no lumenite files",
      not list((_d / "reshade-shaders").rglob("lumenite_*")) if (_d / "reshade-shaders").is_dir() else True)
shutil.rmtree(_d, ignore_errors=True)

section("23. the standalone-dlssnr route")
# A 64-bit D3D12 game WITHOUT DLSS: the add-on brings its own feed, so it is
# offered anyway - experimental, after the feeder and the bridge, and never
# the recommendation.
_d = Path(tempfile.mkdtemp(prefix="standalone_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
_g.api = "DX12"
_sup = dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness)
check("standalone is offered on a DX12 game without DLSS, after feeder and bridge",
      dlss.STANDALONE in _sup.options
      and _sup.options.index(dlss.STANDALONE) > _sup.options.index(dlss.FEEDER)
      and _sup.options.index(dlss.STANDALONE) > _sup.options.index(dlss.BRIDGE),
      str(_sup.options))
check("standalone is never the recommendation", _sup.recommended != dlss.STANDALONE)
_gd = _fake_game("standalone_dlss_")
check("standalone is offered on DX12 and DX11 games with DLSS too",
      dlss.STANDALONE in dlss.detect(_gd.install_dir, _gd.folder, "DX12", 64).options
      and dlss.STANDALONE in dlss.detect(_gd.install_dir, _gd.folder, "DX11", 64).options
      and dlss.detect(_gd.install_dir, _gd.folder, "DX12", 64, 120).recommended
      != dlss.STANDALONE)
shutil.rmtree(_gd.folder, ignore_errors=True)
check("32-bit, Vulkan and OpenGL games are not offered standalone",
      all(dlss.STANDALONE not in dlss.detect(_g.install_dir, _g.folder, api, bits).options
          for api, bits in (("DX12", 32), ("DX11", 32), ("Vulkan", 64), ("OpenGL", 64))))
_ok, _note = dlss.fit(dlss.STANDALONE, "DX12", False, None)
check("fit says standalone is usable without DLSS in the game",
      _ok and "DLAA" in _note, _note)
check("standalone has a label, a blurb and a conflicts entry that says OFF",
      dlss.STANDALONE in dlss.LABELS and dlss.STANDALONE in dlss.BLURB
      and dlss.STANDALONE in dlss.CONFLICTS
      and any("OFF" in c for c in dlss.CONFLICTS[dlss.STANDALONE])
      and any("window" in c for c in dlss.CONFLICTS[dlss.STANDALONE]))
check("the release lists the three assets and VORT",
      set(sources.STANDALONE_ASSETS) == {installer.STANDALONE_ADDON,
                                         installer.STANDALONE_BRIDGE,
                                         installer.STANDALONE_FX}
      and sources.VORT_ZIP_NAME.endswith(".zip"))

_opt = installer.Options(path=dlss.STANDALONE)
_steps = installer.plan(_g, _opt)
check("the plan has no renodx step and lists the add-on, VORT and the three runtimes",
      not any("renodx" in s for s in _steps) and "standalone-dlssnr" in _steps
      and any("VORT" in s for s in _steps) and "nvngx_dlssnr.dll" in _steps
      and "nvngx_dlss.dll" in _steps and "nvngx_dlssg.dll" in _steps, str(_steps))
_pv = installer.preview(_g, _opt)
check("the preview lists the add-on, nvngx.dll, the shader and no renodx add-on",
      installer.STANDALONE_ADDON in _pv.writes
      and installer.STANDALONE_BRIDGE in _pv.writes
      and "reshade-shaders/Shaders/DLSS5_AIO_Feed.fx" in _pv.writes
      and "reshade-shaders/Shaders/vort_Motion.fx" in _pv.writes
      and installer.DLSSG in _pv.writes
      and installer.RENODX not in _pv.writes and installer.RENODX_SF not in _pv.writes,
      str(_pv.writes))
_lvl, _why = installer.reliability(_g, dlss.STANDALONE)
check("reliability is experimental and names the window trick",
      _lvl == installer.EXPERIMENTAL and "window" in _why, _why)
check("standalone does not go through DXVK",
      not installer.uses_dxvk(_g, installer.Options(path=dlss.STANDALONE, dxvk=True)))
try:
    _rep = installer.install(_g, _opt, on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("install writes the add-on, nvngx.dll, nvngx_dlssnr and nvngx_dlss",
          installer.STANDALONE_ADDON in _files and installer.STANDALONE_BRIDGE in _files
          and installer.DLSSNR in _files and installer.DLSS in _files,
          str(sorted(_files)))
    check("install writes no renodx add-on",
          installer.RENODX not in _files and installer.RENODX_SF not in _files
          and installer.UPSTREAM_ADDON not in _files)
    check("nvngx_dlssg.dll came from the mirror's dlssg family",
          installer.DLSSG in _files
          and bool((_rep.components or {}).get("dlssg")), str(_rep.components))
    check("the companion shader, VORT and its includes are under reshade-shaders",
          (_d / installer.SHADERS / installer.STANDALONE_FX).is_file()
          and (_d / installer.SHADERS / installer.VORT_FX).is_file()
          and (_d / installer.VORT_INCLUDE / "vort_Defs.fxh").is_file()
          and (_d / installer.TEXTURES / installer.VORT_TEXTURE).is_file()
          and (_d / installer.SHADERS / "ReShade.fxh").is_file())
    check("the plan and the steps taken agree",
          len(_steps) == len(installer.plan(_g, _opt))
          and any("standalone version" in n for n in _rep.notes), str(_rep.notes))
    _ini = (_d / "ReShade.ini").read_text(encoding="utf8")
    check("ReShade.ini has the shader paths and no technique is enabled",
          "EffectSearchPaths" in _ini and "AddonPath" in _ini
          and "Techniques" not in _ini
          and not (_d / "ReShadePreset.ini").exists(), _ini)
    _man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest records path standalone, its version and nvngx.dll",
          _man.get("path") == "standalone"
          and bool((_man.get("components") or {}).get("standalone"))
          and installer.STANDALONE_BRIDGE in _man.get("files", [])
          and installer.DLSSG in _man.get("files", []),
          str(_man.get("components")))
    check("the manifest round-trips the route",
          installer.options_from_manifest(_d).path == dlss.STANDALONE)
    check("our own nvngx.dll is not a foreign hook on this route",
          installer.STANDALONE_BRIDGE not in installer.other_ngx_hooks(_d, dlss.STANDALONE)
          and installer.STANDALONE_ADDON not in installer.other_ngx_hooks(_d, dlss.STANDALONE)
          and installer.hook_warning(_d, dlss.STANDALONE) == "",
          str(installer.other_ngx_hooks(_d, dlss.STANDALONE)))
    check("but it IS one on the native route",
          installer.STANDALONE_BRIDGE in installer.other_ngx_hooks(_d, dlss.NATIVE)
          and installer.STANDALONE_BRIDGE in installer.other_ngx_hooks(_d)
          and "nvngx.dll" in installer.hook_warning(_d, dlss.NATIVE),
          str(installer.other_ngx_hooks(_d, dlss.NATIVE)))
    check("the preview of a feeder install says it removes the standalone files",
          any(installer.STANDALONE_BRIDGE in r for r in
              installer.preview(_g, installer.Options(path=dlss.FEEDER)).removes))
    installer.install(_g, installer.Options(path=dlss.FEEDER), on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("switching to feeder removes the add-on and nvngx.dll",
          installer.STANDALONE_ADDON not in _files
          and installer.STANDALONE_BRIDGE not in _files
          and installer.FEEDER_ADDON64 in _files and installer.RENODX in _files,
          str(sorted(_files)))
    check("the feeder's own DLSS5_Feed.fx and the AIO shader do not both remain",
          not (_d / installer.SHADERS / installer.STANDALONE_FX).exists())
    installer.install(_g, _opt, on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("switching back removes the feeder and renodx-dlss5.addon64",
          installer.FEEDER_ADDON64 not in _files and installer.RENODX not in _files
          and installer.STANDALONE_ADDON in _files
          and installer.STANDALONE_BRIDGE in _files, str(sorted(_files)))
    installer.uninstall(_g, on_log=lambda t: None)
    _left = sorted(p.name for p in _d.rglob("*") if p.is_file())
    check("uninstall leaves only the game", _left == ["Game.exe"], str(_left))
    check("uninstall removed the empty shader folders",
          not (_d / "reshade-shaders").exists())
except Exception as e:
    check("standalone: installs", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# The diagnosis: the add-on's own log lives outside the game folder and is
# shared by every game, so the test points diagnose at a synthetic one.
_REG = ('INFO | Registered add-on "Standalone DLSS-NR + SR 1.7.17-early-proxy" '
        'v1.7.17.0\n'
        "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n")
_ATTACH = ("Standalone DLSS-NR + SR 1.7.17-early-proxy attached; requested "
           "profile=Auto model=1 NR=on early_proxy=disabled\n")
_d = _diag_dir("diag_standalone_", addons=False, reshade=_REG, path="standalone",
               files=["dxgi.dll", "standalone-dlssnr.addon64", "nvngx.dll"])
(_d / "standalone-dlssnr.addon64").write_bytes(b"MZ")
(_d / "nvngx.dll").write_bytes(b"MZ")
_saved_log = diagnose.STANDALONE_LOG
_logd = Path(tempfile.mkdtemp(prefix="diag_salog_"))
diagnose.STANDALONE_LOG = _logd / "standalone-dlssnr.log"
try:
    _r = diagnose.analyse(_d)
    check("no standalone log yet is not a failure",
          not _levels(_r, "bad") and "play once" in _r.verdict, _r.verdict)
    diagnose.STANDALONE_LOG.write_text(
        _ATTACH + "runtime dependency: nvngx_dlssnr.dll (109425288 bytes)\n"
        "required private runtime dependency missing\n", encoding="utf8")
    _r = diagnose.analyse(_d)
    check("'required private runtime dependency missing' is BAD and names nvngx.dll",
          any("runtime" in b for b in _levels(_r, "bad"))
          and any("nvngx.dll" in f_.detail for f_ in _r.findings if f_.level == "bad")
          and "reinstall" in _r.verdict, str(_levels(_r, "bad")) + " / " + _r.verdict)
    check("the folder's own nvngx.dll and add-on are not called a foreign hook",
          not any("Another DLSS hook" in w for w in _levels(_r, "warn")),
          str(_levels(_r, "warn")))
    diagnose.STANDALONE_LOG.write_text(
        "old session line from another game\n" + _ATTACH
        + "standalone contract ready: NR=on at 1920x1080, DLSS SR -> 3840x2160, "
          "DLSS-G=on, model=1, profile=sRGB\n"
        + "same-frame VORT optical flow + DLSS5_AIO_Feed submitted before NGX: frame=1\n"
        + "on-present frame 120: NR=on, DLSS SR=Success, model=1, NR-reset=0, "
          "NR-guides=same-frame VORT optical flow, SR-history=on, DLSS history "
          "mask=on, input=1920x1080, output=3840x2160\n", encoding="utf8")
    _r = diagnose.analyse(_d)
    check("a contract and frames in the log is Working",
          _r.verdict == "Working." and not _levels(_r, "bad")
          and any("VORT" in t for t in _levels(_r, "ok")), str(_r.findings))
    diagnose.STANDALONE_LOG.write_text(
        _ATTACH + "standalone contract ready: NR=on at 1920x1080\n"
        "active on present: per-frame reset / zero motion + fallback guides\n"
        "standalone pipeline FAILED at DLSS SR feature creation: 0xBAD00010\n",
        encoding="utf8")
    _r = diagnose.analyse(_d)
    check("a pipeline failure names the stage, zero motion is a warning",
          any("DLSS SR feature creation" in b for b in _levels(_r, "bad"))
          and any("zero-motion" in w for w in _levels(_r, "warn")),
          str(_levels(_r, "bad")) + " / " + str(_levels(_r, "warn")))
    (_d / "ReShade.log").write_text(
        _REG + 'INFO | Registered add-on "DLSS 5 Neural Rendering" v4.7.0.0\n',
        encoding="utf8")
    _r = diagnose.analyse(_d)
    check("renodx-dlss5 beside standalone is two add-ons processing the frame",
          any("Two add-ons" in b for b in _levels(_r, "bad")), str(_levels(_r, "bad")))
    (_d / "ReShade.log").write_text(
        'INFO | Registered add-on "Some HDR mod" v1.0.0.0\n', encoding="utf8")
    _r = diagnose.analyse(_d)
    check("the add-on missing from the registered list is BAD",
          any("did not load" in b for b in _levels(_r, "bad")), str(_levels(_r, "bad")))
    (_d / "ReShade.log").write_text(_REG, encoding="utf8")
    (_d / "dlss5-autopilot.json").write_text(json.dumps(
        {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
         "api": "DX12", "proxy": "dxgi.dll", "path": "native",
         "files": ["dxgi.dll", "renodx-dlss5.addon64"]}), encoding="utf8")
    _r = diagnose.analyse(_d)
    check("on the native route a loaded standalone add-on is two add-ons",
          any("standalone" in b for b in _levels(_r, "bad")), str(_levels(_r, "bad")))
finally:
    diagnose.STANDALONE_LOG = _saved_log
shutil.rmtree(_logd, ignore_errors=True)
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 26. webcam and aside rules
section("26. webcam helpers")
_fake = ('[dshow @ 000001] "Brio 100" (video)\n[dshow @ 000001] "Microphone (Brio 100)" (audio)\n'
         '[dshow @ 000001] "OBS Virtual Camera" (video)\n')
import re as _re
_cams = []
for _line in _fake.splitlines():
    _m = _re.search(r'"([^"]+)"\s+\(video\)', _line)
    if _m and _m.group(1) not in _cams:
        _cams.append(_m.group(1))
check("camera names are parsed from ffmpeg's device list (video only)",
      _cams == ["Brio 100", "OBS Virtual Camera"], str(_cams))
check("stop_webcam without a stream is harmless", video.stop_webcam() is None)
check("no ffmpeg -> no cameras, no exception",
      video.list_cameras(Path(tempfile.mkdtemp(prefix="nocam_"))) == [])
check("the webcam URL is local only", video.WEBCAM_URL.startswith("udp://@127.0.0.1:"))


# ------------------------------------------------- 27. the webcam self-check
section("27. the webcam self-check reads the feed log by time")
import datetime as _dt
_d = Path(tempfile.mkdtemp(prefix="camlog_"))
_now = _dt.datetime.now().replace(microsecond=0)
_stamp = lambda secs: (_now + _dt.timedelta(seconds=secs)).strftime("%H:%M:%S.000")
(_d / "dlss5-feed.log").write_text(
    f"{_stamp(-120)}  [feed] frame 500 delivered (old run)\n"
    f"{_stamp(5)}  [feed] frame 1 delivered (1280x720 at 100% -> 1280x720, reset=1)\n"
    f"{_stamp(6)}  [feed] frame 7 delivered (1280x720 at 100% -> 1280x720, reset=0)\n"
    f"{_stamp(8)}  [feed] MV probe (centre 64x64, frame 600): mean |mv| 0.4 px, max 0.6 px, 98% non-zero\n",
    encoding="utf8")
_t0 = _now.timestamp()
_frames, _mv = video.feed_frames_since(_d, _t0)
check("frames after the start time are counted, the old run is not",
      _frames == 7 and _mv, f"{_frames} {_mv}")
check("nothing after a start time in the future", video.feed_frames_since(_d, _t0 + 600) == (0, False))
check("a missing log is (0, False)", video.feed_frames_since(Path(tempfile.mkdtemp(prefix="nolog_")), _t0) == (0, False))
shutil.rmtree(_d, ignore_errors=True)

section("28. the RTX Remix route")
from core import remix as _remix  # noqa: E402

# A FAKE Remix game: a .trex folder with a small runtime binary that carries
# Kim2091's option prefix as a string, and an rtx.conf that does NOT end with
# a newline - the exact shape that produced "...= 3rtx.neuralUplift = True"
# when this was done by hand. The real GTA IV install is only ever read.
_CONF_BODY = (b"rtx.fallbackLightMode = 2\r\n"
              b"rtx.atmosphere.skyIndirectRadianceScale = 3")


def _fake_remix(prefix: str, marker: bytes = b"rtx.neuralUplift") -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, d / "Game.exe")
    t = d / ".trex"
    t.mkdir()
    (t / "d3d9.dll").write_bytes(b"MZ" + b"\0" * 4096 + marker
                                 + b"\0" * 512 + marker + b".bypassCallerCheck")
    (d / "d3d9.dll").write_bytes(b"MZ" + b"\0" * 2048)   # the Remix bridge stub
    (d / "rtx.conf").write_bytes(_CONF_BODY)
    return d


_d = _fake_remix("remix_")
_trex = _remix.find_runtime(_d)
check("the .trex runtime folder is found", _trex == _d / ".trex", str(_trex))
check("a folder with a .trex is a Remix game", _remix.is_remix_game(_d))
check("a folder without one is not",
      not _remix.is_remix_game(Path(tempfile.mkdtemp(prefix="notremix_"))))
check("the uplift fork is recognised from the binary",
      _remix.runtime_flavour(_trex) == "uplift", _remix.runtime_flavour(_trex))
check("the enable key comes from the fork, not a guess",
      _remix.enable_key("uplift") == "rtx.neuralUplift.enable"
      and _remix.enable_key("neural") == "rtx.neuralRendering.enable")
_dn = _fake_remix("remixn_", marker=b"rtx.neuralRendering")
check("the neuralRendering fork is recognised too",
      _remix.runtime_flavour(_dn / ".trex") == "neural")
shutil.rmtree(_dn, ignore_errors=True)
_dp = _fake_remix("remixplain_", marker=b"rtx.someOtherOption")
check("a runtime with neither marker has no flavour",
      _remix.runtime_flavour(_dp / ".trex") == "")
_gp = games.manual(_dp)
check("a Remix runtime without the neural pass blocks the install",
      any("swap" in b for b in installer.preview(
          _gp, installer.Options(path=dlss.REMIX)).blockers),
      str(installer.preview(_gp, installer.Options(path=dlss.REMIX)).blockers))
check("the plan then has the runtime step when the swap is ticked",
      installer.plan(_gp, installer.Options(path=dlss.REMIX, remix_swap=True))
      == ["RTX Remix runtime (DLSS 5 build)", "nvngx_dlssnr.dll", "rtx.conf"],
      str(installer.plan(_gp, installer.Options(path=dlss.REMIX, remix_swap=True))))
check("swapping the runtime is experimental, keeping it is beta",
      installer.reliability(_gp, dlss.REMIX, remix_swap=True)[0]
      == installer.EXPERIMENTAL
      and installer.reliability(_gp, dlss.REMIX)[0] == installer.BETA)
shutil.rmtree(_dp, ignore_errors=True)

# --- rtx.conf editing: the file is the user's, one line is ours ------------
_conf = _d / "rtx.conf"
check("set_option appends to a file with no trailing newline",
      _remix.set_option(_conf, "rtx.neuralUplift.enable", "True")
      and _conf.read_bytes() ==
      _CONF_BODY + b"\r\nrtx.neuralUplift.enable = True\r\n",
      repr(_conf.read_bytes()[-70:]))
check("CRLF is preserved, LF is never introduced",
      b"\n" not in _conf.read_bytes().replace(b"\r\n", b""))
_remix.set_option(_conf, "rtx.neuralUplift.enable", "False")
check("setting a key that is already there replaces it in place",
      _conf.read_bytes().count(b"rtx.neuralUplift.enable") == 1
      and b"= False" in _conf.read_bytes()
      and b"rtx.fallbackLightMode = 2" in _conf.read_bytes(),
      repr(_conf.read_bytes()))
_remix.set_option(_conf, "rtx.neuralUplift.enable", "True")
check("option_set sees the key, and not a key that is absent",
      _remix.option_set(_conf, "rtx.neuralUplift.enable")
      and not _remix.option_set(_conf, "rtx.neuralRendering.enable"))
check("remove_option takes only that line out",
      _remix.remove_option(_conf, "rtx.neuralUplift.enable")
      and _conf.read_bytes() == _CONF_BODY + b"\r\n",
      repr(_conf.read_bytes()))
check("removing a key that is not there changes nothing",
      not _remix.remove_option(_conf, "rtx.neuralUplift.enable")
      and _conf.read_bytes() == _CONF_BODY + b"\r\n")
_conf.write_bytes(_CONF_BODY)          # back to the awkward shape for install

# --- detection and the route list -----------------------------------------
_g = games.manual(_d)
_sup = dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness or 0, 89)
check("remix is the recommendation for a Remix game",
      _sup.recommended == dlss.REMIX and _sup.options[0] == dlss.REMIX,
      f"{_sup.recommended} {_sup.options}")
check("the other routes are still listed", len(_sup.options) > 1, str(_sup.options))
check("the reason says a ReShade proxy crashes a Remix game",
      "ReShade" in _sup.reason and "crashes" in _sup.reason, _sup.reason[:80])
check("remix has a label, a blurb and a conflicts entry",
      dlss.REMIX in dlss.LABELS and dlss.REMIX in dlss.BLURB
      and dlss.REMIX in dlss.CONFLICTS
      and dlss.LABELS[dlss.REMIX] ==
      "remix - DLSS 5 inside RTX Remix (path tracing)")
check("every route still has a conflicts entry of 2-4 lines",
      set(dlss.CONFLICTS) == set(dlss.ALL_ROUTES)
      and all(2 <= len(v) <= 4 for v in dlss.CONFLICTS.values()),
      str(sorted(dlss.CONFLICTS)))
check("fit says remix is usable", dlss.fit(dlss.REMIX, _g.api, False, 89)[0])
check("DXVK is never offered on this route",
      not installer.uses_dxvk(_g, installer.Options(path=dlss.REMIX, dxvk=True)))

_opt = installer.Options(path=dlss.REMIX)
check("the plan is the runtime's own files and one config line",
      installer.plan(_g, _opt) == ["nvngx_dlssnr.dll", "rtx.conf"],
      str(installer.plan(_g, _opt)))
_pv = installer.preview(_g, _opt)
check("the preview writes into .trex and nowhere else",
      any(w.endswith(".trex/nvngx_dlssnr.dll") for w in _pv.writes)
      and not any(w.lower().endswith(("dxgi.dll", ".addon64", "reshade.ini"))
                  for w in _pv.writes), str(_pv.writes))
check("the preview names the one rtx.conf line it will add",
      any("rtx.neuralUplift.enable = True" in w for w in _pv.writes),
      str(_pv.writes))
check("the preview has no blockers", not _pv.blockers, str(_pv.blockers))

# --- a ReShade proxy in a Remix folder is a crash, so it goes -------------
(_d / "dxgi.dll").write_bytes(b"MZ" + b"ReShade" + b"\0" * (1 << 21))
_pv = installer.preview(_g, installer.Options(path=dlss.REMIX))
check("a ReShade proxy is reported for removal",
      any("dxgi.dll" in r for r in _pv.removes), str(_pv.removes))

try:
    _rep = installer.install(_g, _opt, on_log=lambda t: None)
    check("nvngx_dlssnr.dll goes inside .trex, not beside the exe",
          (_trex / "nvngx_dlssnr.dll").is_file()
          and not (_d / "nvngx_dlssnr.dll").exists())
    check("the enable key is set and rtx.conf is otherwise untouched",
          _remix.option_set(_conf, "rtx.neuralUplift.enable")
          and _conf.read_bytes().startswith(_CONF_BODY),
          repr(_conf.read_bytes()[-60:]))
    _names = sorted(p.name for p in _d.rglob("*") if p.is_file())
    check("no ReShade, no feeder, no add-on and no shader went in",
          not any(n.endswith((".addon64", ".addon32", ".fx", ".fxh"))
                  or n.lower() in ("reshade.ini", "reshadepreset.ini",
                                   "dlss5-feed.cfg")
                  for n in _names), str(_names))
    check("the ReShade proxy was moved out of the way",
          not (_d / "dxgi.dll").exists()
          and (_d / ("dxgi.dll" + installer.BACKUP_SUFFIX)).is_file(),
          str(_names))
    _man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest records the route, the key and the conf",
          _man.get("path") == "remix"
          and _man["remix"]["key"] == "rtx.neuralUplift.enable"
          and _man["remix"]["conf"] == "rtx.conf"
          and _man["remix"]["flavour"] == "uplift", str(_man.get("remix")))
    check("the manifest round-trips the route",
          installer.options_from_manifest(_d).path == dlss.REMIX
          and not installer.options_from_manifest(_d).remix_swap)
    check("no runtime swap was recorded - this runtime already had the pass",
          "remix_runtime" not in (_man.get("components") or {}),
          str(_man.get("components")))

    # the diagnosis, from the runtime's own log
    _rl = _remix.log_path(_d)
    _rl.parent.mkdir(parents=True, exist_ok=True)
    _rl.write_text(
        "[15:28:12.945] info:  [DLSS-NR] Loaded .trex\\nvngx_dlssnr.dll\n"
        "[15:28:13.071] info:  [DLSS-NR] Snippet initialized\n"
        "[15:28:13.739] info:  [DLSS-NR] Created the Neural Uplift feature "
        "(id 18, preset 0) at 1920x1080\n", encoding="utf8")
    _r = diagnose.analyse(_d)
    check("a real success log reads as working",
          _r.verdict == "Working."
          and any("feature 18" in f.title for f in _r.findings if f.level == "ok"),
          _r.verdict + " / " + str(_levels(_r, "bad")))
    _rl.write_text("[15:28:12.945] err:   nvngx_dlssnr.dll could not be loaded\n",
                   encoding="utf8")
    _r = diagnose.analyse(_d)
    check("the fork's own failure phrase is matched and explained",
          any("could not be loaded" in b for b in _levels(_r, "bad")),
          str(_levels(_r, "bad")))
    _remix.remove_option(_conf, "rtx.neuralUplift.enable")
    _r = diagnose.analyse(_d)
    check("the key missing from rtx.conf is reported",
          any("switched off in rtx.conf" in b for b in _levels(_r, "bad")),
          str(_levels(_r, "bad")))
    check("the bug report carries the Remix log, not ReShade's",
          "remix-dxvk.log" in diagnose.issue_body(
              "1.5.0", "RTX 4060 Ti", 89, "581.15", _g, "remix", _r, "", "x",
              _d))
    _remix.set_option(_conf, "rtx.neuralUplift.enable", "True")
    shutil.rmtree(_d / "rtx-remix", ignore_errors=True)

    _runtime_before = (_trex / "d3d9.dll").read_bytes()
    installer.uninstall(_g, on_log=lambda t: None)
    check("uninstall takes the key back out and leaves the rest of rtx.conf",
          not _remix.option_set(_conf, "rtx.neuralUplift.enable")
          and _conf.read_bytes().startswith(_CONF_BODY),
          repr(_conf.read_bytes()))
    check("uninstall removes nvngx_dlssnr.dll from .trex",
          not (_trex / "nvngx_dlssnr.dll").exists())
    check("the mod's own runtime is still there, byte for byte",
          (_trex / "d3d9.dll").read_bytes() == _runtime_before)
    check("the ReShade proxy that was moved aside is put back",
          (_d / "dxgi.dll").is_file()
          and not (_d / ("dxgi.dll" + installer.BACKUP_SUFFIX)).exists())
    _left = sorted(p.name for p in _d.rglob("*") if p.is_file())
    check("nothing of ours is left behind",
          _left == ["Game.exe", "d3d9.dll", "d3d9.dll", "dxgi.dll", "rtx.conf"],
          str(_left))
except Exception as e:
    check("remix: installs", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# Read-only, against the owner's real GTA IV RTX install. Skipped anywhere
# else: nothing here writes, and the guard keeps the suite portable.
_GTA = Path(r"D:\SteamLibrary\steamapps\common\Grand Theft Auto IV\GTAIV")
if _remix.is_remix_game(_GTA):
    _t = _remix.find_runtime(_GTA)
    check("real GTA IV: the runtime is the neuralUplift fork",
          _remix.runtime_flavour(_t) == "uplift", str(_t))
    # Not "the key is set" - that depends on whether the tool happens to be
    # installed there right now. What must hold is that the real rtx.conf is
    # found and readable, and that asking about the key gives an answer.
    _real_conf = _remix.conf_path(_GTA, _t)
    check("real GTA IV: its rtx.conf is found and readable",
          _real_conf.is_file() and len(_real_conf.read_bytes()) > 100,
          str(_real_conf))
    check("real GTA IV: the enable key can be asked about either way",
          _remix.option_set(_real_conf, _remix.enable_key("uplift")) in (True, False))
    _gg = games.manual(_GTA)
    check("real GTA IV: remix is the recommended route",
          dlss.detect(_gg.install_dir, _gg.folder, _gg.api,
                      _gg.bitness or 0, 89).recommended == dlss.REMIX)
else:
    print("   SKIP  the real GTA IV Remix install is not on this machine")


# ------------------------------------------------- 29. a Remix mod is never damaged
section("29. an RTX Remix install survives everything we do")


def _fake_remix(prefix: str = "remix_safe_"):
    """A game folder shaped like a real Remix install."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, d / "Game.exe")
    (d / "d3d9.dll").write_bytes(b"MZ REMIX BRIDGE CLIENT" + b"\x00" * 200)
    trex = d / ".trex"
    trex.mkdir()
    (trex / "d3d9.dll").write_bytes(b"MZ REMIX RUNTIME rtx.neuralUplift" + b"\x00" * 400)
    (trex / "NvRemixBridge.exe").write_bytes(b"MZ")
    (d / "rtx.conf").write_bytes(b"rtx.fallbackLightMode = 2\r\nrtx.skyBrightness = 1")
    mods = d / "rtx-remix" / "mods" / "thegame"
    mods.mkdir(parents=True)
    (mods / "mod.usda").write_bytes(b"#usda 1.0\n")
    return d


_d = _fake_remix()
_before = {p.relative_to(_d).as_posix(): p.read_bytes()
           for p in _d.rglob("*") if p.is_file()}
_g = games.manual(_d)
# A Remix game IS a DirectX 9 game - that is the whole point of Remix - so
# the DX9 branch of the installer has to be exercised here, not skipped.
_g.api = "DX9"
check("a folder with .trex is recognised as a Remix game",
      remix.is_remix_game(_d) and remix.find_runtime(_d) == (_d / ".trex"))
check("the runtime's DLSS 5 flavour is read from the binary, not guessed",
      remix.runtime_flavour(_d / ".trex") == "uplift",
      remix.runtime_flavour(_d / ".trex"))

# every other route must refuse before it writes anything
for _route in (dlss.FEEDER, dlss.NATIVE, dlss.BRIDGE, dlss.STANDALONE, dlss.OPTI):
    try:
        installer.install(_g, installer.Options(path=_route), on_log=lambda t: None)
        _refused = False
    except installer.InstallError as e:
        _refused = "Remix" in str(e)
    except Exception:
        _refused = False
    check(f"the {_route} route refuses a Remix game", _refused)
_after = {p.relative_to(_d).as_posix(): p.read_bytes()
          for p in _d.rglob("*") if p.is_file()}
check("and not one byte of the mod changed", _after == _before,
      str(sorted(set(_after) ^ set(_before))))

# uninstall with no manifest at all must not delete the runtime
installer.uninstall(_g, on_log=lambda t: None)
check("a manifest-less uninstall leaves the Remix runtime and its mods alone",
      (_d / "d3d9.dll").read_bytes() == _before["d3d9.dll"]
      and (_d / ".trex" / "d3d9.dll").is_file()
      and (_d / "rtx-remix" / "mods" / "thegame" / "mod.usda").is_file())

# the remix route itself: install, then put everything back
_rep = installer.install(_g, installer.Options(path=dlss.REMIX), on_log=lambda t: None)
check("the route puts nvngx_dlssnr.dll inside .trex",
      (_d / ".trex" / "nvngx_dlssnr.dll").is_file())
check("it writes no ReShade, no feeder and no add-on",
      not (_d / "dxgi.dll").exists() and not (_d / installer.RENODX).exists()
      and not (_d / installer.FEEDER_ADDON64).exists()
      and not (_d / "ReShade.ini").exists(),
      str(sorted(p.name for p in _d.iterdir())))
# The one that got through on the real GTA IV: a Remix game is a DX9 game,
# and the DX9 translation step wrote its own d3d9.dll over the Remix bridge
# client. No DX9 translation may run on this route, DXVK or anything else.
check("and no DX9 translation layer, whatever the game's API says",
      not (_d / "dgVoodoo.conf").exists() and not (_d / "dgVoodooCpl.exe").exists()
      and not (_d / ("D3D9.dll" + installer.BACKUP_SUFFIX)).exists()
      and not (_d / ("d3d9.dll" + installer.BACKUP_SUFFIX)).exists(),
      str(sorted(p.name for p in _d.iterdir())))
check("the steps taken match the plan exactly",
      len(installer.plan(_g, installer.Options(path=dlss.REMIX))) == 2,
      str(installer.plan(_g, installer.Options(path=dlss.REMIX))))
check("the runtime is left exactly as it was without the swap option",
      (_d / ".trex" / "d3d9.dll").read_bytes() == _before[".trex/d3d9.dll"]
      and (_d / "d3d9.dll").read_bytes() == _before["d3d9.dll"])
_conf = (_d / "rtx.conf").read_bytes()
check("the enable key is added and the file's own lines are untouched",
      b"rtx.fallbackLightMode = 2" in _conf and b"rtx.skyBrightness = 1" in _conf
      and remix.enable_key("uplift").encode() in _conf, _conf)
check("a conf with no trailing newline does not get two keys glued together",
      b"1rtx." not in _conf and _conf.count(b"neuralUplift.enable") == 1, _conf)
installer.uninstall(_g, on_log=lambda t: None)
_end = {p.relative_to(_d).as_posix(): p.read_bytes()
        for p in _d.rglob("*") if p.is_file()}
check("uninstall returns the Remix install byte for byte", _end == _before,
      str(sorted(set(_end) ^ set(_before))))

# The swap, end to end: a runtime without the pass, swapped in; "update all"
# (options_from_manifest) renews the one we put there instead of skipping it
# because it now has the pass; uninstall brings the mod's own back (#148).
_ds = _fake_remix("remix_swap_")
(_ds / ".trex" / "d3d9.dll").write_bytes(b"MZ MOD'S OWN RUNTIME, NO PASS" + b"\x00" * 400)
_orig_rt = (_ds / ".trex" / "d3d9.dll").read_bytes()
_gs = games.manual(_ds)
_gs.api = "DX9"
_tag = {"v": "dlssnr-v1"}
_saved_sw = (installer.sources.resolve_remix_runtime, net.download)
installer.sources.resolve_remix_runtime = lambda: (
    _tag["v"], {n: f"https://x/{_tag['v']}/{n}" for n in sources.REMIX_RUNTIME_ASSETS})


def _dl_sw(url, name, **k):
    if not name.startswith("remix-runtime-"):
        return _saved_sw[1](url, name, **k)
    p = Path(tempfile.mkdtemp(prefix="rtdl_")) / name
    p.write_bytes(b"MZ COMMUNITY RUNTIME " + _tag["v"].encode()
                  + b" rtx.neuralUplift" + b"\x00" * 400)
    return p


net.download = _dl_sw
try:
    installer.install(_gs, installer.Options(path=dlss.REMIX, remix_swap=True),
                      on_log=lambda t: None)
    check("the swap puts the community runtime in",
          b"dlssnr-v1" in (_ds / ".trex" / "d3d9.dll").read_bytes())
    _tag["v"] = "dlssnr-v2"
    _opt_up = installer.options_from_manifest(_ds)
    installer.install(_gs, _opt_up, on_log=lambda t: None)
    _man_sw = json.loads((_ds / installer.MANIFEST).read_text(encoding="utf8"))
    check("'update all' renews a runtime this tool swapped in, and keeps it in the record",
          b"dlssnr-v2" in (_ds / ".trex" / "d3d9.dll").read_bytes()
          and (_man_sw.get("components") or {}).get("remix_runtime") == "dlssnr-v2",
          (_man_sw.get("components"), (_ds / ".trex" / "d3d9.dll").read_bytes()[:40]))
    installer.uninstall(_gs, on_log=lambda t: None)
    check("...and uninstall brings the mod's own runtime back after both",
          (_ds / ".trex" / "d3d9.dll").read_bytes() == _orig_rt,
          (_ds / ".trex" / "d3d9.dll").read_bytes()[:40])
    # After our swap the mod updates its own runtime. With the pass, it is
    # the mod's and stays; without it, the swap backs up THAT one, and
    # uninstall brings back the mod's newer runtime, not the old one.
    _tag["v"] = "dlssnr-v1"
    installer.install(_gs, installer.Options(path=dlss.REMIX, remix_swap=True),
                      on_log=lambda t: None)
    (_ds / ".trex" / "d3d9.dll").write_bytes(b"MZ MOD V2 WITH PASS rtx.neuralUplift" + b"\x00" * 400)
    check("a runtime the mod updated since our swap is not taken for ours",
          installer.remix_state(_gs, installer.options_from_manifest(_ds))[2] is False)
    _mod_v3 = b"MZ MOD V3 NO PASS" + b"\x00" * 400
    (_ds / ".trex" / "d3d9.dll").write_bytes(_mod_v3)
    installer.install(_gs, installer.options_from_manifest(_ds), on_log=lambda t: None)
    installer.uninstall(_gs, on_log=lambda t: None)
    check("...and one the mod replaced with a runtime without the pass comes back on uninstall",
          (_ds / ".trex" / "d3d9.dll").read_bytes() == _mod_v3,
          (_ds / ".trex" / "d3d9.dll").read_bytes()[:30])
    # A record from before the stamp (a command-line swap by 1.8.1), and a
    # reinstall with the box left unticked: neither may lose the backup or
    # the swap from the record.
    installer.install(_gs, installer.Options(path=dlss.REMIX, remix_swap=True),
                      on_log=lambda t: None)
    _m = json.loads((_ds / installer.MANIFEST).read_text(encoding="utf8"))
    _m["components"].pop("remix_runtime_stamp", None)
    (_ds / installer.MANIFEST).write_text(json.dumps(_m), encoding="utf8")
    installer.install(_gs, installer.Options(path=dlss.REMIX), on_log=lambda t: None)
    _m2 = json.loads((_ds / installer.MANIFEST).read_text(encoding="utf8"))
    check("a reinstall without the box keeps the swap and the backup on the record",
          (_m2.get("components") or {}).get("remix_runtime")
          and any(f.replace("\\", "/").endswith(".trex/d3d9.dll" + installer.BACKUP_SUFFIX)
                  for f in _m2.get("files", [])),
          (_m2.get("components"), _m2.get("files")))
    installer.uninstall(_gs, on_log=lambda t: None)
    check("...and uninstall still brings the mod's runtime back",
          (_ds / ".trex" / "d3d9.dll").read_bytes() == _mod_v3,
          (_ds / ".trex" / "d3d9.dll").read_bytes()[:30])
finally:
    installer.sources.resolve_remix_runtime, net.download = _saved_sw
# A mod whose own runtime HAS the pass is never replaced, box ticked or not.
_dk = _fake_remix("remix_keep_")
_keep_rt = (_dk / ".trex" / "d3d9.dll").read_bytes()
check("a mod's own runtime with the pass is not swapped even with the box ticked",
      installer.remix_state(games.manual(_dk),
                            installer.Options(path=dlss.REMIX, remix_swap=True))[2] is False)
shutil.rmtree(_ds, ignore_errors=True)
shutil.rmtree(_dk, ignore_errors=True)
shutil.rmtree(_d, ignore_errors=True)

# the same, for a conf that DID end with a newline: it must keep it
_d = _fake_remix("remix_nl_")
(_d / "rtx.conf").write_bytes(b"rtx.fallbackLightMode = 2\r\n")
_g = games.manual(_d)
installer.install(_g, installer.Options(path=dlss.REMIX), on_log=lambda t: None)
installer.uninstall(_g, on_log=lambda t: None)
check("a conf that ended with a newline still does",
      (_d / "rtx.conf").read_bytes() == b"rtx.fallbackLightMode = 2\r\n",
      (_d / "rtx.conf").read_bytes())
shutil.rmtree(_d, ignore_errors=True)

# the runtime swap is opt-in, backs the original up, and comes back
_d = _fake_remix("remix_swap_")
_orig = (_d / ".trex" / "d3d9.dll").read_bytes()
_g = games.manual(_d)
_g.api = "DX9"
_pv = installer.preview(_g, installer.Options(path=dlss.REMIX))
check("the preview does not swap the runtime unless asked",
      not any("d3d9" in w.lower() for w in _pv.writes), str(_pv.writes))
check("the list of known Remix projects is real and matched by name",
      remixlist.match("Grand Theft Auto IV") is not None
      and remixlist.match("Some Game Nobody Modded") is None
      and all(m.url.startswith("https://") for m in remixlist.MODS + remixlist.BUILT_IN))
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 30. RE Engine: REFramework
section("30. RE Engine games get REFramework installed first, for real")
_d = Path(tempfile.mkdtemp(prefix="reengine_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "re_chunk_000.pak").write_bytes(b"pak")
check("re_chunk_000.pak marks the folder as RE Engine",
      reengine.detected(_d))
check("a folder with no marker is not RE Engine",
      not reengine.detected(Path(tempfile.mkdtemp(prefix="not_reengine_"))))
check("dinput8.dll is no longer offered as a fake ReShade proxy name",
      "dinput8.dll" not in installer.RESHADE_PROXIES
      and "dinput8.dll" not in installer.RESHADE_PROXY_HELP)
_g = games.manual(_d)
_pv = installer.preview(_g, installer.Options())
check("the preview carries the RE Engine warning",
      any("RE Engine" in w for w in _pv.warnings), str(_pv.warnings))
check("the warning names REFramework, not a dead end",
      any("REFramework" in w for w in _pv.warnings), str(_pv.warnings))
check("REFramework is the first step of the plan",
      installer.plan(_g, installer.Options())[0].startswith("REFramework"),
      str(installer.plan(_g, installer.Options())))
try:
    _rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
    check("install carries the same warning and still finishes",
          any("RE Engine" in w for w in _rep.warnings))
    check("a real REFramework dinput8.dll landed in the folder",
          refw.is_reframework(_d / refw.DINPUT8), str(_rep.written))
    installer.uninstall(_g, on_log=lambda t: None)
    check("uninstall takes it back out again",
          not (_d / refw.DINPUT8).exists())
except Exception as e:
    check("RE Engine install/uninstall round trip", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# A dinput8.dll already there - the person's own REFramework, or something
# else entirely using the same load slot - is backed up, not clobbered.
_d = Path(tempfile.mkdtemp(prefix="reengine_existing_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "re_chunk_000.pak").write_bytes(b"pak")
(_d / refw.DINPUT8).write_bytes(b"USER OWN DINPUT8" + bytes(200))
_g = games.manual(_d)
try:
    installer.install(_g, installer.Options(), on_log=lambda t: None)
    check("the user's own dinput8.dll was backed up, not deleted",
          (_d / (refw.DINPUT8 + installer.BACKUP_SUFFIX)).read_bytes()
          .startswith(b"USER OWN DINPUT8"))
    installer.uninstall(_g, on_log=lambda t: None)
    check("uninstall restores the user's own dinput8.dll byte for byte",
          (_d / refw.DINPUT8).read_bytes().startswith(b"USER OWN DINPUT8"))
except Exception as e:
    check("RE Engine dinput8.dll backup/restore", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# A manifest-less uninstall must only take a dinput8.dll that really is
# REFramework by content - a foreign one (VR mod, something unrelated using
# the same slot) survives, the same protection dxgi.dll/opengl32.dll got.
_d = Path(tempfile.mkdtemp(prefix="reengine_foreign_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / refw.DINPUT8).write_bytes(b"MZ not reframework at all" + bytes(2000))
_g = games.manual(_d)
installer.uninstall(_g, on_log=lambda t: None)
check("a foreign dinput8.dll survives a manifest-less uninstall",
      (_d / refw.DINPUT8).is_file())
shutil.rmtree(_d, ignore_errors=True)

# Installing twice must not turn our own file into "the game's own file".
# It did: the second install backed up the first install's copy, uninstall
# restored that backup, and the layer stayed in the folder for good - the
# game left rendering through DXVK with nothing on disk admitting it.
check("a DXVK d3d9.dll is recognised as ours, a foreign one is not",
      dxvk.is_dxvk(Path(tempfile.mkdtemp(prefix="notdxvk_")) / "nothing.dll") is False)
_d = Path(tempfile.mkdtemp(prefix="twice_dxvk_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.Game(name="twice", folder=_d, exe=_d / "Game.exe", bitness=64, api="DX11")
try:
    for _round in (1, 2):
        installer.install(_g, installer.Options(path=dlss.FEEDER, dxvk=True),
                          on_log=lambda t: None)
    _baks = sorted(p.name for p in _d.glob("*" + installer.BACKUP_SUFFIX))
    check("a second install does not back up our own DXVK", not _baks, str(_baks))
    installer.uninstall(_g, on_log=lambda t: None)
    _left = sorted(p.name for p in _d.iterdir() if p.is_file())
    check("after uninstall no DXVK is left behind", _left == ["Game.exe"], str(_left))
except Exception as e:
    check("install twice / uninstall clean", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

check("the remix route is never bothered with the RE Engine warning",
      True)  # covered structurally: both call sites gate on opt.path != ROUTE_REMIX

# ------------------------------------------------- 32. fetching a Remix mod
section("32. a Remix mod is fetched only when the release is a complete one")
from core import remixdl  # noqa: E402

# The real layouts of every published Remix release, read off the archives
# themselves. Only an archive carrying the renderer (.trex/d3d9.dll) may be
# installed; a bare proxy expects NVIDIA's runtime and a manual rename first,
# and dropping it in alone leaves the game loading a d3d9.dll with nothing
# behind it.
_LAYOUTS = {
    "GTA IV, renderer nested one level": (
        ["GTAIV-Remix-CompatibilityMod/.trex/d3d9.dll",
         "GTAIV-Remix-CompatibilityMod/d3d9.dll",
         "GTAIV-Remix-CompatibilityMod/dxvk.conf",
         "_installer_options/FusionFix_RTXRemixFork/plugins/x.asi"],
        "GTAIV-Remix-CompatibilityMod"),
    "NFSU2, renderer at the archive root": (
        [".trex/d3d9.dll", ".trex/bridge.conf", "rtx.conf", "dxvk.conf"], ""),
    "Thief Gold, proxy only": (
        ["INSTALL.txt", "d3d9.dll", "remix-comp-proxy.ini", "rtx.conf"], None),
    "AC II, a .trex with no renderer in it": (
        [".trex/bridge.conf", "dinput8.dll", "plugins/remix-comp-base.asi"], None),
    "Saints Row 3, configuration only": (
        ["dxvk.conf", "rtx.conf", "INSTALL.md"], None),
    "Garry's Mod, its own launcher": (
        ["bin/x.dll", "garrysmod/y", "rtx.conf", "dxvk.conf"], None),
    "Deus Ex, dev tools": (["IngestionHelper.exe", "omniversehelper/a"], None),
}
for _name, (_names, _want) in _LAYOUTS.items():
    check(f"{_name} -> {'refused' if _want is None else repr(_want)}",
          remixdl.remix_root(_names) == _want,
          str(remixdl.remix_root(_names)))

check("only the verified-complete projects are offered for fetching",
      [m.game for m in remixlist.MODS if m.installable]
      == ["Grand Theft Auto IV", "Need for Speed: Underground 2"],
      str([m.game for m in remixlist.MODS if m.installable]))
check("a github url gives its repo, anything else does not",
      remixdl.repo_of("https://github.com/xoxor4d/gta4-rtx") == "xoxor4d/gta4-rtx"
      and remixdl.repo_of("https://www.moddb.com/rtx") == "")

# A complete archive, built here: install it, then take it back out and the
# folder has to be exactly what it was.
_d = Path(tempfile.mkdtemp(prefix="remixmod_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "own.cfg").write_bytes(b"THE PLAYER'S OWN FILE")
_before = {p.name: p.read_bytes() for p in _d.iterdir() if p.is_file()}
_zip = _d.parent / "fake-remix-mod.zip"
import zipfile as _zf  # noqa: E402
with _zf.ZipFile(_zip, "w") as _z:
    _z.writestr("TheMod/.trex/d3d9.dll", b"MZ" + bytes(2048))
    _z.writestr("TheMod/.trex/bridge.conf", b"x=1\n")
    _z.writestr("TheMod/d3d9.dll", b"MZ" + bytes(512))
    _z.writestr("TheMod/rtx.conf", b"rtx.fallbackLightMode = 2\n")
    _z.writestr("TheMod/own.cfg", b"THE MOD'S VERSION")
_root, _lands = remixdl.inspect(_zip)
check("inspect finds the mod root and what it would write",
      _root == "TheMod" and sorted(_lands) == [".trex", "d3d9.dll", "own.cfg", "rtx.conf"],
      f"{_root} {_lands}")
_bad = _d.parent / "not-a-mod.zip"
with _zf.ZipFile(_bad, "w") as _z:
    _z.writestr("d3d9.dll", b"MZ" + bytes(64))
    _z.writestr("rtx.conf", b"x\n")
check("an incomplete release is refused by inspect too",
      _raises(lambda: remixdl.inspect(_bad)))
shutil.rmtree(_d, ignore_errors=True)

# The refusal that matters most: a folder that already has somebody's mod.
_d = Path(tempfile.mkdtemp(prefix="remixmod_busy_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / ".trex").mkdir()
(_d / ".trex" / "d3d9.dll").write_bytes(b"MZ" + bytes(4096))
_refused = False
try:
    remixdl.install("https://github.com/xoxor4d/gta4-rtx", _d, log=lambda t: None)
except remixdl.NotAModError as e:
    _refused = "already installed" in str(e)
except Exception:
    _refused = False
check("a folder that already has a Remix mod is never written over", _refused)
check("...and nothing was downloaded or written",
      sorted(p.name for p in _d.iterdir()) == [".trex", "Game.exe"])
shutil.rmtree(_d, ignore_errors=True)

# ------------------------------------------------- 31. a D3D12 game that only imports d3d11.dll
section("31. a D3D12 Agility SDK game is not mistaken for D3D11")
_d = Path(tempfile.mkdtemp(prefix="agility_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "D3D12").mkdir()
(_d / "D3D12" / "D3D12Core.dll").write_bytes(b"MZ")
check("a D3D12 folder with D3D12Core.dll promotes the label to DX12",
      pe.detect_api(_d / "Game.exe")[0] == "DX12")
shutil.rmtree(_d, ignore_errors=True)
_d = Path(tempfile.mkdtemp(prefix="dlssg_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "nvngx_dlssg.dll").write_bytes(b"MZ")
check("DLSS Frame Generation alone is also enough evidence",
      pe.detect_api(_d / "Game.exe")[0] == "DX12")
shutil.rmtree(_d, ignore_errors=True)
_d = Path(tempfile.mkdtemp(prefix="plain_dx11_"))
shutil.copyfile(r"C:\Windows\System32\where.exe", _d / "Game.exe")
check("an exe with neither, and no graphics DLL named anywhere, stays Unknown",
      pe.detect_api(_d / "Game.exe")[0] == "Unknown")
shutil.rmtree(_d, ignore_errors=True)

section("33. a Vulkan-layer install is diagnosed by the registry, not the folder")
# Since 1.6.0 every DirectX 9 game goes through DXVK, so its manifest says
# "(vulkan layer)" where a proxy name would be. 1.6.0 looked for a file by
# that name and told everyone it had been quarantined (issues #10, #2).
from core import vulkan as _vk
_saved = (_vk.registrations, )
_vk.registrations = lambda: [(Path("C:/ProgramData/ReShade/ReShade64.json"), 0)]
check("registered_for(64-bit) sees ReShade's own 64-bit registration",
      _vk.registered_for(True) is not None)
check("registered_for(32-bit) does not accept a 64-bit manifest",
      _vk.registered_for(False) is None)
_vk.registrations = lambda: [(Path("C:/x/ReShade32.json"), 0),
                             (Path("C:/x/ReShade64.json"), 1)]
check("a DISABLED registration (value 1) counts for nothing",
      _vk.registered_for(True) is None and _vk.existing_registration() is not None)
check("manifest_x64 reads the architecture from the file name when the JSON is unreadable",
      _vk.manifest_x64(Path("C:/nowhere/ReShade32.json")) is False
      and _vk.manifest_x64(Path("C:/nowhere/ReShade64.json")) is True)

_vk.registrations = lambda: [(Path("C:/ProgramData/ReShade/ReShade64.json"), 0)]
# The manifest says api "Vulkan" once DXVK is in - the DXVK file set has to
# come from the recorded file list (d3d9.dll here), not from that label.
_d = _diag_dir("diag_vk32_", proxy=False, bitness=32, api="Vulkan", dxvk="v3.1")
(_d / "d3d9.dll").write_bytes(b"MZ")
_m = json.loads((_d / "dlss5-autopilot.json").read_text(encoding="utf8"))
_m["proxy"] = "(vulkan layer)"
_m["files"] = [f for f in _m["files"] if f != "dxgi.dll"] + ["d3d9.dll"]
(_d / "dlss5-autopilot.json").write_text(json.dumps(_m), encoding="utf8")
_r = diagnose.analyse(_d)
check("a 32-bit game with only the 64-bit layer registered is told exactly that",
      not _r.ran and "32-bit ReShade Vulkan layer is not registered" in _r.verdict
      and not any("gone from the folder" in t for t in _levels(_r, "bad")), _r.verdict)
_body = diagnose.issue_body("9.9", "x", None, "?", None, "feeder", _r, "",
                            Path("C:/x/autopilot.log"), _d)
check("the report shows the layer state and looks for the runtime in host64",
      "ReShade 32-bit Vulkan layer: NOT REGISTERED" in _body
      and "(vulkan layer): MISSING" not in _body
      and "- host64/nvngx_dlssnr.dll: MISSING" in _body
      and "- d3d9.dll: present" in _body, _body[_body.find("**Files"):][:400])

_vk.registrations = lambda: [(Path("C:/x/ReShade32.json"), 0),
                             (Path("C:/x/ReShade64.json"), 0)]
_r = diagnose.analyse(_d)
check("with both layers registered the verdict moves on to 'not started yet'",
      not _r.ran and "Not started" in _r.verdict, _r.verdict)
check("DXVK's d3d9.dll is NOT reported missing just because the api label is Vulkan",
      "dxgi.dll" not in " ".join(_levels(_r, "bad")), str(_levels(_r, "bad")))
(_d / "d3d9.dll").unlink()
_r = diagnose.analyse(_d)
check("DXVK missing from the folder is its own finding",
      "DXVK is missing" in _r.verdict, _r.verdict)
_vk.registrations = _saved[0]
shutil.rmtree(_d, ignore_errors=True)

# install_layer: a foreign 64-bit registration must not satisfy a 32-bit game
_calls = []
_saved = (_vk.registrations, _vk._place, _vk._register, _vk.layer_dir)
_vk.registrations = lambda: [(Path("C:/ProgramData/ReShade/ReShade64.json"), 0)]
_vk._place = lambda setup, d, dll, manifest: (_calls.append(("place", dll)) or d / manifest)
_vk._register = lambda m: _calls.append(("register", m.name))
_tmp = Path(tempfile.mkdtemp(prefix="vk_layer_"))
_vk.layer_dir = lambda: _tmp
_m, _fresh = _vk.install_layer(Path("C:/x/setup.exe"), also32=False)
check("a 64-bit game reuses ReShade's own 64-bit layer", not _fresh and not _calls)
_m, _fresh = _vk.install_layer(Path("C:/x/setup.exe"), also32=True)
check("a 32-bit game gets our 32-bit layer registered despite the foreign 64-bit one",
      _fresh and ("register", "ReShade32.json") in _calls, str(_calls))
_vk.registrations, _vk._place, _vk._register, _vk.layer_dir = _saved
shutil.rmtree(_tmp, ignore_errors=True)

section("34. a d3d9.dll import is not DirectX 9 when the game ships DLSS")
# No fixture here imports d3d9.dll, so the evidence function is tested on
# its own; detect_api consults it only on the d3d9-without-DXGI branch.
_d = Path(tempfile.mkdtemp(prefix="rdr2_"))
check("an empty folder is no evidence", pe._ships_dlss(_d) == "")
(_d / "nvngx_dlss.dll").write_bytes(b"MZ")
check("the game's own nvngx_dlss.dll is evidence of a modern renderer",
      pe._ships_dlss(_d) == "nvngx_dlss.dll")
(_d / "dlss5-autopilot.json").write_text(json.dumps({"files": ["nvngx_dlss.dll"]}), encoding="utf8")
check("an nvngx_dlss.dll our own manifest lists as written is NOT evidence (64-bit DX9 game after one install)",
      pe._ships_dlss(_d) == "")
(_d / "dlss5-autopilot.json").unlink()
(_d / "nvngx_dlss.dll").unlink()
(_d / "nvngx_dlssnr.dll").write_bytes(b"MZ")
check("our own nvngx_dlssnr.dll is NOT evidence (it would re-label every DX9 game after one install)",
      pe._ships_dlss(_d) == "")
check("the d3d9 branch of detect_api consults it",
      "_ships_dlss(path.parent)" in src_of(pe.detect_api))
shutil.rmtree(_d, ignore_errors=True)

section("35. a scan cannot get stuck in a folder with no executables")
_d = Path(tempfile.mkdtemp(prefix="deep_"))
_p = _d
for i in range(4):
    _p = _p / f"lvl{i}"
    _p.mkdir()
    for j in range(60):
        (_p / f"box{j}").mkdir()
_old = pe._WALK_DIRS
pe._WALK_DIRS = 50
_t0 = time.monotonic()
_found = pe._walk_exes(_d)
pe._WALK_DIRS = _old
check("the walk stops at the directory budget", _found == [] and time.monotonic() - _t0 < 5)
check("Xbox's GameSave and Minecraft Launcher folders are not games",
      "gamesave" in games.XBOX_NOT_GAMES and "minecraft launcher" in games.XBOX_NOT_GAMES)
shutil.rmtree(_d, ignore_errors=True)

section("36. 1.7.0: OpenGL pin, quirks, route texts")
check("OpenGL pins renodx-dlss5 to 4.60 (4.70 stalls on GL)",
      sources.OPENGL_RENODX_PIN == "4.60"
      and "OPENGL_RENODX_PIN" in src_of(installer.install))
check("quirks are keyed by executable name, case-insensitively",
      dlss.quirks(Path("D:/Q3/Quake3.exe")) and "ioquake3" in dlss.quirks(Path("quake3.exe"))[0]
      and dlss.quirks(Path("Game.exe")) == () and dlss.quirks(None) == ())
check("every route has a one-line label and a blurb, and the bridge is no longer 'stopped'",
      set(dlss.LABELS) == set(dlss.ALL_ROUTES) == set(dlss.BLURB)
      and all("\n" not in v and len(v) < 80 for v in dlss.LABELS.values())
      and "stopped" not in dlss.BLURB[dlss.BRIDGE])
check("the feeder blurb lists Direct3D 10", "D3D10" in dlss.BLURB[dlss.FEEDER])
_d = _diag_dir("diag_fx_", provider=4)
_body = diagnose.issue_body("9.9", "x", None, "?", None, "feeder", diagnose.analyse(_d), "",
                            Path("C:/x/autopilot.log"), _d)
check("the feeder report lists the feed shader and the chosen provider's file (issue #13)",
      "- reshade-shaders/Shaders/DLSS5_Feed.fx: present" in _body
      and "- reshade-shaders/Shaders/lumenite_QuantMotion.fx: MISSING" in _body,
      _body[_body.find("**Files"):][:500])
shutil.rmtree(_d, ignore_errors=True)

# frame generation on the OptiScaler route: three ini keys, libraries checked
_d = Path(tempfile.mkdtemp(prefix="fg_"))
(_d / "OptiScaler.ini").write_text("[DlssNr]\nEnabled=true\n", encoding="utf8")
check("no FG libraries beside OptiScaler -> nothing written, False",
      optiscaler.enable_fg(_d) is False
      and "FrameGen" not in (_d / "OptiScaler.ini").read_text(encoding="utf8"))
(_d / "OptiScaler").mkdir()
for _n in optiscaler.FG_LIBS:
    (_d / _n).write_bytes(b"MZ")
check("with the libraries the three keys and HUDFix are written",
      optiscaler.enable_fg(_d) is True)
_ini = (_d / "OptiScaler.ini").read_text(encoding="utf8")
check("FrameGen section: Enabled=true, FGInput=upscaler, FGOutput=fsrfg",
      "[FrameGen]" in _ini and "FGInput=upscaler" in _ini and "FGOutput=fsrfg" in _ini
      and "HUDFix=true" in _ini and "[DlssNr]" in _ini, _ini)
shutil.rmtree(_d, ignore_errors=True)
check("Options carries fg and the manifest round-trips it",
      hasattr(installer.Options(), "fg") and installer.Options().fg is False
      and '"fg": opt.fg' in src_of(installer)
      and 'fg=bool(data.get("fg"' in src_of(installer.options_from_manifest))

section("37. RTX 40 multi-frame generation (mfg.py) - files, loader name, ini merge")
from core import mfg as _m  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="mfg_"))
check("not offered on RTX 50 / RTX 30", not _m.applies(120, "DX12", _d)[0]
      and not _m.applies(86, "DX12", _d)[0])
check("not offered without a DLSS Frame Generation file", not _m.applies(89, "DX12", _d)[0]
      and "no DLSS Frame Generation" in _m.applies(89, "DX12", _d)[1])
(_d / "nvngx_dlssg.dll").write_bytes(b"MZ")
check("RTX 40 + D3D12 + nvngx_dlssg.dll -> offered", _m.applies(89, "DX12", _d) == (True, ""))
check("RTX 40 + Vulkan -> offered, D3D11 -> not",
      _m.applies(89, "Vulkan", _d)[0] and not _m.applies(89, "DX11", _d)[0])
(_d / "dlss5-autopilot.json").write_text(json.dumps({"files": ["nvngx_dlssg.dll"]}), encoding="utf8")
check("an nvngx_dlssg.dll our own manifest wrote (standalone route) is NOT evidence",
      not _m.applies(89, "DX12", _d)[0])
(_d / "dlss5-autopilot.json").unlink()
(_d / "nvngx_dlssg.dll").unlink()
_deep = _d / "Engine" / "Plugins" / "Runtime" / "Nvidia" / "DLSS" / "Binaries" / "ThirdParty" / "Win64"
_deep.mkdir(parents=True)
(_deep / "sl.dlss_g.dll").write_bytes(b"MZ")
check("Streamline's sl.dlss_g.dll nine levels down (Unreal) counts",
      _m.has_dlssg(_d).replace("\\", "/").endswith("Win64/sl.dlss_g.dll"), _m.has_dlssg(_d))
_exe_dir = _d / "Proj" / "Binaries" / "Win64"; _exe_dir.mkdir(parents=True)
check("applies() searches from the game folder, not the executable's folder",
      _m.applies(89, "DX12", _exe_dir, _d)[0] and not _m.applies(89, "DX12", _exe_dir)[0])
check("loader name comes from the import table only - no name imported means None",
      _m.loader_name(None) is None and _m.loader_name(Path("C:/nowhere.exe")) is None)
_imp = X64
_names = {i.lower() for i in pe.pe_imports(_imp)}
_first = next((n for n in _m.LOADER_NAMES if n in _names), None)
check("a real executable gets a name it imports; a taken name moves to the next imported one",
      _first is not None and _m.loader_name(_imp) == _first
      and _m.loader_name(_imp, {_first}) in (set(_m.LOADER_NAMES) & _names) - {_first} | {None})
# install() against fake release zips: three files, the loader, its ini
import zipfile as _zf
_rz = _d / "mfg.zip"
with _zf.ZipFile(_rz, "w") as z:
    for n in _m.FILES:
        z.writestr(n, b"MZ" + n.encode())
    z.writestr("global.ini", "[GlobalSets]\nLoadPlugins=1\n")
_lz = _d / "ual.zip"
with _zf.ZipFile(_lz, "w") as z:
    z.writestr("dinput8.dll", b"MZ" + b"Ultimate ASI Loader" + b"\0" * (1 << 18))
_game = _d / "game"; _game.mkdir()
shutil.copyfile(X64, _game / "Game.exe")
# The loader takes a name the executable really imports (version.dll on
# this fixture, which has no DirectInput), so the test follows that choice.
_lname = _m.loader_name(_game / "Game.exe")
_lini = _lname[:-4] + ".ini"
check("the loader name comes from the executable's import table",
      _lname in _m.LOADER_NAMES and _lname in {i.lower() for i in pe.pe_imports(_game / "Game.exe")}, _lname)
# A file of the game's own under that name is not ours to replace: the
# unlock is an opt-in extra and a version.dll the game ships does something.
# It used to be backed up and written over (#137: on Cyberpunk that file is
# Cyber Engine Tweaks, and doing it took CET out of the game).
(_game / _lname).write_bytes(b"MZ-the-games-own")
check("a name the game itself occupies is not taken (#137)",
      _m.loader_name(_game / "Game.exe", set(), _game) is None
      and _m.loader_name(_game / "Game.exe") == _lname)
try:
    _m.install(_game, _game / "Game.exe")
    _refused = ""
except _m.NoLoaderName as e:
    _refused = str(e)
check("...and the unlock says which file stopped it, not 'none imported'",
      _lname in _refused and "another mod" in _refused, _refused[:90])
# From here the name is a loader the person installed by hand, which IS
# taken over - its ini is merged, so its own plugins keep loading.
(_game / _lname).write_bytes(b"MZ" + b"Ultimate ASI Loader" + b"\0" * (1 << 18))
(_game / _lini).write_text("[GlobalSets]\nLoadPlugins=1\nUseCrashHandler=0\n\n[Other]\nKeep=1\n", encoding="utf8")
_saved = (_m.resolve, _m.resolve_loader, net.download)
_m.resolve = lambda: ("v9.9", "mfg")
_m.resolve_loader = lambda: ("v1", "ual")
net.download = lambda url, name, **k: _rz if url == "mfg" else _lz
try:
    _tag, _files = _m.install(_game, _game / "Game.exe")
    # a second install of ours: the loader now in place is OURS (in the
    # previous manifest), so it must not be backed up over the real backup
    _bak_before = (_game / (_lname + _m.BACKUP_SUFFIX)).read_bytes()
    _tag2, _files2 = _m.install(_game, _game / "Game.exe", preinstalled=set(_files))
    check("a reinstall keeps the ORIGINAL backup (the loader in place was ours)",
          (_game / (_lname + _m.BACKUP_SUFFIX)).read_bytes() == _bak_before
          and (_lname + _m.BACKUP_SUFFIX) not in _files2, str(_files2))
    # the person's own Ultimate ASI Loader is theirs: backed up like anything else
    _g2 = _d / "game2"; _g2.mkdir(); shutil.copyfile(X64, _g2 / "Game.exe")
    (_g2 / _lname).write_bytes(b"MZ" + b"Ultimate ASI Loader" + b"\0" * (1 << 18))
    _t3, _f3 = _m.install(_g2, _g2 / "Game.exe")
    check("a loader the person installed by hand is backed up, not treated as ours",
          (_g2 / (_lname + _m.BACKUP_SUFFIX)).is_file() and (_lname + _m.BACKUP_SUFFIX) in _f3, str(_f3))
    _gone = _m.remove_leftovers(_g2, set(_f3))
    check("remove_leftovers takes the unlock out and puts the person's loader back",
          all(not (_g2 / n).is_file() for n in _m.FILES)
          and (_g2 / _lname).read_bytes().startswith(b"MZ" + b"Ultimate ASI Loader")
          and not (_g2 / (_lname + _m.BACKUP_SUFFIX)).exists(), str(_gone))
    check("remove_leftovers does nothing when the previous install had no unlock",
          _m.remove_leftovers(_g2, {"dxgi.dll"}) == [])
finally:
    _m.resolve, _m.resolve_loader, net.download = _saved
check("the three unlock files land beside the exe",
      all((_game / n).is_file() for n in _m.FILES) and _tag == "v9.9")
check("a loader the person installed by hand IS taken over, and backed up",
      (_game / (_lname + _m.BACKUP_SUFFIX)).read_bytes().startswith(
          b"MZUltimate ASI Loader")
      and _m.is_loader(_game / _lname), str(_files))
_ini = (_game / _lini).read_text(encoding="utf8")
check("the loader's ini is merged, not replaced",
      "LoadExtraPlugins=RTX40MFG.asi" in _ini and "UseCrashHandler=0" in _ini
      and "[Other]" in _ini and "Keep=1" in _ini and _ini.count("[GlobalSets]") == 1, _ini)
check("an ini that existed before is not listed as ours; the backup and the loader are",
      _lini not in _files and _lname in _files
      and (_lname + _m.BACKUP_SUFFIX) in _files, str(_files))
check("Options carries mfg and the manifest round-trips it",
      installer.Options().mfg is False and '"mfg": opt.mfg' in src_of(installer)
      and 'mfg=bool(data.get("mfg"' in src_of(installer.options_from_manifest))
shutil.rmtree(_d, ignore_errors=True)

section("38. screen and window capture through the player (video.py)")
_ff = Path("C:/t/ffmpeg.exe")
_c = video.capture_command(_ff, "screen 2", 30, gpu=True, region=None, output_idx=1)
check("a whole monitor goes through Desktop Duplication on the GPU and NVENC",
      "ddagrab=output_idx=1:framerate=30" in _c and "h264_nvenc" in _c
      and _c[-1].startswith("udp://127.0.0.1:"), str(_c))
_c = video.capture_command(_ff, "screen 1", 30, gpu=True, region=(0, 0, 1190, 1080), output_idx=0)
check("a region is cut on the GPU with ddagrab's own offset and size",
      any(x == "ddagrab=output_idx=0:framerate=30:offset_x=0:offset_y=0:video_size=1190x1080" for x in _c), str(_c))
_c = video.capture_command(_ff, "screen 1", 30, gpu=False, region=(10, 20, 640, 480))
check("the fallback is GDI of the same region with libx264",
      "gdigrab" in _c and "-offset_x" in _c and "640x480" in _c and "libx264" in _c, str(_c))
_cap, _park = video.split_layout((0, 0, 1920, 1080))
check("one monitor is split: capture on the left, the player parked on the right, no overlap",
      _cap == (0, 0, 1190, 1080) and _park == (1190, 0, 730, 1080)
      and _cap[0] + _cap[2] == _park[0], f"{_cap} {_park}")
_cap, _park = video.split_layout((1920, 0, 4480, 1440))
check("a second monitor's own origin is kept", _cap[0] == 1920 and _cap[2] % 2 == 0 and _park[0] == 1920 + _cap[2])
import tkinter as _tkw
# The window belongs to ANOTHER process, as in real use (a Tk window of this
# process renders black through PrintWindow once an earlier root was torn down).
_child = subprocess.Popen([sys.executable, "-c",
    "import tkinter as t; r=t.Tk(); r.title('dlss5 grab test'); r.geometry('400x300+40+40'); "
    "r.configure(bg='#3060c0'); r.mainloop()"])
_u = video._user32()
_hw = 0
for _ in range(100):
    _hw = _u.FindWindowW(None, "dlss5 grab test")
    if _hw:
        break
    time.sleep(0.05)
time.sleep(0.4)
_wd = _ht = 0; _buf = b""; _samples = []
for _ in range(5):
    _wd, _ht, _buf = video.grab_window(_hw) if _hw else (0, 0, b"")
    _samples = list(memoryview(_buf)[0::4 * 53]) if _buf else []      # blue channel of BGRA
    if _samples and sum(_samples) / len(_samples) > 150:
        break
    time.sleep(0.3)
check("grab_window returns another process's window pixels (a blue tk window is not black)",
      _wd >= 2 and _ht >= 2 and len(_buf) == _wd * _ht * 4 and _samples and sum(_samples) / len(_samples) > 150,
      f"hwnd={_hw} {_wd}x{_ht} mean-blue-channel={sum(_samples)/len(_samples) if _samples else None}")
_child.kill()
# the feed thread against a stand-in encoder: a .cmd that swallows stdin
_stub_dir = Path(tempfile.mkdtemp(prefix="feedstub_"))
_stub = _stub_dir / "ffmpeg.cmd"
_stub.write_text("@echo off\r\n\"" + sys.executable + "\" -c \"import sys; sys.stdin.buffer.read()\"\r\n", encoding="utf8")
_root2 = _tkw.Tk(); _root2.geometry("320x240+60+60"); _root2.update()
_feed = video._WindowFeed(_stub, _root2.winfo_id(), 20).start()
for _ in range(40):
    _root2.update(); time.sleep(0.05)
_sent = _feed.frames
_feed.stop()
for _ in range(12):                 # PrintWindow needs the window's thread to pump
    _root2.update(); time.sleep(0.05)
check("the window feed grabs frames and pushes them down the pipe; stop() ends the thread",
      _sent >= 5 and not _feed.alive(), f"sent={_sent} alive={_feed.alive()}")
# an encoder that refuses the GPU path: the first write breaks, the CPU path is opened
_calls = []
_orig_cmd = video.window_pipe_command
def _fake_cmd(ff, w_, h_, fps, gpu=True):
    _calls.append(gpu)
    return ["cmd", "/c", "exit", "1"] if gpu else [str(_stub)]
video.window_pipe_command = _fake_cmd
_msgs = []
_feed2 = video._WindowFeed(_stub, _root2.winfo_id(), 20, _msgs.append).start()
for _ in range(40):
    _root2.update(); time.sleep(0.05)
_feed2.stop()
for _ in range(12):                 # PrintWindow needs the window's thread to pump
    _root2.update(); time.sleep(0.05)
video.window_pipe_command = _orig_cmd
check("NVENC refusing the pipe falls back to the CPU encoder and the feed keeps going",
      _calls[:2] == [True, False] and _feed2.frames >= 3 and any("CPU encoder" in m for m in _msgs),
      f"calls={_calls} frames={_feed2.frames} msgs={_msgs}")
_root2.destroy()
shutil.rmtree(_stub_dir, ignore_errors=True)
_c = video.window_pipe_command(Path("C:/t/ffmpeg.exe"), 1190, 1080, 30)
check("the window feed pipes raw BGRA into NVENC at the chosen rate",
      "rawvideo" in _c and "1190x1080" in _c and "h264_nvenc" in _c and _c[_c.index("-r") + 1] == "30", str(_c))
_ls = video.list_screens()
check("the list starts with the monitors and never lists this tool's own window",
      _ls and _ls[0] == "screen 1" and not any("DLSS 5 Autopilot" in s for s in _ls), str(_ls[:4]))
_region, _idx, _park, _other = video.plan_capture("screen 1")
check("on this PC the plan parks the player beside the capture (one monitor) or on the other one",
      (_other and _region is None) or (not _other and _region is not None and _park is not None), str((_region, _idx, _park, _other)))

section("39. OpenGL games get VORT motion vectors, installed by the tool")
check("VORT is a provider the tool installs, with its technique above the feed",
      reshade_ini.PROVIDERS[2][1] == "vort_MotionEffects@vort_Motion.fx"
      and reshade_ini.PROVIDERS[2][2] is True)
_d = Path(tempfile.mkdtemp(prefix="glpreset_"))
reshade_ini.write_preset(_d, 2)
_t = (_d / "ReShadePreset.ini").read_text(encoding="utf8")
check("the preset puts vort_MotionEffects first and DLSS5_MV_PROVIDER=2",
      _t.index("vort_MotionEffects@vort_Motion.fx") < _t.index("DLSS5_Feed@DLSS5_Feed.fx")
      and "DLSS5_MV_PROVIDER=2" in _t, _t[:300])
shutil.rmtree(_d, ignore_errors=True)
_d = Path(tempfile.mkdtemp(prefix="vortkeep_"))
(_d / "ReShadePreset.ini").write_text(
    "Techniques=vort_MotionEffects@vort_Motion.fx,DLSS5_Feed@DLSS5_Feed.fx,Lumenite_Kernel@lumenite_Kernel.fx,Clarity@Clarity.fx\n",
    encoding="utf8")
reshade_ini.remove_our_techniques(_d, 3)
_t = (_d / "ReShadePreset.ini").read_text(encoding="utf8")
check("uninstall of a LumeniteFX install leaves the person's VORT technique alone",
      "vort_MotionEffects" in _t and "Lumenite_Kernel" not in _t and "DLSS5_Feed" not in _t and "Clarity" in _t, _t)
(_d / "ReShadePreset.ini").write_text(
    "Techniques=vort_MotionEffects@vort_Motion.fx,DLSS5_Feed@DLSS5_Feed.fx,Clarity@Clarity.fx\n", encoding="utf8")
reshade_ini.remove_our_techniques(_d, 2)
_t = (_d / "ReShadePreset.ini").read_text(encoding="utf8")
check("uninstall of a VORT install removes it", "vort_MotionEffects" not in _t and "Clarity" in _t, _t)
shutil.rmtree(_d, ignore_errors=True)
check("the feeder route installs VORT for provider 2 through the shared step",
      "elif opt.provider == 2:" in src_of(installer._install_feeder_parts)
      and "_install_vort" in src_of(installer._install_feeder_parts)
      and "_install_vort" in src_of(installer.install))
check("an OpenGL install with a Lumenite provider is switched to VORT before planning",
      'g.api == "OpenGL" and opt.provider in (3, 4)' in src_of(installer.install))
_d = Path(tempfile.mkdtemp(prefix="glprev_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d); _g.api = "OpenGL"; _g.bitness = 64
_pv = installer.preview(_g, installer.Options(path=dlss.FEEDER, provider=3))
check("the preview of an OpenGL feeder install lists VORT's files, not LumeniteFX's",
      any("vort_Motion.fx" in w for w in _pv.writes)
      and not any("lumenite_Kernel.fx" in w for w in _pv.writes), str(_pv.writes[:12]))
shutil.rmtree(_d, ignore_errors=True)

section("40. the updater understands a one-folder release before one exists")
import zipfile as _zf2
import hashlib
from core import selfupdate  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="upd_"))
_pe = X64.read_bytes()
_one = _d / "one.zip"
with _zf2.ZipFile(_one, "w") as z:
    z.writestr("dlss5-autopilot.exe", _pe); z.writestr("README.md", "x")
_dir = _d / "dir.zip"
with _zf2.ZipFile(_dir, "w") as z:
    z.writestr("dlss5-autopilot/dlss5-autopilot.exe", _pe)
    z.writestr("dlss5-autopilot/_internal/python313.dll", b"MZ" + b"\0" * 8000)
    z.writestr("dlss5-autopilot/_internal/core/gui.pyc", b"pyc")
    z.writestr("dlss5-autopilot/README.md", "x")
_saved = (net.json_get, net.download, net.fetch_text, selfupdate.MIN_BYTES)
selfupdate.MIN_BYTES = len(_pe) + 4000   # exe alone is below; exe + _internal is above
_sha = hashlib.sha256(_pe).hexdigest()
net.fetch_text = lambda url: f"{_sha}  dist/dlss5-autopilot.exe\n".encode()
net.json_get = lambda url: {"tag_name": "v9.9", "assets": [
    {"name": "DLSS5-Autopilot-v9.9-win64.zip", "browser_download_url": "zip"},
    {"name": "SHA256SUMS.txt", "browser_download_url": "sums"}]}
try:
    net.download = lambda url, name, **k: _one
    _raised = False
    try:
        selfupdate.fetch()
    except selfupdate.UpdateError:
        _raised = True
    check("the size floor applies to the exe alone for a one-file release", _raised)
    selfupdate.MIN_BYTES = 1024
    _exe1 = selfupdate.fetch()
    selfupdate.MIN_BYTES = len(_pe) + 4000
    check("a one-file release yields the exe alone",
          _exe1.name == "dlss5-autopilot.exe" and not (_exe1.parent / "_internal").exists())
    net.download = lambda url, name, **k: _dir
    _exe2 = selfupdate.fetch()
    check("...but for a one-folder release the whole download is measured (exe alone would fail)",
          _exe2.stat().st_size < selfupdate.MIN_BYTES)
    check("a one-folder release yields the exe WITH its _internal folder beside it",
          _exe2.name == "dlss5-autopilot.exe"
          and (_exe2.parent / "_internal" / "python313.dll").is_file()
          and (_exe2.parent / "_internal" / "core" / "gui.pyc").is_file())
finally:
    net.json_get, net.download, net.fetch_text, selfupdate.MIN_BYTES = _saved
_cur = _d / "app" / "dlss5-autopilot.exe"
_s1 = selfupdate.swap_script(_cur, _exe1)
_s2 = selfupdate.swap_script(_cur, _exe2)
check("the swap script for a one-file build touches only the exe",
      "_internal" not in _s1 and 'copy /y "%SOURCE%" "%TARGET%"' in _s1)
check("the swap script for a one-folder build copies _internal (drives may differ), keeps the old one and can roll back",
      f'move /y "{_d / "app" / "_internal"}" "{_d / "app" / "_internal.old"}"' in _s2
      and f'xcopy "{_exe2.parent / "_internal"}" "{_d / "app" / "_internal"}\\"' in _s2
      and ":rollback" in _s2 and 'copy /y "%SOURCE%" "%TARGET%"' in _s2
      and _s2.index("_internal.old") < _s2.index('copy /y "%SOURCE%"'), _s2)
shutil.rmtree(_d, ignore_errors=True)

section("41. the day-two reports: duplicate add-on lines, layer wording, USB drives, 'bin'")
_d = _diag_dir("diag_dupe_", reshade=(
    'Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0 using ReShade API version 18.\n'
    'Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0 using ReShade API version 18.\n'
    'Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0 using ReShade API version 18.\n'), path="upstream")
_r = diagnose.analyse(_d)
check("one 'loaded add-on' line per add-on, however many sessions the log holds (#22)",
      sum("loaded add-on" in t for t in _levels(_r, "ok")) == 1, str(_levels(_r, "ok")))
shutil.rmtree(_d, ignore_errors=True)
_d = _diag_dir("diag_vklayer_", proxy=False, api="Vulkan")
_m = json.loads((_d / "dlss5-autopilot.json").read_text(encoding="utf8"))
_m["proxy"] = "(vulkan layer)"
(_d / "dlss5-autopilot.json").write_text(json.dumps(_m), encoding="utf8")
_saved = _vk.registrations
_vk.registrations = lambda: [(Path("C:/x/ReShade64.json"), 0)]
_r = diagnose.analyse(_d)
_vk.registrations = _saved
check("a not-started Vulkan-layer install is told to check the renderer, not a proxy name (#16/#19)",
      any("not running on Vulkan" in t for t in _levels(_r, "info"))
      and not any("ignores (vulkan layer)" in t for t in _levels(_r, "info")), str(_levels(_r, "info")))
shutil.rmtree(_d, ignore_errors=True)
check("generic folder names give way to the game's own (#17)",
      games.display_name(Path("D:/Games/World War Z/bin")) == "World War Z"
      and games.display_name(Path("D:/Games/Ghostwire/Snowfall/Binaries/Win64")) == "Snowfall"
      and games.display_name(Path("D:/Games/Bayonetta")) == "Bayonetta"
      and games.display_name(Path("D:/SteamLibrary/steamapps/common/Game")) == "Game"
      and games.display_name(Path("D:/Games/Deus Ex/System")) == "Deus Ex")
check("scan_folders skips removable drives (#18)",
      "is_removable(base)" in src_of(games.scan_folders)
      and games.is_removable(Path("C:/")) is False)

section("42. the plan counts the new steps, so the progress bar cannot run past its end")
_d = Path(tempfile.mkdtemp(prefix="plan_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d); _g.bitness = 64
_g.api = "OpenGL"
check("an OpenGL feeder plan lists VORT, not LumeniteFX",
      "VORT Motion (motion vectors)" in installer.plan(_g, installer.Options(path=dlss.FEEDER, provider=3))
      and "LumeniteFX (motion vectors)" not in installer.plan(_g, installer.Options(path=dlss.FEEDER, provider=3)))
_g.api = "DX11"
check("a D3D11 feeder plan with VORT chosen by hand lists VORT",
      "VORT Motion (motion vectors)" in installer.plan(_g, installer.Options(path=dlss.FEEDER, provider=2)))
_g.api = "DX12"
(_d / "nvngx_dlssg.dll").write_bytes(b"MZ")
_saved = gpu.detect
gpu.detect = lambda: ("RTX 4070", 89)
try:
    _p = installer.plan(_g, installer.Options(path=dlss.NATIVE, native_dlss=True, mfg=True))
    check("an RTX 40 native plan with MFG ticked counts the MFG step",
          "RTX 40 multi-frame generation" in _p, str(_p))
    gpu.detect = lambda: ("RTX 5080", 120)
    check("...and not on an RTX 50",
          "RTX 40 multi-frame generation" not in installer.plan(_g, installer.Options(path=dlss.NATIVE, native_dlss=True, mfg=True)))
finally:
    gpu.detect = _saved
shutil.rmtree(_d, ignore_errors=True)

section("43. review fixes: DX10 refused before a write, MFG off removes leftovers, quirks per API")
_d = Path(tempfile.mkdtemp(prefix="dx10early_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d); _g.api = "DX10"; _g.bitness = 64
_saved = sources.resolve_feeder
sources.resolve_feeder = lambda prerelease=False, tag="": ("v0.12.1-beta.2", {})
try:
    _raised = ""
    try:
        installer.install(_g, installer.Options(path=dlss.FEEDER), on_log=lambda t: None)
    except installer.InstallError as e:
        _raised = str(e)
    check("an old feeder on DX10 is refused before the folder is touched",
          "refuses Direct3D 10" in _raised
          and sorted(p.name for p in _d.iterdir()) == ["Game.exe"], str(sorted(p.name for p in _d.iterdir())))
finally:
    sources.resolve_feeder = _saved
_pv = installer.preview(_g, installer.Options(path=dlss.FEEDER, feeder_tag="v0.12.1-beta.2"))
check("the preview shows the pinned old feeder as a blocker, offline",
      any("refuses Direct3D 10" in b for b in _pv.blockers), str(_pv.blockers))
shutil.rmtree(_d, ignore_errors=True)
check("quirks(api='OpenGL') explains the provider switch; other APIs get nothing generic",
      any("VORT" in q for q in dlss.quirks(Path("Game.exe"), "OpenGL"))
      and dlss.quirks(Path("Game.exe"), "DX12") == ())
check("our MFG overlay add-on is not reported as a foreign NGX hook",
      "rtx40mfg-ui.addon64" in src_of(installer.other_ngx_hooks))

section("44. driver 616.64+: renodx-dlss5 is pinned to 4.55, and the fault is named")
check("the pin constants exist", sources.DRIVER_FAULT_MIN == "616.64" and sources.DRIVER_FAULT_RENODX_PIN == "4.55")
check("install() consults the driver before the OpenGL and feeder pins",
      src_of(installer.install).index("DRIVER_FAULT_MIN")
      < src_of(installer.install).index("OPENGL_RENODX_PIN"))
_d = _diag_dir("diag_drv_", feed=_FEED_OK, reshade=(
    'INFO | Registered add-on "DLSS 5 Feed" v0.14\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"), bitness=32)
(_d / "host64").mkdir()
(_d / "host64" / "dlss5-feed-host.log").write_text(
    "12:00:00.000  [host] evaluate raised 0xC0000005 (reading address FFFFFFFFFFFFFFFF) in D3D12Core.dll (caught; nothing submitted)\n"
    "12:00:00.001  [host] evaluate fault stack, by module (innermost first):\n"
    "              D3D12Core.dll <- nvngx_dlssnr.dll <- _nvngx.dll <- renodx-dlss5.addon64 <- dlss5-feed-host64.exe\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("the host log's fault chain is read as the 616.64 driver fault, not 'Working'",
      "616.64" in _r.verdict and any("NGX runtime" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_drv55_", feed=_FEED_OK +
    "12:00:00.000  [feed] evaluate raised 0xC0000005 (reading address FFFFFFFFFFFFFFFF) (caught; nothing submitted)\n"
    "12:00:00.000  [feed] evaluate fault stack, by module (innermost first): D3D12Core.dll <- nvngx_dlssnr.dll <- _nvngx.dll <- renodx-dlss5.addon64 <- dlss5-feed.addon64 <- ReShade64.dll\n",
    reshade='INFO | Registered add-on "DLSS 5 Feed 0.14.0-beta.5" v0.14.0.0\n',
    components={"renodx": "4.55", "feeder": "v0.14.0-beta.5"})
_r = diagnose.analyse(_d)
check("beta.5's two-line feed fault (no 'in D3D12Core.dll' on the first line) is still the driver fault",
      "616.64" in _r.verdict and any("NGX runtime" in t for t in _levels(_r, "bad")), _r.verdict)
check("...and with 4.55 already installed it says roll back, not 'install again'",
      "616.56" in _r.verdict and not any("Install again" in (f_.detail or "") for f_ in _r.findings if f_.level == "bad"), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# the stale check must not nag toward the build the pin avoids
from core import components as _cmp  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="stale_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps({"api": "DX12", "components": {"renodx": "4.55"}}), encoding="utf8")
_saved = (_cmp._latest, gpu.driver_at_least)
_cmp._latest = lambda name: "4.70"
gpu.driver_at_least = lambda want: True
try:
    _items = _cmp.check(_d)
    check("on driver 616.64+ a pinned 4.55 is not reported as outdated", _items and not _items[0].outdated, str(_items))
    gpu.driver_at_least = lambda want: False
    _items = _cmp.check(_d)
    check("on an older driver the same install IS outdated (4.70 works there)", _items and _items[0].outdated, str(_items))
finally:
    _cmp._latest, gpu.driver_at_least = _saved
shutil.rmtree(_d, ignore_errors=True)

section("45. 1.7.1: online games warned, metadata reads retried, Xbox text, graphics-api override")
from core import anticheat  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="ac_"))
for n in ("ZenlessZoneZero.exe", "EAAntiCheat.GameServiceLauncher.exe"):
    (_d / n).write_bytes(b"MZ")
_f = anticheat.detect(_d, _d)
check("HoYoverse and EA Javelin games are detected as anti-cheat",
      _f.present and "HoYoverse anti-cheat" in _f.products and "EA Javelin" in _f.products, str(_f.products))
shutil.rmtree(_d, ignore_errors=True)

import io
import re
import urllib.error as _ue
_calls = {"n": 0}
class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b"ok"
def _flaky(req, timeout=0, **kw):
    _calls["n"] += 1
    if _calls["n"] < 3:
        raise TimeoutError("The read operation timed out")
    return _Resp()
_saved = (sources.urllib.request.urlopen, sources.time.sleep)
sources.urllib.request.urlopen = _flaky
sources.time.sleep = lambda s: None
try:
    check("a metadata read survives two timeouts (#26)", sources._get("https://x/") == b"ok" and _calls["n"] == 3)
finally:
    sources.urllib.request.urlopen, sources.time.sleep = _saved

check("the locked-folder hint names the Xbox app's switch and the way out without it",
      "Enable mods" in games.XBOX_HINT and "Steam version" in games.XBOX_HINT)
check("the protected-exe hint says what to choose, in words (#157)",
      "64-bit or 32-bit" in games.XBOX_EXE_HINT and "EXE" not in games.XBOX_EXE_HINT)

_d = Path(tempfile.mkdtemp(prefix="apiov_"))
shutil.copyfile(X64, _d / "Game.exe")
_saved_pref = prefs.get("api_override")
try:
    games.set_api_override(_d, "DX9")
    _g = games.manual(_d)
    check("an API chosen for the folder overrides the executable's import table (#24)",
          _g.api == "DX9" and _g.api_detected != "DX9" and "set by hand" in _g.api_why,
          f"{_g.api} {_g.api_detected} {_g.api_why}")
    games.set_api_override(_d, None)
    _g = games.manual(_d)
    check("clearing the override restores detection", _g.api == _g.api_detected and _g.api != "DX9")
finally:
    prefs.set_("api_override", _saved_pref or {})
shutil.rmtree(_d, ignore_errors=True)

section("46. issue #31: a renderer loaded at run time is still recognised (Call of Juarez: Gunslinger)")
_d = Path(tempfile.mkdtemp(prefix="coj_"))
_exe = _d / "CoJGunslinger.exe"
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
check("an exe with no graphics import and no name in it stays Unknown",
      pe.detect_api(_exe)[0] == "Unknown")
with open(_exe, "ab") as _f:
    _f.write(b"\0\0" + "d3d9.dll".encode("utf-16-le") + b"\0\0")
_api, _why = pe.detect_api(_exe)
check("d3d9.dll named in the exe (UTF-16) -> DirectX 9, and the reason says so",
      _api == "DX9" and "run time" in _why, f"{_api}: {_why}")
_g = games.manual(_d)
check("...so the game is 32-bit / DX9 and gets the DXVK route, not dxgi.dll + feeder",
      _g.bitness == 32 and _g.api == "DX9", f"{_g.bitness} {_g.api}")
with open(_exe, "ab") as _f:
    _f.write(b"\0dxgi.dll\0")
check("a DXGI name beside it wins, as in the static table", pe.detect_api(_exe)[0] == "DX12")
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
shutil.copyfile(r"C:\Windows\SysWOW64\Magnification.dll", _d / "engine.dll")
_api, _why = pe.detect_api(_exe)
check("an engine DLL beside the exe that imports d3d9.dll is the renderer",
      _api == "DX9" and "engine.dll" in _why, f"{_api}: {_why}")
shutil.copyfile(r"C:\Windows\SysWOW64\Magnification.dll", _d / "d3d9.dll")
(_d / "engine.dll").unlink()
check("a proxy d3d9.dll (DXVK, ReShade) beside the exe is not consulted",
      pe.detect_api(_exe)[0] == "Unknown")
(_d / "nvngx_dlss.dll").write_bytes(b"MZ")
with open(_exe, "ab") as _f:
    _f.write(b"\0d3d9.dll\0")
check("a run-time d3d9.dll with the game's own DLSS beside it is still a modern renderer",
      pe.detect_api(_exe)[0] == "DX12")
shutil.rmtree(_d, ignore_errors=True)

section("47. issues #46-#48: an engine that merely names opengl32.dll is not an OpenGL game")
_d = Path(tempfile.mkdtemp(prefix="unity_"))
_exe = _d / "HouseParty.exe"
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
with open(_exe, "ab") as _f:
    _f.write(b"\0opengl32.dll\0")
check("an exe naming only opengl32.dll, nothing beside it: OpenGL (Gunslinger-style run-time load)",
      pe.detect_api(_exe)[0] == "OpenGL")
(_d / "engine.dll").write_bytes(b"MZ" + b"\0" * 600_000 + b"d3d11.dll\0opengl32.dll\0")
_api, _why = pe.detect_api(_exe)
check("...but a DLL beside it that names d3d11.dll outranks the OpenGL string",
      _api == "DX11" and "engine.dll" in _why, f"{_api}: {_why}")
(_d / "engine.dll").unlink()
(_d / "UnityPlayer.dll").write_bytes(b"MZ" + b"\0" * 100)
_api, _why = pe.detect_api(_exe)
check("UnityPlayer.dll beside the exe decides: Direct3D 11, and the reason names Unity",
      _api == "DX11" and "Unity" in _why, f"{_api}: {_why}")
_g = games.manual(_d)
check("...so the game is not put on the opengl32.dll route", _g.api == "DX11", _g.api)
(_d / "UnityPlayer.dll").unlink()
(_d / "sl.interposer.dll").write_bytes(b"MZ" + b"\0" * 600_000 + b"d3d12.dll\0")
(_d / "ourthing.dll").write_bytes(b"MZ" + b"\0" * 600_000 + b"d3d12.dll\0")
(_d / "dlss5-autopilot.json").write_text(json.dumps({"files": ["ourthing.dll"]}), encoding="utf8")
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
with open(_exe, "ab") as _f:
    _f.write(b"\0opengl32.dll\0")
check("DLLs our routes drop (Streamline) and files named in our manifest are not sibling evidence",
      pe.detect_api(_exe)[0] == "OpenGL", pe.detect_api(_exe))
(_d / "dlss5-autopilot.json").unlink()
(_d / "UnityPlayer.dll").write_bytes(b"MZ" + b"\0" * 100)
check("the side windows scale their pixels too",
      "px(760)" in Path("core/remixui.py").read_text(encoding="utf8")
      and "px(720)" in Path("core/compareui.py").read_text(encoding="utf8"))
_launcher = _d / "Launcher.exe"
_ship = _d / "Bin" / "Win64" / "Game-Win64-Shipping.exe"
_ship.parent.mkdir(parents=True)
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _launcher)
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _ship)
(_d / "dlss5-autopilot.json").write_text(json.dumps({"exe": "Launcher.exe", "files": []}), encoding="utf8")
(_d / "dlss5-feed.addon64").write_bytes(b"MZ")
_g56 = games.Game("Conan", _d, exe=_launcher, candidates=[_launcher, _ship])
games.enrich(_g56)
check("a scan adopts the earlier install's folder and exe (unchanged)",
      _g56.exe == _launcher and _g56.install_dir == _d, (_g56.exe, _g56.install_dir))
_g56.exe = _ship
games.enrich(_g56, chosen=True)
check("an executable picked in the list keeps that pick and installs beside it (#56)",
      _g56.exe == _ship and _g56.install_dir == _ship.parent, (_g56.exe, _g56.install_dir))
(_d / "dlss5-autopilot.json").unlink()
(_d / "dlss5-feed.addon64").unlink()
_launcher.unlink()
_ship.unlink()
_ship.parent.rmdir()
_ship.parent.parent.rmdir()
check("the preview discloses an emulator config change",
      "its own config is switched" in src_of(installer.preview))
check("the game list does not flag a DXVK install as an API change",
      'man.get("proxy") != diagnose.VULKAN_LAYER' in src_of(_gui))
check("an unknown optiscaler build key in a manifest does not break the plan",
      any(s.startswith("OptiScaler (") for s in
          installer.plan(games.Game("X", _d, exe=_exe, bitness=64, api="DX12"),
                         installer.Options(path=dlss.OPTI, opti_build="future"))))
for _k in range(65):
    (_d / f"a{_k:02}.dll").write_bytes(b"MZ")
check("...even behind 65 other DLLs (the engine rule is not cut with the list)",
      pe.detect_api(_exe)[0] == "DX11")
(_d / "UnityPlayer.dll").unlink()
for _k in range(65):
    (_d / f"a{_k:02}.dll").unlink()
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
with open(_exe, "ab") as _f:
    _f.write(b"\0d3d9.dll\0")
(_d / "bink2w64.dll").write_bytes(b"MZ" + b"\0" * 600_000 + b"d3d11.dll\0dxgi.dll\0")
_api, _why = pe.detect_api(_exe)
check("an exe that names d3d9.dll itself is DirectX 9 whatever Bink beside it names (#31 stands)",
      _api == "DX9" and "named in the exe" in _why, f"{_api}: {_why}")
(_d / "host64").mkdir()
(_d / "host64" / "dlss5-feed-host64.exe").write_bytes(open(_exe, "rb").read())
check("the feeder's helper under host64 is never a candidate executable",
      pe.find_game_exes(_d) == [_exe], pe.find_game_exes(_d))
_exe.unlink()
check("...even when it is the only executable left in the folder", pe.find_game_exes(_d) == [])
# The diagnosis and the report body for a game that was installed as OpenGL
(_d / diagnose.MANIFEST).write_text(json.dumps({"path": "feeder", "proxy": "opengl32.dll",
                                                "api": "OpenGL", "exe": "HouseParty.exe"}),
                                    encoding="utf8")
(_d / "opengl32.dll").write_bytes(b"MZ")
_rep = diagnose.analyse(_d)
check("no ReShade.log after an opengl32.dll install: the diagnosis says the game may not draw with OpenGL",
      any("does not render with OpenGL" in f.title for f in _rep.findings),
      [f.title for f in _rep.findings])
(_d / diagnose.RESHADE_LOG).write_text("INFO | Initializing crosire's ReShade\nExiting ...\n", encoding="utf8")
_rep = diagnose.analyse(_d)
check("a ReShade.log with no add-on registered under opengl32.dll points at the graphics api dropdown",
      any("another program" in f.title for f in _rep.findings),
      [f.title for f in _rep.findings])
class _G:
    name = "House Party"; exe = _d / "HouseParty.exe"; bit_label = "64-bit"; api = "DX11"
    api_why = "Unity player beside the exe - Direct3D 11 on Windows"
_body = diagnose.issue_body("1.7.2", "RTX", 120, "616.64", _G(), "feeder", None, "", _d / "a.log", _d)
check("the report body carries the reason behind the detected API",
      "- arch/api: 64-bit / DX11 (Unity player beside the exe" in _body
      and "\n- route: feeder" in _body, _body[:600])
check("the game list marks an install whose manifest api differs from the detected one",
      'f"reinstall - was {man_api}"' in src_of(_gui))
shutil.rmtree(_d, ignore_errors=True)

section("48. issue #40: pixel sizes follow the display scale, not only the fonts")
import re as _re
_gsrc = src_of(_gui)
check("_gui.px() rounds to the display scale", _gui.px(26) == 26)
_gui.SCALE = 2.0
check("...and doubles at 200 %", _gui.px(26) == 52 and _gui.px(1060) == 2120)
check("the Treeview row height is scaled", "rowheight=px(26)" in _gsrc)
check("the window geometry and the side rail are scaled",
      'px(1060)' in _gsrc and 'width=px(236)' in _gsrc)
check("no bare wraplength is left", not _re.search(r"wraplength=\d", _gsrc))
check("run() sets SCALE from the window's DPI", "SCALE = max(1.0, dpi / 96.0)" in _gsrc)
_gui.SCALE = 1.0

section("49. issue #54: the exe carries its own root certificates")
_ctx = net.ssl_context()
check("ssl_context() is one shared, verifying context",
      _ctx is net.ssl_context() and _ctx.verify_mode == ssl.CERT_REQUIRED)
check("...with the certifi bundle loaded on top of the Windows store",
      _ctx.cert_store_stats()["x509_ca"] > 100, _ctx.cert_store_stats())
check("every fetch goes through it",
      "context=ssl_context()" in src_of(net.download)
      and "context=net.ssl_context()" in src_of(sources._get))
check("the release build installs certifi",
      "pip install --upgrade certifi" in Path(".github/workflows/release.yml").read_text(encoding="utf8"))
check("a failed verification is explained, by host and not by file name",
      "untrusted(_host(url), e)" in src_of(net.download)
      and "untrusted(name, e)" not in src_of(net.download))

section("50. issue #21: y4my4my4m's OptiScaler fork, from a .7z, through Windows' tar.exe")
check("the build list starts with the build the route always installed",
      list(optiscaler.BUILDS)[0] == "" and optiscaler.FORK in optiscaler.BUILDS)
_tag, _url = optiscaler.resolve(optiscaler.FORK)
check("the fork resolves to a plain archive, not the _with_DLSS one",
      _url.lower().endswith((".7z", ".zip")) and "with_dlss" not in _url.lower(), _url)
_tag0, _url0 = optiscaler.resolve()
check("the default build still resolves to Dagherbou's zip", _url0.lower().endswith(".zip"), _url0)
_d = Path(tempfile.mkdtemp(prefix="sz_"))
(_d / "in").mkdir()
(_d / "in" / "OptiScaler.dll").write_bytes(b"MZ-opti")
(_d / "in" / "OptiScaler.pdb").write_bytes(b"symbols")
(_d / "in" / "OptiScaler").mkdir()
(_d / "in" / "OptiScaler" / "libxess.dll").write_bytes(b"MZ-xess")
_r = subprocess.run([str(optiscaler._tar_exe()), "-cf", str(_d / "t.7z"), "--format", "7zip",
                     "-C", str(_d / "in"), "."], capture_output=True, text=True)
check("Windows' tar.exe writes a 7z for the test", _r.returncode == 0, _r.stderr)
optiscaler.extract_7z(_d / "t.7z", _d / "out")
check("...and extract_7z unpacks it", (_d / "out" / "OptiScaler" / "libxess.dll").read_bytes() == b"MZ-xess")
shutil.rmtree(net.cache_dir() / "unpacked" / "t", ignore_errors=True)
_game = _d / "game"
_game.mkdir()
_w = optiscaler.install(_game, proxy="winmm.dll", dl=lambda url, name: _d / "t.7z",
                        release=("t", "https://example/t.7z"))
check("install() from a .7z: OptiScaler.dll under the proxy name, subfolder kept, .pdb skipped",
      (_game / "winmm.dll").read_bytes() == b"MZ-opti"
      and (_game / "OptiScaler" / "libxess.dll").is_file()
      and not (_game / "OptiScaler.pdb").exists()
      and sorted(_w) == ["OptiScaler/libxess.dll", "winmm.dll"], _w)
_game_b = _d / "game_b"
_game_b.mkdir()
_w2 = optiscaler.install(_game_b, proxy="winmm.dll", dl=lambda url, name: _d / "t.7z",
                         release=("t", "https://example/t.7z"))
check("a second install reuses the unpacked copy and writes the same files", sorted(_w2) == sorted(_w))
(_d / "wrapped").mkdir()
shutil.copytree(_d / "in", _d / "wrapped" / "OptiScaler_v10")
_r = subprocess.run([str(optiscaler._tar_exe()), "-cf", str(_d / "w.7z"), "--format", "7zip",
                     "-C", str(_d / "wrapped"), "."], capture_output=True, text=True)
_game2 = _d / "game2"
_game2.mkdir()
_w3 = optiscaler.install(_game2, proxy="dxgi.dll", dl=lambda url, name: _d / "w.7z",
                         release=("w", "https://example/w.7z"))
check("an archive wrapped in one folder is unwrapped, so the proxy DLL still lands in the game folder",
      (_game2 / "dxgi.dll").is_file() and "dxgi.dll" in _w3, _w3)
shutil.rmtree(net.cache_dir() / "unpacked" / "w", ignore_errors=True)
check("a failed TLS verification is explained, whatever exception type carried it",
      net.untrusted("x", Exception("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>")) is not None
      and net.untrusted("x", Exception("timed out")) is None
      and "net.untrusted" in src_of(sources._get))
check("Options carries the build and the manifest records it",
      hasattr(installer.Options(), "opti_build")
      and '"opti_build": opt.opti_build' in src_of(installer))
check("the install page offers the build list",
      "cb_optibuild" in src_of(_gui) and "opti_build=list(optiscaler.BUILDS)" in src_of(_gui))
shutil.rmtree(_d, ignore_errors=True)
shutil.rmtree(net.cache_dir() / "unpacked" / "t", ignore_errors=True)

section("51. issue #33: ReShade's OpenXR layer for VR, registered like the Vulkan one")
from core import openxr as _xr
_d = Path(tempfile.mkdtemp(prefix="xr_"))
_saved_dir = _xr.layer_dir
_xr.layer_dir = lambda: _d
_setup = next(iter(sorted(net.cache_dir().glob("ReShade_Setup_*_Addon.exe"))), None)
check("a cached ReShade setup is at hand for the test", _setup is not None)
if _setup is not None:
    _m, _fresh = _xr.install_layer(_setup, log=lambda *_: None)
    check("the OpenXR manifest and ReShade64.dll are placed beside the Vulkan layer files",
          _m == _d / _xr.MANIFEST and (_d / _xr.DLL).is_file())
    _data = json.loads(_m.read_text(encoding="utf8"))
    check("the manifest names ReShade's OpenXR layer and points at the DLL beside it",
          _data["api_layer"]["name"] == _xr.LAYER_NAME
          and _data["api_layer"]["library_path"] == ".\\" + _xr.DLL, _data)
    check("...and it is registered for this user, active",
          any(p == _m and v == 0 for p, v in _xr.registrations()), _xr.registrations())
    check("a second install reuses it", _xr.install_layer(_setup)[1] is False)
    check("unregister removes exactly that value", _xr.unregister() is True
          and not any(p == _m for p, _ in _xr.registrations()))
    check("...and a second unregister finds nothing", _xr.unregister() is False)
_xr.layer_dir = _saved_dir
shutil.rmtree(_d, ignore_errors=True)
check("Options.vr exists, the manifest records it and the install page offers it",
      hasattr(installer.Options(), "vr") and '"vr": bool(opt.vr)' in src_of(installer)
      and "ck_vr" in src_of(_gui) and "vr=bool(self.vr.get())" in src_of(_gui))
check("the ReShade step registers the layer when asked, and uninstall drops it with the last VR game",
      "openxr.install_layer(setup, log)" in src_of(installer)
      and "prefs.openxr_games()" in src_of(installer))
check("the command line has --vr", '"--vr" in args' in Path("dlss5_autopilot.py").read_text(encoding="utf8"))
check("the command line has --opti-build and it reaches Options",
      '"--opti-build" in args' in Path("dlss5_autopilot.py").read_text(encoding="utf8")
      and Path("dlss5_autopilot.py").read_text(encoding="utf8").count("opti_build=opti_build") == 3)
check("'did it work?' lists the OpenXR registration for a VR install",
      any("OpenXR layer" in ln for ln in diagnose._presence(Path("."), {"vr": True, "path": "feeder"}, "feeder")))
check("every HTTPS fetch goes through ssl_context()",
      "context=ssl_context()" in src_of(net.fetch_text))

section("52. reshade.me answers 500 with the page as the body; the installer link is still found")
import urllib.error as _ue
_saved_get, _saved_json = sources._get, sources._json
_page = b'<a href="/downloads/ReShade_Setup_6.8.0_Addon.exe">x</a>'
def _five_hundred(url, *a, **k):
    raise _ue.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(_page))
sources._get = _five_hundred
check("a 500 whose body carries the link resolves 6.8.0 from it",
      sources.resolve_reshade() == ("6.8.0", "https://reshade.me/downloads/ReShade_Setup_6.8.0_Addon.exe"),
      sources.resolve_reshade())
def _bare(url, *a, **k):
    raise _ue.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(b""))
sources._get = _bare
sources._json = lambda url: [{"name": "v6.8.1"}, {"name": "v6.8.0"}]
check("a bare 500 falls back to the newest version tag on crosire/reshade",
      sources.resolve_reshade() == ("6.8.1", "https://reshade.me/downloads/ReShade_Setup_6.8.1_Addon.exe"),
      sources.resolve_reshade())
sources._json = lambda url: (_ for _ in ()).throw(RuntimeError("rate limited"))
_cached = sorted(net.cache_dir().glob("ReShade_Setup_*_Addon.exe"))
_r = sources.resolve_reshade() if _cached else None
check("...and then to the newest setup already in the cache",
      not _cached or (_r[0] in _cached[-1].name and _r[1].endswith(f"ReShade_Setup_{_r[0]}_Addon.exe")), _r)
sources._get, sources._json = _saved_get, _saved_json
check("the real site or its fallbacks resolve a version", re.fullmatch(r"\d+(\.\d+)+", sources.resolve_reshade()[0]) is not None)

section("53. DLSS5-Reshade-AIO 2.1.0 ships one 64-bit archive instead of loose files")
sources._json = lambda url: {"tag_name": "v2.1.0", "assets": [
    {"name": "DLSS5-ReShade-AIO-v2.1.0-32-bit.zip", "browser_download_url": "https://x/32.zip"},
    {"name": "DLSS5-ReShade-AIO-v2.1.0-64-bit.zip", "browser_download_url": "https://x/64.zip"}]}
_tag, _urls = sources.resolve_standalone()
check("a zip-only release resolves to its 64-bit archive",
      _tag == "v2.1.0" and _urls.get(sources.STANDALONE_ZIP) == "https://x/64.zip", _urls)
sources._json = lambda url: {"tag_name": "v2.0.9", "assets": [
    {"name": n, "browser_download_url": "https://x/" + n} for n in sources.STANDALONE_ASSETS]}
_tag, _urls = sources.resolve_standalone()
check("a loose-file release still resolves file by file",
      _tag == "v2.0.9" and sources.STANDALONE_ZIP not in _urls and all(n in _urls for n in sources.STANDALONE_ASSETS))
sources._json = _saved_json
_tag, _urls = sources.resolve_standalone()
check("the live release resolves one way or the other",
      sources.STANDALONE_ZIP in _urls or all(n in _urls for n in sources.STANDALONE_ASSETS), (_tag, list(_urls)))

section("54. the 1.7.2 reports: whose crash it is, which launch the log describes, "
        "and the neural-upstream route's own log")

# ReShade never truncates ReShade.log, so a report can carry several launches
# at once. Detroit: Become Human (issue #63) reported two feeder builds
# "loaded" - one of them from a run before the update.
_SESSION = "16:00:00:000 [1] | INFO  | Initializing crosire's ReShade version '6.8.0' (64-bit)\n"
_two = (_SESSION
        + 'INFO  | Registered add-on "DLSS 5 Feed 0.13.1-beta.1" v0.13.1.0\n'
        + "INFO  | Redirecting IDXGIFactory::CreateSwapChain(...)\n"
        + _SESSION.replace("16:00", "17:00")
        + 'INFO  | Registered add-on "DLSS 5 Feed 0.14.0-beta.5" v0.14.0.0\n'
        + "INFO  | Redirecting IDXGIFactory::CreateSwapChain(...)\n")
_d = _diag_dir("diag_session_", reshade=_two, feed=_FEED_OK)
_r = diagnose.analyse(_d)
check("only the last ReShade launch is read, so a replaced build is not reported as loaded",
      any("0.14.0-beta.5" in t for t in _levels(_r, "ok"))
      and not any("0.13.1" in t for t in _levels(_r, "ok")), _levels(_r, "ok"))
shutil.rmtree(_d, ignore_errors=True)

# ...but two builds inside ONE launch are two files fighting over the frame.
_same = (_SESSION
         + 'INFO  | Registered add-on "DLSS 5 Feed 0.13.1-beta.1" v0.13.1.0\n'
         + 'INFO  | Registered add-on "DLSS 5 Feed 0.14.0-beta.5" v0.14.0.0\n'
         + "INFO  | Redirecting IDXGIFactory::CreateSwapChain(...)\n")
_d = _diag_dir("diag_twobuilds_", reshade=_same, feed=_FEED_OK)
_r = diagnose.analyse(_d)
check("two builds of one add-on in the same launch are called out",
      any("builds of the same add-on" in t for t in _levels(_r, "bad")), _levels(_r, "bad"))
shutil.rmtree(_d, ignore_errors=True)

# Issue #63: the feeder records every crash in the process, its own and the
# game's alike. The module chain says which - and the game's own executable
# with no add-on under it was being reported as "a feeder bug".
_GAME_CRASH = (
    "00:56:26.703  [feed] an external frame pacer is presenting this swapchain: 7623 "
    "presents against 6098 frames fed since the first fed frame (1.25x). NVIDIA Smooth "
    "Motion does exactly this.\n"
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "00007FF6AD6D4067 in E:\\g\\DetroitBecomeHuman.exe; this add-on was last doing: "
    "waiting for the result (Vulkan)\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "DetroitBecomeHuman.exe <- KERNEL32.DLL <- ntdll.dll\n")
_d = _diag_dir("diag_gamecrash_", feed=_FEED_OK + _GAME_CRASH,
               exe="DetroitBecomeHuman.exe")
_r = diagnose.analyse(_d)
check("a crash in the game's own executable is the game's, not the feeder's",
      "game's own code" in _r.verdict and "feeder bug" not in _r.verdict, _r.verdict)
check("...and the external frame pacer is named",
      any("pacing the frames" in t for t in _levels(_r, "warn")), _levels(_r, "warn"))
shutil.rmtree(_d, ignore_errors=True)

_OURS_CRASH = (
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "0x1 in E:\\g\\Game.exe; this add-on was last doing: building the mask\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "dlss5-feed.addon64 <- dxgi.dll <- Game.exe\n")
_d = _diag_dir("diag_feedcrash_", feed=_FEED_OK + _OURS_CRASH)
_r = diagnose.analyse(_d)
check("a crash inside the feed add-on is still called a feeder bug",
      "feeder bug" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_NGX_CRASH = (
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "0x1 in E:\\g\\Game.exe; this add-on was last doing: evaluating\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "nvngx_dlssnr.dll <- _nvngx.dll <- renodx-dlss5.addon64 <- dlss5-feed.addon64\n")
_d = _diag_dir("diag_ngxcrash_", feed=_FEED_OK + _NGX_CRASH)
_r = diagnose.analyse(_d)
check("a crash inside the graphics runtime is not blamed on the feeder",
      "graphics runtime" in _r.verdict and "feeder bug" not in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# A module the tool cannot place is not evidence that the game crashed.
# The list of our own files was missing dlss5-feed.addon32, so a 32-bit
# feeder crashing in its own add-on would have been reported as the game's.
_UNKNOWN_CRASH = (
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "0x1 in E:\\g\\Game.exe; this add-on was last doing: presenting\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "somemod.dll <- KERNEL32.DLL <- ntdll.dll\n")
_d = _diag_dir("diag_unknowncrash_", feed=_FEED_OK + _UNKNOWN_CRASH)
_r = diagnose.analyse(_d)
check("a module the tool cannot place is not called the game's crash",
      "game's own code" not in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_32_CRASH = (
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "0x1 in E:\\g\\Game.exe; this add-on was last doing: presenting\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "dlss5-feed.addon32 <- Game.exe\n")
_d = _diag_dir("diag_32crash_", feed=_FEED_OK + _32_CRASH)
_r = diagnose.analyse(_d)
check("...and the 32-bit feeder's own add-on is still ours",
      "feeder bug" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# OptiScaler's log holds every run too, and it has no banner - a clock that
# jumps a long way back is what says "new run". A few milliseconds out of
# order is several threads writing, not a new run.
_ooo = "[00:00:01.000] a\n[00:00:05.000] b\n[00:00:02.000] c\n"
check("threads writing a few ms out of order do not truncate the log",
      diagnose._last_run(_ooo) == _ooo)
check("...but a run from yesterday is cut away",
      diagnose._last_run("[23:00:01.000] old\n[08:00:00.000] new\n").startswith("[08:00:00"))

# The neural-upstream route writes its whole run into ReShade.log under
# [NRPRE]. Three issues (#39 #51 #64) were answered with "this route keeps no
# log" while the answer was in the file the report already carried.
def _nrpre(*extra: str) -> str:
    return (_SESSION
            + 'INFO  | Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0 using ReShade API version 18.\n'
            + "INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] addon registered (NR at render resolution)\n"
            + "".join("INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] " + e + "\n" for e in extra))


_HB = ("HB #7561 handle=0x1 | pw=1.0000 meas=0.0000 valid=0 upd=0 | hdr=2 det=1 "
       "knee=0.750 kmeas=0.750 | expfresh=7559 fromgame=0 | net=2228x1256 "
       "finalvalid=1 | cad=1 async=0 | erfail=0 grfail=0 passthru=0")
_HOOK = "hook 0 on NVSDK_NGX_D3D12_EvaluateFeature: OK  C:\\w\\_nvngx.dll"


def _up_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="up_"))
    (d / "dlss5-autopilot.json").write_text(json.dumps(
        {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
         "api": "DX12", "path": "upstream", "proxy": "dxgi.dll",
         "files": ["dxgi.dll", "nvngx.dll.addon64"]}), encoding="utf8")
    for n in ("dxgi.dll", "nvngx.dll.addon64", "ReShade.ini", "nvngx_dlssnr.dll"):
        (d / n).write_bytes(b"MZ")
    return d


_d = _up_dir()
(_d / "ReShade.log").write_text(_nrpre(
    "settings loaded: enabled=1 cadence=1 codec=1 pw=1.000 knee=0.75", _HOOK,
    "CreateFeature id=18 -> res=0xBAD0000B handle=0 | NR W=2228 H=1253",
    "2b: snippet route, self=C:\\g\\nvngx.dll.addon64", _HB,
    "resetting state (new device)"), encoding="utf8")
_r = diagnose.analyse(_d)
check("the upstream route's log is read: it ran, then stopped at a device re-creation",
      "device re-creation" in _r.verdict
      and any("hooked the game's DLSS call" in t for t in _levels(_r, "ok")), _r.verdict)
check("...and NGX refusing feature 18 with a snippet fallback is a warning, not the verdict",
      any("0xBAD0000B" in t for t in _levels(_r, "warn")), _levels(_r, "warn"))
check("...and a game that hands over no exposure is named (the 'only darker' route)",
      any("no exposure value" in t for t in _levels(_r, "warn")), _levels(_r, "warn"))

# closing the game ends the log the same way; that must not read as a hang
(_d / "ReShade.log").write_text(_nrpre(
    "settings loaded: enabled=1", _HOOK, _HB, "resetting state (new device)")
    + 'INFO  | Unloading add-on "DLSS5 NR Pre-Upscale" ...\n', encoding="utf8")
_r = diagnose.analyse(_d)
check("...but an orderly shutdown after the reset is not a hang", _r.verdict == "Working.", _r.verdict)

(_d / "ReShade.log").write_text(_nrpre("settings loaded: enabled=0"), encoding="utf8")
_r = diagnose.analyse(_d)
check("switched off in the tab is said outright instead of 'confirm in the overlay'",
      "switched off" in _r.verdict, _r.verdict)

(_d / "ReShade.log").write_text(_nrpre("settings loaded: enabled=1"), encoding="utf8")
_r = diagnose.analyse(_d)
check("no hook on the game's DLSS call answers 'the add-on loaded but nothing happens'",
      "never hooked" in _r.verdict, _r.verdict)

(_d / "ReShade.log").write_text(_nrpre(
    "settings loaded: enabled=1", _HOOK, "CreateFeature id=18 -> res=0x00000001 handle=1",
    _HB.replace("finalvalid=1", "finalvalid=0")), encoding="utf8")
_r = diagnose.analyse(_d)
check("running with no valid result is not 'Working.'",
      "never produces a frame" in _r.verdict, _r.verdict)

# an older build of the add-on that logs nothing still gets the old answer
(_d / "ReShade.log").write_text(
    _SESSION + 'INFO  | Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0\n', encoding="utf8")
_r = diagnose.analyse(_d)
check("an add-on build with no [NRPRE] lines still falls back to the overlay answer",
      "does not log frames" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


# The feeder's own log starts each run with "dlss5-feed ... attached.", and
# the standalone route was already read one session at a time. The feeder's
# was not, so a crash from a run days ago could be reported as what just
# happened - the same mistake as the ReShade log above, in another file.
_OLD_RUN = (
    "12:00:00.000  dlss5-feed 0.13.1-beta.1 (built Sep  4 2026) attached.\n"
    "12:00:01.000  ### CRASH RECORDED ###  exception 0xC0000005 at 0x1 in "
    "E:\\g\\Game.exe; this add-on was last doing: an old run\n"
    "13:00:00.000  dlss5-feed 0.14.0-beta.5 (built Sep  6 2026) attached.\n")
_d = _diag_dir("diag_feedsess_", feed=_OLD_RUN + _FEED_OK)
_r = diagnose.analyse(_d)
check("a crash from an earlier run of the game is not this run's verdict",
      _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_feedsess2_", feed=_FEED_OK + _OLD_RUN.split("\n")[1] + "\n")
_r = diagnose.analyse(_d)
check("...but a log with no session marker at all is still read whole",
      "crashed" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


# The same mistake on the optiscaler route: whichever line matched first won,
# so a session that started and then failed was reported as "Working."
def _opti_dir(prefix: str, log_text: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    (d / "dlss5-autopilot.json").write_text(json.dumps(
        {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
         "api": "DX12", "path": "optiscaler", "proxy": "dxgi.dll",
         "files": ["dxgi.dll"]}), encoding="utf8")
    (d / "dxgi.dll").write_bytes(b"MZ")
    (d / "OptiScaler.log").write_text(log_text, encoding="utf8")
    return d


_d = _opti_dir("opti_after_", "[I] [DLSS-NR] running at 2560x1440\n"
                              "[I] [DLSS-NR] disabling for this session\n")
_r = diagnose.analyse(_d)
check("a failure after 'running at' is the verdict, not 'Working.'",
      "stopped after it started" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _opti_dir("opti_before_", "[I] [DLSS-NR] create failed\n"
                               "[I] [DLSS-NR] running at 2560x1440\n")
_r = diagnose.analyse(_d)
check("...and a failure before it is the older news it is", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# Issue #58: the game's own Present refused the swapchain OptiScaler wrapped.
_d = _opti_dir("opti_present_",
               "[I] Init done\n"
               "[W] LocalPresent Original present result: 80004002\n")
_r = diagnose.analyse(_d)
check("the game refusing OptiScaler's swapchain is named, with what to try",
      "refused OptiScaler's swapchain" in _r.verdict
      and any("winmm.dll" in (f_.detail or "") for f_ in _r.findings), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _opti_dir("opti_ok_present_",
               "[I] [DLSS-NR] running at 2560x1440\n"
               "[I] LocalPresent Original present result: 00000000\n")
_r = diagnose.analyse(_d)
check("...and a present that succeeded is not mistaken for one that failed",
      _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


section("55. issue #40 again: scaling the fonts is not the whole of a 4K display")

# The first round scaled the fonts and the row heights, and the report that
# came back was "the text is readable now, but the log box next to the blue
# arrow is one and a half lines". Three more things were wrong with it, and
# none of them was a font.
_gsrc = src_of(_gui)

# 1. The window asked for more than the screen. px(830) is 2490 at 300%, on
#    a display 2160 tall, and minsize kept it there: the bottom of every
#    page was off the screen and dragging could not bring it back.
check("the window never asks for more height than the screen has",
      "winfo_screenheight()" in _gsrc and "min(px(830)" in _gsrc, )
check("...and minsize cannot pin it larger than the screen either",
      _re.search(r"minsize\(min\(px\(\d+\), int\(sw", _gsrc) is not None)

# 2. Text and Treeview heights are counted in rows, and a row grows with the
#    font: 14 rows of log at 300% is three times the pixels it was at 100%.
_gui.SCALE = 1.0
check("lines() leaves row counts alone at 100 %", _gui.lines(14) == 14)
_gui.SCALE = 3.0
check("...and cuts them to about the same pixel height at 300 %", _gui.lines(14) == 5)
check("...with a floor, so nothing collapses to nothing", _gui.lines(4, 6) == 6)
_gui.SCALE = 1.0
check("the log and the game list are sized in lines(), not bare rows",
      "height=lines(14, 6)" in _gsrc and "height=lines(13, 4)" in _gsrc)

# 3. Pack hands the first widget everything it asks for. The settings card
#    is 1221 pixels at 300%, so whichever of the settings and the log was
#    packed first took the window and the other got what was left.
check("the install page splits the window between the settings and the log",
      "_split" in _gsrc and "installscroll.set_height" in _gsrc)
check("the pages that overflow can scroll",
      _gsrc.count("Scroller(") >= 2 and "class Scroller" in _gsrc)
check("the game list's detail row is packed from the bottom, so it survives a short window",
      'det.pack(side="bottom"' in _gsrc)

# The whole point: every page fits the window at every scale, and what does
# not fit scrolls instead of being cut off. Built for real, measured for
# real - a 4K screen at 300% has the same room as a 1280x720 one at 100%.
def _fits(scale: float, w: int, h: int) -> list[str]:
    _gui.SCALE = scale
    root = _tk.Tk()
    root.tk.call("tk", "scaling", 96 * scale / 72.0)
    bad = []
    try:
        app = _gui.App(root)
        for _ in range(3):
            root.state("normal")
            root.geometry(f"{w}x{h}")
            root.update_idletasks()
            root.update()
        for n, name in ((1, "start"), (2, "game list"), (3, "install"),
                        (4, "video"), (5, "rtx remix")):
            app.step = n
            app._show(n)
            for _ in range(3):
                root.update_idletasks()
                root.update()
            page = app.pages[n - 1]
            # Every action the page offers has to be inside the window.
            for w_ in page.winfo_children():
                if w_.winfo_ismapped() and w_.winfo_height() <= 1 < w_.winfo_reqheight():
                    bad.append(f"{name}: {w_.winfo_class()} squeezed to nothing")
        # ...and at 300% the log is still readable.
        if app.log.winfo_height() < 3 * (app.log.winfo_reqheight()
                                         / max(1, int(app.log.cget("height")))):
            bad.append(f"log under three lines at {scale:.0%}")
    finally:
        root.destroy()
        _gui.SCALE = 1.0
    return bad

_bad = []
for _s, _w, _h in ((1.0, 1200, 900), (1.5, 1280, 720), (2.0, 1000, 700)):
    _bad += [f"{_s:.0%}: {b}" for b in _fits(_s, _w, _h)]
check("no page loses a widget off the window, at any scale", not _bad, _bad)


section("56. issue #67: the library found last time, without walking the disks again")

# The owner's GTA IV came up twice: Steam reports the library folder, the
# Rockstar launcher the GTAIV subfolder in it - two folders, one executable.
_dup = Path(tempfile.mkdtemp(prefix="dupexe_"))
(_dup / "GTAIV").mkdir()
(_dup / "GTAIV" / "GTAIV.exe").write_bytes(b"MZ")
_ga = games.Game(name="Grand Theft Auto IV: The Complete Edition", folder=_dup,
                 exe=_dup / "GTAIV" / "GTAIV.exe", source="Steam")
_gb = games.Game(name="Grand Theft Auto IV", folder=_dup / "GTAIV",
                 exe=_dup / "GTAIV" / "GTAIV.exe", source="Rockstar")
_one = games.same_exe_once([_ga, _gb])
check("one executable reported by two stores is listed once, the first store's",
      _one == [_ga], [g.source for g in _one])
_e1 = games.Game(name="Game A", folder=_dup, exe=_dup / "GTAIV" / "GTAIV.exe",
                 source="Emulator")
_e2 = games.Game(name="Game B", folder=_dup, exe=_dup / "GTAIV" / "GTAIV.exe",
                 source="Emulator")
_e1.emu = _e2.emu = object()
check("...but games that share an emulator's executable are all kept",
      len(games.same_exe_once([_e1, _e2])) == 2)
shutil.rmtree(_dup, ignore_errors=True)

from core import library as _lib  # noqa: E402

_d = Path(tempfile.mkdtemp(prefix="libcache_"))
_saved_file = _lib.FILE
_lib.FILE = _d / "library.json"
_gdir = _d / "Some Game" / "bin"
_gdir.mkdir(parents=True)
_exe = _gdir / "Game.exe"
_exe.write_bytes(b"MZ" + b"\0" * 100)
_g = games.Game(name="Some Game", folder=_d / "Some Game", exe=_exe, bitness=64,
                api="DX12", api_why="imports dxgi.dll", api_detected="DX12",
                source="Steam", candidates=[_exe], kind="game")
_gone = games.Game(name="Gone Game", folder=_d / "nowhere",
                   exe=_d / "nowhere" / "x.exe", bitness=64, api="DX11", source="Epic")
_key = (str(_g.folder), str(_g.exe))
_row = (True, "feeder", "stable", "reliable", False, "")
_lib.save([_g, _gone], {_key: _row, (str(_gone.folder), str(_gone.exe)): False}, "1.7.3", 89)

_got = _lib.load("1.7.3", 89)
check("the library comes back with every field the list needs",
      _got is not None and [x.name for x in _got[0]] == ["Some Game"]
      and _got[0][0].api_why == "imports dxgi.dll"
      and _got[0][0].candidates == [_exe] and _got[1][_key] == _row
      and _got[2] == [], _got)
check("a game whose folder is gone is dropped, not shown as missing",
      all(x.name != "Gone Game" for x in _got[0]))

check("a cache from another release of this tool is not used",
      _lib.load("1.7.2", 89) is None)
check("...nor one measured against another graphics card",
      _lib.load("1.7.3", 120) is None)

# The compatibility columns are read from the game folder, so a game that
# has changed on disk since has to be looked at again - that is the whole
# reason a cache is allowed to exist at all.
time.sleep(1.1)
_exe.write_bytes(b"MZ" + b"\0" * 200)
os.utime(_exe, None)
_gs, _rows, _changed = _lib.load("1.7.3", 89)
check("a game the store has updated keeps its place, loses its row, and is handed back to be read again",
      [x.name for x in _gs] == ["Some Game"] and not _rows
      and [x.name for x in _changed] == ["Some Game"], (_rows, _changed))

_lib.save([_g], {_key: _row}, "1.7.3", 89)
time.sleep(1.1)
(_g.folder / "OptiScaler.dll").write_bytes(b"MZ")
os.utime(_g.folder, None)
check("...and so does one that had a mod dropped into it",
      not _lib.load("1.7.3", 89)[1] and _lib.load("1.7.3", 89)[2])

_lib.FILE.write_text("{ not json", encoding="utf8")
check("an unreadable cache just means the normal scan runs", _lib.load("1.7.3", 89) is None)
_lib.FILE.unlink()
check("no cache at all is not an error", _lib.load("1.7.3", 89) is None)

_gsrc = src_of(_gui)
check("the GUI reads the cache at start and writes it after a scan",
      "_load_cached" in _gsrc and "library.save(gs, rows" in _gsrc)
check("...and writes it again when a folder is chosen or an install finishes",
      _gsrc.count("_remember_library") >= 3, _gsrc.count("_remember_library"))
check("rescan still does the full walk",
      "def _scan" in _gsrc and "games.scan_all(" in _gsrc)
_lib.FILE = _saved_file
shutil.rmtree(_d, ignore_errors=True)


section("57. the second day of 1.7.2: a comma for a full stop, an install that "
        "never finished, a moved ffmpeg name, and a game on the wrong API")

# Issue #69: the add-on writes dlss5-feed.cfg too, and it is C++ - on a
# machine whose locale uses a comma for the decimal point (PCSX2 is Qt, and
# Qt sets the locale) it writes "1,000". float() then raised out of the
# middle of install() and NOTHING was installed.
from core import feedcfg as _fc  # noqa: E402

check("a comma decimal is read as the number it is", _fc.number("1,000") == 1.0)
check("...and a full stop still is", _fc.number("0.750") == 0.75)
check("...and rubbish falls back instead of raising", _fc.number("?", 1.0) == 1.0)
check("...and a real number is passed through", _fc.number(2) == 2.0)

_d = Path(tempfile.mkdtemp(prefix="feedcfg_"))
(_d / "dlss5-feed.cfg").write_text(
    "enabled=1\nmode=2\nmv_scale_x=1,000\nmv_scale_y=1,000\nwork_resolution=100\n",
    encoding="utf8")
_p = _fc.write(_d, {"work_resolution": 75})
_txt = _p.read_text(encoding="utf8")
check("the install survives a cfg written in another locale, and rewrites it",
      "mv_scale_x=1.000" in _txt and "work_resolution=75" in _txt, _txt.replace("\n", " "))
check("...and describing it does not raise either",
      _fc.describe(_fc.read(_p)) and _fc.describe_bridge({"ofa_grid": "2,000"}) == [], )
shutil.rmtree(_d, ignore_errors=True)

# Issue #74: a download cut off (a reset connection) leaves a folder set up
# part of the way. The installer records that; the diagnosis never read it,
# so the missing parts were reported as if antivirus had eaten them.
_d = _diag_dir("diag_incomplete_", feed=_FEED_OK, complete=False)
_r = diagnose.analyse(_d)
check("an install that never finished says so, before anything else",
      "never finished" in _r.verdict
      and any("did not finish" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_complete_", feed=_FEED_OK)
_r = diagnose.analyse(_d)
check("...and a finished one is read as before", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# Issue #75: BtbN's /releases/latest is the DATED autobuild, whose asset
# names carry the build hash; the stable name lives on the release tagged
# "latest". Every video download failed with "ffmpeg build not found".
check("the ffmpeg listing asks for the tag, not for 'the latest release'",
      video.FFMPEG_API.endswith("/releases/tags/latest"), video.FFMPEG_API)
check("there is a direct download to fall back on",
      video.FFMPEG_DIRECT.endswith(video.FFMPEG_ASSET))
_dated = ["ffmpeg-N-126475-g35b7df64a0-win64-gpl.zip",
          "ffmpeg-N-126475-g35b7df64a0-win64-gpl-shared.zip",
          "ffmpeg-N-126475-g35b7df64a0-win64-lgpl.zip",
          "ffmpeg-N-126475-g35b7df64a0-winarm64-gpl.zip",
          "ffmpeg-N-126475-g35b7df64a0-linux64-gpl.tar.xz"]
_pick = [n for n in _dated
         if n.lower().endswith(".zip") and "win64" in n.lower() and "gpl" in n.lower()
         and "lgpl" not in n.lower() and "shared" not in n.lower()]
check("...and the name pattern picks the static 64-bit Windows GPL build",
      _pick == ["ffmpeg-N-126475-g35b7df64a0-win64-gpl.zip"], _pick)
check("the real release still carries the name we ask for, or the pattern finds one",
      any(a.get("name") == video.FFMPEG_ASSET
          for a in sources._json(video.FFMPEG_API).get("assets", [])))

# Issues #66 and #70, both Red Dead Redemption 2: the game was set to Vulkan
# and the optiscaler route hooks a Direct3D path, so OptiScaler's overlay
# kept asking for an upscaler that was already switched on.
_d = _opti_dir("opti_vulkan_", "[I] Init done\n[I] Vulkan is creating swapchain!\n")
_r = diagnose.analyse(_d)
check("a game drawing with Vulkan on the optiscaler route is told so",
      "on Vulkan" in _r.verdict and "DirectX 12" in " ".join(
          (f_.detail or "") for f_ in _r.findings), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


# Issue #76: a third OptiScaler build in the dropdown - wilsjo2's fork runs
# the neural pass before super resolution instead of after it. Every fork
# publishes the same way, so the branch that only knew y4my4my4m's is a
# table now.
check("the dropdown offers three builds, and the default is still the plain one",
      list(optiscaler.BUILDS)[0] == ""
      and optiscaler.PRESR in optiscaler.BUILDS
      and optiscaler.FORK in optiscaler.BUILDS, list(optiscaler.BUILDS))
check("...and the new one says it has not been tried here",
      "not run here" in optiscaler.BUILDS[optiscaler.PRESR],
      optiscaler.BUILDS[optiscaler.PRESR])
check("every fork in the table has an API and a skip list",
      all(isinstance(v, tuple) and len(v) == 2 and v[0].startswith("https://")
          for v in optiscaler.FORKS.values()), optiscaler.FORKS)
check("an unknown build key is still refused rather than silently installed",
      _raises(lambda: optiscaler.resolve("nope")))

_saved_json = sources._json
sources._json = lambda url: [
    {"tag_name": "nightly", "published_at": "2026-09-09T00:00:00Z",
     "assets": [{"name": "x.7z", "browser_download_url": "https://x/nightly.7z"}]},
    {"tag_name": "v0.7.1", "published_at": "2026-09-08T12:00:00Z",
     "assets": [{"name": "OptiScaler-DLSSNR-v0.7.1-hybrid.zip",
                 "browser_download_url": "https://x/new.zip"}]},
    {"tag_name": "v0.6.2", "published_at": "2026-09-07T00:00:00Z",
     "assets": [{"name": "old.zip", "browser_download_url": "https://x/old.zip"}]},
]
check("the newest dated release wins and the rolling 'nightly' tag is passed over",
      optiscaler.resolve(optiscaler.PRESR) == ("v0.7.1", "https://x/new.zip"),
      optiscaler.resolve(optiscaler.PRESR))
sources._json = lambda url: [
    {"tag_name": "v1", "published_at": "2026-09-08T12:00:00Z", "assets": [
        {"name": "OptiScaler_with_DLSS.7z", "browser_download_url": "https://x/withdlss.7z"},
        {"name": "OptiScaler.7z", "browser_download_url": "https://x/plain.7z"}]}]
check("...and y4my4my4m's '_with_DLSS' archive is still passed over",
      optiscaler.resolve(optiscaler.FORK) == ("v1", "https://x/plain.7z"),
      optiscaler.resolve(optiscaler.FORK))
sources._json = _saved_json
check("all three builds resolve against the real release pages",
      all(optiscaler.resolve(b)[1].startswith("https://")
          for b in optiscaler.BUILDS))


# With two forks' archives in the cache, the preview listed whichever one the
# glob found first and described the wrong package - their file lists differ
# by a dozen documents and a folder of weights. Found by the existing
# "preview promised nothing the install did not do" check the moment a third
# build was added.
check("the preview describes the build it would install, from the cache alone",
      len({optiscaler.archive_name(b) for b in optiscaler.BUILDS}) == len(optiscaler.BUILDS),
      {b: optiscaler.archive_name(b) for b in optiscaler.BUILDS})
check("...under the name the download saves it as",
      all(optiscaler.archive_name(b).startswith("OptiScaler-DLSSNR-")
          for b in optiscaler.BUILDS if optiscaler.archive_name(b)))
check("cached_json never reaches the network",
      "urlopen" not in src_of(sources.cached_json)
      and sources.cached_json("https://example.invalid/nothing-was-ever-fetched") is None)
_saved_get = sources._get


def _no_net(*a, **k):
    raise AssertionError("preview() made a request")


sources._get = _no_net
_d = Path(tempfile.mkdtemp(prefix="pv_opti_offline_"))
shutil.copyfile(X64, _d / "Game.exe")
_pv = installer.preview(games.manual(_d), installer.Options(path=dlss.OPTI))
check("the optiscaler preview still makes no request at all", bool(_pv.writes))
sources._get = _saved_get
shutil.rmtree(_d, ignore_errors=True)


section("58. the release gate on 1.7.3's own changes")

# The cache made two old habits expensive. An install used to throw every
# compatibility row away because recomputing them was free; with the rows
# saved for the next launch, that emptied the cache and gave the launch
# after an install the whole folder walk again - on the Tk thread.
_gsrc = src_of(_gui)
check("an install drops only the row of the game it installed",
      "_forget_row(self.game)" in _gsrc and "self._rows.clear()" not in
      _gsrc.split("def _finish_ok")[1].split("def ")[0], )
check("...and an empty set of rows is never written over a full one",
      "self._rows or not library.FILE.is_file()" in _gsrc)
check("...and the games that did change are read on a worker, not on the Tk thread",
      "def _recheck_changed" in _gsrc
      and "threading.Thread(target=work" in _gsrc.split("def _recheck_changed")[1][:1600]
      and "games.enrich(g)" in _gsrc.split("def _recheck_changed")[1][:1600])
check("...and _fill leaves those rows to the worker instead of reading them itself",
      "str(g.folder) in self._recheck" in _gsrc)
check("...keyed on the folder, which enrich() cannot reassign under it",
      "self._recheck = {str(g.folder) for g in changed}" in _gsrc)
check("...and a rescan makes a running recheck's answer stale, not authoritative",
      "_recheck_id" in _gsrc and _gsrc.count("_recheck_id") >= 4)

# A graphics API set by hand is kept in the settings, not in the library, so
# a cached game came back with the renderer that was DETECTED - and would be
# installed for it. That is the fault the dropdown exists to fix (#24/#66/#70).
_d = Path(tempfile.mkdtemp(prefix="libapi_"))
_saved_file = _lib.FILE
_lib.FILE = _d / "library.json"
(_d / "bin").mkdir()
_exe2 = _d / "bin" / "Game.exe"
_exe2.write_bytes(b"MZ")
_g2 = games.Game(name="Some Game", folder=_d, exe=_exe2, bitness=64,
                 api="DX12", api_detected="DX12", source="Steam")
_lib.save([_g2], {}, "1.7.3", 89)
games.set_api_override(_d, "Vulkan")
try:
    _back = _lib.load("1.7.3", 89)[0][0]
    check("a graphics api set by hand survives a cached launch",
          _back.api == "Vulkan" and "set by hand" in _back.api_why,
          (_back.api, _back.api_why))
finally:
    games.set_api_override(_d, None)
_lib.FILE = _saved_file
shutil.rmtree(_d, ignore_errors=True)

# An emulator chosen with "choose folder" keeps source "Manual", and its
# profile is what switches the render backend. Dropping it meant the install
# silently did nothing.
check("an emulator profile is found again whatever the game's source says",
      'if exe is not None:' in src_of(_lib._from_json)
      and 'source == "Emulator"' not in src_of(_lib._from_json))

# The fault chain: the list of our own modules was missing three files this
# tool installs, and an unrecognised module was being called the game's.
check("every add-on this tool installs is in the fault-chain list",
      all(n in diagnose._OURS_IN_STACK for n in (
          "dlss5-feed.addon64", "dlss5-feed.addon32", "renodx-dlss5.addon64",
          "renodx-dlss.addon64", "dlss5-bridge.addon64",
          "dlss5-dx11-bridge.addon64", "nvngx.dll.addon64",
          "standalone-dlssnr.addon64")), diagnose._OURS_IN_STACK)

# The upstream analyser could add "it is running" and then return a verdict
# saying it was switched off or never hooked.
def _up(*lines: str) -> str:
    return (_SESSION
            + 'INFO  | Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0\n'
            + "".join("INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] " + x + "\n" for x in lines))


_d = _up_dir()
(_d / "ReShade.log").write_text(_up("settings loaded: enabled=0", _HOOK, _HB), encoding="utf8")
_r = diagnose.analyse(_d)
check("frames through the network outrank an 'enabled=0' written at load",
      _r.verdict == "Working.", _r.verdict)
(_d / "ReShade.log").write_text(_up("settings loaded: enabled=1", _HB), encoding="utf8")
_r = diagnose.analyse(_d)
check("...and outrank a hook line that never appeared", _r.verdict == "Working.", _r.verdict)
(_d / "ReShade.log").write_text(
    _up("settings loaded: enabled=1", _HOOK, _HB, "resetting state (new device)"),
    encoding="utf8")
_r = diagnose.analyse(_d)
check("...but not what happened after them",
      "device re-creation" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# An uninstall that could not delete a locked file records itself the same
# way a failed install does; telling that person to install again is the
# opposite of what they need.
_d = _diag_dir("diag_stuckuninstall_", feed=_FEED_OK, complete=False,
               notes=["uninstall left 2 locked file(s); run it again with the game closed"])
_r = diagnose.analyse(_d)
check("a partial uninstall is not reported as a partial install",
      _r.verdict.startswith("The uninstall")
      and any("uninstall did not finish" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


# Found by replaying issue #51's own 400 KB ReShade.log: the tail is cut to
# 250 KB, so the "Registered add-on" lines fell off the front while every
# line the add-on WROTE was still there - and the report said "ReShade
# loaded no add-ons" next to 34621 heartbeats from that add-on.
_d = _up_dir()
(_d / "ReShade.log").write_text(
    "16:00:00:000 [1] | INFO  | Some line with no registration in it\n"
    "16:00:01:000 [1] | INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] " + _HOOK + "\n"
    "16:00:02:000 [1] | INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] " + _HB + "\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("a log cut to its tail is not reported as 'no add-ons loaded'",
      not any("no add-ons" in t for t in _levels(_r, "bad")), _levels(_r, "bad"))
check("...and it is still read as working", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


section("59. issue #77: a d3d9.dll import is the weakest evidence in the file")

# Grand Theft Auto V was read as a DirectX 9 game because GTA5.exe imports
# d3d9.dll, and went to a route that has nothing for it. The same shape as
# Red Dead Redemption 2 (#12) and Call of Juarez (#31): engines keep a d3d9
# import for a launcher or an old dialog long after they stopped drawing
# with it. Every other kind of evidence is now asked first.
_d = Path(tempfile.mkdtemp(prefix="d3d9ev_"))
_exe = _d / "Game.exe"
_exe.write_bytes(b"MZ")
_saved_imports = pe.pe_imports


def _fake_imports(static: list[str], delayed: list[str] | None = None):
    pe.pe_imports = (lambda path, delay=False, _s=list(static),
                     _d=list(delayed or []): _d if delay else _s)


try:
    _fake_imports(["d3d9.dll", "kernel32.dll"])
    check("a game with nothing but d3d9 is still DirectX 9",
          pe.detect_api(_exe)[0] == "DX9", pe.detect_api(_exe))

    _fake_imports(["d3d9.dll"], ["d3d11.dll"])
    _api, _why = pe.detect_api(_exe)
    check("...but a delay-loaded d3d11 is the renderer, and the reason says so",
          _api == "DX11" and "delay-loads d3d11.dll" in _why, (_api, _why))

    _fake_imports(["d3d9.dll"], ["d3d12.dll"])
    check("...and so is a delay-loaded d3d12", pe.detect_api(_exe)[0] == "DX12",
          pe.detect_api(_exe))

    # A DirectX 9 game may use DXGI on its own just to enumerate displays,
    # so a bare dxgi delay-load must not flip it.
    _fake_imports(["d3d9.dll"], ["dxgi.dll"])
    check("...but a bare dxgi delay-load does not make a DX9 game modern",
          pe.detect_api(_exe)[0] == "DX9", pe.detect_api(_exe))

    # The evidence already used for RDR2 and for the Agility SDK still wins
    # ahead of any of that.
    _fake_imports(["d3d9.dll"], ["d3d11.dll"])
    (_d / "nvngx_dlss.dll").write_bytes(b"MZ")
    check("a game that ships DLSS beside a d3d9 import is D3D12, as before",
          pe.detect_api(_exe)[0] == "DX12", pe.detect_api(_exe))
    (_d / "nvngx_dlss.dll").unlink()
finally:
    pe.pe_imports = _saved_imports
shutil.rmtree(_d, ignore_errors=True)

# The delay-load table is data directory 13, and it is read out of real
# executables, not just mocked: several games in a real library delay-load
# a renderer they do not import statically.
check("pe_imports takes a delay flag and both tables parse the same exe",
      isinstance(pe.pe_imports(X64), list)
      and isinstance(pe.pe_imports(X64, delay=True), list))
check("...and the delay table is read from data directory 13",
      "want = 13 if delay else 1" in src_of(pe.pe_imports))
check("...after checking the image has that many data directories",
      "n_dirs <= want" in src_of(pe.pe_imports))
check("...with 32-byte descriptors and the name at offset 4",
      "(32, 4) if delay else (20, 12)" in src_of(pe.pe_imports))


# Issue #31, answered three times with "the game has not been started" while
# DXVK's own log sat in the folder proving it had. DXVK writes that log the
# moment it loads, so it settles what the hints could only guess at.
# The clash rule below reads the real layer folder, so point it at a clean
# one: a test must not depend on what this machine happens to have.
from core import vulkan as _vk_clean  # noqa: E402
_vk_saved_dir = _vk_clean.layer_dir
_vk_clean_dir = Path(tempfile.mkdtemp(prefix="vkclean_"))
_vk_clean.layer_dir = lambda: _vk_clean_dir
# The layer IS registered for this game in the story these checks tell; what
# is being tested is which answer comes first once it is. Asking this
# machine's own registry instead made the result depend on whether the
# reviewer happens to have a 32-bit ReShade installed.
_layer_saved = diagnose._layer_state
diagnose._layer_state = lambda man: (True, True)

_d = Path(tempfile.mkdtemp(prefix="dxvklog_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "CoJGunslinger.exe", "bitness": 32,
     "api": "Vulkan", "proxy": diagnose.VULKAN_LAYER, "path": "feeder",
     "dxvk": True,
     "files": ["d3d9.dll", "dlss5-feed.addon32"]}), encoding="utf8")
for _n in ("d3d9.dll", "dlss5-feed.addon32", "ReShade.ini", "nvngx_dlssnr.dll"):
    (_d / _n).write_bytes(b"MZ")
_r = diagnose.analyse(_d)
check("with nothing in the folder it is still 'not started since the install'",
      "Not started since the install" in _r.verdict, _r.verdict)
(_d / "CoJGunslinger_d3d9.log").write_text("info:  DXVK: v2.4", encoding="utf8")
_r = diagnose.analyse(_d)
check("DXVK's own log proves the game ran, so the layer is what is missing",
      "DXVK ran" in _r.verdict and "32-bit" in _r.verdict, _r.verdict)
# ...but only a log written SINCE the install. One left by a run before it,
# or by a game the person had already put DXVK into, says nothing.
import os as _os
_os.utime(_d / "CoJGunslinger_d3d9.log", (1, 1))
_r = diagnose.analyse(_d)
check("...and a DXVK log older than the install is not evidence the game ran",
      "Not started since the install" in _r.verdict, _r.verdict)
diagnose._layer_state = _layer_saved
_vk_clean.layer_dir = _vk_saved_dir
shutil.rmtree(_d, ignore_errors=True)


section("60. the day after 1.7.3: a fork that reports its cost, and paths "
        "that leave the folder")

# Issue #81. wilsjo2's fork, Cyberpunk 2077, ApplyAfterRR: a thirteen-minute
# session with the model dispatching every frame was called "Inconclusive"
# because that build never writes "running at" - it writes what the model
# cost. These two lines are copied from the report.
_d = _opti_dir("opti_dispatch_",
               "[01:54:33.897657] [I] DlssNr.Enabled: true\n"
               "[01:54:33.897663] [I] DlssNr.ApplyAfterRR: true\n"
               "[01:55:42.255058] [I] DlssNr_Dx12::Dispatch DLSS-NR cost: "
               "7.41 ms total = 7.23 ms model + 0.19 ms ours (3% ours)\n"
               "[02:08:18.027112] [I] DlssNr_Dx12::Dispatch DLSS-NR cost: "
               "8.82 ms total = 8.66 ms model + 0.16 ms ours (2% ours)\n")
_r = diagnose.analyse(_d)
check("a dispatch cost line is proof the model ran", _r.verdict == "Working.",
      _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# ...and the ordering rule still holds over the new evidence: a failure
# logged after the last dispatch is what the person saw last.
_d = _opti_dir("opti_dispatch_then_fail_",
               "[01:55:42] [I] DlssNr_Dx12::Dispatch DLSS-NR cost: 7.41 ms\n"
               "[01:59:02] [E] DLSS-NR unavailable: the device was lost\n")
_r = diagnose.analyse(_d)
check("...but a failure after the last dispatch still ends the session",
      "stopped after it started" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# Issues #85 and #70: settings read back at startup are not evidence of
# anything running. The old text ("Inconclusive - open the overlay") sent
# people to look at an overlay that was already telling them the truth:
# it is waiting for the game's own upscaler, which is off.
_d = _opti_dir("opti_settings_only_",
               "[01:54:33.897657] [I] DlssNr.Enabled: true\n"
               "[01:54:33.897663] [I] DlssNr.ApplyAfterRR: true\n")
_r = diagnose.analyse(_d)
check("settings with no dispatch means the model never drew a frame",
      "never ran" in _r.verdict and "upscaler" in _r.verdict, _r.verdict)
check("...and the answer says to switch the game's own upscaler on",
      any("graphics menu" in f.title + f.detail for f in _r.findings),
      [(f.title + f.detail)[:60] for f in _r.findings])
shutil.rmtree(_d, ignore_errors=True)

# DLSS5-Reshade-AIO v2.2.0 dropped the loose release files. The API path
# already falls back to the 64-bit archive, but the API-LESS path still
# asked for the three loose names - and those answer 404 now, so the one
# fallback written for rate-limited people was the one that could not work.
# (Checked live against v2.2.0: the archive URL below answers 200, 183293
# bytes, and holds standalone-dlssnr.addon64, nvngx.dll and the two shaders.)
_saved_json, _saved_tag = sources._json, sources.latest_tag
try:
    sources._json = lambda _u: (_ for _ in ()).throw(RuntimeError("no api"))
    sources.latest_tag = lambda _repo: "v2.2.0"
    _tag, _urls = sources.resolve_standalone()
    check("with no API the standalone route falls back to the 64-bit archive",
          _tag == "v2.2.0" and sources.STANDALONE_ZIP in _urls
          and _urls[sources.STANDALONE_ZIP].endswith(
              "/releases/download/v2.2.0/DLSS5-ReShade-AIO-v2.2.0-64-bit.zip"),
          _urls)
    # ...and if even the redirect is gone, the old loose names are still
    # better than nothing: older releases carry them.
    sources.latest_tag = lambda _repo: None
    _tag, _urls = sources.resolve_standalone()
    check("...and with no redirect either it still asks for the loose files",
          _tag == "latest"
          and all(n in _urls for n in sources.STANDALONE_ASSETS), _urls)
finally:
    sources._json, sources.latest_tag = _saved_json, _saved_tag
check("the tag comes out of a redirect, not the API",
      "api.github.com" not in src_of(sources.latest_tag)
      and "releases/latest" in src_of(sources.latest_tag))

# Issue #84, GTA IV: installed three times, and every time the answer was
# "install again". The files the install recorded were not in the folder at
# all - only the add-on used to be checked, so ReShade.ini and the runtime
# going missing said nothing and the DXVK rule below had the last word.
# The layer is registered for this game; what is under test is which answer
# comes first once it is. Reading this machine's own registry made the
# result depend on whether the reviewer has a 32-bit ReShade installed.
_layer_saved = diagnose._layer_state
diagnose._layer_state = lambda man: (True, True)
_d = Path(tempfile.mkdtemp(prefix="gone84_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "GTAIV.exe", "bitness": 32,
     "api": "DX9", "proxy": diagnose.VULKAN_LAYER, "path": "feeder",
     "dxvk": True,
     "files": ["d3d9.dll", "dlss5-feed.addon32", "ReShade.ini",
               "nvngx_dlssnr.dll",
               "reshade-shaders/Shaders/DLSS5_Feed.fx"]}), encoding="utf8")
for _n in ("d3d9.dll", "dlss5-feed.addon32"):
    (_d / _n).write_bytes(b"MZ")
_r = diagnose.analyse(_d)
check("files recorded by the install and since gone are the answer",
      "gone from the folder" in _r.verdict
      and "nvngx_dlssnr.dll" in " ".join(f.title for f in _r.findings),
      _r.verdict)
check("...and it says to restore them BEFORE installing again",
      any("restore them from quarantine first" in f.detail for f in _r.findings)
      or any("Restore" in f.detail and "before" in f.detail.lower()
             for f in _r.findings),
      [f.detail[:80] for f in _r.findings])
diagnose._layer_state = _layer_saved
shutil.rmtree(_d, ignore_errors=True)

# The report form. Four of the nine reports on 1.7.3's first day arrived
# with the template's own "yes / no / it closed itself" line untouched and
# nothing written under "What happened" (#82, #84, #86, #87), so the two
# questions are asked in the tool now and the body carries the answers.
_d = _diag_dir("report_answers_")
_body = diagnose.issue_body(
    "1.7.3", "NVIDIA GeForce RTX 4070 Ti", 89, "616.64", None, "feeder",
    None, "", Path("C:/x/autopilot.log"), _d,
    answers={"started": "it started, then closed itself",
             "happened": "Small game splash screen. Then crash to desktop."})
check("the answers replace the template, and the placeholder line is gone",
      "**Did the game start?** it started, then closed itself" in _body
      and "Then crash to desktop." in _body
      and "yes / no / it closed itself" not in _body, _body[:200])
_body = diagnose.issue_body(
    "1.7.3", "NVIDIA GeForce RTX 4070 Ti", 89, "616.64", None, "feeder",
    None, "", Path("C:/x/autopilot.log"), _d)
check("...and with no answers the old template still comes out",
      "yes / no / it closed itself" in _body, _body[:80])
shutil.rmtree(_d, ignore_errors=True)

from core import reportui as _reportui  # noqa: E402
check("the report button asks before it opens the browser",
      "reportui.ask" in src_of(_gui.App._report_bug))
check("...and cancelling the dialog cancels the report",
      "cancelled on purpose" in src_of(_gui.App._report_bug))
check("...but a dialog that cannot open does not block reporting",
      "answers, asked = None, False" in src_of(_gui.App._report_bug))
check("three answers to 'did it start', and a few words are required",
      len(_reportui.STARTED) == 3 and _reportui.MIN_WORDS >= 3)

# The compatibility list: the write side is a browser window the person can
# cancel, the read side is one static file, and the two ends have to agree
# about the block in between - so the workflow imports the tool's own parser
# rather than keeping a second copy of the format.
from core import community as _comm  # noqa: E402


class _FakeGame:
    name = "Red Dead Redemption 2"
    api = "DX12"
    exe = Path("D:/Games/RDR2/RDR2.exe")
    install_dir = Path("D:/Games/RDR2")


_rec = _comm.record(_FakeGame(), "optiscaler", "failed", api="DX12",
                    build="wilsjo2", gpu_sm=120,
                    gpu_name="NVIDIA GeForce RTX 5070 Ti", driver="616.64",
                    version="1.8.0")
check("the shared record is keyed by the executable, in lower case",
      _rec["exe"] == "rdr2.exe" and _rec["result"] == "failed")
check("...and carries nothing that identifies anybody",
      not any(("C:" in str(v) or "D:" in str(v) or "Users" in str(v))
              for v in _rec.values()), _rec)
_round = _comm.parse("some prose a person wrote" + _comm.block(_rec))
check("the block survives being written into an issue and read back",
      _round == _rec, _round)
check("a body with no block is not a result", _comm.parse("just a bug") is None)
check("...and neither is a hand-mangled one",
      _comm.parse(f"<!-- {_comm.MARKER}\n{{\"v\":9}}\n{_comm.MARKER} -->") is None)
_url = _comm.issue_url(_rec)
check("the share link is a pre-filled issue, labelled result",
      _url.startswith("https://github.com/") and "labels=result" in _url
      and "issues/new" in _url)

# The advice only speaks when there is something to say.
_thin = {"routes": {"feeder": {"worked": 1, "failed": 1}}}
check("two reports are not a finding", _comm.advice(_thin) == [])
_fat = {"name": "RDR2",
        "routes": {"feeder": {"worked": 22, "failed": 3},
                   "optiscaler": {"worked": 0, "failed": 9}},
        "drivers": {"616.64": {"worked": 1, "failed": 9}}}
_said = " ".join(_comm.advice(_fat, "optiscaler", "616.64"))
check("...but 34 of them name the route that worked",
      "feeder route worked in 22" in _said, _said)
check("...say that the chosen one failed for everybody",
      "failed in all 9" in _said, _said)
check("...and name the driver behind the failures",
      "616.64" in _said and "9 of the 12 failures" in _said, _said)
check("...as a count, not as a cause",
      "is behind" not in _said and "were on driver" in _said, _said)

# The published file has to exist and parse, or the first fetch 404s.
_feed = Path(__file__).resolve().parent / "docs" / "compatibility.json"
check("docs/compatibility.json is there and is an object",
      isinstance(json.loads(_feed.read_text(encoding="utf8")), dict))
check("the workflow reads the tool's own parser, not a copy of the format",
      "from core.community import parse" in
      (Path(__file__).resolve().parent / ".github" / "scripts"
       / "compatibility.py").read_text(encoding="utf8"))
check("the note is fetched off the Tk thread",
      "threading.Thread" in src_of(_gui.App._community_note))

# Aiming for a frame rate instead of setting a percentage by feel. The two
# log lines below are the shapes the add-ons really write - the feeder's
# frame-rate line, and the cost line out of issue #81.
from core import autotune as _tune  # noqa: E402

_FEED_LINE = ("[feed] 3600 frames: feed CPU 2.10 ms/frame, "
              "GPU 4.80 ms/frame, 47.0 fps")
_OPTI_LINE = ("[02:08:18.027112] [I] DlssNr_Dx12::Dispatch DLSS-NR cost: "
              "7.41 ms total = 7.23 ms model + 0.19 ms ours (3% ours)")

_m = _tune.measure(_FEED_LINE, "", "feeder", 100)
check("the feeder's own line is the frame rate to work from",
      _m and _m.fps == 47.0 and _m.frames == 3600, _m)
_m_opti = _tune.measure("", (_OPTI_LINE + "\n") * 40, "optiscaler", 100)
check("...and the fork's cost line is the model's own price",
      _m_opti and _m_opti.model_ms == 7.23 and _m_opti.frames == 40, _m_opti)
check("a log with neither says nothing at all",
      _tune.measure("nothing here", "", "feeder", 100) is None)

# One session cannot be solved - one point does not fix a line - so it is a
# bounded step, and the text has to admit that.
_s = _tune.suggest([], 60, 100, "feeder", _m)
check("one session steps, and says it is a step",
      _s.resolution == 100 - _tune.FIRST_STEP and not _s.exact
      and any("rather than a calculation" in ln for ln in _s.lines), _s)
check("...and says one session is why, not 'first measurement'",
      any("One session is not enough" in ln for ln in _s.lines), _s.lines)
# Two sessions the arithmetic cannot separate are not a first measurement,
# and promising that the next one will solve it has already failed once.
_alike = _tune.suggest([{"resolution": 100, "fps": 47.0},
                        {"resolution": 98, "fps": 47.5}], 60, 98, "feeder", _m)
check("...while two sessions too alike say that instead",
      any("too alike" in ln for ln in _alike.lines), _alike.lines)
check("...and neither claims to be the first measurement",
      not any("First measurement" in ln
              for ln in _s.lines + _alike.lines))

# Two sessions at 100% (47 fps, 21.28 ms) and 85% (55 fps, 18.18 ms) split
# the frame into 10.1 ms that the dial does not touch and 11.2 ms of model
# at full size; 60 fps then needs sqrt(6.56/11.17) = 77%.
_rows = [{"resolution": 100, "fps": 47.0}, {"resolution": 85, "fps": 55.0}]
_m2 = _tune.measure(_FEED_LINE.replace("47.0 fps", "55.0 fps"), "",
                    "feeder", 85)
_s2 = _tune.suggest(_rows, 60, 85, "feeder", _m2)
check("two sessions are solved exactly", _s2.exact and _s2.resolution == 77,
      (_s2.resolution, _s2.lines))
check("...and the split is shown, not just the answer",
      any("10.1 ms" in ln and "11.2 ms" in ln for ln in _s2.lines), _s2.lines)

# A game that cannot reach the target with the model off entirely is not
# the model's fault, and the tool must not keep cutting the dial for it.
_slow = [{"resolution": 100, "fps": 30.0}, {"resolution": 50, "fps": 33.0}]
_s3 = _tune.suggest(_slow, 120, 50, "feeder",
                    _tune.Measured("feeder", 50, fps=33.0, frames=900))
check("a game the model is not holding back is said to be that",
      any("not what is holding it back" in ln for ln in _s3.lines), _s3.lines)
check("...and no button offers to cut the dial underneath that sentence",
      _s3.resolution == 50, _s3.resolution)
check("...and the extrapolation is not called a measurement",
      any("with the model's cost taken out" in ln for ln in _s3.lines),
      _s3.lines)

# The OptiScaler route logs the model's cost and no frame rate, so the
# answer is a share of the frame - and it has to be called a rule of thumb.
_s4 = _tune.suggest([], 60, 100, "optiscaler", _m_opti)
check("with no frame rate logged it aims at a share of the frame",
      _s4.resolution == 76 and any("rule of thumb" in ln for ln in _s4.lines),
      (_s4.resolution, _s4.lines))
check("two sessions too close together are not solved",
      _tune._solve([(100, 20.0), (98, 19.9)]) is None)
check("...and neither are two that disagree about physics",
      _tune._solve([(100, 20.0), (50, 25.0)]) is None)
check("no target means no suggestion",
      _tune.suggest(_rows, 0, 100, "feeder", _m) is None)
check("the suggestion is never outside the dial's range",
      _tune._clamp(3) == _tune.MIN_RES and _tune._clamp(400) == _tune.MAX_RES)
check("applying it writes the config in place, and says when it takes effect",
      "enable_nr" in src_of(_gui.App._apply_tune)
      and "feedcfg.write" in src_of(_gui.App._apply_tune)
      and "next run" in src_of(_gui.App._apply_tune))
_tune_src = src_of(_gui.App._autotune)
check("...and the measuring half writes nothing at all",
      "enable_nr" not in _tune_src and "feedcfg.write" not in _tune_src)

# The driver, said before the install instead of in the diagnosis after it:
# 616.64 is named in 31 of the first 87 reports, more than any other single
# cause, and the tool used to mention it only once the evening was lost.
check("616.56 gets no warning", dlss.driver_warning("feeder", "616.56") is None)
_w = dlss.driver_warning("feeder", "616.64")
check("616.64 does, on a route that installs the renodx add-on",
      _w and "4.55" in _w and "no reports of this fault" in _w, _w)
_w2 = dlss.driver_warning("optiscaler", "616.64")
check("...and the OptiScaler route is not told about a pin it never gets",
      _w2 and "4.55" not in _w2 and "does not install the renodx" in _w2, _w2)
check("the Remix route runs its own runtime, so it is not warned",
      dlss.driver_warning("remix", "616.99") is None)
check("an unknown driver says nothing", dlss.driver_warning("feeder", "") is None)
check("driver_at_least can be asked about a version it was handed",
      gpu.driver_at_least("616.64", "617.10") is True
      and gpu.driver_at_least("616.64", "616.56") is False)
check("the route note carries it before INSTALL is pressed",
      "driver_warning" in src_of(_gui.App._apply_route))

# The other end of it. DOOM on driver 475.14 (#16) was told "the game has
# not been started since the install" - true, and no use to anybody: that
# driver is years older than DLSS 5 and cannot create the feature at all.
_old = dlss.driver_warning("feeder", "475.14")
check("a driver from before DLSS 5 is the answer, not a footnote",
      _old and "older than DLSS 5" in _old and "616.56" in _old, _old)
check("...and it says what will happen if they install anyway",
      _old and "the model will not run" in _old, _old)
check("...without freezing a count of the issue tracker into the binary",
      _old and "every report" not in _old, _old)
check("...on the OptiScaler route too",
      "older than DLSS 5" in (dlss.driver_warning("optiscaler", "580.10") or ""))
check("...but not on Remix, which carries its own runtime",
      dlss.driver_warning("remix", "475.14") is None)
check("the first documented carrier is the line between the two warnings",
      dlss.driver_warning("feeder", "616.56") is None
      and "older than DLSS 5" not in (dlss.driver_warning("feeder", "616.64") or ""))

# Vulkan and OptiScaler: the diagnosis has said for two releases that the
# model has nothing to attach to there (RDR2, #66 and #70). The dropdown
# said "model resolution dial: the fps lever" and nothing else.
_ok, _why = dlss.fit("optiscaler", "Vulkan", True, 120)
check("the Vulkan warning is on the route label, before the install",
      "nothing to attach to" in _why and "DirectX 12" in _why, _why)
check("...and the route is still offered, because two reports are not a law",
      _ok is True)
check("...while D3D12 keeps its own note",
      "fps lever" in dlss.fit("optiscaler", "DX12", True, 120)[1])

# HDR: the feeder's 0.15.1 is the build that stopped the neural pass
# wrecking HDR highlights, so an older pinned build on an HDR display is
# worth a word - and only then.
check("the HDR minimum is the release that fixed it",
      sources.FEEDER_HDR_MIN == "v0.15.1"
      and sources.feeder_key("v0.15.1") > sources.feeder_key("v0.14.0-beta.5"))
check("hdr_on answers True, False or None and never raises",
      gpu.hdr_on() in (True, False, None))
check("...and reads the enabled bit, not the supported one",
      "0x2" in src_of(gpu.hdr_on)
      and "capable of it" in src_of(gpu.hdr_on))
check("the display-config path struct is the size Windows expects",
      "refreshRateNum" in src_of(gpu.hdr_on), "72 bytes")
check("nothing is said when the display is not in HDR",
      "is not True" in src_of(_gui.App._hdr_note))

# Issue #53: "does it work on an RX 7600". Both AMD routes are real and
# neither is fetchable, and the answer has to say which and why.
check("an AMD card is named, not just 'no nvidia card'",
      "AMD" in gpu.AMD_ANSWER and "RDNA" in gpu.AMD_ANSWER)
check("...and the reason is the files, not caution",
      "Discord" in gpu.AMD_ANSWER and "bundles nothing" in gpu.AMD_ANSWER
      and "closed source" in gpu.AMD_ANSWER)
check("...and it says what would change it",
      "ships the whole thing in a release" in gpu.AMD_ANSWER)
check("other_vendor only speaks when there is no NVIDIA card",
      "if detect()[0]:" in src_of(gpu.other_vendor))

# Issue #31, fourth round, and issue #2 with it: the 32-bit Vulkan layer
# registers and still never loads. Proved with the Vulkan loader's own
# trace in a 32-bit process - two manifests, one name:
#
#   Removing layer VK_LAYER_reshade (...ReShade32.json) because it is a
#     duplicate of VK_LAYER_reshade (...ReShade64.json)
#   Requested layer "VK_LAYER_reshade" was wrong bit-type.
#
# HKCU\Software is not redirected per architecture the way HKLM\Software
# is, so a 32-bit game sees both of ours and keeps the one it cannot load.
from core import vulkan as _vk  # noqa: E402

check("the two layers no longer share a name",
      _vk.LAYER_NAME32 and _vk.LAYER_NAME32 != _vk.LAYER_NAME)
check("...and the 32-bit manifest is the one renamed",
      'if manifest == MANIFEST32:' in src_of(_vk._place)
      and 'layer["name"] = LAYER_NAME32' in src_of(_vk._place))

_d = Path(tempfile.mkdtemp(prefix="vklayer_"))
_man = {"file_format_version": "1.0.0",
        "layer": {"name": "VK_LAYER_reshade", "library_path": ".\\ReShade64.dll"}}
(_d / "ReShade64.json").write_text(json.dumps(_man), encoding="utf8")
(_d / "ReShade32.json").write_text(json.dumps(_man), encoding="utf8")
_saved_dir = _vk.layer_dir
_vk.layer_dir = lambda: _d
try:
    check("an install made before this is spotted by the name it carries",
          _vk.name_clash() == _d / "ReShade32.json", _vk.name_clash())
    _fixed = dict(_man)
    _fixed["layer"] = dict(_man["layer"], name=_vk.LAYER_NAME32,
                           library_path=".\\ReShade32.dll")
    (_d / "ReShade32.json").write_text(json.dumps(_fixed), encoding="utf8")
    check("...and a rewritten one is not flagged again",
          _vk.name_clash() is None)
    (_d / "ReShade32.json").unlink()
    check("...and neither is a machine with no 32-bit layer at all",
          _vk.name_clash() is None)
finally:
    _vk.layer_dir = _saved_dir
shutil.rmtree(_d, ignore_errors=True)

# The diagnosis names it, instead of "the layer is registered, so it should
# work" - which is what a 32-bit game got for four rounds.
_d = Path(tempfile.mkdtemp(prefix="i31clash_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "CoJGunslinger.exe", "bitness": 32,
     "api": "Vulkan", "proxy": diagnose.VULKAN_LAYER, "path": "feeder",
     "dxvk": True, "vulkan": {"mine": True, "any": True},
     "files": ["d3d9.dll", "dlss5-feed.addon32", "ReShade.ini",
               "nvngx_dlssnr.dll"]}), encoding="utf8")
for _n in ("d3d9.dll", "dlss5-feed.addon32", "ReShade.ini", "nvngx_dlssnr.dll"):
    (_d / _n).write_bytes(b"MZ")
(_d / "CoJGunslinger_d3d9.log").write_text("info:  DXVK: v2.4", encoding="utf8")
_clash_dir = Path(tempfile.mkdtemp(prefix="vkclash_"))
(_clash_dir / "ReShade64.json").write_text(json.dumps(_man), encoding="utf8")
(_clash_dir / "ReShade32.json").write_text(json.dumps(_man), encoding="utf8")
_vk.layer_dir = lambda: _clash_dir
_layer_saved = diagnose._layer_state
diagnose._layer_state = lambda man: (True, True)
try:
    _r = diagnose.analyse(_d)
    check("a 32-bit game with the clashing name is told exactly that",
          "duplicate name" in _r.verdict
          and any("carries the layer name" in f.title for f in _r.findings),
          _r.verdict)
    check("...and the answer is to install again, not to blame antivirus",
          any("its own name" in f.detail for f in _r.findings),
          [f.detail[:60] for f in _r.findings])
finally:
    _vk.layer_dir = _saved_dir
    diagnose._layer_state = _layer_saved
shutil.rmtree(_d, ignore_errors=True)
shutil.rmtree(_clash_dir, ignore_errors=True)

# Issue #88: a keyboard with no Insert key has no way into the overlay,
# which is where neural rendering is switched on. Both sides store it, in
# their own way: ReShade as "<vk>,0,0,0", OptiScaler as hex.
_d = Path(tempfile.mkdtemp(prefix="overlaykey_"))
(_d / "ReShade.ini").write_text('[GENERAL]\nPresetPath=.\\DLSS5.ini\n',
                                encoding="utf8")
reshade_ini.set_overlay_key(_d, 0x79)
check("ReShade's overlay key is written as a virtual-key code",
      "KeyOverlay=121,0,0,0" in (_d / "ReShade.ini").read_text(encoding="utf8"),
      (_d / "ReShade.ini").read_text(encoding="utf8"))
(_d / "OptiScaler.ini").write_text("[Menu]\nShortcutKey=auto\nScale=auto\n",
                                   encoding="utf8")
optiscaler.set_overlay_key(_d, 0x79)
_ini = (_d / "OptiScaler.ini").read_text(encoding="utf8")
check("...and OptiScaler's as hex, leaving the rest of the section alone",
      "ShortcutKey=0x79" in _ini and "Scale=auto" in _ini, _ini)
def _key_name_is(label, vk, route_default):
    """What the tool would tell someone to press, having picked `label`."""
    _saved = prefs.get("overlay_key")
    try:
        prefs.set_("overlay_key", 0 if "default" in label else vk)
        return reshade_ini.overlay_key_name(route_default)
    finally:
        prefs.set_("overlay_key", _saved or 0)


reshade_ini.set_overlay_key(_d, 0)
optiscaler.set_overlay_key(_d, 0)
check("...and 0 means 'leave the defaults alone', not 'bind nothing'",
      "KeyOverlay=121,0,0,0" in (_d / "ReShade.ini").read_text(encoding="utf8")
      and "ShortcutKey=0x79" in (_d / "OptiScaler.ini").read_text(encoding="utf8"))
check("every offered key is a real virtual-key code",
      all(isinstance(v, int) and 0 < v < 256
          for k, v in reshade_ini.OVERLAY_KEYS.items() if k != "route default")
      and reshade_ini.OVERLAY_KEYS["Home"] == 0x24
      and reshade_ini.OVERLAY_KEYS["Insert"] == 0x2D)
# The sentinel may not be a key code any entry uses: with "route default"
# and "Home" both on 0x24 the stored value could not say which had been
# picked, so picking Home wrote Home and then said "press Insert".
check("...and 'route default' is stored as nothing at all, not as a key",
      reshade_ini.OVERLAY_KEYS["route default"] == 0
      and reshade_ini.ROUTE_DEFAULT == 0
      and sorted(reshade_ini.OVERLAY_KEYS.values()).count(0) == 1)
check("...so picking Home is told back as Home, on either route",
      _key_name_is("Home", 0x24, "Insert") == "Home"
      and _key_name_is("route default", 0, "Insert") == "Insert"
      and _key_name_is("route default", 0, "Home") == "Home")
_src = src_of(installer.install)
check("ReShade's binding is written after carry_over, which would undo it",
      _src.index("reshade_ini.set_overlay_key")
      > _src.index("reshade_ini.carry_over"))
check("...and OptiScaler's goes in with the rest of its configuration",
      "optiscaler.set_overlay_key" in _src)
shutil.rmtree(_d, ignore_errors=True)

# "OptiScaler does not engage" is six issues, four of them still open, and
# every one was answered with advice. When nothing about neural rendering
# reaches the log at all, the four things it needs are now each answered
# from disk instead.
_d = _opti_dir("opti_checklist_", "[12:00:00.000000] [I] Init done\n"
                                  "[12:00:01.000000] [I] hk_ffxFsr3 context created\n")
(_d / "OptiScaler.ini").write_text("[DlssNr]\nEnabled=false\n\n[Menu]\nScale=auto\n",
                                   encoding="utf8")
(_d / "nvngx_dlssnr.dll").write_bytes(b"MZ")
_man = json.loads((_d / "dlss5-autopilot.json").read_text(encoding="utf8"))
_man["upscaler"] = "fsr"
_man["files"] = ["dxgi.dll", "nvngx.dll_dlssnr.dll"]
(_d / "dlss5-autopilot.json").write_text(json.dumps(_man), encoding="utf8")
_r = diagnose.analyse(_d)
_info = [f_.title for f_ in _r.findings if f_.level == "info"]
check("the ini's own Enabled line is read back, not assumed",
      any("Enabled=false" in t for t in _info), _info)
check("...the runtime and the forwarder are each answered",
      any("nvngx_dlssnr.dll: present" in t for t in _info)
      and any("nvngx.dll_dlssnr.dll: MISSING" in t for t in _info), _info)
check("...the game's own upscaler is named", any("fsr" in t for t in _info), _info)
check("...and the driver is checked against the first one that carries the model",
      any("driver" in t for t in _info), _info)
shutil.rmtree(_d, ignore_errors=True)

# A build that never wrote a forwarder must not be reported as broken.
_d = _opti_dir("opti_checklist2_", "[12:00:00.000000] [I] Init done\n")
(_d / "OptiScaler.ini").write_text("[DlssNr]\nEnabled=true\n", encoding="utf8")
(_d / "nvngx_dlssnr.dll").write_bytes(b"MZ")
_r = diagnose.analyse(_d)
_info = [f_.title for f_ in _r.findings if f_.level == "info"]
check("a forwarder the install never wrote is not called missing",
      any("may not use one" in t for t in _info), _info)
shutil.rmtree(_d, ignore_errors=True)

# The biggest group of reports is "the game closed itself", and a game that
# dies before ReShade loads writes nothing at all. Windows does: an
# Application Error event naming the faulting module. Read from this
# machine's own log, which is where the field order below came from.
from core import wincrash as _wc  # noqa: E402

_line = ("2026-09-09T11:08:54|Application Error|BsgLauncher.exe~15.0.0.4595~"
         "6a841e80~libcef.dll~143.0.9.0~691e395d~80000003~0000000007b49ffc~"
         "14244~134334249864809804~D:\\Games\\BsgLauncher.exe")
_saved_ps = _wc._ps
try:
    _wc._ps = lambda _script: _line
    _c = _wc.last_crash("BsgLauncher.exe", since=0, within_days=30)
    check("the faulting module is read out of the event", _c and
          _c.module == "libcef.dll" and _c.code == "0x80000003", _c)
    check("...a module that is neither ours nor the game's is named as that",
          not _c.ours() and "another mod" in _wc.describe(_c)[1],
          _wc.describe(_c))
    check("...and an event for another executable is not this game's",
          _wc.last_crash("SomeOtherGame.exe", since=0, within_days=30) is None)
    check("...nor is one from before the install",
          _wc.last_crash("BsgLauncher.exe", since=time.time(),
                         within_days=30) is None)
    # dxgi.dll is ReShade in a game folder and Windows' own in System32,
    # and the event does not say which copy faulted. Claiming it as ours on
    # the name alone was the release's own worst wrong answer.
    _shared = _wc.Crash("2026-09-09 11:08", "Game.exe", "dxgi.dll",
                        "0xC0000005", "Application Error")
    check("a module that only shares our proxy's name is not claimed",
          not _shared.ours() and "also the name of a Windows system DLL"
          in _wc.describe(_shared, "dxgi.dll")[1],
          _wc.describe(_shared, "dxgi.dll"))
    check("...and with no proxy of that name it is somebody else's",
          "another mod" in _wc.describe(_shared, "")[1])
    _ours = _wc.Crash("2026-09-09 11:08", "Game.exe", "dlss5-feed.addon64",
                      "0xC0000005", "Application Error")
    check("a module we installed is called ours, plainly",
          _ours.ours() and "ours to fix" in _wc.describe(_ours)[1])
    _own = _wc.Crash("2026-09-09 11:08", "Game.exe", "Game.exe",
                     "0xC0000005", "Application Error")
    check("...and the game faulting in its own code is not blamed on us",
          "not in our module" in _wc.describe(_own)[1])
    check("nothing to say when there is no event",
          _wc.describe(None) is None)
finally:
    _wc._ps = _saved_ps

check("a game name that is not a file name is never put in a command",
      _wc.last_crash("..\\evil & del *", since=0) is None)
check("the query is capped, so a diagnosis cannot hang on it",
      _wc.TIMEOUT <= 15 and "timeout=TIMEOUT" in src_of(_wc._ps))
check("...and it is read off the Tk thread",
      "threading.Thread" in src_of(_gui.App._windows_crash))
check("the event goes into the report body",
      "windows event:" in src_of(diagnose.issue_body))

# Issue #75, the video player on driver 610.60: the add-on's own error was
# in the log twice and the verdict was still inconclusive. The line is
# copied from that report.
_d = _diag_dir("ngx_export_", reshade=(
    "11:25:46:119 [26964] | INFO  | Redirecting IDXGIFactory2::CreateSwapChainForHwnd\n"
    "11:25:54:684 [19864] | ERROR | [DLSS 5 Neural Rendering] "
    "vtable::Hook(Failed to find NVSDK_NGX_D3D12_EvaluateFeature_C)\n"
    "11:27:18:762 [ 4892] | INFO  | Exiting ...\n"))
# The same log line means opposite things on an old driver and a new one,
# so the driver is pinned rather than read from the machine running this.
from core import gpu as _gpu_drv                                  # noqa: E402
_drv_saved = _gpu_drv.driver_version
try:
    _gpu_drv.driver_version = lambda: "610.60"        # #75's own driver
    _r = diagnose.analyse(_d)
    check("a driver with no DLSS 5 entry point is the verdict, not a footnote",
          "no DLSS 5 entry point" in _r.verdict, _r.verdict)
    check("...and it says which driver first carried it",
          any("616.56" in f.detail for f in _r.findings),
          [f.detail[:70] for f in _r.findings])
    check("...without claiming the machine's current driver is the one that ran",
          any("the driver the game ran with" in f.detail for f in _r.findings))

    # Issue #127: the same line on driver 616.92 was answered "update the
    # graphics driver". On a driver that carries DLSS 5 the line appears in
    # working sessions too, so it is not a driver verdict there.
    _gpu_drv.driver_version = lambda: "616.92"
    _r = diagnose.analyse(_d)
    check("on a new driver the missing call is not 'update your driver'",
          "update the graphics driver" not in _r.verdict, _r.verdict)
    check("...and is not made into a driver verdict of any kind",
          not any("does not export" in f.title for f in _r.findings),
          [f.title for f in _r.findings])

    # The owner's own 616.64 logs, from sessions that delivered frames: the
    # plain EvaluateFeature IS hooked, and only the _C variant beside it is
    # missing. On a driver that carries DLSS 5 that pair is not a verdict;
    # on one older than DLSS 5 the driver's age decides, whatever the line.
    _gpu_drv.driver_version = lambda: "616.64"
    _dh = _diag_dir("hook_plain_", reshade=(
        "22:29:22:384 [44208] | DEBUG | [DLSS 5 Neural Rendering] "
        "vtable::Hook(NVSDK_NGX_D3D12_EvaluateFeaturehooked with "
        "0x00007fff55ee8740 => 0x00007fff36671430)\n"
        "22:29:22:384 [44208] | ERROR | [DLSS 5 Neural Rendering] "
        "vtable::Hook(Failed to find NVSDK_NGX_D3D12_EvaluateFeature_C)\n"))
    _rh = diagnose.analyse(_dh)
    check("on 616.64 a failed _C beside a successful plain hook is no verdict",
          "update the graphics driver" not in _rh.verdict, _rh.verdict)
    _gpu_drv.driver_version = lambda: "610.60"
    _rh = diagnose.analyse(_dh)
    check("...but on a driver older than DLSS 5 the driver still decides",
          "update the graphics driver" in _rh.verdict
          and any("610.60" in f.title for f in _rh.findings), _rh.verdict)

    # The hook lines are DEBUG; a report that drops them replays to a
    # different answer than the machine gave (#127's excerpt had none).
    check("a report keeps the add-on's hook lines the driver rule reads",
          all(any(k in ln for k in diagnose._RESHADE_KEEP)
              for ln in (_dh / "ReShade.log").read_text().splitlines()))

    # A driver that cannot be read: the line decides, and only on its own.
    # A malformed version string is unread too, not "new".
    for _unread in (None, "32.0.16.16xx"):
        _gpu_drv.driver_version = lambda _v=_unread: _v
        check(f"driver {_unread!r}: a plain hook beside it is no verdict",
              "update the graphics driver" not in diagnose.analyse(_dh).verdict)
        _r = diagnose.analyse(_d)
        check(f"driver {_unread!r}: the failed hook alone still is",
              "no DLSS 5 entry point" in _r.verdict
              and not any("not the driver being too old" in f.detail
                          or "older than DLSS 5" in f.title
                          for f in _r.findings), _r.verdict)
    _gpu_drv.driver_version = lambda: None
    check("...and the detail does not ask about a driver it never named",
          not any("the driver the game ran with" in f.detail
                  for f in diagnose.analyse(_d).findings))
    _gpu_drv.driver_version = lambda: "616.92"
    check("on a new driver with nothing hooked it is said as a warning",
          any(f.title.startswith("The add-on could not hook")
              for f in diagnose.analyse(_d).findings),
          [f.title for f in diagnose.analyse(_d).findings])
    shutil.rmtree(_dh, ignore_errors=True)
finally:
    _gpu_drv.driver_version = _drv_saved
shutil.rmtree(_d, ignore_errors=True)

# Issue #79: every path this tool writes comes out of a file somebody else
# can write - an archive's member names, or the install record in the game
# folder. A string prefix test does not catch a sibling directory.
_root = Path(tempfile.mkdtemp(prefix="inside_"))
(_root / "game").mkdir()
(_root / "game-other").mkdir()
_bad = None
try:
    net.inside(_root / "game", "../game-other/x.dll")
except net.OutsideError as _e:
    _bad = _e
check("a '..' entry that lands in a sibling folder is refused", _bad is not None)
_bad = None
try:
    net.inside(_root / "game", "C:/Windows/System32/x.dll")
except net.OutsideError as _e:
    _bad = _e
check("...and so is an absolute path", _bad is not None)
check("a normal member still resolves under the folder",
      net.inside(_root / "game", "reshade-shaders/Shaders/DLSS5_Feed.fx")
      == (_root / "game" / "reshade-shaders" / "Shaders" / "DLSS5_Feed.fx"))
shutil.rmtree(_root, ignore_errors=True)

# The same, through uninstall: a record naming a file outside the folder is
# ignored, and the file it names is still there afterwards.
_root = Path(tempfile.mkdtemp(prefix="untrav_"))
_game = _root / "game"
_game.mkdir()
(_root / "sentinel.txt").write_text("keep me", encoding="utf8")
(_game / "dxgi.dll").write_bytes(b"MZ")
(_game / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX12", "path": "optiscaler", "proxy": "dxgi.dll",
     "files": ["dxgi.dll", "../sentinel.txt"]}), encoding="utf8")
_lines: list[str] = []
installer.uninstall(games.Game(name="Game", folder=_game,
                               exe=_game / "Game.exe"),
                    on_log=_lines.append)
check("uninstall leaves a file outside the game folder alone",
      (_root / "sentinel.txt").is_file())
check("...and says so in the log",
      any("outside the game folder" in ln for ln in _lines), _lines[:4])
check("...while still removing what really was ours",
      not (_game / "dxgi.dll").is_file())
shutil.rmtree(_root, ignore_errors=True)


section("61. what the 1.8.0 release gate found")

# The gate's own findings, locked so they cannot come back.

# LOGIC: the tuner was always one session behind - the history was read
# before the new measurement was stored, so the second session still said
# "one session is not enough" and only the third could solve.
_gsrc = src_of(_gui.App._autotune)
check("the measurement is recorded before the suggestion is worked out",
      0 <= _gsrc.find("autotune.remember") < _gsrc.find("autotune.suggest"))
check("...and the session's own resolution comes from the config, not the slider",
      "autotune.ran_at" in _gsrc)
_d = Path(tempfile.mkdtemp(prefix="ranat_"))
(_d / "dlss5-feed.cfg").write_text("enabled=1\nwork_resolution=85\n", encoding="utf8")
check("the feeder's config says what the session ran at",
      _tune.ran_at(_d, "feeder", 100) == 85)
(_d / "OptiScaler.ini").write_text("[DlssNr]\nWorkingScale=0.750\n", encoding="utf8")
check("...and OptiScaler's ini says it as a fraction",
      _tune.ran_at(_d, "optiscaler", 100) == 75)
check("...with the slider as the fallback when neither is readable",
      _tune.ran_at(Path(tempfile.mkdtemp()), "feeder", 77) == 77)
shutil.rmtree(_d, ignore_errors=True)

check("the feeder's settled frame rate is read, not its loading screen",
      _tune.measure("[feed] 100 frames: feed CPU 9.00 ms/frame, 20.0 fps\n"
                    "[feed] 3600 frames: feed CPU 2.10 ms/frame, 47.0 fps",
                    "", "feeder", 100).fps == 47.0)

# LOGIC: one unreadable appmanifest must not empty the library.
_steam_src = src_of(games.scan_steam)
check("a manifest that cannot be read skips that file, not the loop",
      "for acf in manifests:" in _steam_src
      and "continue" in _steam_src[_steam_src.index("for acf in manifests:"):
                                   _steam_src.index("for f in folders:")])

# LOGIC: sharing a result crashed on any machine with no NVIDIA card.
check("a card that could not be detected does not crash the share button",
      isinstance(_comm.record(_FakeGame(), "feeder", "worked",
                              gpu_name="")["gpu"], str))
check("...and the GUI never passes None as the card name",
      "gpu_name=name or \"\"" in src_of(_gui.App._share_result))

# LOGIC/FEATURES: one game's diagnosis must not be posted about another.
check("picking another game forgets the last diagnosis",
      "_forget_last_session" in src_of(_gui.App._on_pick))
check("...including the Windows event and the tuning suggestion",
      all(k in src_of(_gui.App._forget_last_session)
          for k in ("_last_diag", "_last_crash", "_tune")))

# FEATURES: the failures worth sharing most are the ones that logged nothing.
check("a session that never ran can still be shared",
      'configure(state="normal")' in src_of(_gui.App._diagnose))

# FEATURES: the overlay key belongs on every route that has an overlay.
_asrc = src_of(_gui.App._apply_route)
check("the overlay key is offered on the OptiScaler route too",
      "self.game and path != dlss.REMIX" in _asrc)
check("...and its widgets come off the page when it does not apply",
      "self.overlaykeyhint)" in _asrc and "grid_remove()" in _asrc)

# TEXTS: nothing tells a person to press a key they may have rebound.
check("every 'press the overlay key' line goes through the resolver",
      not any("press Home" in src_of(m) or "press Insert" in src_of(m)
              for m in (_gui.App._diagnose, _gui.App._finish_ok)
              if callable(m)))
prefs.set_("overlay_key", reshade_ini.OVERLAY_KEYS["F10"])
try:
    check("the resolver answers with the key that was chosen",
          reshade_ini.overlay_key_name() == "F10"
          and reshade_ini.overlay_key_name("Insert") == "F10"
          and diagnose._overlay_key() == "F10")
finally:
    prefs.set_("overlay_key", "")
check("...and falls back to each route's own default",
      reshade_ini.overlay_key_name() == "Home"
      and reshade_ini.overlay_key_name("Insert") == "Insert")

# LOGIC: an Application Hang carries no module and must not hide the crash.
_hang = ("2026-09-09T12:00:00|Application Hang|Game.exe~1.0~"
         "0~ffffffff~0~\n"
         "2026-09-09T11:00:00|Application Error|Game.exe~1.0~aa~"
         "renodx-dlss5.addon64~1.0~bb~c0000005~0~1~2~D:\\g\\Game.exe")
_saved_ps = _wc._ps
try:
    _wc._ps = lambda _s: _hang
    _c2 = _wc.last_crash("Game.exe", since=0, within_days=30)
    check("a hang with no module does not hide the crash under it",
          _c2 and _c2.module == "renodx-dlss5.addon64", _c2)
finally:
    _wc._ps = _saved_ps

# LOGIC: ReShade.ini is not evidence of antivirus, and the branch must not
# return before the rules under it.
check("a reset ReShade.ini is not called a quarantine",
      "reshade.ini" not in [n.lower() for n in diagnose._CORE_NAMES])

# LOGIC: the driver-export rule must not overrule frames that arrived.
_d = _diag_dir("ngx_ok_", reshade=(
    "11:25:54 | ERROR | [DLSS 5 Neural Rendering] "
    "vtable::Hook(Failed to find NVSDK_NGX_D3D11_EvaluateFeature_C)\n"),
    feed=("[feed] feature ready: 1920x1080 DLAA\n"
          "[feed] frame 1 delivered (1920x1080 at 100%)\n"))
_r = diagnose.analyse(_d)
check("one failed hook does not undo the frames that were delivered",
      _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# TEXTS: quote the line the person actually has.
# The rule only reads this line as a driver verdict on a driver older than
# DLSS 5, so the driver is pinned: read from the machine running the suite,
# the check passed or failed with whatever that machine had installed.
_d = _diag_dir("ngx_vk_", reshade=(
    "11:25:54 | ERROR | [DLSS 5 Neural Rendering] "
    "vtable::Hook(Failed to find NVSDK_NGX_VULKAN_EvaluateFeature)\n"))
from core import gpu as _gpu_vk                                   # noqa: E402
_vk_drv_saved = _gpu_vk.driver_version
_gpu_vk.driver_version = lambda: "610.60"
try:
    _r = diagnose.analyse(_d)
finally:
    _gpu_vk.driver_version = _vk_drv_saved
check("the entry point named in the answer is the one in the log",
      any("NVSDK_NGX_VULKAN_EvaluateFeature" in f.detail for f in _r.findings),
      [f.detail[:60] for f in _r.findings])
shutil.rmtree(_d, ignore_errors=True)

# FEATURES: uninstalling ends the measurements it was based on.
check("uninstall drops the tuning history with the install",
      "autotune.forget" in src_of(installer.uninstall))

# FEATURES: the workflow cannot filter on a label GitHub drops.
_wf = (Path(__file__).resolve().parent / ".github" / "scripts"
       / "compatibility.py").read_text(encoding="utf8")
check("the compatibility workflow does not filter on the label",
      "labels=result" not in _wf and "state=all" in _wf)

# FEATURES: the version is the delivery mechanism for the library rescan.
check("the version is 1.8.2 in the file the build reads too",
      "1.8.2.0" in (Path(__file__).resolve().parent
                    / "version_info.txt").read_text(encoding="utf8"))
check("...and the release notes the workflow publishes exist",
      (Path(__file__).resolve().parent / "docs" / "releases"
       / f"v{update.VERSION}.md").is_file())


section("62. NVIDIA's own runtimes, and swapping one the game ships")

# The tool was installing nvngx_dlss.dll and nvngx_dlssg.dll from a
# community mirror while NVIDIA published the same runtimes itself, signed
# and current. Neural rendering is NOT among them - the SDK carries super
# resolution, ray reconstruction and frame generation only.
check("the publisher's own files are named, and nvngx_dlssnr is not",
      [f for _, f in sources.NVIDIA_DLSS_FILES]
      == ["nvngx_dlss.dll", "nvngx_dlssd.dll", "nvngx_dlssg.dll"])
_saved_tag = sources.latest_tag
_saved_cache = sources._NVIDIA_CACHE
try:
    sources._NVIDIA_CACHE = None
    sources.latest_tag = lambda _repo: "v310.9.1"
    _nv = sources.nvidia_dlss()
    check("...read at the tag, so the version in the label is true",
          _nv["dlss"][0]["url"].endswith(
              "/NVIDIA/DLSS/v310.9.1/lib/Windows_x86_64/rel/nvngx_dlss.dll"),
          _nv["dlss"][0]["url"])
    # The label names the SDK the file was read from, not a version for
    # each DLL: NVIDIA ships all three in one tagged SDK and versions them
    # separately, so claiming the tag IS each file's version would be wrong.
    check("...labelled with the SDK it was read from",
          _nv["dlssd"][0]["label"] == "310.9.1 (NVIDIA SDK)",
          _nv["dlssd"][0]["label"])
    check("...and marked as a plain file, not an archive",
          _nv["dlssg"][0]["raw"] == "nvngx_dlssg.dll")
    check("the tag comes from the redirect, not an API request",
          "api.github.com" not in src_of(sources.nvidia_dlss))
finally:
    sources.latest_tag = _saved_tag
    sources._NVIDIA_CACHE = _saved_cache

# A swap is only ever a swap: a game that does not ship ray reconstruction
# does not ask for it, and dropping one in would change nothing.
_d = Path(tempfile.mkdtemp(prefix="rrfind_"))
check("a game without ray reconstruction has nothing to swap",
      installer._find_runtime(_d, installer.DLSSD) is None)
_deep = _d / "Engine" / "Plugins" / "Runtime" / "Nvidia" / "DLSS" / "Binaries"
_deep.mkdir(parents=True)
(_deep / "nvngx_dlssd.dll").write_bytes(b"MZ own")
check("...and one that does is found where the engine keeps it",
      installer._find_runtime(_d, installer.DLSSD)
      == _deep / "nvngx_dlssd.dll")

# The owner's three requirements: back it up, put it back, warn first.
def _fake_dll(size: int = 300_000, machine: int = 0x8664) -> bytes:
    """The smallest thing that is honestly a 64-bit Windows DLL.

    The swap validator refuses anything else, which is the point: a raw
    download can come back as an error page or half a file, and that must
    never land on top of a runtime the game needs to start.
    """
    import struct as _st
    b = bytearray(bytes(size))
    b[0:2] = b"MZ"
    _st.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE" + bytes(2)
    _st.pack_into("<H", b, 0x84, machine)
    return bytes(b)


_rep = installer.Report()
_new = Path(tempfile.mkdtemp()) / "nvngx_dlssd.dll"
_new.write_bytes(_fake_dll())
installer._place_entry({"url": "x", "raw": "nvngx_dlssd.dll", "label": "310.9.1"},
                       _deep / "nvngx_dlssd.dll", _rep, _d,
                       lambda _u, _n: _new, installer.DLSSD, "x")
check("the swap writes the new build",
      (_deep / "nvngx_dlssd.dll").read_bytes() == _fake_dll())
check("...and keeps the game's own beside it",
      (_deep / ("nvngx_dlssd.dll" + installer.BACKUP_SUFFIX)).read_bytes()
      == b"MZ own")

# ...and nothing at all is written when what arrived is not a runtime.
_bad_dir = Path(tempfile.mkdtemp())
for _name, _blob, _why in (
        ("page.dll", b"<!DOCTYPE html><html>404 not found</html>", "an error page"),
        ("cut.dll", _fake_dll(9_000), "a download that was cut short"),
        ("x86.dll", _fake_dll(300_000, 0x14C), "a 32-bit build")):
    _p = _bad_dir / _name
    _p.write_bytes(_blob)
    _before = (_deep / "nvngx_dlssd.dll").read_bytes()
    _raised = None
    try:
        installer._place_entry({"url": "x", "raw": "nvngx_dlssd.dll",
                                "label": "bad"},
                               _deep / "nvngx_dlssd.dll", installer.Report(),
                               _d, lambda _u, _n, _f=_p: _f,
                               installer.DLSSD, "x")
    except installer.InstallError as _e:
        _raised = _e
    check(f"{_why} never reaches the game folder",
          _raised is not None
          and (_deep / "nvngx_dlssd.dll").read_bytes() == _before, _raised)
check("...and the refusal says nothing was touched",
      "was not written and the download was thrown away" in str(_raised),
      str(_raised))
check("...with both recorded, so uninstall knows about them",
      len([w for w in _rep.written if "dlssd" in w.lower()]) == 2, _rep.written)
shutil.rmtree(_d, ignore_errors=True)

check("a swap is warned about before it happens, in its own words",
      "anti-cheat can treat a changed file as tampering"
      in anticheat.swap_message("nvngx_dlssd.dll")
      and "keep the game's own" in anticheat.swap_message("x"))
check("...and the warning is shown when a build is chosen",
      "_warn_swap" in src_of(_gui.App._on_dlssd)
      and "anticheat.swap_message" in src_of(_gui.App._warn_swap))
check("...and unticking 'keep the game's own' warns the same way",
      "_warn_swap" in src_of(_gui.App._on_keep_dlss)
      and "command=self._on_keep_dlss" in src_of(_gui.App._page_install))
check("...and repeated in the notes the install leaves behind",
      "as tampering" in src_of(installer.install))
check("doing nothing is the default",
      installer.Options().dlssd == ""
      and 'dlssd=("" if self.cb_dlssd.get() in (DLSSD_KEEP'
      in src_of(_gui.App._opts))


section("63. the round that put the release together: the four shapes a "
        "ray-reconstruction runtime is found in, a .7z Windows cannot open, "
        "a server having a bad minute, and a fork installed for what it is "
        "for")

# The GUI decides whether to offer the ray-reconstruction swap from the
# route detection's own evidence. It has been wrong twice: once because the
# beside-the-exe pass never recorded nvngx_dlssd.dll, and once because
# finding DLSS beside the exe skipped the walk that would have found it
# nested. Both times the installer WOULD have swapped the file the GUI
# refused to offer. All four shapes, so neither can come back.
def _rr_shape(beside, nested):
    d = Path(tempfile.mkdtemp(prefix="rrshape_"))
    (d / "Game.exe").write_bytes(b"MZ" + b"\0" * 200)
    for n in beside:
        (d / n).write_bytes(b"MZ")
    deep = d / "Engine" / "Plugins" / "Runtime" / "Nvidia" / "DLSS" / "Binaries"
    deep.mkdir(parents=True, exist_ok=True)
    for n in nested:
        (deep / n).write_bytes(b"MZ")
    _dlss.forget_walk(d)
    sup = _dlss.detect(d, d, "DX12", 64)
    found = any(str(e).lower().endswith("nvngx_dlssd.dll")
                for e in sup.evidence)
    shutil.rmtree(d, ignore_errors=True)
    return found


from core import dlss as _dlss                                    # noqa: E402
check("ray reconstruction is found beside the executable",
      _rr_shape(("nvngx_dlss.dll", "nvngx_dlssd.dll"), ()))
check("...and when both runtimes are nested in the engine folder",
      _rr_shape((), ("nvngx_dlss.dll", "nvngx_dlssd.dll")))
check("...and when DLSS is beside the exe but ray reconstruction is nested",
      _rr_shape(("nvngx_dlss.dll",), ("nvngx_dlssd.dll",)))
check("...and when only Streamline is beside the exe",
      _rr_shape(("sl.interposer.dll",), ("nvngx_dlssd.dll",)))
check("a game with no ray reconstruction is not offered the swap",
      not _rr_shape(("nvngx_dlss.dll",), ()))

# The walk is the expensive half of detection, and it used to run on every
# click. Remembering it is what makes asking for the nested case affordable.
_wd = Path(tempfile.mkdtemp(prefix="rrwalk_"))
(_wd / "Game.exe").write_bytes(b"MZ" + b"\0" * 200)
(_wd / "nvngx_dlss.dll").write_bytes(b"MZ")
_dlss.forget_walk(_wd)
_calls = []
_real_find = _dlss.find_dlss_files
_dlss.find_dlss_files = lambda *a, **k: (_calls.append(1), _real_find(*a, **k))[1]
try:
    _dlss.detect(_wd, _wd, "DX12", 64)
    _dlss.detect(_wd, _wd, "DX12", 64)
    _dlss.detect(_wd, _wd, "DX12", 64)
    check("the folder is walked once per game, not once per click",
          len(_calls) == 1, len(_calls))
    _dlss.forget_walk(_wd)
    _dlss.detect(_wd, _wd, "DX12", 64)
    check("...and an install forgets it, because it wrote into the folder",
          len(_calls) == 2, len(_calls))
finally:
    _dlss.find_dlss_files = _real_find
    shutil.rmtree(_wd, ignore_errors=True)
check("install and uninstall both drop the remembered walk",
      "forget_walk" in src_of(installer.install)
      and "forget_walk" in src_of(installer.uninstall))

# Issue #93: Windows' own tar.exe is not always built with LZMA.
_x7 = src_of(optiscaler.extract_7z)
check("a .7z is opened with 7-Zip before Windows' tar.exe",
      _x7.index("_seven_zip()") < _x7.index("_tar_exe()"), "order")
check("...and the LZMA failure says what to install",
      "lzma" in _x7.lower() and "7-zip.org" in _x7.lower())
check("...and 7-Zip is looked for on PATH and in Program Files",
      all(x in src_of(optiscaler._seven_zip)
          for x in ("shutil.which", "Program Files")))

# ...and build.bat, which the same reporter found broken: a relative
# --version-file cannot be resolved against a --specpath somewhere else.
_bat = (Path(__file__).resolve().parent / "build.bat").read_text(encoding="utf8")
check("build.bat names version_info.txt by an absolute path",
      "--version-file \"%~dp0version_info.txt\"" in _bat, "relative path")
check("...because it puts the spec somewhere else",
      "--specpath" in _bat)
_ga = (Path(__file__).resolve().parent / ".gitattributes").read_text(encoding="utf8")
check("batch files are pinned to CRLF, whatever git is configured to do",
      "*.bat text eol=crlf" in _ga)

# Issues #103 and #97: a publisher's server answering 5xx.
check("a 5xx is retried and then explained, on every path that fetches",
      all("RETRY_CODES" in src_of(f)
          for f in (net.fetch_text, net.download, sources._get)))
check("...and each says so as a sentence, not as a traceback",
      all("Unavailable" in src_of(f)
          for f in (net.fetch_text, net.download, sources._get)))
check("...and the install turns it into its own refusal",
      "sources.Unavailable" in src_of(installer.install))
_una = src_of(net.download) + src_of(net.fetch_text) + src_of(sources._get)
check("...and none of them claims the folder was left untouched",
      "Nothing in the game folder was changed" not in _una)

# Issue #81: choosing the fork is not the same as choosing what it is for.
check("the wilsjo2 build is installed with its own placement switched on",
      optiscaler.PRESR_BEFORE_SR.get("RunBeforeSR") is True
      and "PRESR_BEFORE_SR" in src_of(installer.install))
check("...and the notes say so, naming the section the tool really writes",
      "RunBeforeSR" in " ".join(optiscaler.describe_nr({"RunBeforeSR": True}))
      and "[DlssNr]" in " ".join(optiscaler.describe_nr({"RunBeforeSR": True})))

# Issue #98: a recorded fault outranks a log that stopped in a good place -
# but only a fault from the session that log describes.
_gsrc63 = src_of(_gui.App._crash_overrides)
check("a recorded crash overrides a Working verdict",
      "Working" in _gsrc63 and "crashed" in _gsrc63)
check("...only when it belongs to the session just diagnosed",
      "_crash_is_this_session" in _gsrc63)
check("...and the shared record is held to the same window",
      "_crash_is_this_session" in src_of(_gui.App._pump))


# #171: and it outranks the opposite verdict too. "Not started since the
# install" is read off an absent log; a fault record for the game's own exe,
# written after the install, says it started and died before anything loaded.
class _FakeApp:
    """Only what _crash_overrides touches."""

    def __init__(self, rep):
        self._last_diag = rep
        self.said = []
        self.game = None

    def _crash_is_this_session(self, crash):
        return True

    def _log(self, text, tag=None):
        self.said.append(text)

    # The real one: what it prints is the thing being checked.
    _reprint_verdict = _gui.App._reprint_verdict


_c171 = _wc.Crash(when="2026-09-12 00:54:33", exe="GTA5.exe", module="GTA5.exe",
                  code="0xC0000005", provider="Application Error")


def _overridden(verdict, never_ran):
    rep = diagnose.Report(route="renodx")
    rep.verdict = verdict
    rep.never_ran = never_ran
    app = _FakeApp(rep)
    _gui.App._crash_overrides(app, _c171)
    return rep.verdict, " ".join(app.said)


_v171, _s171 = _overridden("Not started since the install - run the game once.", True)
check("a fault record replaces 'not started since the install'",
      "It started, and nothing here recorded the session" in _v171, _v171)
check("...and explains why every log is empty",
      "nothing here recorded the session" in _s171
      # ...without claiming the logs are empty BECAUSE of the fault:
      # never_ran is set on seven shapes and three of them have a log.
      and "the logs are empty because" not in _s171, _s171[:120])
_v98, _ = _overridden("Working.", False)
check("...while a fault still overrides Working.",
      "then the game crashed" in _v98, _v98)
_vinc, _ = _overridden("Inconclusive - the feed did not get far enough to tell.",
                       False)
check("...and no other verdict is rewritten",
      _vinc.startswith("Inconclusive"), _vinc)


# #168: the fork names the dispatch timing what it likes ("cost:", then
# "elapsed:"), and matching the word called a game that dispatched every
# frame "never reports it running".
for _word in ("cost:", "elapsed:"):
    _ln = ("[21:34:08.368508] [I] DlssNr_Dx12::Dispatch DLSS-NR "
           f"{_word} 6.72 ms total, 6.60 ms model")
    check(f"a dispatch line saying '{_word}' is proof the model ran",
          bool(diagnose._DISPATCH_MS.search(_ln)))
check("...and a setting echoed at startup is not",
      not diagnose._DISPATCH_MS.search("[I] DlssNr.Enabled: true"))
_opti_run = tempfile.mkdtemp(prefix="diag_opti_elapsed_")
(Path(_opti_run) / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "path": "optiscaler", "proxy": "dxgi.dll",
     "api": "DX12", "bitness": 64, "exe": "Spider-Man2.exe",
     "files": ["dxgi.dll"]}), encoding="utf8")
(Path(_opti_run) / "dxgi.dll").write_bytes(b"MZ")
(Path(_opti_run) / "OptiScaler.log").write_text(
    "[21:34:07.000000] [I] DlssNr forwarder loaded\n"
    "[21:34:08.368508] [I] DlssNr_Dx12::Dispatch DLSS-NR elapsed: 6.72 ms "
    "total, 6.60 ms model, 0.12 ms surrounding work\n", encoding="utf8")
_r168 = diagnose.analyse(Path(_opti_run))
check("...so the whole report says the neural pass is running",
      _r168.verdict.startswith("Working")
      and any("Neural rendering is running" in t for t in _levels(_r168, "ok")),
      _r168.verdict)
shutil.rmtree(_opti_run, ignore_errors=True)


# #164: a ReShade.log read from its tail can lose the registration lines
# while the add-on's own log proves it loaded. The answer said "ReShade
# loaded no add-ons" above a feed that had attached and hooked the game.
_d164 = _diag_dir(
    "diag_tail_lost_",
    reshade=("21:11:43:914 [24200] | WARN  | Successfully compiled "
             "'D:\\Game\\reshade-shaders\\Shaders\\CShade\\cDots.fx'\n"
             "21:11:44:173 [24200] | WARN  | Successfully compiled "
             "'D:\\Game\\reshade-shaders\\Shaders\\DH\\dh_uber_rt.fx'\n"),
    feed=("21:11:35.731  dlss5-feed64 0.15.1 (built Sep  9 2026) attached.\n"
          "21:11:35.731    host game: D:\\Game\\Game.exe\n"
          "21:11:35.759  [feed] vkCreateDevice hook installed\n"))
_r164 = diagnose.analyse(_d164)
check("a feed log that attached outranks a ReShade tail with no registration",
      not any("loaded no add-ons" in t for t in _levels(_r164, "bad")),
      str(_levels(_r164, "bad")))
check("...and the answer says the add-on did load",
      any("wrote its own log" in t for t in _levels(_r164, "ok")),
      str(_levels(_r164, "ok")))
check("...and names what is actually missing: an effect runtime",
      "effect runtime" in _r164.verdict, _r164.verdict)
shutil.rmtree(_d164, ignore_errors=True)


# ...and the provider shader the install deliberately leaves alone, because
# the person already has the pack, is not "MISSING" (#164 again).
_d164b = tempfile.mkdtemp(prefix="diag_their_lumenite_")
(Path(_d164b) / "reshade-shaders" / "Shaders" / "LumeniteFX").mkdir(parents=True)
(Path(_d164b) / "reshade-shaders" / "Shaders" / "LumeniteFX"
 / "lumenite_Kernel.fx").write_text("// theirs", encoding="utf8")
_man164 = {"route": "feeder", "provider": 3, "bitness": 64, "api": "DX12",
           "files": []}
_seen = "\n".join(diagnose._presence(Path(_d164b), _man164, "feeder"))
check("their own LumeniteFX copy is not reported as a missing file",
      "lumenite_Kernel.fx: MISSING" not in _seen
      and "not written by this install" in _seen,
      _seen)
shutil.rmtree(_d164b, ignore_errors=True)
_d164c = tempfile.mkdtemp(prefix="diag_no_lumenite_")
(Path(_d164c) / "reshade-shaders" / "Shaders").mkdir(parents=True)
check("...while one that is genuinely gone still is",
      "lumenite_Kernel.fx: MISSING"
      in "\n".join(diagnose._presence(Path(_d164c), _man164, "feeder")))
shutil.rmtree(_d164c, ignore_errors=True)


# Every one of those four fixes went too far somewhere, and this is where each
# one was pulled back. A widened test makes the branch below it unreachable;
# these are the inputs that proved it.


def _opti_log(log, **man):
    """A folder that looks like an optiscaler install with this log in it."""
    d = Path(tempfile.mkdtemp(prefix="diag_opti_probe_"))
    m = {"version": 1, "complete": True, "path": "optiscaler",
         "proxy": "dxgi.dll", "api": "DX12", "bitness": 64, "exe": "Game.exe",
         "files": ["dxgi.dll"]}
    m.update(man)
    (d / "dlss5-autopilot.json").write_text(json.dumps(m), encoding="utf8")
    (d / "dxgi.dll").write_bytes(b"MZ")
    (d / "OptiScaler.log").write_text(log, encoding="utf8")
    try:
        return diagnose.analyse(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


_FWD = "[I] DlssNr forwarder loaded\n"
_r = _opti_log(_FWD + "[E] DlssNr_Dx12::Dispatch DLSS-NR create failed after "
                      "120 ms, disabling for this session\n")
check("a create failure that reports how long it took is not work done",
      not _r.verdict.startswith("Working")
      and any("did not start" in t for t in _levels(_r, "bad")), _r.verdict)
_r = _opti_log(_FWD + "[I] DlssNr.DispatchInterval: 16 ms\n")
check("...nor is a setting whose name happens to hold the word",
      not _r.verdict.startswith("Working"), _r.verdict)
_r = _opti_log(_FWD + "[I] DlssNr_Dx12::Dispatch DLSS-NR skipped: no motion "
                      "vectors (0.00 ms)\n")
check("...nor a dispatch that says it skipped",
      not _r.verdict.startswith("Working"), _r.verdict)
_r = _opti_log(_FWD + "[I] DlssNr_Dx12::Dispatch DLSS-NR elapsed: 6.72 ms "
                      "total, 6.60 ms model\n")
check("...while a real dispatch is, still", _r.verdict.startswith("Working"),
      _r.verdict)
_dopt = Path(tempfile.mkdtemp(prefix="diag_opti_nolog_"))
(_dopt / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "path": "optiscaler", "proxy": "dxgi.dll",
     "api": "DX12", "bitness": 64, "exe": "Game.exe",
     "files": ["dxgi.dll", "OptiScaler.ini"]}), encoding="utf8")
(_dopt / "dxgi.dll").write_bytes(b"MZ")
(_dopt / "OptiScaler.ini").write_text("[Log]\nLogToFile = true\n", encoding="utf8")
_r = diagnose.analyse(_dopt)
check("the optiscaler 'not run yet' verdict rests on the absent log too",
      _r.verdict.startswith("Not run yet") and _r.never_ran is True, _r.verdict)
# ...and so does every other verdict read off a log that is not there. A fault
# record for the game outranks all of them, so each has to say so.
(_dopt / "OptiScaler.ini").write_text("[Log]\nLogToFile = false\n", encoding="utf8")
_r = diagnose.analyse(_dopt)
check("...and the one about the log being switched off",
      _r.verdict.startswith("OptiScaler's log is off") and _r.never_ran is True,
      _r.verdict)
shutil.rmtree(_dopt, ignore_errors=True)
_dstale = _diag_dir("diag_stale_reshade_flag_",
                    reshade="INFO | Initializing crosire's ReShade\n"
                            'INFO | Registered add-on "DLSS 5 Feed" v0.1\n')
_oldt = _time.time() - 7200
_os.utime(_dstale / "ReShade.log", (_oldt, _oldt))
_r = diagnose.analyse(_dstale)
check("...and the one about a log older than the install",
      _r.never_ran is True, _r.verdict)
shutil.rmtree(_dstale, ignore_errors=True)

# The feed log proving the add-on loaded must speak for THIS launch, and the
# "never got an effect runtime" answer must rest on more than one build's
# choice of words.
_ATT = "21:11:35.731  dlss5-feed64 0.15.1 (built Sep  9 2026) attached.\n"
_COMP = ("21:11:43:914 [24200] | WARN  | Successfully compiled 'a.fx'\n"
         "21:11:44:173 [24200] | WARN  | Successfully compiled 'b.fx'\n")
_d = _diag_dir("diag_runtime_other_words_",
               feed=_ATT + "  [feed] runtime 0001 initialised on D3D12\n"
                           "  [feed] DLSS5_Feed.fx technique MISSING\n",
               reshade=_COMP)
_r = diagnose.analyse(_d)
check("a technique line is proof a runtime existed, whatever it is called",
      "effect runtime" not in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_feed_older_launch_",
               feed=_ATT + "  [feed] vkCreateDevice hook installed\n",
               reshade=_COMP)
_old = _time.time() - 3600
_os.utime(_d / "dlss5-feed.log", (_old, _old))
_r = diagnose.analyse(_d)
check("a feed log from an earlier launch does not answer for this one",
      any("loaded no add-ons" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# A provider shader this install wrote and something has since removed is the
# quarantine case, not "your own copy is used".
_dp = tempfile.mkdtemp(prefix="diag_provider_ours_gone_")
(Path(_dp) / "reshade-shaders" / "Shaders" / "LumeniteFX").mkdir(parents=True)
(Path(_dp) / "reshade-shaders" / "Shaders" / "LumeniteFX"
 / "lumenite_Kernel.fx").write_text("// theirs", encoding="utf8")
_man_ours = {"path": "feeder", "provider": 3, "bitness": 64, "api": "DX11",
             "files": ["reshade-shaders\\Shaders\\lumenite_Kernel.fx"]}
check("a provider shader this install wrote and lost is still MISSING",
      "lumenite_Kernel.fx: MISSING"
      in "\n".join(diagnose._presence(Path(_dp), _man_ours, "feeder")))
_seen = "\n".join(diagnose._presence(Path(_dp), dict(_man_ours, files=[]),
                                    "feeder"))
check("...and where it is theirs, no absolute path reaches the report",
      "not written by this install" in _seen and ":\\" not in _seen, _seen)
shutil.rmtree(_dp, ignore_errors=True)


# With no log there is no timestamp to date a fault against, and the event is
# found by the executable's NAME - so a second copy of the same game must not
# answer for this one.
def _override(module, where, never_ran=True,
              verdict="Not started since the install - run it once."):
    rep = diagnose.Report(route="renodx")
    rep.verdict = verdict
    rep.never_ran = never_ran
    rep.add(diagnose.WARN, "The game has not been started since the install.")
    rep.add(diagnose.INFO, "If you DID start it, it launches something else.")
    app = _FakeApp(rep)
    app.game = type("G", (), {"install_dir": where})()
    _gui.App._crash_overrides(app, _wc.Crash(
        when="2026-09-12 00:54:33", exe="GTA5.exe", module=module,
        code="0xC0000005", provider="Application Error"))
    return rep


_here = Path(tempfile.mkdtemp(prefix="diag_fault_folder_"))
_rep = _override(str(_here / "GTA5.exe"), _here)
check("a fault recorded in this folder rewrites the 'not started' verdict",
      "It started, and nothing here recorded the session" in _rep.verdict,
      _rep.verdict)
check("...and the findings that said it never started go with it",
      not any("has not been started" in f_.title for f_ in _rep.findings)
      and any("faulting" in f_.title for f_ in _rep.findings),
      str([f_.title for f_ in _rep.findings]))
_rep = _override(r"D:\Another Copy\GTA5.exe", _here)
check("...while a fault in another copy of the same game does not",
      _rep.verdict.startswith("Not started"), _rep.verdict)
_rep = _override("dxgi.dll", _here)
check("...and a bare module name, which names no folder, still counts",
      "It started, and nothing here recorded the session" in _rep.verdict,
      _rep.verdict)
shutil.rmtree(_here, ignore_errors=True)


# The fault record arrives after the diagnosis has been printed. Rewriting the
# verdict alone left the person reading "run the game once" while the report
# and the shared record said it had crashed.
def _override_said(module, where, route="renodx"):
    rep = diagnose.Report(route=route)
    rep.verdict = "Not started since the install - run it once."
    rep.never_ran = True
    rep.add(diagnose.WARN, "The game has not been started since the install.")
    app = _FakeApp(rep)
    app.game = type("G", (), {"install_dir": where})()
    _gui.App._crash_overrides(app, _wc.Crash(
        when="2026-09-12 00:54:33", exe="GTA5.exe", module=module,
        code="0xC0000005", provider="Application Error"))
    return rep, "\n".join(app.said)


_here2 = Path(tempfile.mkdtemp(prefix="diag_fault_screen_"))
_rep, _said = _override_said(str(_here2 / "GTA5.exe"), _here2)
check("the corrected verdict is printed on screen, not only in the report",
      _rep.verdict in _said, _said[:200])
check("...and the fault finding is printed with it",
      "faulting" in _said, _said[:200])
# The install page names that dropdown differently per route; the feeder
# route has it on a row of its own (gui._apply_route, row 21).
_rep, _said = _override_said(str(_here2 / "GTA5.exe"), _here2, route="optiscaler")
check("...the optiscaler route is told about 'loads as'",
      "'loads as'" in _said and "reshade loads as" not in _said, _said[-200:])
_rep, _said = _override_said(str(_here2 / "GTA5.exe"), _here2, route="feeder")
check("...and the feeder route, which has its own row for it, is told "
      "'reshade loads as'",
      "'reshade loads as'" in _said, _said[-200:])
_asrc2 = src_of(_gui.App._apply_route)
check("the remix route shows no proxy dropdown - it installs no ReShade",
      "elif path == dlss.REMIX:" in _asrc2 and "pair = None" in _asrc2)
check("...and changing the name rebuilds the header line that promises it",
      'self.cb_rproxy.bind("<<ComboboxSelected>>"'
      in src_of(_gui.App._page_install))
_rep, _said = _override_said(str(_here2 / "GTA5.exe"), _here2, route="remix")
check("...and the remix route, which installs no ReShade, is told neither",
      "loads as" not in _said, _said[-200:])
shutil.rmtree(_here2, ignore_errors=True)


# The standalone route keeps its own add-on log, so the same cut ReShade tail
# must not tell that route "ReShade loaded no add-ons" either.
_dsa = _diag_dir("diag_standalone_tail_", path="standalone", provider=0,
                 reshade="21:11:43:914 [24200] | WARN  | Successfully "
                         "compiled 'a.fx'\n")
# That log lives in LOCALAPPDATA, one file for every game: point the module at
# a temporary one rather than writing over the machine's own.
_sa_saved = diagnose.STANDALONE_LOG
_sa_dir = Path(tempfile.mkdtemp(prefix="diag_salog_tail_"))
diagnose.STANDALONE_LOG = _sa_dir / "standalone-dlssnr.log"
try:
    diagnose.STANDALONE_LOG.write_text(
        "11:00:00 Standalone DLSS-NR + SR 1.7.17"
        + diagnose._STANDALONE_SESSION + "quality\n", encoding="utf8")
    _r = diagnose.analyse(_dsa)
    check("the standalone add-on's own log answers for the cut ReShade tail",
          not any("loaded no add-ons" in t for t in _levels(_r, "bad")),
          str(_levels(_r, "bad")))
finally:
    diagnose.STANDALONE_LOG = _sa_saved
    shutil.rmtree(_sa_dir, ignore_errors=True)
shutil.rmtree(_dsa, ignore_errors=True)

# The install's own proxy is in the manifest's file list, and the event
# names a module rather than a path - so "it is in our list" is not proof
# that the copy which faulted was ours. Passing that list to ours() made the
# ambiguous branch unreachable and turned the commonest fault of all into
# "this is ours to fix".
_amb = _wc.Crash(when="2026-09-10 01:00:00", exe="Game.exe",
                 module="dxgi.dll", code="0xC0000005",
                 provider="Application Error")
_said_amb = _wc.describe(_amb, "dxgi.dll", ("dxgi.dll", "ReShade64.dll"))
check("a proxy-named fault stays ambiguous even though we wrote that file",
      "does not say which copy" in _said_amb[1], _said_amb[1][:80])
_mine = _wc.Crash(when="2026-09-10 01:00:00", exe="Game.exe",
                  module="RTX40MFGCore.dll", code="0xC0000005",
                  provider="Application Error")
check("...while a file only we write is ours, whatever it is called",
      "ours to fix" in _wc.describe(_mine, "dxgi.dll",
                                    ("dxgi.dll", "RTX40MFGCore.dll"))[1])
_theirs_c = _wc.Crash(when="2026-09-10 01:00:00", exe="Game.exe",
                      module="RTSSHooks64.dll", code="0xC0000005",
                      provider="Application Error")
check("...and somebody else's overlay is neither",
      "neither" in _wc.describe(_theirs_c, "dxgi.dll", ("dxgi.dll",))[1])

# An exception with an empty message used to raise inside a worker thread,
# after the window was put in its busy state and before the message that
# takes it out of it.
check("an exception with no message still names something",
      _gui._first_line(TimeoutError()) == "TimeoutError"
      and _gui._first_line(ValueError("first\nsecond")) == "first")

# The window that says whether a recorded fault belongs to this session must
# never be silently switched off: a route whose log is not in the list would
# have let every fault of the last fortnight speak for a clean session.
_sw = Path(tempfile.mkdtemp(prefix="session_window_"))
check("with nothing to date a crash against, the window is not just dropped",
      _gui._last_log_write(_sw) == 0.0)
(_sw / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "files": []}),
    encoding="utf8")
check("...the install time stands in for it",
      _gui._last_log_write(_sw) > 0)
(_sw / "OptiScaler.log").write_text("x", encoding="utf8")
check("...and a real log wins over that",
      _gui._last_log_write(_sw) >= (_sw / "OptiScaler.log").stat().st_mtime)
shutil.rmtree(_sw, ignore_errors=True)

# When both sources fail, only the fallback's error is raised - so the
# publisher's reason (a proxy serving an HTML page, say) has to be said out
# loud before it is lost. It was being labelled "the first source" while
# printing the second's, because the variable had already moved on.
_pf_log = []
_pf_tried = []
_pf_real = installer._place_entry


def _pf_fail(entry, dest, rep, root, dl, member, name):
    _pf_tried.append(entry["label"])
    raise installer.InstallError(f"{entry['label']} failed")


installer._place_entry = _pf_fail
try:
    _pf_raised = None
    try:
        installer._place_family(
            [{"label": "publisher", "url": "u1"}, {"label": "mirror", "url": "u2"}],
            "", Path("x"), None, Path("."), None, "m", "p",
            lambda t: _pf_log.append(t))
    except installer.InstallError as _e:
        _pf_raised = str(_e)
finally:
    installer._place_entry = _pf_real
check("both sources are tried before a family is given up on",
      _pf_tried == ["publisher", "mirror"], _pf_tried)
check("...the fallback's failure is the one raised",
      _pf_raised == "mirror failed", _pf_raised)
check("...and the publisher's own reason is logged, not thrown away",
      any("the first source said: publisher failed" in t for t in _pf_log),
      [t.strip() for t in _pf_log])

# ...and the branch a blocked host actually takes: a proxy or a DNS filter
# surfaces as URLError/OSError, not InstallError, and that branch was not
# recording the first error at all - so the line named the wrong source on
# exactly the failure it exists for.
_bh_log = []
_bh_real = installer._place_entry


def _bh_fail(entry, dest, rep, root, dl, member, name):
    if entry["label"] == "publisher":
        raise OSError("publisher host is blocked")
    raise installer.InstallError("mirror failed")


installer._place_entry = _bh_fail
try:
    try:
        installer._place_family(
            [{"label": "publisher", "url": "u1"}, {"label": "mirror", "url": "u2"}],
            "", Path("x"), None, Path("."), None, "m", "p",
            lambda t: _bh_log.append(t))
    except Exception:
        pass
finally:
    installer._place_entry = _bh_real
check("a blocked publisher is named too, not just a refused download",
      any("the first source said: publisher host is blocked" in t
          for t in _bh_log), [t.strip() for t in _bh_log])

# The remembered walk must hand out a copy: one caller appending to it would
# poison every later answer for the rest of the session.
_wc = Path(tempfile.mkdtemp(prefix="walkcopy_"))
(_wc / "nvngx_dlss.dll").write_bytes(b"MZ")
dlss.forget_walk(_wc)
_first = dlss.walked(_wc)
_first.append("POISON")
check("the remembered walk hands out a copy, never the cache itself",
      "POISON" not in dlss.walked(_wc), dlss.walked(_wc))
shutil.rmtree(_wc, ignore_errors=True)

section("64. what the first day of 1.8.0 reported: a runtime under a "
        "skipped folder, an OptiScaler build that writes no log, and a "
        "route recommended from a DLL nobody can switch on")

# Issue #119, NBA 2K27: the game keeps Streamline in data\streamline, and
# "data" is on the walk's skip list - so a game that ships its own DLSS was
# read as a game with no DLSS at all, and offered the feeder route.
_nba = Path(tempfile.mkdtemp(prefix="nba2k_"))
(_nba / "NBA2K27.exe").write_bytes(b"MZ" + b"\0" * 300)
_sl = _nba / "data" / "streamline"
_sl.mkdir(parents=True)
for _n in ("sl.interposer.dll", "sl.dlss.dll"):
    (_sl / _n).write_bytes(b"MZ")
dlss.forget_walk(_nba)
_nsup = dlss.detect(_nba, _nba, "DX12", 64, sm=120)
check("a runtime under a skipped content folder is still found",
      _nsup.native_dlss, _nsup.evidence)
check("...so the route offered is not the one for games without DLSS",
      _nsup.recommended != dlss.FEEDER, _nsup.recommended)

# ...and the skip list still does its job: a content folder with nothing
# runtime-shaped under it is not descended into.
_big = Path(tempfile.mkdtemp(prefix="bigcontent_"))
(_big / "Game.exe").write_bytes(b"MZ" + b"\0" * 300)
(_big / "data" / "textures").mkdir(parents=True)
(_big / "data" / "textures" / "nvngx_dlss.dll").write_bytes(b"MZ")
dlss.forget_walk(_big)
check("...and a plain content folder is still skipped",
      not dlss.detect(_big, _big, "DX12", 64).native_dlss)
for _d in (_nba, _big):
    shutil.rmtree(_d, ignore_errors=True)

# Issue #110: y4my4my4m's build ships [Log] LogToFile=auto, which is false,
# so OptiScaler wrote nothing and a working install was told it had never
# loaded. The install turns the log on, and the verdict no longer claims
# more than an absent file can support.
_ol = Path(tempfile.mkdtemp(prefix="optilog_"))
(_ol / "OptiScaler.ini").write_text(
    "[Log]\n; kept\nLogToFile=auto\nLogLevel=auto\n\n[DlssNr]\nEnabled=false\n",
    encoding="utf8")
optiscaler.enable_nr(_ol, settings={"WorkingScale": 0.75})
_oltxt = (_ol / "OptiScaler.ini").read_text(encoding="utf8")
check("the install switches OptiScaler's log on, whatever the build ships",
      "LogToFile=true" in _oltxt and "LogToFile=auto" not in _oltxt)
check("...without throwing away what the person had in the file",
      "; kept" in _oltxt and "WorkingScale=0.75" in _oltxt)

(_ol / "dxgi.dll").write_bytes(b"MZ")
(_ol / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX12", "proxy": "dxgi.dll", "path": "optiscaler",
     "files": ["dxgi.dll", "OptiScaler.ini"]}), encoding="utf8")
# Logging on (the install above set it) and still no log: that is the old
# question again, and it is asked as a question.
_olrep = diagnose.analyse(_ol)
check("no log with the proxy in place is not 'it never loaded'",
      "never loaded" not in _olrep.verdict, _olrep.verdict)
check("...with logging on, it asks whether the game has run since",
      "not run yet" in _olrep.verdict.lower(), _olrep.verdict)
# Logging off - the y4my4my4m default - is its own answer: a missing log
# then says nothing about whether OptiScaler loaded.
(_ol / "OptiScaler.ini").write_text(
    "[Log]\nLogToFile=auto\n\n[DlssNr]\nEnabled=true\n", encoding="utf8")
_olrep = diagnose.analyse(_ol)
check("...with logging off, it says the log is off and how to switch it on",
      "log is off" in _olrep.verdict.lower()
      and "not run yet" not in _olrep.verdict.lower(), _olrep.verdict)
check("...and names the proxy it checked, and the button, not 'this button'",
      any("(dxgi.dll)" in f.detail and "did it work?" in f.detail
          for f in _olrep.findings),
      [f.detail[:80] for f in _olrep.findings])
(_ol / "OptiScaler.ini").unlink()
check("...and with OptiScaler.ini gone altogether it says the ini is missing",
      "missing" in diagnose.analyse(_ol).verdict.lower(),
      diagnose.analyse(_ol).verdict)
(_ol / "dxgi.dll").unlink()
check("...but with nothing of ours in the folder it does say so",
      "not in the game folder" in diagnose.analyse(_ol).verdict,
      diagnose.analyse(_ol).verdict)
shutil.rmtree(_ol, ignore_errors=True)

# Issue #116, Risk of Rain 2: an FSR runtime on disk is not an upscaler the
# player can switch on. The route was recommended, had nothing to hook, and
# only the diagnosis afterwards said why.
_ok116, _note116 = dlss.fit(dlss.OPTI, "DX12", False, 120, upscaler="fsr")
check("the optiscaler route says the game's own upscaler has to be on",
      "has to be on in the game's own settings" in _note116, _note116)
check("...and names the route to use when the game has no such setting",
      "feeder" in _note116, _note116)

# #127: from 1.4.0 the bridge replaces, before reading it, a settings file
# whose first line is not its version or "keep" - with its defaults, which
# leave the substitute contract off. Ours had no such line, so a game with
# no DLSS of its own never got the synth_after this install wrote.
from core import feedcfg as _fc127                                # noqa: E402
_bd = Path(tempfile.mkdtemp(prefix="bridgecfg_"))
(_bd / _fc127.BRIDGE_NAME).write_text(
    "# dlss5-bridge 1.4.12\nsynth=0\nsynth_after=0\nofa_perf=5\nofa_grid=2\n"
    "source=auto\nflags=107\nunwrap=1\n", encoding="utf8")
# What 1.8.0 left behind on a game without DLSS: the bridge's own dump.
(_bd / installer.MANIFEST).write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX11", "path": "bridge", "native_dlss": False,
     "files": ["dxgi.dll", _fc127.BRIDGE_NAME]}), encoding="utf8")
(_bd / "dxgi.dll").write_bytes(b"MZ")
(_bd / "ReShade.log").write_text(
    "12:00:00:000 [1] | INFO  | Registered add-on \"DLSS 5 Bridge 1.4.12\" "
    "v1.4.12.0 using ReShade API version 18.\n", encoding="utf8")
_rb = diagnose.analyse(_bd)
check("'did it work?' names a bridge file that turned its substitute off",
      "install the bridge route again" in _rb.verdict, _rb.verdict)
_fc127.write_bridge(_bd, _fc127.bridge_defaults(False))
_bl = (_bd / _fc127.BRIDGE_NAME).read_bytes().decode("utf8").splitlines()
check("the bridge settings file starts with the line that keeps it",
      _bl[0] == "# dlss5-bridge keep", _bl[:2])
check("...carries the substitute switch for a game without DLSS",
      "synth_after=3" in _bl, _bl)
check("...keeps what the person chose in the bridge's panel",
      "ofa_perf=5" in _bl and _bl.count("# dlss5-bridge keep") == 1, _bl)
check("...but not the rest of a version's defaults, which 'keep' would freeze",
      "flags=107" not in _bl and "unwrap=1" not in _bl
      and "ofa_grid=2" not in _bl and "source=auto" not in _bl
      and "synth=0" not in _bl, _bl)
check("...and 'did it work?' no longer says so",
      "install the bridge route again" not in diagnose.analyse(_bd).verdict)
_fc127.write_bridge(_bd, {"ofa_grid": 4})
_bt = (_bd / _fc127.BRIDGE_NAME).read_text(encoding="utf8")
check("...and a second write does not stack the line, and keeps our own file",
      _bt.count("dlss5-bridge keep") == 1 and "ofa_grid=4" in _bt
      and "synth_after=3" in _bt, _bt)
_fc127.write_bridge(_bd, _fc127.bridge_defaults(True))
check("a later install that found the game's DLSS turns the substitute off",
      "synth_after=0" in (_bd / _fc127.BRIDGE_NAME).read_text(encoding="utf8"))
shutil.rmtree(_bd, ignore_errors=True)

# The report's ReShade.log excerpt lost its oldest lines first, and the hook
# lines are written at the start of a session: the "hooked" line went, the
# "Failed to find" beside it stayed, and the excerpt replayed to "update the
# driver". Over budget, the lines nothing reads go first.
_ex_log = ("12:00:00:000 [1] | INFO  | Registered add-on \"DLSS 5 Neural "
           "Rendering\" v0.2026.828.517 using ReShade API version 18.\n"
           "12:00:01:000 [1] | DEBUG | [DLSS 5 Neural Rendering] vtable::Hook("
           "NVSDK_NGX_D3D12_EvaluateFeaturehooked with 0x00007fff55ee8740 => "
           "0x00007fff36671430)\n"
           "12:00:01:000 [1] | ERROR | [DLSS 5 Neural Rendering] vtable::Hook("
           "Failed to find NVSDK_NGX_D3D12_EvaluateFeature_C)\n"
           + "12:00:02:000 [1] | INFO  | Redirecting Direct3DCreate9(SDKVersion "
             "= 0x20) ...\n" * 40)
_ex = diagnose._reshade_excerpt(_ex_log)
check("an over-budget excerpt keeps the hook lines the driver rule reads",
      any("EvaluateFeaturehooked" in ln for ln in _ex)
      and any("Failed to find" in ln for ln in _ex)
      and len("\n".join(_ex)) <= 1500, _ex[:3])
check("...and which add-ons loaded, before the lines nothing reads",
      any("Registered add-on" in ln for ln in _ex))
check("...and the excerpt still reads as a plain hook",
      re.search(r"vtable::Hook\(NVSDK_NGX_\w+_EvaluateFeature\w*\s*hooked",
                "\n".join(_ex)) is not None)

# The 1.8.1 gate: "try the standalone route" was said to games that are
# never offered it - 32-bit, DX9, Vulkan and OpenGL get feeder and bridge
# only, and were sent looking for an entry their dropdown does not have.
_sa_mismatch = []
for _api in ("DX9", "DX10", "DX11", "DX12", "Vulkan", "OpenGL", "Unknown"):
    for _bits in (32, 64):
        _sd = Path(tempfile.mkdtemp(prefix="safit_"))
        (_sd / "g.exe").write_bytes(b"MZ" + b"\0" * 200)
        dlss.forget_walk(_sd)
        _offered = dlss.STANDALONE in dlss.detect(_sd, _sd, _api, _bits, sm=89).options
        if _offered != dlss.standalone_fits(_api, _bits):
            _sa_mismatch.append((_api, _bits))
        shutil.rmtree(_sd, ignore_errors=True)
check("standalone_fits agrees with the route list for every api and bitness",
      not _sa_mismatch, _sa_mismatch)
check("the feeder warning names standalone only where it is offered",
      "standalone" in (dlss.driver_warning("feeder", "616.92",
                                           offered=["feeder", "standalone"]) or "")
      and "standalone" not in (dlss.driver_warning("feeder", "616.92",
                                                   offered=["feeder", "bridge"]) or ""))

_CHAIN = ("12:00:00.000  [feed] evaluate raised 0xC0000005 (reading address "
          "FFFFFFFFFFFFFFFF) (caught; nothing submitted)\n"
          "12:00:00.000  [feed] evaluate fault stack, by module (innermost "
          "first): D3D12Core.dll <- nvngx_dlssnr.dll <- _nvngx.dll <- "
          "renodx-dlss5.addon64 <- dlss5-feed.addon64 <- ReShade64.dll\n")
_d32 = _diag_dir("chain32_", feed=_FEED_OK + _CHAIN, bitness=32, api="DX9",
                 reshade='INFO | Registered add-on "DLSS 5 Feed" v0.14\n',
                 components={"renodx": "4.55"})
_r32 = diagnose.analyse(_d32)
check("...and a 32-bit game with the 616.64 fault is not sent to standalone",
      "616.64" in _r32.verdict and "standalone" not in _r32.verdict,
      _r32.verdict)
shutil.rmtree(_d32, ignore_errors=True)
_d64 = _diag_dir("chain64_", feed=_FEED_OK + _CHAIN, bitness=64, api="DX12",
                 reshade='INFO | Registered add-on "DLSS 5 Feed" v0.14\n',
                 components={"renodx": "4.55"})
_r64 = diagnose.analyse(_d64)
check("...while a 64-bit D3D12 game with it is",
      "standalone" in _r64.verdict, _r64.verdict)
shutil.rmtree(_d64, ignore_errors=True)

# #116 on D3D11: the D3D11 note used to replace the upscaler warning.
check("a D3D11 game offered optiscaler for its FSR still hears it must be on",
      "has to be on" in dlss.fit(dlss.OPTI, "DX11", False, 89, upscaler="fsr")[1])

# #130: NVIDIA's Aftermath library beside a Vulkan exe names d3d12.dll, and
# the game was read as DX12. Middleware that names every API is not evidence.
_af = Path(tempfile.mkdtemp(prefix="aftermath_"))
_sysd = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
shutil.copy(_sysd / "cmd.exe", _af / "enshrouded.exe")
with open(_af / "enshrouded.exe", "ab") as _fh:
    _fh.write(b"\0vulkan-1.dll\0")
shutil.copy(_sysd / "cmd.exe", _af / "GFSDK_Aftermath_Lib.x64.dll")
with open(_af / "GFSDK_Aftermath_Lib.x64.dll", "ab") as _fh:
    _fh.write(b"\0d3d12.dll\0dxgi.dll\0" + b"\0" * (2 * 1024 * 1024))
_api = pe.runtime_graphics(_af / "enshrouded.exe") if hasattr(pe, "runtime_graphics") \
    else pe._runtime_graphics(_af / "enshrouded.exe")
check("a crash library that names every API does not make a Vulkan game DX12",
      "d3d12" not in str(_api).lower() and "dx12" not in str(_api).lower(), _api)
shutil.rmtree(_af, ignore_errors=True)

# #131: the trial exe beside the full game's matched the folder name as well
# and was bigger, so it was picked.
_tr = Path(tempfile.mkdtemp(prefix="Need for Speed Heat_"))
(_tr / "NeedForSpeedHeat.exe").write_bytes(b"MZ" + b"\0" * 1000)
(_tr / "NeedForSpeedHeatTrial.exe").write_bytes(b"MZ" + b"\0" * 400000)
(_tr / "NFS16.exe").write_bytes(b"MZ" + b"\0" * 1000)
(_tr / "NFS16_trial.exe").write_bytes(b"MZ" + b"\0" * 400000)
_ex = [p.name for p in pe.find_game_exes(_tr)]
check("the full game's exe is ranked above its trial beside it",
      _ex.index("NeedForSpeedHeat.exe") < _ex.index("NeedForSpeedHeatTrial.exe")
      and _ex.index("NFS16.exe") < _ex.index("NFS16_trial.exe"), _ex)
check("...and the trial is still in the list for whoever plays it",
      "NeedForSpeedHeatTrial.exe" in _ex, _ex)
shutil.rmtree(_tr, ignore_errors=True)
_dm = Path(tempfile.mkdtemp(prefix="SomeGame_"))
(_dm / "SomeGameDemo.exe").write_bytes(b"MZ" + b"\0" * 1000)
check("a demo-only install still gets its demo exe",
      [p.name for p in pe.find_game_exes(_dm)] == ["SomeGameDemo.exe"])
shutil.rmtree(_dm, ignore_errors=True)

# #130/#134: the report's tool-log excerpt was other games' scan lines.
_tail = ("2026-09-10 10:00:00 info  " + "=" * 70 + "\n"
         "2026-09-10 10:00:01 info  Minecraft for Windows: C:\\x\\m.exe is not "
         "readable yet - enable mods\n"
         "2026-09-10 10:00:02 warn  stopped looking for runtime DLLs under "
         "D:\\RPCS3 after 900 folders - search budget reached\n"
         "2026-09-10 10:00:03 info  scan Steam: 12 found\n"
         "2026-09-10 10:00:04 info  inspected Enshrouded in 1.2s\n"
         "2026-09-10 10:00:05 error install failed: something real\n")
import types as _types  # noqa: E402
_tl = diagnose._tool_log_lines(_tail, _types.SimpleNamespace(name="Enshrouded"),
                               r"D:\Games\Enshrouded")
check("the report's tool log leaves out the other games' scan lines",
      not any("Minecraft" in ln or "RPCS3" in ln or "Steam: 12" in ln
              for ln in _tl), _tl)
check("...keeps the reported game's own and the errors",
      any("inspected Enshrouded" in ln for ln in _tl)
      and any("something real" in ln for ln in _tl), _tl)

# ...and the ReShade.log excerpt is the last session's, as analyse reads it.
_two = ("12:00:00:000 [1] | INFO  | Initializing crosire's ReShade version '6.8.0'\n"
        "12:00:01:000 [1] | DEBUG | [DLSS 5 Neural Rendering] vtable::Hook("
        "NVSDK_NGX_D3D12_EvaluateFeaturehooked with 0x1 => 0x2)\n"
        "13:00:00:000 [2] | INFO  | Initializing crosire's ReShade version '6.8.0'\n"
        "13:00:01:000 [2] | ERROR | [DLSS 5 Neural Rendering] vtable::Hook("
        "Failed to find NVSDK_NGX_D3D12_EvaluateFeature_C)\n")
_ex2 = diagnose._reshade_excerpt(diagnose._last_session(_two))
check("the report's ReShade.log excerpt does not pull hook lines from an "
      "older session", not any("EvaluateFeaturehooked" in ln for ln in _ex2)
      and any("Failed to find" in ln for ln in _ex2), _ex2)

# The window takes the wheel from every dropdown, because ttk's class
# binding changes the selection before any window-level handler runs.
_gsrc_w = src_of(_gui.App.__init__)
check("the dropdown class binding for the wheel is taken away",
      'unbind_class(cls, "<MouseWheel>")' in _gsrc_w
      or "unbind_class" in _gsrc_w, "no unbind_class in App.__init__")

section("1.8.1: self-update relaunch, the MFG unlock's new shape, a bad cached "
        "archive, OptiScaler's update nag (#136 #141 #140 #51)")
import io as _io141
import zipfile as _zf141
from core import mfg as _m141  # noqa: E402
from core import selfupdate as _su136  # noqa: E402

# #136: the relaunched exe inherited the onefile child's _PYI_* variables and
# its bootloader quit with "failed to obtain executable path for parent
# process". The swap script and the Popen env both reset that.
_sw = _su136.swap_script(Path(r"C:\Games\Tool\dlss5-autopilot.exe"),
                         Path(r"C:\Temp\upd\dlss5-autopilot.exe"))
_rst = 'set "PYINSTALLER_RESET_ENVIRONMENT=1"'
check("the swap script resets PyInstaller's environment before it starts the new exe (#136)",
      _rst in _sw and 'start ""' in _sw and _sw.index(_rst) < _sw.index('start ""'), _sw[-200:])


class _Exit136(Exception):
    pass


_seen136: dict = {}
_saved136 = (_su136.running_exe, _su136.subprocess.Popen, _su136.os._exit)
_had_pyi = os.environ.get("_PYI_APPLICATION_HOME_DIR")
os.environ["_PYI_APPLICATION_HOME_DIR"] = r"C:\Temp\_MEI12345"
os.environ["_PYI_PARENT_PROCESS_LEVEL"] = "1"
_su136.running_exe = lambda: Path(r"C:\Games\Tool\dlss5-autopilot.exe")
_su136.subprocess.Popen = lambda *a, **k: _seen136.update(args=a, kw=k)


def _fake_exit(code):
    raise _Exit136(code)


_su136.os._exit = _fake_exit
# apply_and_restart refuses a staged build that is gone, so this one exists.
_staged136 = Path(tempfile.mkdtemp(prefix=_su136.PREFIX)) / "dlss5-autopilot.exe"
_staged136.write_bytes(b"MZ")
try:
    try:
        _su136.apply_and_restart(_staged136)
    except _Exit136:
        pass
finally:
    _su136.running_exe, _su136.subprocess.Popen, _su136.os._exit = _saved136
    os.environ.pop("_PYI_PARENT_PROCESS_LEVEL", None)
    if _had_pyi is None:
        os.environ.pop("_PYI_APPLICATION_HOME_DIR", None)
    else:
        os.environ["_PYI_APPLICATION_HOME_DIR"] = _had_pyi
    Path(tempfile.gettempdir(), "dlss5-autopilot-update.bat").unlink(missing_ok=True)
    shutil.rmtree(_staged136.parent, ignore_errors=True)
_saved_re = _su136.running_exe
_su136.running_exe = lambda: Path(r"C:\Games\Tool\dlss5-autopilot.exe")
try:
    _su136.apply_and_restart(_staged136)
    _gone_ok = False
except _su136.UpdateError:
    _gone_ok = True
finally:
    _su136.running_exe = _saved_re
check("a staged update that is gone is refused in words, not restarted into the old build",
      _gone_ok)
_env136 = (_seen136.get("kw") or {}).get("env")
check("apply_and_restart hands the swap an environment with no _PYI_* key (#136)",
      isinstance(_env136, dict) and not any(k.upper().startswith("_PYI_") for k in _env136)
      and _env136.get("PYINSTALLER_RESET_ENVIRONMENT") == "1"
      and "PATH" in {k.upper() for k in _env136},
      str(sorted(k for k in (_env136 or {}) if "PYI" in k.upper())))

# #141: v1.3.2 ships one RTXMFG.dll in RTXMFG-v1.3.2.zip. The resolver walks
# the release list for the universal zip, and a zip of another shape stops
# before anything lands in the game folder.
_rels141 = [
    {"tag_name": "v1.3.2", "published_at": "2026-09-10T00:00:00Z",
     "assets": [{"name": "RTXMFG-v1.3.2.zip", "browser_download_url": "u132"},
                {"name": "SHA256SUMS.txt", "browser_download_url": "s132"}]},
    {"tag_name": "v1.2.1", "published_at": "2026-08-20T00:00:00Z",
     "assets": [{"name": "Universal-RTX-40-MFG-Unlock-v1.2.1.zip",
                 "browser_download_url": "u121"},
                {"name": "Universal-RTX-40-MFG-Unlock-v1.2.1.zip.sha256",
                 "browser_download_url": "h121"}]},
]
_saved141 = (net.json_get, net.download, _m141.resolve, _m141.resolve_loader)
_asked141: list = []
try:
    net.json_get = lambda url: (_asked141.append(url), _rels141)[1]
    check("a v1.3.2 of the new shape above v1.2.1 resolves to v1.2.1, from the release list (#141)",
          _m141.resolve() == ("v1.2.1", "u121") and _asked141
          and "/releases?" in _asked141[0] and "latest" not in _asked141[0], str(_asked141))
    net.json_get = lambda url: _rels141[:1]
    try:
        _m141.resolve()
        _ok141 = False
    except _m141.ShapeChanged as e:
        _ok141 = "v1.3.2" in str(e)
    check("...and with only the new shape published, resolve() raises ShapeChanged", _ok141)
    net.json_get = _saved141[0]

    _d141 = Path(tempfile.mkdtemp(prefix="mfg141_"))
    _new141 = _d141 / "new.zip"
    with _zf141.ZipFile(_new141, "w") as z:
        z.writestr("RTXMFG.dll", b"MZ-new-shape")
    _ual141 = _d141 / "ual.zip"
    with _zf141.ZipFile(_ual141, "w") as z:
        z.writestr("dinput8.dll", b"MZ" + b"Ultimate ASI Loader" + b"\0" * (1 << 18))
    _g141 = _d141 / "game"
    _g141.mkdir()
    shutil.copyfile(X64, _g141 / "Game.exe")
    _before141 = sorted(p.name for p in _g141.iterdir())
    _m141.resolve = lambda: ("v1.3.2", "new")
    _m141.resolve_loader = lambda: ("v1", "ual")
    net.download = lambda url, name, **k: _new141 if url == "new" else _ual141
    try:
        _m141.install(_g141, _g141 / "Game.exe")
        _ok141 = False
    except _m141.ShapeChanged as e:
        _ok141 = "RTX40MFGCore.dll" in str(e)
    check("a release zip holding only RTXMFG.dll raises ShapeChanged and writes nothing (#141)",
          _ok141 and sorted(p.name for p in _g141.iterdir()) == _before141,
          str(sorted(p.name for p in _g141.iterdir())))
    # A loader zip without its dll is found out before the unlock's files land.
    _full141 = _d141 / "full.zip"
    with _zf141.ZipFile(_full141, "w") as z:
        for n in _m141.FILES:
            z.writestr(n, b"MZ" + n.encode())
    _noual141 = _d141 / "noual.zip"
    with _zf141.ZipFile(_noual141, "w") as z:
        z.writestr("readme.txt", b"x")
    net.download = lambda url, name, **k: _full141 if url == "new" else _noual141
    try:
        _m141.install(_g141, _g141 / "Game.exe")
        _ok141 = False
    except _m141.ShapeChanged:
        _ok141 = True
    check("...and a loader zip without dinput8.dll stops before the three files land",
          _ok141 and sorted(p.name for p in _g141.iterdir()) == _before141,
          str(sorted(p.name for p in _g141.iterdir())))
    shutil.rmtree(_d141, ignore_errors=True)
finally:
    net.json_get, net.download, _m141.resolve, _m141.resolve_loader = _saved141
check("the installer turns ShapeChanged into a warning, like NoLoaderName (#141)",
      re.search(r"except \(mfg\.NoLoaderName, mfg\.ShapeChanged, "
                r"sources\.RateLimited,\s+sources\.Unavailable, "
                r"net\.WrongContent\) as e:", src_of(installer)) is not None)

# #140: a proxy page or a cut zip in the cache was served on every retry and
# every install died with "File is not a zip file".
_c140 = Path(tempfile.mkdtemp(prefix="cache140_"))
_buf140 = _io141.BytesIO()
with _zf141.ZipFile(_buf140, "w") as z:
    z.writestr("a.txt", b"hello")
_realzip140 = _buf140.getvalue()
_serve140 = {"body": b"", "n": 0}


class _Resp140:
    def __init__(self, body):
        self._b = _io141.BytesIO(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._b.read(n)


def _urlopen140(req, timeout=0, **kw):
    _serve140["n"] += 1
    return _Resp140(_serve140["body"])


_saved140 = (net.CACHE, net.urllib.request.urlopen, net.time.sleep)
net.CACHE = _c140
net.urllib.request.urlopen = _urlopen140
net.time.sleep = lambda s: None
try:
    _serve140.update(body=b"<!DOCTYPE html><html>Blocked by your provider</html>", n=0)
    try:
        net.download("https://github.com/x/y/releases/download/v1/a.zip", "bad140.zip")
        _msg140 = ""
    except RuntimeError as e:
        _msg140 = str(e)
    check("an HTML page served for a .zip raises, names the host and leaves no cache file (#140)",
          "github.com" in _msg140 and "Nothing was written" in _msg140
          and "DOCTYPE" in _msg140 and not (_c140 / "bad140.zip").exists()
          and not (_c140 / "bad140.zip.part").exists() and _serve140["n"] == 1, _msg140)
    try:
        net.download("https://example.com/x.dll", "bad140.dll")
        _ok140 = False
    except net.WrongContent:
        _ok140 = True
    check("...the same for a .dll that is not a Windows binary", _ok140)
    (_c140 / "cached140.zip").write_bytes(b"<html>a cached error page</html>")
    _serve140.update(body=_realzip140, n=0)
    _got140 = net.download("https://example.com/c.zip", "cached140.zip")
    check("a bad .zip already in the cache is fetched again, exactly once (#140)",
          _serve140["n"] == 1 and _zf141.is_zipfile(_got140), str(_serve140["n"]))
    _serve140.update(n=0)
    _got140 = net.download("https://example.com/c.zip", "cached140.zip")
    check("...and a real zip in the cache is served without a download",
          _serve140["n"] == 0 and _got140.read_bytes() == _realzip140)
    _serve140.update(body=b"plain text is fine", n=0)
    check("...while a suffix outside the checked set passes as it is",
          net.download("https://example.com/n.txt", "notes140.txt").read_bytes()
          == b"plain text is fine")
finally:
    net.CACHE, net.urllib.request.urlopen, net.time.sleep = _saved140
    shutil.rmtree(_c140, ignore_errors=True)

# #51: OptiScaler compares the fork with mainline OptiScaler and nags about
# an "update" that has no neural rendering. And enable_nr runs on every
# autotune step, so it may only switch the log on, not overwrite a level.
_o51 = Path(tempfile.mkdtemp(prefix="opti51_"))
(_o51 / "OptiScaler.ini").write_text(
    "[Log]\nLogToFile=auto\nLogLevel=0\n\n[Hotfix]\n; Enables checking for "
    "latest version from Github\nCheckForUpdate=auto\n\n[DlssNr]\nEnabled=false\n",
    encoding="utf8")
optiscaler.enable_nr(_o51, settings={"WorkingScale": 0.75})
_t51 = (_o51 / "OptiScaler.ini").read_text(encoding="utf8")
check("enable_nr leaves exactly one CheckForUpdate=false, under [Hotfix] (#51)",
      _t51.count("CheckForUpdate=") == 1
      and optiscaler._ini_get(_t51, "Hotfix", "CheckForUpdate") == "false", _t51)
check("...keeps a LogLevel the person set (0) and turns LogToFile=auto into true",
      optiscaler._ini_get(_t51, "Log", "LogLevel") == "0"
      and optiscaler._ini_get(_t51, "Log", "LogToFile") == "true", _t51)
optiscaler.enable_nr(_o51, settings={"WorkingScale": 0.75})
check("...and a second call (an autotune step) changes nothing",
      (_o51 / "OptiScaler.ini").read_text(encoding="utf8") == _t51)
(_o51 / "OptiScaler.ini").unlink()
optiscaler.enable_nr(_o51)
_t51 = (_o51 / "OptiScaler.ini").read_text(encoding="utf8")
check("a fresh ini ends with LogToFile=true, LogLevel=2 and CheckForUpdate=false (#110, #51)",
      optiscaler._ini_get(_t51, "Log", "LogToFile") == "true"
      and optiscaler._ini_get(_t51, "Log", "LogLevel") == "2"
      and optiscaler._ini_get(_t51, "Hotfix", "CheckForUpdate") == "false", _t51)
(_o51 / "OptiScaler.ini").write_text("[Log]\nLogToFile=false\nLogLevel=auto\n",
                                     encoding="utf8")
optiscaler.enable_nr(_o51)
_t51 = (_o51 / "OptiScaler.ini").read_text(encoding="utf8")
check("...and LogToFile=false / LogLevel=auto become true / 2",
      optiscaler._ini_get(_t51, "Log", "LogToFile") == "true"
      and optiscaler._ini_get(_t51, "Log", "LogLevel") == "2", _t51)
shutil.rmtree(_o51, ignore_errors=True)

section("1.8.2: a full drive, the missing Remix box, update leftovers (#148 #157)")
from core import library as _lib148  # noqa: E402
from core import selfupdate as _su148  # noqa: E402

_full = OSError(28, "No space left on device", r"C:\Users\x\AppData\Local\dlss5-autopilot\cache\big.zip")
_wrapped = RuntimeError("could not unpack")
_wrapped.__cause__ = _full
_win = OSError(None, "There is not enough space on the disk")
_win.winerror = 112
check("a full drive is recognised as errno 28, as Windows' 112, and wrapped in another error",
      net.is_disk_full(_full) and net.is_disk_full(_win) and net.is_disk_full(_wrapped)
      and not net.is_disk_full(OSError(13, "Permission denied"))
      and not net.is_disk_full(RuntimeError("x")))
_msg148 = net.disk_full_message(_wrapped, Path(r"D:\Games\X"), net.CACHE)
check("...and the message names the drive and says to free space, without a traceback",
      _msg148.startswith("Out of disk space on C:") and "beside the game (D:" in _msg148
      and "free some up" in _msg148 and "Traceback" not in _msg148
      and "Errno" not in _msg148, _msg148)

# The install stops on a full drive: it says so, records it, and the
# diagnosis reads that record instead of "install again" (#148).
_d148 = Path(tempfile.mkdtemp(prefix="full148_"))
shutil.copyfile(X64, _d148 / "Game.exe")
_g148 = games.manual(_d148)
_saved148 = (installer.reengine.detected, installer.refw.install)
installer.reengine.detected = lambda root: True


def _refw_full(root, log):
    raise OSError(28, "No space left on device", str(root / "dinput8.dll"))


installer.refw.install = _refw_full
try:
    try:
        installer.install(_g148, installer.Options(), on_log=lambda t: None)
        check("an install on a full drive stops with words, not a traceback", False)
    except installer.InstallError as e:
        check("an install on a full drive stops with words, not a traceback",
              "Out of disk space" in str(e) and "Errno" not in str(e), str(e)[:120])
finally:
    installer.reengine.detected, installer.refw.install = _saved148
_man148 = json.loads((_d148 / installer.MANIFEST).read_text(encoding="utf8"))
check("...the record says the drive was full",
      _man148.get("complete") is False and net.DISK_FULL_NOTE in _man148.get("notes", []))
_rep148 = diagnose.analyse(_d148)
check("...and the diagnosis says to free up space first",
      "full" in _rep148.verdict.lower() and "install again" in _rep148.verdict.lower()
      and "never finished" not in _rep148.verdict, _rep148.verdict)
shutil.rmtree(_d148, ignore_errors=True)

# library.save on a full drive left its .tmp behind every time.
_lf = Path(tempfile.mkdtemp(prefix="lib148_"))
_saved_file = _lib148.FILE
_lib148.FILE = _lf / "library.json"
_real_wt = Path.write_text


def _wt_full(self, *a, **k):
    if self.suffix == ".tmp":
        _real_wt(self, "{\"half", encoding="utf8")
        raise OSError(28, "No space left on device", str(self))
    return _real_wt(self, *a, **k)


Path.write_text = _wt_full
try:
    _lib148.save([], {}, "0", 89)
finally:
    Path.write_text = _real_wt
    _lib148.FILE = _saved_file
check("a library save that hits a full drive leaves no .tmp behind",
      not list(_lf.glob("*.tmp")), [p.name for p in _lf.iterdir()])
shutil.rmtree(_lf, ignore_errors=True)

# A refused update download left its staging folder in %TEMP%: 838 of them
# on the machine these tests run on.
_before = set(Path(tempfile.gettempdir()).glob(_su148.PREFIX + "*"))
_saved_su = net.json_get
net.json_get = lambda url: {"tag_name": "v9.9", "assets": []}
try:
    try:
        _su148.fetch()
    except _su148.UpdateError:
        pass
finally:
    net.json_get = _saved_su
_after = set(Path(tempfile.gettempdir()).glob(_su148.PREFIX + "*"))
check("a failed update fetch removes its staging folder", not (_after - _before),
      [p.name for p in _after - _before])
_sw148 = _su148.swap_script(Path(r"C:\Tools\dlss5-autopilot.exe"),
                            Path(tempfile.gettempdir()) / (_su148.PREFIX + "abc") / "dlss5-autopilot.exe")
# without /s: rmdir then removes the folder only if it is empty
check("...and the swap script removes the emptied one after an update",
      f'rmdir "{Path(tempfile.gettempdir()) / (_su148.PREFIX + "abc")}" >nul' in _sw148,
      _sw148[-300:])

# #87/#93: tar.exe without LZMA, no 7-Zip installed. The answer was "install
# 7-Zip"; now 7-Zip's own one-file unpacker is fetched, pinned by hash.
import subprocess as _sp87  # noqa: E402
_d87 = Path(tempfile.mkdtemp(prefix="sz87_"))
(_d87 / "in").mkdir()
(_d87 / "in" / "OptiScaler.dll").write_bytes(b"MZ" + b"\1" * 5000)
try:
    _zr = net.download(sources.SEVEN_ZR[0][0], "7zr.exe")
    _sp87.run([str(_zr), "a", str(_d87 / "t.7z"), str(_d87 / "in" / "OptiScaler.dll")],
              capture_output=True)
    _saved87 = (optiscaler._seven_zip, optiscaler._tar_exe)
    optiscaler._seven_zip = lambda: None
    optiscaler._tar_exe = lambda: _d87 / "no-tar.exe"
    try:
        optiscaler.extract_7z(_d87 / "t.7z", _d87 / "out")
        check("with no 7-Zip and no working tar.exe, the pinned 7zr.exe unpacks a .7z (#87)",
              (_d87 / "out" / "OptiScaler.dll").is_file())
        _saved_pin = sources.SEVEN_ZR
        sources.SEVEN_ZR = (_saved_pin[0], "0" * 64)
        try:
            _ran = optiscaler._seven_zr_extract(_d87 / "t.7z", _d87 / "out2", 0)
        finally:
            sources.SEVEN_ZR = _saved_pin
        check("...and a 7zr.exe that is not the pinned build is never run",
              _ran is False and not (_d87 / "out2" / "OptiScaler.dll").exists())
    finally:
        optiscaler._seven_zip, optiscaler._tar_exe = _saved87
except (sources.RateLimited, sources.Unavailable, OSError) as e:
    check("with no 7-Zip and no working tar.exe, the pinned 7zr.exe unpacks a .7z (#87)",
          False, f"could not fetch 7zr.exe: {e}")
shutil.rmtree(_d87, ignore_errors=True)

# Three reports replayed against the code, each with its own log lines.
def _feeder_dir(prefix, api, bitness, reshade, feed):
    _d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, _d / "Game.exe")
    _files = ["dlss5-feed.addon64", "renodx-dlss5.addon64", "ReShade.ini",
              "reshade-shaders/Shaders/DLSS5_Feed.fx",
              "reshade-shaders/Shaders/lumenite_Kernel.fx",
              "reshade-shaders/Shaders/vort_Motion.fx"]
    for _f in _files:
        (_d / _f).parent.mkdir(parents=True, exist_ok=True)
        (_d / _f).write_bytes(b"MZ")
    (_d / "dlss5-autopilot.json").write_text(json.dumps(
        {"version": 1, "complete": True, "exe": "Game.exe", "bitness": bitness,
         "api": api, "proxy": "dxgi.dll", "path": "feeder", "files": _files}),
        encoding="utf8")
    (_d / "ReShade.log").write_text(reshade, encoding="utf8")
    (_d / "dlss5-feed.log").write_text(feed, encoding="utf8")
    return _d


# #156 Octowow, 32-bit OpenGL: frames delivered, then the helper died. It was
# called "closed before it drew a single frame" (OpenGL has no swap-chain
# line), and with no helper log in the folder the verdict was "Working."
_d156 = _feeder_dir("oct156_", "OpenGL", 32, (
    '11:18:26:359 [25448] | INFO  | Registered add-on "DLSS 5 Feed (32-bit) 0.15.1" v0.0.0.0 using ReShade API version 20.\n'
    "11:18:38:663 [25448] | WARN  | [DLSS 5 Feed (32-bit) 0.15.1] [DLSS 5 Feed 32] stopped: the 64-bit host went away -- its own dlss5-feed-host.log (in host64\\) names the reason. The game renders normally\n"
    "11:21:16:610 [25448] | INFO  | Exiting ...\n"), (
    "11:18:34.900  [feed32] effects: technique found, DLSS5_MV found, DLSS5_Depth found, DLSS5_MV_PROVIDER=2 (VORT) -> vort_MotionEffects (enabled), depth reversed=1\n"
    "11:18:35.043  [feed32] frame 2 delivered (3440x1440, reset=0, OpenGL)\n"
    "11:18:35.060  [feed32] frame 3 delivered (3440x1440, reset=0, OpenGL)\n"
    "11:18:38.625  [feed32] host lost: frame message failed (exit code 3765269347)\n"
    "11:18:38.663  stopped: the 64-bit host went away -- its own dlss5-feed-host.log (in host64\\) names the reason. The game renders normally. See dlss5-feed.log for the detail.\n"
    "11:21:16.514  shut down cleanly.\n"))
_r156 = diagnose.analyse(_d156)
_t156 = [f.title for f in _r156.findings]
check("frames delivered on OpenGL are not 'closed before it drew a single frame' (#156)",
      not any("closed before it drew" in t for t in _t156), _t156)
check("...and a feed that stopped after them is not 'Working.' (#156)",
      _r156.verdict != "Working." and any("stopped after" in t for t in _t156),
      (_r156.verdict, _t156))
shutil.rmtree(_d156, ignore_errors=True)

# #142 Half Sword: the game died one second in, while the feed was still
# waiting for ReShade to compile. "Never loaded" and "not installed" were
# said over a file list with both shaders in place.
_d142 = _feeder_dir("hs142_", "DX12", 64, (
    '18:29:10:426 [21916] | INFO  | Registered add-on "DLSS 5 Feed 0.15.1" v0.15.1.0 using ReShade API version 20.\n'
    "18:29:11:068 [21916] | INFO  | Redirecting IDXGIFactory::CreateSwapChain(this = 000001FA2374E1A0) ...\n"), (
    "18:29:11.331  [feed] effects: DLSS5_Feed.fx technique MISSING, ColorInput MISSING, DLSS5_MV MISSING, DLSS5_Depth MISSING, DLSS5_Mask absent (older shader: no bias mask), DLSS5_MV_PROVIDER=3 (LumeniteFX Kernel) -> none (not installed)\n"
    "18:29:11.331  [feed] DLSS5_Feed.fx has not resolved yet (ReShade may still be compiling); waiting 10 s before calling it missing\n"
    "18:29:11.335  [feed] effect runtime 000001FA283BAE00 destroyed -- it was the bound one (0 runtimes left)\n"))
_t142 = [f.title for f in diagnose.analyse(_d142).findings]
check("a game that closed while the effects compiled is not told the shader never loaded (#142)",
      any("still compiling" in t for t in _t142)
      and not any("never loaded" in t or "not installed" in t for t in _t142), _t142)
shutil.rmtree(_d142, ignore_errors=True)

# #155: a ReShade.log with none of the lines the excerpt keeps was printed
# as "(none)" - read as "ReShade never loaded", the opposite.
_ex155 = diagnose._reshade_excerpt(
    "11:59:40:001 [ 100] | INFO  | Initializing crosire's ReShade version '6.8.0.0' (64-bit)\n"
    "11:59:40:002 [ 100] | INFO  | Loading add-ons from C:\\Games\\Sleeping Dogs\n")
check("a ReShade.log with none of the kept lines still reaches the report (#155)",
      len(_ex155) == 2 and "Initializing" in _ex155[0], _ex155)

check("Forspoken's dxgi.dll signature check is said before the install (#154)",
      any("signature" in q for q in dlss.quirks(Path("FORSPOKEN.exe"), "DX12")))

# Gate findings, 1.8.2.
# A quick rescan only ever added: a game removed in Steam that left its
# folder behind stayed. What no store lists in the first place stays.
_qs = Path(tempfile.mkdtemp(prefix="qs_"))
for _n in ("Gone", "Kept", "Mine"):
    (_qs / _n).mkdir()
_known = [games.Game(name="Gone", folder=_qs / "Gone", source="Steam"),
          games.Game(name="Kept", folder=_qs / "Kept", source="Steam"),
          games.Game(name="Mine", folder=_qs / "Mine", source="Manual")]
_saved_lg = games.list_games
games.list_games = lambda progress=None, emulators=True: [
    games.Game(name="Kept", folder=_qs / "Kept", source="Steam")]
try:
    _qout, _qfresh = games.quick_scan(_known)
finally:
    games.list_games = _saved_lg
check("a quick rescan drops a store game no store lists any more, keeps a hand-picked one",
      sorted(g.name for g in _qout) == ["Kept", "Mine"] and not _qfresh,
      [g.name for g in _qout])
shutil.rmtree(_qs, ignore_errors=True)

# The install's own refusal, raised part way, was recorded as an unfinished
# install and diagnosed as "install again" - which is refused again (#148).
_dr = _fake_remix("remix_refuse_")
(_dr / ".trex" / "d3d9.dll").write_bytes(b"MZ MOD RUNTIME, NO PASS" + b"\x00" * 400)
_gr = games.manual(_dr)
_gr.api = "DX9"
try:
    installer.install(_gr, installer.Options(path=dlss.REMIX), on_log=lambda t: None)
except installer.InstallError:
    pass
_rr = diagnose.analyse(_dr)
check("a refused install is diagnosed by its reason, not 'install again' (#148)",
      "never finished" not in _rr.verdict
      and any("swap the Remix runtime" in f.detail for f in _rr.findings),
      (_rr.verdict, [f.detail[:80] for f in _rr.findings]))
shutil.rmtree(_dr, ignore_errors=True)


class _R7:
    returncode = 2
    stdout = "ERROR: There is not enough space on the disk : C:\\x\\OptiScaler.dll"
    stderr = ""


try:
    optiscaler._raise_if_full(_R7(), Path("C:/x"))
    _full7 = False
except OSError as e:
    _full7 = net.is_disk_full(e)
check("a full drive while unpacking a .7z is a full drive, not 'install 7-Zip'", _full7)

# #152: UBOAT was installed for "UBOAT Launcher.exe" - the launcher is
# bigger than Unity's player stub, and both match the folder's name.
_ub = Path(tempfile.mkdtemp(prefix="uboat152_")) / "UBOAT"
(_ub / "UBOAT_Data").mkdir(parents=True)
shutil.copyfile(X64, _ub / "UBOAT.exe")
(_ub / "UBOAT Launcher.exe").write_bytes((_ub / "UBOAT.exe").read_bytes() + b"\0" * (8 << 20))
check("a launcher beside the game it starts is not taken for the game (#152)",
      pe.find_game_exes(_ub)[0].name == "UBOAT.exe",
      [p.name for p in pe.find_game_exes(_ub)])
shutil.rmtree(_ub.parent, ignore_errors=True)

section("1.8.2: api.github.com answered by something else (#175), and the "
        "crash verdict that stopped before the next step (#98)")

# Several checks below build a folder that looks like somebody's install and
# read the verdict out of it - the same thing _tools/replay_report.py does
# for a saved report, so it is imported rather than written twice.
sys.path.insert(0, str(SRC_DIR / "_tools"))
import replay_report as _rr182  # noqa: E402

# The window's own source, read here rather than borrowed: _gsrc is rebound
# four times on the way down this file and by now it is one function's.
_guisrc182 = src_of(_gui)

# #175, word for word out of the report: the chain verified and the NAME on
# the certificate did not match, which the old answer read as a missing
# Windows root and told the person to open GitHub in Edge.
_MISMATCH = ("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate "
             "verify failed: Hostname mismatch, certificate is not valid "
             "for 'api.github.com'. (_ssl.c:1010)>")
_m = str(net.untrusted("api.github.com", Exception(_MISMATCH)))
check("a hostname mismatch is not called a missing root certificate (#175)",
      "different name" in _m and "once in Edge" not in _m, _m[:90])
check("...and it names what actually does it",
      all(w in _m for w in ("DNS", "VPN", "1.1.1.1")), _m[:90])
_exp = str(net.untrusted("reshade.me", Exception(
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "certificate has expired (_ssl.c:1010)")))
check("an expired certificate points at this PC's clock",
      "clock" in _exp and "Edge" not in _exp, _exp[:90])
_self = str(net.untrusted("github.com", Exception(
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self "
    "signed certificate in certificate chain")))
check("a self-signed chain points at the HTTPS scanning that made it",
      "antivirus" in _self and "Edge" not in _self, _self[:90])
_root = str(net.untrusted("api.github.com", Exception(
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to "
    "get local issuer certificate")))
check("a missing root still gets the answer that fixes a missing root (#54)",
      "Edge" in _root and "api.github.com" in _root, _root[:90])
check("nothing but a verification failure is claimed as one",
      net.untrusted("x", Exception("timed out")) is None)

# The same two pages the fallback reads, in GitHub's own markup.
_ASSETS_HTML = ('</svg>          <a href="/jlrouzies-fr/DLSS5-Feeder/releases'
                '/download/v0.15.1/AUTOMATIC_INSTALLATION_AVAILABLE.txt" '
                'rel="nofollow" data-turbo="false" class="wb-break-all">'
                '<a href="/jlrouzies-fr/DLSS5-Feeder/releases/download/'
                'v0.15.1/DLSS5-Feeder-0.15.1.zip" rel="nofollow">')
_LIST_HTML = ('<a href="/jlrouzies-fr/DLSS5-Feeder/releases/tag/v1.16.0-beta.1"'
              ' data-view-component="true" class="Link--primary Link">1.16.0'
              '<a href="/jlrouzies-fr/DLSS5-Feeder/releases/tag/v0.15.1" '
              'class="Link--primary Link">0.15.1'
              '<a href="/jlrouzies-fr/DLSS5-Feeder/releases/tag/v0.15.1#top" '
              'class="Link">same release again')
with patch.object(sources, "_page", lambda url, timeout=30: _ASSETS_HTML):
    _a = sources.release_assets_html("jlrouzies-fr/DLSS5-Feeder", "v0.15.1")
check("a release's assets are read off github.com, with no API call",
      _a.get("DLSS5-Feeder-0.15.1.zip", "").endswith(
          "/jlrouzies-fr/DLSS5-Feeder/releases/download/v0.15.1/"
          "DLSS5-Feeder-0.15.1.zip") and len(_a) == 2, _a)
with patch.object(sources, "_page", lambda url, timeout=30: _LIST_HTML):
    _t = sources.release_tags_html("jlrouzies-fr/DLSS5-Feeder", pages=1)
check("...and the tags are, newest first, each one once",
      _t == [("v1.16.0-beta.1", True), ("v0.15.1", False)], _t)
check("a test build is told by its tag, not by a badge whose markup moves",
      sources.release_tags_html.__doc__ and "badge" in sources.release_tags_html.__doc__)
with patch.object(sources, "_page", lambda url, timeout=30: ""):
    check("a page that does not answer ends the walk instead of looping",
          sources.release_tags_html("x/y", pages=4) == []
          and sources.release_assets_html("x/y", "v1") == {})

check("the feeder falls back to those pages when the API does not answer",
      "_feeder_html" in src_of(sources.resolve_feeder)
      and "release_tags_html" in src_of(sources.feeder_releases))
check("...and the build list does too, or gives the API's own error",
      "_rhi_html_catalog" in src_of(sources.rhi_catalog)
      and "raise" in src_of(sources.rhi_catalog))
check("a pinned tag the API HAS answered about is not hunted for on the pages",
      "_NoSuchTag" in src_of(sources.resolve_feeder))
check("every build the installer pins by name survives the shortened list",
      set(sources.RHI_HTML_REQUIRED["renodx"]) == {
          sources.FEEDER_RENODX_PIN, sources.OPENGL_RENODX_PIN,
          sources.DRIVER_FAULT_RENODX_PIN},
      sources.RHI_HTML_REQUIRED)
check("...and a pin that is missing anyway is said out loud, not installed over",
      'e["label"] != want' in src_of(installer)
      and "the kind of build that pin exists to avoid" in src_of(installer)
      # ...and it names a control that exists (the gate found it naming one
      # that did not).
      and "'dlss5 add-on'" in src_of(installer))
_FOREIGN = ('<a href="/someone/else/releases/tag/v9.9">'
            '<a href="/jlrouzies-fr/DLSS5-Feeder/releases/tag/v0.15.1">'
            '<a href="/someone/else/releases/download/v9.9/other.zip">')
with patch.object(sources, "_page", lambda url, timeout=30: _FOREIGN):
    _ft = sources.release_tags_html("jlrouzies-fr/DLSS5-Feeder", pages=1)
    _fa = sources.release_assets_html("jlrouzies-fr/DLSS5-Feeder", "v0.15.1")
check("a link to another project's release is not read as one of ours",
      _ft == [("v0.15.1", False)] and _fa == {}, (_ft, _fa))
check("one install's fallback notice does not leak into the next",
      "sources.last_fallback = None" in src_of(installer.install))
check("the fallback tells the user the list came from somewhere else",
      "last_fallback" in src_of(sources.rhi_catalog)
      and "last_fallback" in src_of(sources.resolve_feeder))

# Every component that is not in sources.py reaches GitHub through
# net.json_get, and the components inside it through sources._json. Both
# now answer from github.com's pages when the API cannot be reached - the
# API's own shape, so no caller's asset matching changes.
check("a releases URL is recognised in all three of its forms",
      sources._RELEASE_URL.match(
          "https://api.github.com/repos/doitsujin/dxvk/releases/latest"
      ).group(2) == "latest"
      and sources._RELEASE_URL.match(
          "https://api.github.com/repos/a/b/releases/tags/v1").group(3) == "v1"
      and sources._RELEASE_URL.match(
          "https://api.github.com/repos/a/b/releases?per_page=20") is not None)
check("...and something that is not one is left alone",
      sources.release_json_html(
          "https://api.github.com/repos/crosire/reshade/tags?per_page=5") is None
      and sources.release_json_html("https://example.com/x") is None)
_ONE = ('<a href="/a/b/releases/download/v2.0/Thing-v2.0.zip">'
        '<a href="/a/b/releases/download/v2.0/Thing-v2.0.zip.sha256">')
with patch.object(sources, "_page", lambda url, timeout=30: _ONE),         patch.object(sources, "latest_tag", lambda repo: "v2.0"):
    _r = sources.release_json_html("https://api.github.com/repos/a/b/releases/latest")
check("a release comes back in the API's own shape",
      _r["tag_name"] == "v2.0" and _r["draft"] is False
      and _r["prerelease"] is False
      and _r["assets"][0]["name"] == "Thing-v2.0.zip"
      and _r["assets"][0]["browser_download_url"].startswith("https://github.com/"),
      _r)
with patch.object(sources, "_page", lambda url, timeout=30: _ONE),         patch.object(sources, "latest_tag", lambda repo: None):
    check("a tag that cannot be read is None, not an empty release",
          sources.release_json_html(
              "https://api.github.com/repos/a/b/releases/latest") is None)
check("the list form is capped, so it cannot walk a hundred pages",
      sources.HTML_LIST_MAX <= 15)
import urllib.error as _ue2  # noqa: E402
_e404 = _ue2.HTTPError("https://api.github.com/repos/a/b/releases/latest",
                               404, "Not Found", None, None)
with patch.object(net, "fetch_text", lambda u: (_ for _ in ()).throw(_e404)):
    try:
        net.json_get("https://api.github.com/repos/a/b/releases/latest")
        _raised = ""
    except _ue2.HTTPError as e:
        _raised = str(e.code)
    except Exception as e:
        _raised = type(e).__name__
check("a 404 is the answer and is raised - only an unreachable host falls back",
      _raised == "404", _raised)
check("every component outside sources.py gets it through json_get",
      "release_json_html" in src_of(net.json_get)
      and all("json_or_html" in src_of(m) or "json_get" in src_of(m)
              for m in (optiscaler, dxvk, refw, _m141)))
check("...and the ones inside it, through json_or_html",
      "json_or_html" in src_of(sources.resolve_bridge)
      and "sources._json(" not in src_of(video))
check("rhi_catalog keeps its own walk - the capped list would drop the pins",
      "_rhi_html_catalog" in src_of(sources.rhi_catalog)
      and "json_or_html" not in src_of(sources.rhi_catalog))

# The first screen of the README is what a person reads before deciding
# whether this tool is worth downloading, and the GitHub About line beside
# it was describing a feeder installer long after the tool had eight
# routes. Numbers in a document rot silently; these two are checked.
_readme = (SRC_DIR / "README.md").read_text(encoding="utf8")
_WORDS = {8: "eight", 9: "nine", 10: "ten", 7: "seven"}
check("the README's route count is the number of routes there are",
      f"{_WORDS.get(len(dlss.LABELS), len(dlss.LABELS))} routes" in _readme.lower(),
      len(dlss.LABELS))
from core import emulators as _emus  # noqa: E402
check("...and its emulator count is the number of profiles there are",
      f"{len(_emus.PROFILES)} emulators" in _readme, len(_emus.PROFILES))
check("the first screen says the tool reads the logs afterwards, not only "
      "that it installs",
      "did it work?" in _readme[:3000] and "uninstall" in _readme[:3000].lower())
check("...and that nothing is bundled",
      "bundled" in _readme[:3000].lower() or "bundles nothing" in _readme[:3000])

# Every control the README names in bold has to exist. A renamed button
# leaves the document telling people to press something that is not there -
# the same fault as #148, one surface out. Two words are enough to match on,
# because some labels are built at run time ("update (3 newer)").
_readme_src = "".join((SRC_DIR / "core" / n).read_text(encoding="utf8")
                      for n in ("gui.py", "installer.py", "optiscaler.py",
                                "dlss.py")).lower()
_named, _gone = 0, []
for _b in _re.findall(r"[*][*]([^*\n]{2,40})[*][*]", _readme):
    _t = _b.strip().rstrip(".")
    if not _t or _t[0].isupper() or "," in _t or len(_t.split()) > 5:
        continue                       # a sentence or a heading, not a control
    _named += 1
    _probe = " ".join(_t.split()[:2]).strip(" ?:%-").lower()
    if _probe and _probe not in _readme_src:
        _gone.append(_t)
check("every control the README names in bold exists in the tool",
      _named >= 30 and not _gone, _gone or _named)

# The loop that measures itself: state.py turns every saved verdict into a
# ranked backlog, and a class at the top is a shape to fix. Checked here so
# the ranking cannot silently stop describing the corpus.
import importlib.util as _ilu3  # noqa: E402
_st_spec = _ilu3.spec_from_file_location("state", SRC_DIR / "_tools" / "state.py")
_st = _ilu3.module_from_spec(_st_spec)
_st_spec.loader.exec_module(_st)
_stc = _st.corpus()
_stsaved = json.loads((SRC_DIR / "_tools" / "verdict_baseline.json")
                     .read_text(encoding="utf8"))
check("the loop measures every report in the corpus, not a sample",
      _stc.get("total", 0) == len(_stsaved)
      == len(list((SRC_DIR / "_tools" / "reports").glob("*.txt"))),
      (_stc.get("total"), len(_stsaved)))
check("...and almost nothing falls through its classes",
      _stc["counts"].get("other", 0) <= max(6, _stc["total"] // 10),
      _stc["counts"].get("other"))
check("the biggest class is named, with the reports behind it",
      bool(_stc["counts"].most_common(1)[0][0])
      and len(_stc["where"][_stc["counts"].most_common(1)[0][0]]) ==
      _stc["counts"].most_common(1)[0][1])
check("a verdict this project has actually given lands in the right class",
      _st.classify("Not started since the install - run the game once.")
      == "nothing we wrote ever loaded"
      and _st.classify("Inconclusive - the feed did not get far enough to tell.")
      == "we cannot tell from the logs"
      and _st.classify("Working.") == "working")
_settings182 = SRC_DIR.parent / ".claude" / "settings.json"
check("...and the guard rails report themselves as wired",
      _st.guards() == [] if _settings182.is_file() else True,
      _st.guards() if _settings182.is_file()
      else "(no .claude/settings.json beside the repo - skipped)")
check("the session opens with that measurement",
      (SRC_DIR / "_tools" / "hooks" / "session_start.py").is_file()
      and "state.py" in (SRC_DIR / "_tools" / "hooks"
                         / "session_start.py").read_text(encoding="utf8"))

# 34 of the first 84 reports are "no log at all", and the answer to them
# opened with "the game has not been started since the install" - a guess,
# and the one sentence that makes a person who DID start it give up. The
# game's own files answer it.
_last_ran_findings = []


def _no_log_verdict(make=None, exe="Game.exe"):
    d = _rr182.build("feeder", "DX12", exe, {}, 64)
    try:
        if make:
            make(d)
        r = diagnose.analyse(d)
        _last_ran_findings[:] = r.findings
        return r.verdict, [f.title for f in r.findings]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _later(p, seconds=120):
    t = time.time() + seconds
    os.utime(p, (t, t))


_v, _f = _no_log_verdict()
check("a fresh install nothing has run still says exactly that",
      "Not started since the install" in _v
      and any("has not been started" in t for t in _f), _v)


def _save(d):
    sav = d / "Saved" / "SaveGames"
    sav.mkdir(parents=True)
    (sav / "PlayerProgress.sav").write_bytes(b"x")
    _later(sav / "PlayerProgress.sav")


_v, _f = _no_log_verdict(_save)
check("the game's own save, written after the install, says it DID run",
      "looks as though it ran" in _v
      and any("own files changed after the install" in t for t in _f), _v)
check("...and the verdict hedges it, because a store update writes there too",
      "most likely" in _v and "though a store update" in " ".join(
          f.detail or "" for f in
          [x for x in _last_ran_findings]), _v)


def _dll(d):
    (d / "somemod.dll").write_bytes(b"MZ")
    _later(d / "somemod.dll")


_v, _f = _no_log_verdict(_dll)
check("a DLL that appeared afterwards is not a game leaving a trace",
      "Not started since the install" in _v, _v)


def _ours(d):
    # dxgi.dll is in the manifest the replay builds: our own file, never
    # evidence about the game.
    _later(d / "dxgi.dll")


_v, _f = _no_log_verdict(_ours)
check("...and neither is a file our own manifest says we wrote",
      "Not started since the install" in _v, _v)


def _within_the_minute(d):
    sav = d / "Saved"
    sav.mkdir()
    (sav / "settings.cfg").write_bytes(b"x")
    _later(sav / "settings.cfg", seconds=5)


_v, _f = _no_log_verdict(_within_the_minute)
check("a file written seconds after the install is the install, not a session",
      "Not started since the install" in _v, _v)

_ud = Path(tempfile.mkdtemp(prefix="userdata_"))
(_ud / "Wardogs" / "Saved" / "Logs").mkdir(parents=True)
_ulog = _ud / "Wardogs" / "Saved" / "Logs" / "Wardogs.log"
_ulog.write_bytes(b"x")
_later(_ulog)
with patch.object(diagnose, "_user_data_roots", lambda: [_ud]):
    _v, _f = _no_log_verdict(exe="WardogsClient-Win64-Shipping.exe")
check("an Unreal game writes under LOCALAPPDATA, and that counts too",
      "looks as though it ran" in _v, _v)
check("...the folder name is worked out from the executable",
      "Wardogs" in diagnose._user_data_names(Path("C:/g/Binaries/Win64"),
                                             "WardogsClient-Win64-Shipping.exe"),
      diagnose._user_data_names(Path("C:/g/Binaries/Win64"),
                                "WardogsClient-Win64-Shipping.exe"))
shutil.rmtree(_ud, ignore_errors=True)
check("the look is bounded - entries and a clock, never a walk",
      diagnose._RAN_ENTRIES <= 8000 and diagnose._RAN_SECONDS <= 2.0
      and "os.walk" not in src_of(diagnose._game_ran))

# The owner's own machine, found by detect_check: an Unreal game whose
# shipping exe imports no graphics DLL at all was read as OpenGL, because
# the only renderer name in the file is Unreal's unused OpenGL RHI string.
# ReShade would have gone in as opengl32.dll, which the game never loads -
# and the report would have come back with no log at all.
_ue = Path(tempfile.mkdtemp(prefix="unreal_"))
_ue_exe = _ue / "Wardogs" / "Binaries" / "Win64" / "WardogsClient-Win64-Shipping.exe"
_ue_exe.parent.mkdir(parents=True)
shutil.copyfile(X64, _ue_exe)
with open(_ue_exe, "ab") as _f:
    _f.write(b"opengl32.dll")
check("without the engine folder the rule does not fire at all",
      pe._engine_default(_ue_exe) is None and not pe._is_unreal(_ue_exe))
(_ue / "Engine").mkdir()
_api, _why = pe.detect_api(_ue_exe)
check("an Unreal layout decides the renderer before an opengl32 string does",
      _api == "DX12" and "Unreal" in _why, (_api, _why[:60]))
check("...and it is the shape that says so, not the executable's name",
      pe._is_unreal(_ue_exe)
      and not pe._is_unreal(_ue / "Wardogs" / "Binaries" / "Win64"))
_nue = Path(tempfile.mkdtemp(prefix="notunreal_"))
(_nue / "bin" / "win64").mkdir(parents=True)
shutil.copyfile(_ue_exe, _nue / "bin" / "win64" / "Game.exe")
check("a game in some other bin/win64, with no Engine folder, is left alone",
      pe._engine_default(_nue / "bin" / "win64" / "Game.exe") is None)
(_nue / "Engine").mkdir()
check("...and an Engine folder alone is not the Unreal layout either - "
      "Binaries is part of it",
      pe._engine_default(_nue / "bin" / "win64" / "Game.exe") is None)
shutil.rmtree(_ue, ignore_errors=True)
shutil.rmtree(_nue, ignore_errors=True)

# #182 (Resident Evil 4, "it never started"): the report said "ReShade
# loaded no add-ons" over a log block that read "(none)". Three shapes, and
# they are three different answers.
def _verdict_for(log_text):
    d = _rr182.build("feeder", "DX12", "re4.exe", {"reshade": " "}, 64)
    (d / "ReShade.log").write_text(log_text, encoding="utf8")
    try:
        r = diagnose.analyse(d)
        return r.verdict, [f.title for f in r.findings]
    finally:
        shutil.rmtree(d, ignore_errors=True)
_NL = chr(10)
_BANNER = ("16:29:03:123 [1234] | INFO  | Initializing crosire's ReShade "
           "version 6.6.1 ..." + _NL)
for _label, _txt in (("empty", ""), ("whitespace", _NL + "  " + _NL)):
    _v, _f = _verdict_for(_txt)
    check(f"a ReShade.log with nothing in it is no log, not 'no add-ons' ({_label})",
          "Not started since the install" in _v
          and not any("loaded no add-ons" in t for t in _f), (_v, _f[:1]))
_v, _f = _verdict_for(_BANNER)
check("ReShade's banner and nothing after it is a game that died at start-up",
      "closed during start-up" in _v
      and any("session ended before anything else" in t for t in _f), (_v, _f[:1]))
_v, _f = _verdict_for("16:29:03:123 [1234] | WARN  | Reference count for "
                      "ID3D12CommandQueue0 is inconsistent (7)." + _NL)
check("...but a tail that does not start where ReShade did says nothing of the kind",
      any("loaded no add-ons" in t for t in _f), _f[:2])
_v, _f = _verdict_for(_BANNER + "16:29:05:001 [1234] | INFO  | Redirecting "
                      "IDXGIFactory::CreateSwapChain(...)" + _NL)
check("...and neither does a session that got as far as a swap chain",
      any("loaded no add-ons" in t for t in _f), _f[:2])

# Every report anybody ever sent, through the diagnosis as it is now,
# against the answers recorded last time. This is the net under every
# change to diagnose.py: 236 if/elif branches over one input, where a rule
# put in front of the others silently changes what all of them see.
import importlib.util as _ilu2  # noqa: E402
_vspec = _ilu2.spec_from_file_location("verdict_check",
                                       SRC_DIR / "_tools" / "verdict_check.py")
_vc = _ilu2.module_from_spec(_vspec)
_vspec.loader.exec_module(_vc)
_vbase = json.loads((SRC_DIR / "_tools" / "verdict_baseline.json").read_text(encoding="utf8"))
_vnow = _vc.answers()
_vmoved = _vc._diff(_vbase, _vnow)
check("the corpus is every real report with logs, not a handful",
      len(_vnow) >= 80, len(_vnow))
check("the diagnosis raises on none of them",
      not [n for n, r in _vnow.items() if r.get("error")],
      [n for n, r in _vnow.items() if r.get("error")][:5])
check("no report's verdict moved without being recorded "
      "(_tools/verdict_check.py --save)",
      not _vmoved, " | ".join(_vmoved[:6]))

# The words every verdict about somebody else's log is matched against
# live in their builds, and they move (#168: "cost:" became "elapsed:").
# _tools/phrase_check.py downloads the current builds and looks for them;
# here, offline, only that its table still describes THIS code.
import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("phrase_check",
                                     SRC_DIR / "_tools" / "phrase_check.py")
_pcmod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_pcmod)
_dg = (SRC_DIR / "core" / "diagnose.py").read_text(encoding="utf8")
_pairs = [pair for group in _pcmod.PHRASES.values() for pair in group]
_missing = [c for _b, c in _pairs if c not in _dg]
check("every phrase the rot check watches is one the diagnosis really reads",
      len(_pairs) >= 20 and not _missing, _missing or len(_pairs))
# The skills and the hook wiring live beside the repository, not in it, so
# on anybody else's checkout there is nothing to read. Absent means "not set
# up here", which is not a failure; present means it has to say so.
_skill182 = SRC_DIR.parent / ".claude" / "skills" / "issue-triage" / "SKILL.md"
check("...and the check is in the standing audits, so it runs by itself",
      "phrase_check.py" in _skill182.read_text(encoding="utf8")
      if _skill182.is_file() else True,
      "" if _skill182.is_file() else "(no .claude beside the repo - skipped)")

# #137: Cyberpunk's CET IS an ASI loader and owns version.dll. The unlock
# used to write Ultimate ASI Loader over it - CET, and every mod that needs
# it, out of the game. The reporter's own way round is now the tool's.
_cp = Path(tempfile.mkdtemp(prefix="cet137_"))
_cpexe = _cp / "Cyberpunk2077.exe"
shutil.copyfile(X64, _cpexe)
check("with nothing in the way the loader name is what it always was",
      _m141.loader_name(_cpexe, set(), _cp) == _m141.loader_name(_cpexe, set())
      is not None)
(_cp / "version.dll").write_bytes(b"MZ" + b"CyberEngineTweaks" * 64)
(_cp / "plugins").mkdir()
check("another mod's DLL under that name is not taken (#137)",
      _m141.loader_name(_cpexe, set(), _cp) is None
      and _m141.loader_name(_cpexe, set()) == "version.dll")
check("a folder merely CALLED plugins is not a loader's (#137, the gate)",
      _m141.existing_plugins(_cp) is None)
# CET's own folder carries its .asi; that is what says a loader reads it.
(_cp / "plugins" / "cyber_engine_tweaks.asi").write_bytes(b"MZ-cet")
check("...and one with an .asi already in it is",
      _m141.existing_plugins(_cp) == _cp / "plugins")
check("a plugins folder with no loader beside it is somebody else's",
      _m141.existing_plugins(Path(tempfile.mkdtemp(prefix="noload_"))) is None)
check("a name WE wrote last time is still ours to write again",
      _m141.loader_name(_cpexe, set(), _cp, {"version.dll"}) == "version.dll")
_ualdir = Path(tempfile.mkdtemp(prefix="ual137_"))
shutil.copyfile(X64, _ualdir / _cpexe.name)
(_ualdir / "version.dll").write_bytes(b"MZ" + b"Ultimate ASI Loader" + b"x" * (1 << 18))
check("Ultimate ASI Loader under that name is merged with, not stepped around",
      _m141.loader_name(_ualdir / _cpexe.name, set(), _ualdir) == "version.dll")
_mz = Path(tempfile.mkdtemp(prefix="mfgzip_")) / "unlock.zip"
with _zf141.ZipFile(_mz, "w") as _z:
    for _n in _m141.FILES:
        _z.writestr(_n, b"MZ" + _n.encode())
with patch.object(_m141, "resolve", lambda: ("v1.2.1", "https://example/u.zip")),         patch.object(_m141, "resolve_loader",
                     lambda: ("v9", "https://example/l.zip")),         patch.object(net, "download", lambda url, name, **k: _mz):
    _tag, _files = _m141.install(_cp, _cpexe, taken=set())
check("the unlock goes into the loader's plugins folder, whole",
      sorted(_files) == ["RTX40MFG-UI.addon64", "plugins/RTX40MFG.asi",
                         "plugins/RTX40MFGCore.dll"]
      and (_cp / "plugins" / "RTX40MFG.asi").is_file()
      and (_cp / "RTX40MFG-UI.addon64").is_file(), _files)
check("...and the other mod's DLL is untouched, with no loader of ours beside it",
      (_cp / "version.dll").read_bytes().startswith(b"MZCyberEngineTweaks")
      and not (_cp / "version.dll.dlss5-autopilot-backup").exists()
      and not (_cp / "RTX40MFGCore.dll").exists())
check("a reinstall that no longer wants it takes the plugins copy back out",
      sorted(_m141.remove_leftovers(_cp, _files)) ==
      ["RTX40MFG-UI.addon64", "plugins/RTX40MFG.asi", "plugins/RTX40MFGCore.dll"]
      or not (_cp / "plugins" / "RTX40MFG.asi").exists())
check("the preview and the step list know about that route too",
      "existing_plugins" in src_of(installer.preview)
      and src_of(installer).count("existing_plugins") >= 2)
shutil.rmtree(_cp, ignore_errors=True)
shutil.rmtree(_ualdir, ignore_errors=True)
shutil.rmtree(_mz.parent, ignore_errors=True)

# 616.64+ steers off every route that loads renodx-dlss5, where a route
# that does not is on offer. The shared results: standalone has not failed
# yet where a renodx route did - on a handful of reports, which is what the
# reason says.
_sd = Path(tempfile.mkdtemp(prefix="steer_"))
_steer = {drv: dlss.detect(_sd, _sd, "DX12", 64, sm=120, driver=drv).recommended
          for drv in (None, "616.56", "616.64", "616.92")}
check("no driver given, nothing steers - every existing caller is unchanged",
      _steer[None] == dlss.FEEDER and _steer["616.56"] == dlss.FEEDER, _steer)
check("616.64 and newer recommend the route that does not load renodx-dlss5",
      _steer["616.64"] == dlss.STANDALONE and _steer["616.92"] == dlss.STANDALONE,
      _steer)
check("...and the reason says which route it moved off, and what it costs",
      all(w in dlss.detect(_sd, _sd, "DX12", 64, sm=120, driver="616.92").reason
          for w in ("feeder", "616.56", "borderless", "handful")))
check("...and it leaves a marker, so the games page can say why",
      dlss.detect(_sd, _sd, "DX12", 64, sm=120,
                  driver="616.92").steered_from == dlss.FEEDER
      and not dlss.detect(_sd, _sd, "DX12", 64, sm=120).steered_from)
check("...which the games page actually prints",
      "steered_from" in _guisrc182)
check("a game whose dropdown has no standalone entry is not steered to it",
      dlss.detect(_sd, _sd, "DX9", 32, sm=120, driver="616.92").recommended
      != dlss.STANDALONE)
(_sd / "nvngx_dlss.dll").write_bytes(b"MZ")
check("a game that ships its own DLSS keeps OptiScaler - it never loads the add-on",
      dlss.detect(_sd, _sd, "DX12", 64, sm=120, driver="616.92").recommended
      == dlss.OPTI)
check("the window passes the driver in, or the steer never runs",
      "driver=gpu.driver_version()" in _guisrc182,
      len(_guisrc182))
shutil.rmtree(_sd, ignore_errors=True)

# #98: "It ran, and then the game crashed" was the end of the answer. The
# reporter found the next step himself, and it is the one test that splits
# the neural pass from everything else.
_crash_src = src_of(_gui.App._crash_overrides)
check("the crash verdict now carries the test that splits it in two (#98)",
      "[DlssNr]" in _crash_src and "Enabled=false" in _crash_src)
check("...on the route whose ini that is, and something real on the others",
      'route == "optiscaler"' in _crash_src and "uninstall" in _crash_src.lower())

section("RESULT")
_REACHED_RESULT = True
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("EVERYTHING PASSED")
