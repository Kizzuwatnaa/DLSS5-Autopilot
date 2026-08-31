"""Kurulum motoru: 64-bit ve 32-bit yollari.

64-bit yol (oyunun yaninda):
    <proxy>.dll                 ReShade64.dll  (dxgi.dll ya da opengl32.dll)
    dlss5-feed.addon64
    renodx-dlss5.addon64
    nvngx_dlssnr.dll
    nvngx_dlss.dll
    ReShade.ini / ReShadePreset.ini
    reshade-shaders/Shaders/{basliklar, DLSS5_Feed.fx, lumenite_*.fx}
    reshade-shaders/Shaders/include/lumenite_*.fxh
    reshade-shaders/Textures/lumenite_bluenoise256.png

32-bit yol: 32-bit surec 64-bit NGX'i yukleyemez, bu yuzden yardimci bir
64-bit surec gerekir. Oyunun yaninda 32-bit ReShade + addon32; host64/
klasorunde ise kendi 64-bit ReShade'i ve tum DLSS parcalari bulunur.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import dgvoodoo, feedcfg, games, gpu, net, pe, prefs, reshade_ini, sources

MANIFEST = "dlss5kur-kurulum.json"

FEEDER_ADDON64 = "dlss5-feed.addon64"
FEEDER_ADDON32 = "dlss5-feed.addon32"
FEEDER_HOST = "dlss5-feed-host64.exe"
FEEDER_FX = "DLSS5_Feed.fx"
RENODX = "renodx-dlss5.addon64"
DLSSNR = "nvngx_dlssnr.dll"
DLSS = "nvngx_dlss.dll"
HOST_DIR = "host64"

SHADERS = Path("reshade-shaders") / "Shaders"
INCLUDE = SHADERS / "include"
TEXTURES = Path("reshade-shaders") / "Textures"


class InstallError(Exception):
    pass


@dataclass
class Options:
    provider: int = 3                       # DLSS5_MV_PROVIDER
    renodx: str | None = sources.RENODX_DEFAULT   # surum etiketi
    renodx_local: Path | None = None        # kullanicinin kendi dosyasi (Discord'dan)
    dlssnr: str | None = None               # None = en guncel
    dlss: str | None = None                 # None = en guncel
    keep_game_dlss: bool = True             # oyunun kendi nvngx_dlss.dll'i varsa dokunma
    feed: dict = field(default_factory=dict)   # dlss5-feed.cfg ayarlari
    ignore_gpu_mismatch: bool = False       # uyumsuz dlssnr'a ragmen devam et


@dataclass
class Report:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- yardimcilar

def _is_reshade(path: Path) -> bool:
    """ReShade proxy DLL'i literal 'ReShade' stringi tasir ve 1 MB'tan buyuktur."""
    try:
        if not path.is_file() or path.stat().st_size < (1 << 20):
            return False
        return b"ReShade" in path.read_bytes()
    except OSError:
        return False


def _proxy_name(api: str) -> str:
    return "opengl32.dll" if api == "OpenGL" else "dxgi.dll"


def check_supported(g: games.Game) -> tuple[bool, str]:
    """Bu oyun otomatik kurulabilir mi?"""
    if not g.exe:
        return False, "Oyunun çalıştırılabilir dosyası bulunamadı."
    if g.bitness not in (32, 64):
        return False, "Mimari okunamadı."
    if g.api == "Vulkan":
        return False, ("Vulkan oyunları otomatik kurulmuyor: ReShade'in Vulkan katmanını "
                       "sistem genelinde kaydetmesi gerekir. ReShade kurulumunu elle "
                       "çalıştırıp Vulkan'ı seç, sonra bu aracı tekrar aç.")
    if g.api == "DX9":
        if g.bitness != 32:
            return False, "64-bit DirectX 9 oyunu desteklenmiyor (çok nadir)."
        return True, ""       # dgVoodoo2 otomatik kurulur, sonrası 32-bit yol
    return True, ""


def _copy(src: Path, dst: Path, rep: Report, root: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    try:
        rep.written.append(str(dst.relative_to(root)))
    except ValueError:
        rep.written.append(str(dst))


# ----------------------------------------------------------------- plan

def plan(g: games.Game, opt: Options) -> list[str]:
    steps: list[str] = []
    if g.api == "DX9":
        steps.append("dgVoodoo2 (DX9 -> D3D11)")
    steps += ["ReShade", "ReShade shader başlıkları", "DLSS5-Feeder"]
    if opt.provider in (3, 4):
        steps.append("LumeniteFX (hareket vektörleri)")
    steps += ["DLSS 5 eklentisi (renodx)", "nvngx_dlssnr.dll", "nvngx_dlss.dll"]
    if g.bitness == 32:
        steps.append("host64 yardımcı süreç")
    steps += ["ReShade ayarları", "dlss5-feed.cfg"]
    return steps


# ----------------------------------------------------------------- kurulum

def install(g: games.Game, opt: Options, on_step=None, on_prog=None, on_log=None) -> Report:
    ok, why = check_supported(g)
    if not ok:
        raise InstallError(why)

    log = on_log or (lambda *_: None)
    step = on_step or (lambda *_: None)
    prog = on_prog or (lambda *_: None)

    root = g.install_dir
    rep = Report()
    x64 = g.bitness == 64
    proxy = _proxy_name(g.api)
    host = root / HOST_DIR

    # Baska bir enjektor var mi?
    existing = root / proxy
    if existing.is_file() and not _is_reshade(existing):
        raise InstallError(
            f"{proxy} zaten var ama ReShade değil (DXVK, Special K veya başka bir "
            f"enjektör olabilir). Önce onu kaldır, sonra tekrar dene.")

    steps = plan(g, opt)
    n = len(steps)
    i = 0

    def begin(name: str) -> None:
        nonlocal i
        step(i, n, name)
        log(f"[{i + 1}/{n}] {name}")
        i += 1

    def dl(url: str, fname: str) -> Path:
        def p(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            prog(pct, f"{fname} - {net.human(done)}"
                      + (f" / {net.human(total)}" if total else ""))
        return net.download(url, fname, progress=p)

    # --- 0) DX9 ise once dgVoodoo2 ---------------------------------------
    # Oyun D3D9.dll'i dgVoodoo2'den alacak, o da D3D11'e cevirecek; ReShade
    # bu yuzden d3d9.dll degil dxgi.dll olarak kurulur.
    if g.api == "DX9":
        begin("dgVoodoo2 (DX9 -> D3D11)")
        for f in dgvoodoo.install(root, log):
            rep.written.append(f)
        rep.notes.append("dgVoodoo2 kuruldu (DX9 -> D3D11). Oyun açılmazsa "
                         "dgVoodooCpl.exe ile VRAM'i artır.")

    # --- 1) ReShade -------------------------------------------------------
    begin("ReShade")
    ver, url = sources.resolve_reshade()
    setup = dl(url, f"ReShade_Setup_{ver}_Addon.exe")
    log(f"      ReShade {ver}")
    # Kurulum exesinin sonuna eklenmis zip: ReShade32.dll ve ReShade64.dll birlikte gelir.
    net.extract_one(setup, "ReShade64.dll" if x64 else "ReShade32.dll", root / proxy)
    rep.written.append(proxy)
    log(f"      {proxy} <- ReShade{'64' if x64 else '32'}.dll")
    if not x64:
        net.extract_one(setup, "ReShade64.dll", host / "dxgi.dll")
        rep.written.append(f"{HOST_DIR}/dxgi.dll")
        log(f"      {HOST_DIR}/dxgi.dll <- ReShade64.dll (yardımcı süreç için)")

    # --- 2) Shader basliklari --------------------------------------------
    begin("ReShade shader başlıkları")
    for h in sources.RESHADE_HEADERS:
        dest = root / SHADERS / h
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(net.fetch_text(sources.RESHADE_HEADERS_BASE + h))
        rep.written.append(str(Path(SHADERS) / h))
    log(f"      {', '.join(sources.RESHADE_HEADERS)}")

    # --- 3) DLSS5-Feeder --------------------------------------------------
    begin("DLSS5-Feeder")
    tag, assets = sources.resolve_feeder()
    log(f"      DLSS5-Feeder {tag}")
    addon = FEEDER_ADDON64 if x64 else FEEDER_ADDON32
    for name in (addon, FEEDER_FX) + ((FEEDER_HOST,) if not x64 else ()):
        if name not in assets:
            raise InstallError(f"DLSS5-Feeder sürümünde {name} yok.")
        f = dl(assets[name], f"{tag}-{name}")
        dest = (root / SHADERS / name) if name.endswith(".fx") else \
               (host / name if name == FEEDER_HOST else root / name)
        _copy(f, dest, rep, root)
        log(f"      {dest.relative_to(root)}")

    # --- 4) LumeniteFX ----------------------------------------------------
    if opt.provider in (3, 4):
        begin("LumeniteFX (hareket vektörleri)")
        z = dl(sources.LUMENITE_ZIP, "LumeniteFX-mainline.zip")
        w = net.extract_tree(z, "Shaders", str(SHADERS), root, only_ext=(".fx",))
        w += net.extract_tree(z, "Shaders/include", str(INCLUDE), root, only_ext=(".fxh",))
        w += net.extract_tree(z, "Textures", str(TEXTURES), root, only_ext=(".png",))
        for p_ in w:
            rep.written.append(str(p_.relative_to(root)))
        log(f"      {len(w)} dosya (shader + include + doku)")

    # --- 5/6/7) DLSS parcalari -------------------------------------------
    # 32-bit yolda bunlar host64/ icine, 64-bit yolda oyunun yanina gider.
    dlss_dir = root if x64 else host
    catalog = sources.rhi_catalog()

    begin("DLSS 5 eklentisi (renodx)")
    # Kullanici acikca bir dosya vermediyse bile yerelde bir renodx varsa onu
    # kullan: Discord'dan gelen surumler aynada bulunmuyor ve genelde daha yeni.
    if not opt.renodx_local:
        found, _ = prefs.find_renodx()
        if found:
            opt.renodx_local = found
            log(f"      yerel renodx bulundu: {found.name}")
    if opt.renodx_local:
        src = Path(opt.renodx_local)
        if not src.is_file():
            raise InstallError(f"Seçilen renodx dosyası bulunamadı: {src}")
        try:
            if pe.exe_bitness(src) != 64:
                raise InstallError("Seçilen renodx dosyası 64-bit değil.")
        except pe.PEError as e:
            raise InstallError(f"Seçilen renodx dosyası geçerli değil: {e}") from e
        _copy(src, dlss_dir / RENODX, rep, root)
        log(f"      {src.name} (yerel dosyan) -> {RENODX}")
        rep.notes.append(f"renodx: yerel dosya kullanıldı ({src.name})")
    else:
        e = sources.pick(catalog["renodx"], opt.renodx)
        f = dl(e["url"], f"renodx-{e['label']}.zip")
        net.extract_one(f, ".addon64", dlss_dir / RENODX)
        rep.written.append(str((dlss_dir / RENODX).relative_to(root)))
        log(f"      renodx-dlss5 {e['label']}")
        rep.notes.append(f"renodx sürümü: {e['label']}")

    begin("nvngx_dlssnr.dll")
    e = sources.pick(catalog["dlssnr"], opt.dlssnr)
    f = dl(e["url"], f"dlssnr-{e['label']}.zip")
    net.extract_one(f, DLSSNR, dlss_dir / DLSSNR)
    rep.written.append(str((dlss_dir / DLSSNR).relative_to(root)))
    log(f"      nvngx_dlssnr {e['label']}")
    rep.notes.append(f"dlssnr sürümü: {e['label']}")

    # Bu dosyanin icinde kartimiz icin GERCEKTEN kod var mi? Sizdirilan
    # kutuphane surumlerinin bir kismi yalnizca RTX 50 icin derlenmis.
    card, sm = gpu.detect()
    compat, why = gpu.check(dlss_dir / DLSSNR, sm)
    if compat is True:
        log(f"      GPU denetimi: {card} -> {why}")
    elif compat is False:
        log(f"      GPU denetimi: {why}", )
        if not opt.ignore_gpu_mismatch:
            raise InstallError(
                f"{e['label']} sürümü {card} ile çalışmaz.\n\n{why}\n\n"
                f"Sürüm listesinden kartını destekleyen bir yapı seç "
                f"(RTX 40 için '310.8.0-RTX40' ya da 'SF' yapıları).")
        rep.warnings.append(f"dlssnr {e['label']} kartınla uyumsuz - yine de kuruldu")
    else:
        rep.warnings.append(f"dlssnr GPU uyumluluğu doğrulanamadı ({why})")

    begin("nvngx_dlss.dll")
    game_has = (root / DLSS).is_file() and str(Path(DLSS)) not in rep.written
    if x64 and game_has and opt.keep_game_dlss:
        log("      oyunun kendi nvngx_dlss.dll'i var, dokunulmadı")
        rep.skipped.append(DLSS)
    else:
        e = sources.pick(catalog["dlss"], opt.dlss)
        f = dl(e["url"], f"dlss-{e['label']}.zip")
        net.extract_one(f, DLSS, dlss_dir / DLSS)
        rep.written.append(str((dlss_dir / DLSS).relative_to(root)))
        log(f"      nvngx_dlss {e['label']}")
        rep.notes.append(f"dlss sürümü: {e['label']}")

    # --- 8) host64 ayarlari ----------------------------------------------
    if not x64:
        begin("host64 yardımcı süreç")
        reshade_ini.write_addon_only_ini(host)
        rep.written.append(f"{HOST_DIR}/ReShade.ini")
        log(f"      {HOST_DIR}/ hazır (ReShade + DLSS parçaları içinde)")

    # --- 9) ReShade ayarlari ---------------------------------------------
    begin("ReShade ayarları")
    reshade_ini.write_reshade_ini(root, opt.provider)
    reshade_ini.write_preset(root, opt.provider)
    rep.written += ["ReShade.ini", "ReShadePreset.ini"]
    label, tech, _ = reshade_ini.PROVIDERS[opt.provider]
    log(f"      DLSS5_MV_PROVIDER={opt.provider} ({label})")
    if tech:
        log(f"      teknik sırası: {tech} -> {reshade_ini.FEED_TECHNIQUE}")
    else:
        rep.notes.append("Seçtiğin sağlayıcının shader'ını kendin kurmalısın; "
                         "tekniğini ReShade'de DLSS 5 Feed'in ÜSTÜNE al.")

    # --- 10) dlss5-feed.cfg ----------------------------------------------
    # Eklenti bu dosyayi kendi de olusturur; onceden yazarsak ilk acilista
    # dogru ayarlarla baslar. 32-bit yolda cfg eklentinin yaninda durur.
    begin("dlss5-feed.cfg")
    feedcfg.write(root, opt.feed, host_window=None if x64 else True)
    rep.written.append(feedcfg.NAME)
    summary = feedcfg.describe(opt.feed) if opt.feed else []
    if summary:
        for s in summary:
            log(f"      {s}")
        rep.notes += summary
    else:
        log("      varsayılan ayarlar (work_resolution=100, preset=0)")

    # --- kayit ------------------------------------------------------------
    (root / MANIFEST).write_text(json.dumps({
        "surum": 1,
        "exe": g.exe.name,
        "mimari": g.bitness,
        "api": g.api,
        "proxy": proxy,
        "saglayici": opt.provider,
        "dosyalar": rep.written,
        "atlananlar": rep.skipped,
        "notlar": rep.notes,
        "uyarilar": rep.warnings,
        "feed_cfg": opt.feed,
    }, ensure_ascii=False, indent=2), encoding="utf8")

    prog(100, "Bitti")
    return rep


# ----------------------------------------------------------------- kaldirma

def uninstall(g: games.Game, on_log=None) -> list[str]:
    """Sadece bu aracin yazdigi dosyalari siler; oyunun kendi dosyalarina dokunmaz."""
    log = on_log or (lambda *_: None)
    root = g.install_dir
    man = root / MANIFEST
    removed: list[str] = []

    if man.is_file():
        try:
            data = json.loads(man.read_text(encoding="utf8"))
            files = data.get("dosyalar", [])
        except (OSError, json.JSONDecodeError):
            files = []
    else:
        # Kayit yoksa bilinen adlarla temizle
        files = [FEEDER_ADDON64, FEEDER_ADDON32, RENODX, DLSSNR, feedcfg.NAME,
                 "dxgi.dll", "opengl32.dll",
                 str(SHADERS / FEEDER_FX)]
        log("Kurulum kaydı yok; bilinen dosya adlarıyla temizleniyor.")

    for rel in files:
        p = root / rel
        try:
            if p.is_file():
                p.unlink()
                removed.append(rel)
                log(f"silindi: {rel}")
        except OSError as e:
            log(f"silinemedi: {rel} ({e})")

    # host64 klasoru tamamen bizim
    hostdir = root / HOST_DIR
    if hostdir.is_dir():
        shutil.rmtree(hostdir, ignore_errors=True)
        removed.append(HOST_DIR + "/")
        log(f"silindi: {HOST_DIR}/")

    # Preset'ten tekniklerimizi cikar, kullanicininkileri birak
    reshade_ini.remove_our_techniques(root)

    # Bos kalan shader klasorlerini topla
    for d in (root / INCLUDE, root / SHADERS, root / TEXTURES,
              root / "reshade-shaders"):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    man.unlink(missing_ok=True)
    log(f"Toplam {len(removed)} öğe kaldırıldı.")
    return removed
