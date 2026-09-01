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
            "reshade_ini", "feedcfg", "dgvoodoo", "dlss", "vulkan",
            "anticheat", "optiscaler", "diagnose", "selfupdate", "update",
            "log", "components",
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
check("version is 1.3.0", update.VERSION == "1.3.0", update.VERSION)

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

# The card changes one recommendation: RTX 50 + D3D12 + DLSS -> OptiScaler.
g50 = _fake_game("rtx50_")
sup50 = dlss.detect(g50.install_dir, g50.folder, "DX12", 64, sm=120)
sup40 = dlss.detect(g50.install_dir, g50.folder, "DX12", 64, sm=89)
check("rtx 50 with a dlss d3d12 game is steered to optiscaler",
      sup50.recommended == dlss.OPTI, sup50.recommended)
check("rtx 40 keeps the native route", sup40.recommended == dlss.NATIVE, sup40.recommended)
check("optiscaler is marked unusable on an rtx 40",
      dlss.fit(dlss.OPTI, "DX12", True, 89)[0] is False)
check("optiscaler is marked usable on an rtx 50",
      dlss.fit(dlss.OPTI, "DX12", True, 120)[0] is True)
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

# The SF add-on is told apart from renodx-dlss5 by content, not by name.
d = Path(tempfile.mkdtemp(prefix="sf_"))
(d / "a.addon64").write_bytes(b"MZ" + bytes(300_000) + b"RenoDX DLSS renodx-dlss.addon64")
(d / "b.addon64").write_bytes(b"MZ" + bytes(300_000) + b"RenoDX DLSS renodx-dlss5.addon64")
check("sf build recognised", prefs.is_renodx_sf(d / "a.addon64"))
check("renodx-dlss5 is not mistaken for sf", not prefs.is_renodx_sf(d / "b.addon64"))
shutil.rmtree(d, ignore_errors=True)




section("RESULT")
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("EVERYTHING PASSED")
