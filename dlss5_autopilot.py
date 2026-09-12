r"""DLSS 5 Autopilot - entry point.

GUI:            dlss5-autopilot.exe
Command line:   dlss5-autopilot.exe "D:\Games\Game" [--check | --remove]
                                                    [--route native|upstream|optiscaler|renodx|bridge|feeder|standalone|remix]
                                                    [--dxvk | --no-dxvk] [--remix-swap] [--vr]
                                                    [--opti-build y4my4my4m|wilsjo2]
                dlss5-autopilot.exe --video ["D:\DLSS5 Player"]  the video player

--dxvk runs a D3D11 game on Vulkan through DXVK, with ReShade as a Vulkan
layer instead of a DLL inside the game. Games known to need it (MGS V) get
it by default; --no-dxvk turns that off.

--remix-swap applies to --route remix only: when the game's RTX Remix mod
ships a runtime with no DLSS 5 neural pass, replace it with a community
build that has one. Experimental - it also replaces whatever game-specific
fixes the mod's own runtime carried.

--vr registers ReShade's OpenXR layer as well, for a game that draws
through OpenXR. The desktop window is only a mirror, so a proxy DLL on its
own changes nothing in the headset; OpenVR/SteamVR titles are not reached
either way. The registration is per user rather than per game, and the
last VR uninstall removes it. Untried with a headset here.

--opti-build applies to --route optiscaler only and picks a fork other
than Dagherbou's: y4my4my4m (multi-pass, multi-frame generation) or
wilsjo2 (the neural pass before the upscaler - installed with that
placement switched on, which is off in the fork's own default). Neither
has been run here.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import (dlss, games, gpu, installer, optiscaler,  # noqa: E402
                  prefs, reshade_ini, update)


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
        dxvk: bool | None = None, game=None, remix_swap: bool = False,
        vr: bool = False, opti_build: str = "") -> int:
    g = game or games.manual(target)
    if not g.exe:
        print(f"error: no executable found in {target}", file=sys.stderr)
        return 1
    need = installer.wants_dxvk(g)
    use_dxvk = bool(need) if dxvk is None else dxvk
    card, sm = gpu.detect()
    sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm,
                      driver=gpu.driver_version())
    level, why_rel = installer.reliability(g, sup.recommended)
    print(f"game    : {g.name}")
    if card:
        drv = gpu.driver_version()
        print(f"gpu     : {card} ({gpu.label(sm)})" + (f"  driver {drv}" if drv else ""))
        # ...the warning itself is printed after --route has had its say,
        # a few lines below: it depends on which route is actually used.
    else:
        vendor = gpu.other_vendor()
        if vendor == "AMD":
            print("gpu     : no NVIDIA card detected")
            for line in gpu.AMD_ANSWER.splitlines():
                print("          " + line.strip())
        else:
            print("gpu     : no NVIDIA card detected - dlss5 will not run")
    print(f"exe     : {g.exe}")
    print(f"arch    : {g.bit_label}   API: {g.api} ({g.api_why})")
    if route:
        if route not in sup.options:
            print(f"error: route '{route}' is not available for this game "
                  f"(options: {', '.join(sup.options)})", file=sys.stderr)
            return 1
        sup.recommended = route
    if card:
        # The same warning the install page shows, for the route that will
        # actually be installed. A command-line install on a driver that
        # cannot run any of this used to proceed in silence.
        warn = dlss.driver_warning(sup.recommended, gpu.driver_version(),
                                   offered=sup.options)
        if warn:
            print(f"driver  : {warn}")
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
                                     native_dlss=sup.native_dlss, dxvk=use_dxvk, vr=vr, opti_build=opti_build,
                                     upscaler=sup.upscaler,
                                     remix_swap=remix_swap)
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
                                 dxvk=use_dxvk, vr=vr, opti_build=opti_build, upscaler=sup.upscaler,
                                 remix_swap=remix_swap),
            on_log=print,
            on_prog=lambda p, m: print(f"\r  {p:3d}%  {m:<60}", end="", flush=True))
    except installer.InstallError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    print(f"\n\nDone - {len(rep.written)} files written.")
    for w in rep.warnings:
        print(f"  ! {w}")
    # The remix route has no ReShade overlay at all - its settings live in
    # the Remix menu, so the usual advice would send people to a key that
    # does nothing there.
    if sup.recommended == dlss.REMIX or route == dlss.REMIX:
        print("In game: press Alt+X, then 'Developer Settings Menu', then the "
              "Post-Processing tab: 'Enable Neural Uplift (DLSS-NR)'.")
    elif sup.recommended == dlss.FEEDER and g.bitness == 32:
        # See the matching note in gui.py: the DLSS 5 page is in the game's
        # own overlay and drives the 64-bit helper; the helper's separate
        # window must NOT be alt-tabbed to while playing.
        print(f"In game: press {reshade_ini.overlay_key_name()}, then turn on "
              f"neural rendering in the "
              "DLSS 5 page (F6 toggles it) - it drives the 64-bit helper for "
              "you. Play BORDERLESS or true fullscreen at your display's own "
              "resolution: in a bordered window the game presents a few "
              "pixels short and the result never lands on screen. Do NOT "
              "alt-tab to the helper's window while playing. Turn the game's "
              "own MSAA/SSAA off.")
    else:
        # The route's own key, not ReShade's: OptiScaler opens on Insert.
        _dflt = (optiscaler.OVERLAY_KEY if sup.recommended == dlss.OPTI
                 else "Home")
        print(f"In game: press {reshade_ini.overlay_key_name(_dflt)}, then "
              f"enable neural rendering in the DLSS 5 panel. Turn the game's "
              f"own MSAA/SSAA off.")
    return 0


def main() -> int:
    args = list(sys.argv[1:])
    route = ""
    if "--route" in args and args.index("--route") + 1 < len(args):
        route = args[args.index("--route") + 1]
        args.remove(route)
    opti_build = ""
    if "--opti-build" in args and args.index("--opti-build") + 1 < len(args):
        opti_build = args[args.index("--opti-build") + 1]
        args.remove(opti_build)
        from core import optiscaler as _opti
        if opti_build not in _opti.BUILDS:
            print(f"error: --opti-build takes one of: "
                  f"{', '.join(k for k in _opti.BUILDS if k)}", file=sys.stderr)
            return 2
        if route and route != "optiscaler":
            print("note: --opti-build applies to --route optiscaler only; "
                  "ignored here", file=sys.stderr)
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
                   vr="--vr" in args,
                   opti_build=opti_build,
                   dxvk=(True if "--dxvk" in args
                         else False if "--no-dxvk" in args else None),
                   remix_swap="--remix-swap" in args)
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
