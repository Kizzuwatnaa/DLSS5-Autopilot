"""Windows PE (exe) incelemesi: bit genisligi, import tablosu, grafik API tespiti.

Hicbir harici bagimlilik yok - dosyayi elle parse ediyoruz ki ne okudugunu
satir satir dogrulayabilesin.
"""
from __future__ import annotations

import os
import re
import struct
from pathlib import Path

PE_X64 = 0x8664
PE_X86 = 0x014C

# Oyun exesi ararken atlanacak yardimci programlar.
_SKIP_PARTS = (
    "unins", "setup", "vcredist", "dxsetup", "dotnet", "prereq", "redist",
    "crashhandler", "crashreport", "crashpad", "easyanticheat", "battleye",
    "touchup", "installer", "activation", "cleanup", "helper", "webhelper",
    "unitycrashhandler", "ue4prereqsetup", "ue5prereqsetup", "epicwebhelper",
)


class PEError(Exception):
    pass


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise PEError(f"{path.name} okunamadi: {e}") from e


def _pe_offset(data: bytes) -> int:
    if data[:2] != b"MZ":
        raise PEError("Windows calistirilabilir dosyasi degil (MZ imzasi yok).")
    (off,) = struct.unpack_from("<I", data, 0x3C)
    if data[off:off + 4] != b"PE\0\0":
        raise PEError("PE basligi bulunamadi.")
    return off


def exe_bitness(path: Path) -> int:
    """PE COFF basligindaki Machine alanindan 32 veya 64 dondurur.

    Sadece gereken birkac bayti okur - yuzlerce oyun taranirken tum exeyi
    belege almak gereksiz yavaslik olurdu.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                raise PEError("Windows calistirilabilir dosyasi degil (MZ imzasi yok).")
            (off,) = struct.unpack_from("<I", head, 0x3C)
            f.seek(off)
            sig = f.read(6)
            if len(sig) < 6 or sig[:4] != b"PE\0\0":
                raise PEError("PE basligi bulunamadi.")
            (machine,) = struct.unpack_from("<H", sig, 4)
    except OSError as e:
        raise PEError(f"{path.name} okunamadi: {e}") from e
    if machine == PE_X64:
        return 64
    if machine == PE_X86:
        return 32
    raise PEError(f"Desteklenmeyen islemci tipi: 0x{machine:04x}")


def pe_imports(path: Path) -> list[str]:
    """Exenin statik import tablosundaki DLL adlari (kucuk harf).

    Parse edilemezse bos liste doner - bu bir hata degil, sadece 'bilinmiyor'.
    """
    try:
        data = _read(path)
        off = _pe_offset(data)
        n_sections = struct.unpack_from("<H", data, off + 6)[0]
        opt_size = struct.unpack_from("<H", data, off + 20)[0]
        opt = off + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        if magic == 0x20B:      # PE32+
            dd = 112
        elif magic == 0x10B:    # PE32
            dd = 96
        else:
            return []
        import_rva = struct.unpack_from("<I", data, opt + dd + 8)[0]
        if import_rva == 0:
            return []

        sec_off = opt + opt_size
        sections = []
        for i in range(n_sections):
            s = sec_off + i * 40
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, s + 8)
            sections.append((vaddr, max(vsize, rawsize), rawptr))

        def to_off(rva: int) -> int | None:
            for vaddr, size, rawptr in sections:
                if vaddr <= rva < vaddr + size:
                    return rawptr + (rva - vaddr)
            return None

        desc = to_off(import_rva)
        if desc is None:
            return []
        names: list[str] = []
        for _ in range(1024):
            if desc + 20 > len(data):
                break
            name_rva = struct.unpack_from("<I", data, desc + 12)[0]
            first_thunk = struct.unpack_from("<I", data, desc + 16)[0]
            if name_rva == 0 and first_thunk == 0:
                break
            n_off = to_off(name_rva)
            if n_off is not None:
                end = data.find(b"\0", n_off)
                if end > n_off:
                    names.append(data[n_off:end].decode("ascii", "ignore").lower())
            desc += 20
        return names
    except Exception:
        return []


# API etiketi -> ReShade'in kullanacagi proxy DLL adi
API_PROXY = {
    "DX11": "dxgi.dll",
    "DX12": "dxgi.dll",
    "OpenGL": "opengl32.dll",
    "Vulkan": None,   # global layer kurulumu gerekir, otomatiklestirmiyoruz
    "DX9": None,      # once dgVoodoo2 gerekir
}


def detect_api(path: Path) -> tuple[str, str]:
    """(api_etiketi, aciklama) dondurur.

    Sira onemli. Bircok Unreal oyunu DX12 ile calistigi halde opengl32.dll'i
    baska bir amacla statik baglar (ornek: Hell is Us, Fatekeeper - ikisi de
    hem dxgi.dll hem opengl32.dll import eder). Onceligi OpenGL'e verirsek
    yanlis proxy DLL secip oyunu bozardik; bu yuzden DXGI varligi belirleyici.

    DX11 ile DX12'yi karistirmak zararsizdir: ikisinde de ReShade ayni
    dxgi.dll proxy'si olarak kurulur.
    """
    imports = pe_imports(path)
    has = lambda d: any(d in i for i in imports)

    if has("d3d12.dll"):
        return "DX12", "d3d12.dll statik olarak import ediliyor"
    if has("d3d11.dll"):
        return "DX11", "d3d11.dll statik olarak import ediliyor"
    # DXGI var ama d3d11/d3d12 yok: API calisma aninda seciliyor, proxy yine dxgi.dll
    if has("dxgi.dll"):
        return "DX12", "dxgi.dll import ediliyor (DX11/DX12; proxy her iki durumda dxgi.dll)"
    # Buradan sonrasi: DXGI yok, yani gercekten DXGI disi bir API
    if has("vulkan-1.dll"):
        return "Vulkan", "vulkan-1.dll import ediliyor, DXGI yok"
    if has("opengl32.dll"):
        return "OpenGL", "opengl32.dll import ediliyor, DXGI yok"
    if has("d3d9.dll"):
        return "DX9", "d3d9.dll import ediliyor, DXGI yok"
    return "Bilinmiyor", "grafik DLL'i calisma aninda yukleniyor; DX11/DX12 varsayilip dxgi.dll kullanilir"


def looks_like_game(exe: Path) -> bool:
    low = exe.name.lower()
    return not any(p in low for p in _SKIP_PARTS)


# Icine hic girilmeyecek klasorler: dagitim paketleri, anti-cheat, motor araclari.
_PRUNE_DIRS = {
    "_commonredist", "commonredist", "redist", "redistributable", "redistributables",
    "directx", "dotnet", "vcredist", "vc_redist", "easyanticheat", "easyanticheat_eos",
    "battleye", "punkbuster", "installers", "installer", "prerequisites", "prereq",
    "support", "docs", "manual", "soundtrack", "artbook", "extras", "dxsetup",
    "crashreportclient", "epicwebhelper", "thirdparty", "steamvr", "openvr",
    "__installer", "dotnetfx", "movies", "content", "data", "assets", "textures",
}
_MAX_DEPTH = 5


def _walk_exes(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Klasoru derinlik sinirli gezip .exe toplar; ise yaramaz dallara hic girmez."""
    found: list[Path] = []
    base_depth = len(folder.parts)
    for root, dirs, files in os.walk(folder, topdown=True):
        rp = Path(root)
        depth = len(rp.parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.lower() not in _PRUNE_DIRS
                       and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".exe"):
                found.append(rp / f)
        if len(found) > 400:      # patolojik klasorlerde takilma
            break
    return found


def _score(exe: Path, folder: Path) -> float:
    """Buyuk puan = asil oyun exesi olma ihtimali yuksek."""
    rel = str(exe.relative_to(folder)).lower().replace("\\", "/")
    stem = exe.stem.lower()
    s = 0.0

    # Unreal'in "-Shipping" eki en guclu isaret
    if stem.endswith("-shipping") or "shipping" in stem:
        s += 1000
    # Bilinen ikili klasorleri
    if "/binaries/win64/" in rel or "/bin/win64/" in rel or rel.startswith("binaries/win64/"):
        s += 400
    elif "/binaries/win32/" in rel or "/bin/win32/" in rel:
        s += 300
    elif "/bin/" in rel or rel.startswith("bin/"):
        s += 200
    # Motorun kendi araclari asil oyun degildir
    if "/engine/binaries/" in rel or rel.startswith("engine/binaries/"):
        s -= 900
    # Exe adi klasor adina benziyorsa
    fn = re.sub(r"[^a-z0-9]", "", folder.name.lower())
    sn = re.sub(r"[^a-z0-9]", "", stem)
    if fn and sn and (sn in fn or fn in sn):
        s += 350
    # Kok dizindeki exe genelde iyi bir aday
    if exe.parent == folder:
        s += 120
    # Yardimci program adlari
    if not looks_like_game(exe):
        s -= 1500
    # Boyut (log olcekli, birkac yuz puana kadar)
    try:
        mb = exe.stat().st_size / (1024 * 1024)
        s += min(mb, 300) * 1.2
    except OSError:
        pass
    # Cok derin = muhtemelen yardimci
    s -= rel.count("/") * 15
    return s


def find_game_exes(folder: Path) -> list[Path]:
    """Klasordeki muhtemel oyun exelerini, en olasi olan basta olacak sekilde siralar."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    cands = _walk_exes(folder)
    if not cands:
        return []
    scored = sorted(cands, key=lambda p: _score(p, folder), reverse=True)
    # Puani cok dusuk olanlari (kesin yardimci program) listeden dusur,
    # ama hicbiri kalmazsa elimizdekini vermeye devam et.
    good = [p for p in scored if _score(p, folder) > -500]
    return good or scored


def resolve_target(target: Path) -> tuple[Path, list[Path]]:
    """Kullanicinin verdigi yol bir exe ya da klasor olabilir. (secilen, tum adaylar)."""
    target = Path(target)
    if target.is_file() and target.suffix.lower() == ".exe":
        return target, [target]
    if target.is_dir():
        cands = find_game_exes(target)
        if not cands:
            raise PEError(f"{target} icinde .exe bulunamadi.")
        return cands[0], cands
    raise PEError(f"{target} bulunamadi.")
