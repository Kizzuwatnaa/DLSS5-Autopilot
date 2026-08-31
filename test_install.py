"""End-to-end install test: performs a real install into fake game folders."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
from core import games, installer, pe, reshade_ini  # noqa: E402

DL = Path(r"C:\Users\Mustafa\Downloads")
X64_SRC = DL / "dlss5-feed-host64.exe"          # a real 64-bit PE
X86_SRC = Path(r"C:\Windows\SysWOW64\notepad.exe")


def find_x86():
    for c in (X86_SRC,
              Path(r"C:\Windows\SysWOW64\write.exe"),
              Path(r"C:\Windows\SysWOW64\mspaint.exe")):
        try:
            if c.is_file() and pe.exe_bitness(c) == 32:
                return c
        except Exception:
            pass
    return None


def build_fake(src: Path, name: str, nested: bool) -> Path:
    d = Path(tempfile.mkdtemp(prefix="dlss5test_"))
    exedir = d / "Bin" / "Win64" if nested else d
    exedir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, exedir / name)
    return d


def show_tree(root: Path, limit=60):
    items = sorted(p for p in root.rglob("*") if p.is_file())
    for p in items[:limit]:
        size = p.stat().st_size
        unit = f"{size/1048576:7.1f} MB" if size > 1048576 else f"{size/1024:7.1f} KB"
        print(f"    {unit}  {p.relative_to(root)}")
    if len(items) > limit:
        print(f"    ... +{len(items)-limit} more files")
    return items


def run(label: str, src: Path, exename: str, nested: bool):
    print("=" * 78)
    print(f"TEST: {label}")
    print("=" * 78)
    root = build_fake(src, exename, nested)
    g = games.manual(root)
    print(f"  folder      : {root}")
    print(f"  found exe   : {g.exe.relative_to(root)}")
    print(f"  arch / API  : {g.bit_label} / {g.api}")
    print(f"  install dir : {g.install_dir.relative_to(root)}  <-- next to the exe")
    ok, why = installer.check_supported(g)
    assert ok, why

    rep = installer.install(g, installer.Options(),
                            on_log=lambda t: print("   ", t))
    print(f"\n  --- files written ({len(rep.written)}) ---")
    items = show_tree(root)

    idir = g.install_dir
    proxy = installer._proxy_name(g.api)
    x64 = g.bitness == 64

    # --- dogrulamalar ---
    assert (idir / proxy).is_file(), f"{proxy} missing"
    assert installer._is_reshade(idir / proxy), f"{proxy} not ReShade"
    assert pe.exe_bitness(idir / proxy) == g.bitness, "proxy DLL bitness does not match the game"

    addon = installer.FEEDER_ADDON64 if x64 else installer.FEEDER_ADDON32
    assert (idir / addon).is_file(), f"{addon} missing"

    sh = idir / installer.SHADERS
    assert (sh / "DLSS5_Feed.fx").is_file()
    assert (sh / "ReShade.fxh").is_file()
    assert (sh / "lumenite_Kernel.fx").is_file()
    assert (sh / "include" / "lumenite_Helpers.fxh").is_file()
    assert (idir / installer.TEXTURES / "lumenite_bluenoise256.png").is_file()

    dl_dir = idir if x64 else idir / installer.HOST_DIR
    for f in (installer.RENODX, installer.DLSSNR, installer.DLSS):
        assert (dl_dir / f).is_file(), f"{f} missing ({dl_dir.name})"
        assert pe.exe_bitness(dl_dir / f) == 64, f"{f} is not 64-bit"

    if not x64:
        h = idir / installer.HOST_DIR
        assert (h / installer.FEEDER_HOST).is_file(), "host64 exe missing"
        assert (h / "dxgi.dll").is_file(), "host64 dxgi.dll missing"
        assert pe.exe_bitness(h / "dxgi.dll") == 64, "host64 dxgi.dll must be 64-bit"
        assert (h / "ReShade.ini").is_file()
        print("  [32-bit] host64/ verified")

    ini = reshade_ini.Ini.load(idir / "ReShade.ini")
    pre = reshade_ini.Ini.load(idir / "ReShadePreset.ini")
    assert ini.get("GENERAL", "PreprocessorDefinitions") == "DLSS5_MV_PROVIDER=3"
    assert ini.get("ADDON", "AddonPath") == chr(92).join([".", ""])
    techs = reshade_ini.split_list(pre.get("", "Techniques"))
    assert techs == ["Lumenite_Kernel@lumenite_Kernel.fx", "DLSS5_Feed@DLSS5_Feed.fx"], techs
    assert g.installed, "not detected as installed"
    print(f"  ini/preset  : technique order {techs}")

    print("\n  --- UNINSTALL ---")
    installer.uninstall(g, on_log=lambda t: print("   ", t))
    for f in (proxy, addon, "ReShade.ini"):
        pass
    assert not (idir / proxy).is_file(), "proxy not removed"
    assert not (idir / addon).is_file(), "addon not removed"
    assert not (idir / installer.HOST_DIR).exists(), "host64 not removed"
    assert not (idir / installer.MANIFEST).is_file()
    assert (idir / exename).is_file(), "THE GAME'S OWN EXE WAS DELETED!"
    kalan = [p.relative_to(root) for p in root.rglob("*") if p.is_file()]
    print(f"    remaining files: {[str(k) for k in kalan]}")
    assert len(kalan) == 1, f"cleanup incomplete: {kalan}"

    shutil.rmtree(root, ignore_errors=True)
    print(f"\n  {label}: PASSED\n")


if __name__ == "__main__":
    run("64-bit game (exe in a subfolder)", X64_SRC, "OyunumX64.exe", nested=True)
    x86 = find_x86()
    if x86:
        run("32-bit game (exe in the root)", x86, "OyunumX86.exe", nested=False)
    else:
        print("32-bit test skipped: no suitable 32-bit exe found")
    print("=" * 78)
    print("ALL INSTALL TESTS PASSED")
