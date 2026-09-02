r"""DLSS 5 Autopilot - entry point.

GUI:            dlss5-autopilot.exe
Command line:   dlss5-autopilot.exe "D:\Games\Game" [--check | --remove]
                                                    [--route native|upstream|optiscaler|renodx|bridge|feeder]
                                                    [--dxvk | --no-dxvk]
                dlss5-autopilot.exe --video ["D:\DLSS5 Player"]  the video player

--dxvk runs a D3D11 game on Vulkan through DXVK, with ReShade as a Vulkan
layer instead of a DLL inside the game. Games known to need it (MGS V) get
it by default; --no-dxvk turns that off.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import dlss, games, gpu, installer, prefs, update  # noqa: E402


def _console() -> None:
    """The exe is built without a console window, so attach to the calling
    terminal when run from one - otherwise CLI output goes nowhere."""
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


def cli(target: Path, remove: bool, check: bool, route: str = "",
        dxvk: bool | None = None, game=None) -> int:
    g = game or games.manual(target)
    if not g.exe:
        print(f"error: no executable found in {target}", file=sys.stderr)
        return 1
    need = installer.wants_dxvk(g)
    use_dxvk = bool(need) if dxvk is None else dxvk
    card, sm = gpu.detect()
    sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm)
    level, why_rel = installer.reliability(g, sup.recommended)
    print(f"game    : {g.name}")
    if card:
        drv = gpu.driver_version()
        print(f"gpu     : {card} ({gpu.label(sm)})" + (f"  driver {drv}" if drv else ""))
    print(f"exe     : {g.exe}")
    print(f"arch    : {g.bit_label}   API: {g.api} ({g.api_why})")
    if route:
        if route not in sup.options:
            print(f"error: route '{route}' is not available for this game "
                  f"(options: {', '.join(sup.options)})", file=sys.stderr)
            return 1
        sup.recommended = route
    print(f"route   : {dlss.LABELS[sup.recommended]}")
    for o in sup.options:
        usable, note = dlss.fit(o, g.api, sup.native_dlss, sm,
                                upscaler=sup.upscaler)
        print(f"          {'*' if o == sup.recommended else ' '} {o:<11}"
              f"{'' if usable else 'NOT FOR THIS PC - '}{note}")
    if sup.native_dlss:
        print(f"          this game ships its own DLSS "
              f"({', '.join(sup.evidence[:3])})")
    elif sup.upscaler:
        print(f"          this game ships {dlss.UPSCALER_NAMES[sup.upscaler]} "
              f"and no DLSS ({', '.join(sup.upscaler_evidence[:3])})")
    print(f"          {sup.reason}")
    print(f"outlook : {level} - {why_rel}")
    if use_dxvk:
        print(f"dxvk    : yes - {need + ' closes itself when ReShade hooks it; ' if need else ''}"
              f"the game will render on Vulkan and ReShade loads as a Vulkan "
              f"layer (--no-dxvk to turn this off)")

    ok, why = installer.check_supported(g)
    if check:
        print(f"installed: {'yes' if g.installed else 'no'}")
        print(f"status   : {'ready' if ok else why}")
        local, _ = prefs.find_renodx()
        print(f"renodx   : {local.name if local else 'will download from the mirror'}")
        if ok:
            popt = installer.Options(path=sup.recommended,
                                     native_dlss=sup.native_dlss, dxvk=use_dxvk,
                                     upscaler=sup.upscaler)
            print(f"plan     : {' -> '.join(installer.plan(g, popt))}")
        return 0
    if not ok:
        print(f"error: {why}", file=sys.stderr)
        return 1

    if remove:
        installer.uninstall(g, on_log=print)
        return 0

    try:
        rep = installer.install(
            g, installer.Options(path=sup.recommended, native_dlss=sup.native_dlss,
                                 dxvk=use_dxvk, upscaler=sup.upscaler),
            on_log=print,
            on_prog=lambda p, m: print(f"\r  {p:3d}%  {m:<60}", end="", flush=True))
    except installer.InstallError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    print(f"\n\nDone - {len(rep.written)} files written.")
    for w in rep.warnings:
        print(f"  ! {w}")
    print("In game: press Home, then enable neural rendering in the DLSS 5 panel. "
          "Turn the game's own MSAA/SSAA off.")
    return 0


def main() -> int:
    args = list(sys.argv[1:])
    route = ""
    if "--route" in args and args.index("--route") + 1 < len(args):
        route = args[args.index("--route") + 1]
        args.remove(route)
    positional = [a for a in args if not a.startswith("-")]
    if "--video" in args:
        _console()
        from core import video
        folder = Path(positional[0]) if positional else video.default_dir()
        print(f"video player -> {folder}")
        try:
            g = video.prepare(folder, on_log=print,
                              on_prog=lambda p, m: print(f"\r  {p:3d}%  {m:<60}",
                                                         end="", flush=True))
        except Exception as e:
            print(f"\nerror: {e}", file=sys.stderr)
            return 1
        print()
        return cli(g.exe, remove="--remove" in args, check="--check" in args,
                   route=route or "feeder", dxvk=False, game=g)
    if positional:
        _console()
        return cli(Path(positional[0]),
                   remove="--remove" in args,
                   check="--check" in args,
                   route=route,
                   dxvk=(True if "--dxvk" in args
                         else False if "--no-dxvk" in args else None))
    if "--help" in args or "-h" in args:
        _console()
        print(__doc__)
        return 0
    if "--version" in args:
        _console()
        print(update.VERSION)
        return 0
    from core import gui
    return gui.run()


if __name__ == "__main__":
    sys.exit(main())
