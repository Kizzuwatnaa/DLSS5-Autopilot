"""DLSS 5 Kurulum Araci - giris noktasi.

Arayuz icin:   dlss5kur.exe
Komut satiri:  dlss5kur.exe "D:\\Oyunlar\\Oyun" [--kontrol | --kaldir]
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import games, installer, prefs, sources  # noqa: E402


def _console() -> None:
    """Exe penceresiz derlendigi icin, terminalden calistirilinca cikti
    gorunsun diye cagiran konsola tutunuruz."""
    try:
        import ctypes
        ATTACH_PARENT = -1
        if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                              buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                              buffering=1)
            return
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def cli(target: Path, remove: bool, check: bool) -> int:
    g = games.manual(target)
    if not g.exe:
        print(f"hata: {target} icinde calistirilabilir bulunamadi", file=sys.stderr)
        return 1
    print(f"oyun   : {g.name}")
    print(f"exe    : {g.exe}")
    print(f"mimari : {g.bit_label}   API: {g.api} ({g.api_why})")

    ok, why = installer.check_supported(g)
    if check:
        print(f"kurulu : {'evet' if g.installed else 'hayir'}")
        print(f"durum  : {'kurulabilir' if ok else why}")
        local, _ = prefs.find_renodx()
        print(f"renodx : {local.name if local else 'aynadan indirilecek'}")
        if ok:
            print(f"plan   : {' -> '.join(installer.plan(g, installer.Options()))}")
        return 0
    if not ok:
        print(f"hata: {why}", file=sys.stderr)
        return 1

    if remove:
        installer.uninstall(g, on_log=print)
        return 0

    local, _ = prefs.find_renodx()
    if local:
        print(f"renodx  : yerel dosya kullanılıyor -> {local.name}")
    try:
        rep = installer.install(
            g, installer.Options(renodx_local=local),
            on_log=print,
            on_prog=lambda p, m: print(f"\r  {p:3d}%  {m:<60}", end="", flush=True))
    except installer.InstallError as e:
        print(f"\nhata: {e}", file=sys.stderr)
        return 1
    print(f"\n\nBitti - {len(rep.written)} dosya yazildi.")
    print("Oyunda: Home -> DLSS 5 Neural Rendering panelinden ac. "
          "Oyunun MSAA/SSAA ayarini kapat.")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:]]
    positional = [a for a in args if not a.startswith("-")]
    if positional:
        _console()
        return cli(Path(positional[0]),
                   remove="--kaldir" in args or "--remove" in args,
                   check="--kontrol" in args or "--check" in args)
    if "--yardim" in args or "--help" in args or "-h" in args:
        _console()
        print(__doc__)
        return 0
    from core import gui
    return gui.run()


if __name__ == "__main__":
    sys.exit(main())
