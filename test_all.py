"""Full verification pass: every module imports, every route installs and
uninstalls cleanly, and the guard rails actually fire.

Run this before cutting a release.
"""
import json
import shutil
import subprocess
import sys
import tempfile
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
            "reshade_ini", "feedcfg", "dgvoodoo", "dxvk", "dlss", "vulkan",
            "anticheat", "optiscaler", "diagnose", "selfupdate", "update",
            "log", "components", "profiles",
            "installer", "gui")
    for m in mods:
        try:
            __import__(f"core.{m}")
            check(f"core.{m}", True)
        except Exception as e:
            check(f"core.{m}", False, f"{type(e).__name__}: {e}")

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

# a 32-bit game must never be offered the 64-bit-only bridge
for g in playable:
    if g.bitness == 32:
        s = dlss.detect(g.install_dir, g.folder, g.api, g.bitness)
        check(f"32-bit {g.name[:22]} is feeder-only", s.options == [dlss.FEEDER],
              str(s.options))

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

# ---------------------------------------------------------------- 6. vulkan
section("6. vulkan layer handling")
before = vulkan.existing_registration()
check("existing ReShade registration is detected or absent", True, str(before))
check("our layer dir is not the existing one",
      before is None or not vulkan.is_ours(before))

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
check("version is 1.4.4", update.VERSION == "1.4.4", update.VERSION)

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
_old = _comp.check(_d)
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

# DirectX 10: nothing reaches it, and the tool must say so rather than install.
d = Path(tempfile.mkdtemp(prefix="dx10_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
g.api = "DX10"
ok, why = installer.check_supported(g)
check("dx10 is refused with a reason", not ok and "DirectX 10" in why, why)
s10 = dlss.detect(d, d, "DX10", 64)
check("dx10 route report says unsupported", not s10.supported)
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
check("dx9 through dxvk: no dgvoodoo, dxvk first, vulkan layer, host64 helper",
      s9[0] == "DXVK (DX9 -> Vulkan)" and "dgVoodoo2 (DX9 -> D3D11)" not in s9
      and s9[1] == "ReShade (Vulkan layer)" and "host64 helper process" in s9, str(s9))
check("dx9 without dxvk still goes through dgvoodoo",
      installer.plan(g9, installer.Options(path=dlss.FEEDER))[0].startswith("dgVoodoo2"))
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
    check("the mpc-hc sample reads as working with no failures",
          _r.ran and _r.verdict == "Working." and not _levels(_r, "bad"),
          _r.verdict + " " + str(_levels(_r, "bad")))

# the bug report body
_d = _diag_dir("diag_body_", feed=_FEED_OK, reshade=(
    "INFO | Redirecting RegisterClassW(...)\n"
    'INFO | Registered add-on "DLSS 5 Feed" v0.11\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"))
_r = diagnose.analyse(_d)
_body = diagnose.issue_body("9.9", "RTX 4060 Ti", 89, "581.0", None, "feeder", _r,
                            "scan steam: 3 found\nscan epic: 0 found\nreal line\n",
                            Path("C:/x/autopilot.log"), _d, last_error="Traceback: boom")
_order = ["**What happened**", "**What I expected**", "- version: 9.9",
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
check("an unsupported api is a blocker", any("DirectX 10" in b for b in pv.blockers))
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

section("RESULT")
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("EVERYTHING PASSED")
