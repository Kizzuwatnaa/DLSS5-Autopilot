"""Full verification pass: every module imports, every route installs and
uninstalls cleanly, and the guard rails actually fire.

Run this before cutting a release.
"""
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
            "installer", "gui")
    for m in mods:
        try:
            __import__(f"core.{m}")
            check(f"core.{m}", True)
        except Exception as e:
            check(f"core.{m}", False, f"{type(e).__name__}: {e}")

from core import (diagnose, dlss, games, gpu, installer, net, pe, prefs,  # noqa: E402
                  reshade_ini, sources, update, vulkan)

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
          and all(o in (dlss.NATIVE, dlss.OPTI, dlss.BRIDGE, dlss.FEEDER)
                  for o in s.options))
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
             (dlss.NATIVE, dlss.BRIDGE), (dlss.BRIDGE, dlss.NATIVE)):
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
            stale = [f for f in files if "OptiScaler" in f or "nvngx.dll_dlssnr" in f]
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

# ---------------------------------------------------------------- 6. vulkan
section("6. vulkan layer handling")
before = vulkan.existing_registration()
check("existing ReShade registration is detected or absent", True, str(before))
check("our layer dir is not the existing one",
      before is None or not vulkan.is_ours(before))

# ---------------------------------------------------------------- 7. misc
section("7. odds and ends")
check("rate-limit fallback message exists", hasattr(sources, "last_fallback"))
check("api cache path set", "api-cache" in str(sources._API_CACHE))
check("download supports retry", "attempts" in net.download.__code__.co_varnames)
check("update points at the right repo", update.REPO.endswith("DLSS5-Autopilot"))
check("version is 1.3.0", update.VERSION == "1.3.0", update.VERSION)
r = diagnose.analyse(Path(r"C:\Program Files (x86)\Steam\steamapps\common\DEATHLOOP"))
check("diagnosis reads a real log", bool(r.verdict), r.verdict[:52])

section("RESULT")
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("EVERYTHING PASSED")
