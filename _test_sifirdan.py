"""ARKADASININ BILGISAYARI TESTI.

Bos onbellek + yerel renodx dosyasi YOK. Her sey aynadan sifirdan iner.
Amac: "eksik kurulum" ihtimalini varsaymak yerine olcmek.

Kullanicinin gercek onbellegine dokunmaz; gecici bir onbellek kullanir.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core import games, installer, net, pe, prefs, reshade_ini, feedcfg, gpu

# --- arkadasinin bilgisayarini taklit et --------------------------------
TMP_CACHE = Path(tempfile.mkdtemp(prefix="sifir_cache_"))
net.CACHE = TMP_CACHE                       # bos onbellek
prefs.find_renodx = lambda: (None, [])      # yerel renodx yok
print(f"gecici onbellek : {TMP_CACHE}")
print("yerel renodx    : YOK (arkadasinda olmayacak)")
print()

DL = Path(r"C:\Users\Mustafa\Downloads")


def build(src: Path, name: str, nested: bool) -> Path:
    d = Path(tempfile.mkdtemp(prefix="sifir_oyun_"))
    ex = d / "Bin" / "Win64" if nested else d
    ex.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, ex / name)
    return d


def check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"   {'OK  ' if cond else 'EKSIK'}  {label}" + (f"   {detail}" if detail else ""))
    return cond


def run(label: str, src: Path, exename: str, nested: bool) -> bool:
    print("=" * 76)
    print(f"TEST: {label}")
    print("=" * 76)
    root = build(src, exename, nested)
    g = games.manual(root)
    print(f"  {g.bit_label} / {g.api}  ->  {g.exe.name}")

    ok, why = installer.check_supported(g)
    if not ok:
        print(f"  DESTEKLENMIYOR: {why}")
        return False

    # renodx surumunu acikca vererek aynayi zorla (yerel dosya yok zaten)
    opt = installer.Options(renodx="4.60", keep_game_dlss=False)
    rep = installer.install(g, opt, on_log=lambda t: print("   ", t))

    idir = g.install_dir
    x64 = g.bitness == 64
    dl_dir = idir if x64 else idir / installer.HOST_DIR
    proxy = installer._proxy_name(g.api)
    print()
    print("  --- eksiksizlik denetimi ---")
    good = True

    # 1) ReShade
    good &= check("ReShade proxy", (idir / proxy).is_file() and
                  installer._is_reshade(idir / proxy), proxy)
    good &= check("proxy mimarisi oyunla ayni",
                  pe.exe_bitness(idir / proxy) == g.bitness)

    # 2) shader basliklari
    sh = idir / installer.SHADERS
    for h in ("ReShade.fxh", "ReShadeUI.fxh", "DrawText.fxh"):
        good &= check(f"başlık {h}", (sh / h).is_file())

    # 3) feeder
    addon = installer.FEEDER_ADDON64 if x64 else installer.FEEDER_ADDON32
    good &= check(f"feeder {addon}", (idir / addon).is_file())
    good &= check("DLSS5_Feed.fx", (sh / "DLSS5_Feed.fx").is_file())

    # 4) LumeniteFX
    good &= check("lumenite_Kernel.fx", (sh / "lumenite_Kernel.fx").is_file())
    good &= check("lumenite include", (sh / "include" / "lumenite_Helpers.fxh").is_file())
    good &= check("bluenoise dokusu",
                  (idir / installer.TEXTURES / "lumenite_bluenoise256.png").is_file())

    # 5) DLSS parcalari + GPU uyumu
    for f in (installer.RENODX, installer.DLSSNR, installer.DLSS):
        p = dl_dir / f
        good &= check(f"{f}", p.is_file(),
                      f"{p.stat().st_size/1048576:.0f} MB" if p.is_file() else "")
        if p.is_file():
            good &= check(f"  {f} 64-bit", pe.exe_bitness(p) == 64)
    _, sm = gpu.detect()
    compat, why = gpu.check(dl_dir / installer.DLSSNR, sm)
    good &= check("dlssnr bu kartla uyumlu", compat is not False, why[:60])

    # 6) 32-bit / DX9 ek parcalari
    if not x64:
        h = idir / installer.HOST_DIR
        good &= check("host64/dlss5-feed-host64.exe", (h / installer.FEEDER_HOST).is_file())
        good &= check("host64/dxgi.dll 64-bit",
                      (h / "dxgi.dll").is_file() and pe.exe_bitness(h / "dxgi.dll") == 64)
        good &= check("host64/ReShade.ini", (h / "ReShade.ini").is_file())
    if g.api == "DX9":
        good &= check("D3D9.dll (dgVoodoo2)", (idir / "D3D9.dll").is_file())
        good &= check("D3D9.dll 32-bit", pe.exe_bitness(idir / "D3D9.dll") == 32)
        conf = (idir / "dgVoodoo.conf")
        good &= check("dgVoodoo.conf", conf.is_file())
        if conf.is_file():
            txt = conf.read_text(encoding="utf8", errors="replace")
            good &= check("  OutputAPI=d3d11_fl11_0", "d3d11_fl11_0" in txt)

    # 7) ayar dosyalari
    ini = reshade_ini.Ini.load(idir / "ReShade.ini")
    pre = reshade_ini.Ini.load(idir / "ReShadePreset.ini")
    good &= check("ReShade.ini DLSS5_MV_PROVIDER=3",
                  ini.get("GENERAL", "PreprocessorDefinitions") == "DLSS5_MV_PROVIDER=3")
    good &= check("ReShade.ini AddonPath",
                  ini.get("ADDON", "AddonPath") == chr(92).join([".", ""]))
    techs = reshade_ini.split_list(pre.get("", "Techniques") or "")
    good &= check("teknik sırası (sağlayıcı ÜSTTE)",
                  techs == ["Lumenite_Kernel@lumenite_Kernel.fx",
                            "DLSS5_Feed@DLSS5_Feed.fx"], str(techs))
    cfg = feedcfg.read(idir / feedcfg.NAME)
    good &= check("dlss5-feed.cfg", bool(cfg), f"{len(cfg)} anahtar")
    good &= check("  mode=2 (tam DLSS)", cfg.get("mode") == "2")
    good &= check("kurulum kaydı", (idir / installer.MANIFEST).is_file())
    good &= check("araç 'kurulu' olarak görüyor", g.installed)

    shutil.rmtree(root, ignore_errors=True)
    print(f"\n  SONUC: {'EKSIKSIZ' if good else 'EKSIK VAR'}\n")
    return good


if __name__ == "__main__":
    x64 = DL / "dlss5-feed-host64.exe"
    x86 = Path(r"C:\Windows\SysWOW64\notepad.exe")
    dx9 = Path(r"D:\SteamLibrary\steamapps\common\Grand Theft Auto IV\GTAIV\GTAIV.exe")

    results = []
    results.append(("64-bit DX11/DX12", run("64-bit oyun", x64, "Oyun64.exe", True)))
    results.append(("32-bit", run("32-bit oyun", x86, "Oyun32.exe", False)))
    if dx9.is_file():
        results.append(("DX9 (dgVoodoo2)", run("DX9 oyunu", dx9, "OyunDX9.exe", False)))

    print("=" * 76)
    print(f"indirilen toplam: {net.cache_size()/1048576:.0f} MB  (bos onbellekten)")
    for name, ok in results:
        print(f"   {'GECTI' if ok else 'KALDI'}  {name}")
    shutil.rmtree(TMP_CACHE, ignore_errors=True)
    print("=" * 76)
    print("HEPSI EKSIKSIZ" if all(o for _, o in results) else "EKSIK VAR")
    sys.exit(0 if all(o for _, o in results) else 1)
