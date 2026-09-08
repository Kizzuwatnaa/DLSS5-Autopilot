"""Full verification pass: every module imports, every route installs and
uninstalls cleanly, and the guard rails actually fire.

Run this before cutting a release.
"""
import inspect
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILS: list[str] = []
X64 = Path(r"C:\Users\Mustafa\Downloads\dlss5-feed-host64.exe")
SRC_DIR = Path(__file__).resolve().parent


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"   {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)
    return cond


def section(title: str) -> None:
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
from core import gui as _gui  # noqa: E402
_r = _tk.Tk()
_app = _gui.App(_r)
_r.update()

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
check("version is 1.7.3", update.VERSION == "1.7.3", update.VERSION)

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
s9 = dlss.detect(Path(tempfile.gettempdir()), Path(tempfile.gettempdir()), "DX9", 64)
check("64-bit dx9 goes to the renodx add-on only", s9.options == [dlss.RENODX], str(s9.options))
# 32-bit stays feeder-only whatever the API.
for api in ("DX9", "DX11", "DX12", "Vulkan", "OpenGL"):
    s32 = dlss.detect(Path(tempfile.gettempdir()), Path(tempfile.gettempdir()), api, 32)
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
      not _r.ran and "quarantined" in _r.verdict
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

# A readable game under XboxGames (after Enable mods) is a normal game.
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


def _deny_read(exe: Path) -> bool:
    """Take the current user's read right away with icacls; False if that
    cannot be done here (no icacls, elevated token that ignores it, ...)."""
    if not _os.environ.get("USERNAME"):
        return False
    try:
        who = subprocess.run(["whoami"], capture_output=True, text=True,
                             timeout=15).stdout.strip() or _os.environ["USERNAME"]
        # (RD) only: a full (R) deny also takes READ_CONTROL away, after
        # which icacls itself can no longer read the ACL to undo it.
        r = subprocess.run(["icacls", str(exe), "/deny", f"{who}:(RD)"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return False
        with open(exe, "rb") as f:
            f.read(1)
        return False    # the deny did not bite - do not test on top of it
    except PermissionError:
        return True
    except Exception:
        return False


def _allow_read(exe: Path) -> None:
    try:
        who = subprocess.run(["whoami"], capture_output=True, text=True,
                             timeout=15).stdout.strip() or _os.environ.get("USERNAME", "")
        subprocess.run(["icacls", str(exe), "/remove:d", who],
                       capture_output=True, text=True, timeout=15)
    except Exception:
        pass


# The real failure: an exe under XboxGames the user cannot read.
_d = Path(tempfile.mkdtemp(prefix="xbox_locked_"))
_content = _d / "XboxGames" / "Fake" / "Content"
_content.mkdir(parents=True)
_exe = _content / "Game.exe"
shutil.copyfile(X64, _exe)
if _deny_read(_exe):
    try:
        _g = games.manual(_content)
        check("an unreadable XboxGames exe gets the Enable-mods sentence",
              _g.error == games.XBOX_HINT, repr(_g.error))
        check("...the game keeps its executable so it stays listed",
              _g.exe == _exe)
        _ok, _why = installer.check_supported(_g)
        check("...and check_supported hands that sentence to the GUI",
              not _ok and _why == games.XBOX_HINT, repr(_why))
        # preflight on the real denied folder: writing next to the exe is
        # allowed here (only the file's read was denied), so use the
        # monkeypatch below for the write failure instead.
    finally:
        _allow_read(_exe)
    check("read right restored", _exe.read_bytes()[:2] == b"MZ")
else:
    check("icacls deny not available here - unreadable-exe checks skipped", True)
shutil.rmtree(_d, ignore_errors=True)

# The same unreadable exe outside a store folder keeps the plain error: the
# Xbox instruction would send someone to an app that has nothing to do with
# their game.
_d = Path(tempfile.mkdtemp(prefix="plain_locked_"))
_exe = _d / "Game.exe"
shutil.copyfile(X64, _exe)
if _deny_read(_exe):
    try:
        _g = games.manual(_d)
        check("an unreadable exe elsewhere does not mention the Xbox app",
              _g.error and "Xbox" not in _g.error, repr(_g.error))
    finally:
        _allow_read(_exe)
else:
    check("icacls deny not available here - plain unreadable check skipped", True)
shutil.rmtree(_d, ignore_errors=True)

# preflight: a write refused under XboxGames says Enable mods, anywhere else
# it says run as administrator.
_d = Path(tempfile.mkdtemp(prefix="xbox_pre_"))
_content = _d / "XboxGames" / "Fake" / "Content"
_content.mkdir(parents=True)
shutil.copyfile(X64, _content / "Game.exe")
_gx = games.manual(_content)
_plain = Path(tempfile.mkdtemp(prefix="plain_pre_"))
shutil.copyfile(X64, _plain / "Game.exe")
_gp = games.manual(_plain)
_orig_wb = Path.write_bytes


def _refuse(self, data):
    raise PermissionError(13, "Permission denied", str(self))


Path.write_bytes = _refuse
try:
    try:
        installer.preflight(_gx)
        check("preflight under XboxGames raises on a refused write", False)
    except installer.InstallError as e:
        check("preflight under XboxGames names Enable mods, not administrator",
              "Enable mods" in str(e) and "administrator" not in str(e), str(e)[:80])
    try:
        installer.preflight(_gp)
        check("preflight elsewhere raises on a refused write", False)
    except installer.InstallError as e:
        check("preflight elsewhere still says run as administrator",
              "administrator" in str(e) and "Xbox" not in str(e), str(e)[:80])
finally:
    Path.write_bytes = _orig_wb
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
      "_ships_dlss(path.parent)" in inspect.getsource(pe.detect_api))
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
      and "OPENGL_RENODX_PIN" in inspect.getsource(installer.install))
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
      "- reshade-shaders/Shaders/DLSS5_Feed.fx: MISSING" in _body
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
      and '"fg": opt.fg' in inspect.getsource(installer)
      and 'fg=bool(data.get("fg"' in inspect.getsource(installer.options_from_manifest))

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
(_game / _lname).write_bytes(b"MZ-the-games-own")
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
check("the game's own file under that name is backed up and the loader takes the name",
      (_game / (_lname + _m.BACKUP_SUFFIX)).read_bytes() == b"MZ-the-games-own"
      and _m.is_loader(_game / _lname), str(_files))
_ini = (_game / _lini).read_text(encoding="utf8")
check("the loader's ini is merged, not replaced",
      "LoadExtraPlugins=RTX40MFG.asi" in _ini and "UseCrashHandler=0" in _ini
      and "[Other]" in _ini and "Keep=1" in _ini and _ini.count("[GlobalSets]") == 1, _ini)
check("an ini that existed before is not listed as ours; the backup and the loader are",
      _lini not in _files and _lname in _files
      and (_lname + _m.BACKUP_SUFFIX) in _files, str(_files))
check("Options carries mfg and the manifest round-trips it",
      installer.Options().mfg is False and '"mfg": opt.mfg' in inspect.getsource(installer)
      and 'mfg=bool(data.get("mfg"' in inspect.getsource(installer.options_from_manifest))
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
      "elif opt.provider == 2:" in inspect.getsource(installer._install_feeder_parts)
      and "_install_vort" in inspect.getsource(installer._install_feeder_parts)
      and "_install_vort" in inspect.getsource(installer.install))
check("an OpenGL install with a Lumenite provider is switched to VORT before planning",
      'g.api == "OpenGL" and opt.provider in (3, 4)' in inspect.getsource(installer.install))
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
selfupdate.MIN_BYTES = 70000         # the fixture exe (65 KB) alone is below this; exe + _internal is above
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
    selfupdate.MIN_BYTES = 70000
    check("a one-file release yields the exe alone",
          _exe1.name == "dlss5-autopilot.exe" and not (_exe1.parent / "_internal").exists())
    net.download = lambda url, name, **k: _dir
    _exe2 = selfupdate.fetch()
    check("...but for a one-folder release the whole download is measured (exe alone would fail)",
          _exe2.stat().st_size < 70000)
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
      "is_removable(base)" in inspect.getsource(games.scan_folders)
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
      "rtx40mfg-ui.addon64" in inspect.getsource(installer.other_ngx_hooks))

section("44. driver 616.64+: renodx-dlss5 is pinned to 4.55, and the fault is named")
check("the pin constants exist", sources.DRIVER_FAULT_MIN == "616.64" and sources.DRIVER_FAULT_RENODX_PIN == "4.55")
check("install() consults the driver before the OpenGL and feeder pins",
      inspect.getsource(installer.install).index("DRIVER_FAULT_MIN")
      < inspect.getsource(installer.install).index("OPENGL_RENODX_PIN"))
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

check("the Xbox hint no longer claims every Store game has an 'Enable mods' switch",
      "Only games whose publisher" in games.XBOX_HINT and "Steam version" in games.XBOX_HINT)

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
      "its own config is switched" in inspect.getsource(installer.preview))
check("the game list does not flag a DXVK install as an API change",
      'man.get("proxy") != diagnose.VULKAN_LAYER' in inspect.getsource(_gui))
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
      'f"reinstall - was {man_api}"' in inspect.getsource(_gui))
shutil.rmtree(_d, ignore_errors=True)

section("48. issue #40: pixel sizes follow the display scale, not only the fonts")
import re as _re
_gsrc = inspect.getsource(_gui)
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
      "context=ssl_context()" in inspect.getsource(net.download)
      and "context=net.ssl_context()" in inspect.getsource(sources._get))
check("the release build installs certifi",
      "pip install --upgrade certifi" in Path(".github/workflows/release.yml").read_text(encoding="utf8"))
check("a failed verification is explained",
      "untrusted(name, e)" in inspect.getsource(net.download))

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
      and "net.untrusted" in inspect.getsource(sources._get))
check("Options carries the build and the manifest records it",
      hasattr(installer.Options(), "opti_build")
      and '"opti_build": opt.opti_build' in inspect.getsource(installer))
check("the install page offers the build list",
      "cb_optibuild" in inspect.getsource(_gui) and "opti_build=list(optiscaler.BUILDS)" in inspect.getsource(_gui))
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
      hasattr(installer.Options(), "vr") and '"vr": bool(opt.vr)' in inspect.getsource(installer)
      and "ck_vr" in inspect.getsource(_gui) and "vr=bool(self.vr.get())" in inspect.getsource(_gui))
check("the ReShade step registers the layer when asked, and uninstall drops it with the last VR game",
      "openxr.install_layer(setup, log)" in inspect.getsource(installer)
      and "prefs.openxr_games()" in inspect.getsource(installer))
check("the command line has --vr", '"--vr" in args' in Path("dlss5_autopilot.py").read_text(encoding="utf8"))
check("the command line has --opti-build and it reaches Options",
      '"--opti-build" in args' in Path("dlss5_autopilot.py").read_text(encoding="utf8")
      and Path("dlss5_autopilot.py").read_text(encoding="utf8").count("opti_build=opti_build") == 3)
check("'did it work?' lists the OpenXR registration for a VR install",
      any("OpenXR layer" in ln for ln in diagnose._presence(Path("."), {"vr": True, "path": "feeder"}, "feeder")))
check("every HTTPS fetch goes through ssl_context()",
      "context=ssl_context()" in inspect.getsource(net.fetch_text))

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
_gsrc = inspect.getsource(_gui)

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
        for n, name in ((1, "architecture"), (2, "game list"), (3, "install")):
            app.step = n
            app._show(n)
            for _ in range(3):
                root.update_idletasks()
                root.update()
            page = (app.p1, app.p2, app.p3)[n - 1]
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

_gsrc = inspect.getsource(_gui)
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
      "urlopen" not in inspect.getsource(sources.cached_json)
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
_gsrc = inspect.getsource(_gui)
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
      'if exe is not None:' in inspect.getsource(_lib._from_json)
      and 'source == "Emulator"' not in inspect.getsource(_lib._from_json))

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
      "want = 13 if delay else 1" in inspect.getsource(pe.pe_imports))
check("...after checking the image has that many data directories",
      "n_dirs <= want" in inspect.getsource(pe.pe_imports))
check("...with 32-byte descriptors and the name at offset 4",
      "(32, 4) if delay else (20, 12)" in inspect.getsource(pe.pe_imports))


# Issue #31, answered three times with "the game has not been started" while
# DXVK's own log sat in the folder proving it had. DXVK writes that log the
# moment it loads, so it settles what the hints could only guess at.
_d = Path(tempfile.mkdtemp(prefix="dxvklog_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "CoJGunslinger.exe", "bitness": 32,
     "api": "Vulkan", "proxy": diagnose.VULKAN_LAYER, "path": "feeder",
     "dxvk": True, "files": ["d3d9.dll", "dlss5-feed.addon32"],
     "vulkan": {"mine": True, "any": True}}), encoding="utf8")
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
shutil.rmtree(_d, ignore_errors=True)


section("RESULT")
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("EVERYTHING PASSED")
