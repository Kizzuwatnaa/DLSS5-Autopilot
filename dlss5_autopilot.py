r"""DLSS 5 Autopilot - entry point.

GUI:            dlss5-autopilot.exe
Command line:   dlss5-autopilot.exe "D:\Games\Game" [--check | --remove]
                                                    [--route native|upstream|optiscaler|renodx|bridge|feeder|standalone|remix]
                                                    [--dxvk | --no-dxvk] [--remix-swap] [--vr]
                                                    [--no-gpu-pref]
                                                    [--opti-build y4my4my4m|wilsjo2|wilsjo2-mfg|janblade]
                                                    [--own-fg "D:\Downloads\version.dll" | sm86 | enabler]
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

--no-gpu-pref leaves Windows' graphics setting alone. By default, on a
machine with an NVIDIA card and another GPU (a laptop), the game - and the
32-bit feeder's helper - are set to High performance so they draw on the
NVIDIA card; a choice already made there is kept, --remove takes it out.

--opti-build applies to --route optiscaler only and picks a fork other
than Dagherbou's: y4my4my4m (multi-pass, multi-frame generation),
wilsjo2 (the neural pass before the upscaler - installed with that
placement switched on, which is off in the fork's own default), or
wilsjo2-mfg (the same, from that fork's RTX 40 multi-frame generation
package; refused on a card known not to be an RTX 40), or janblade (a fork
of wilsjo2's, the same placement). None of them has been run here.

--own-fg applies to --route optiscaler on a D3D12 game only: frame
generation files you downloaded yourself - dlssg_for_sm86 (its version.dll,
with dlssg_sm86.ini beside it, or one of its alternatives\ proxies with the
ini copied beside it) or DLSS Enabler (its version.dll). Give the path of
the dll the first time; the tool keeps a copy, and after that its name is
enough: sm86, sm86-winmm, sm86-dxgi, sm86-d3d12, sm86-dbghelp or enabler.
Experimental, and out again on --remove.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import (community, dlss, games, gpu, installer,  # noqa: E402
                  optiscaler, prefs, reshade_ini, update)


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
        vr: bool = False, opti_build: str = "", gpu_pref: bool = True,
        own_fg: str = "") -> int:
    g = game or games.manual(target)
    if not g.exe:
        print(f"error: no executable found in {target}", file=sys.stderr)
        return 1
    need = installer.wants_dxvk(g)
    use_dxvk = bool(need) if dxvk is None else dxvk
    card, sm = gpu.detect()
    sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm,
                      driver=gpu.driver_version(), shared=community.cached(),
                      exe=g.exe)
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
        # An explicit --route is the answer; the reason and the outlook
        # belong to the recommendation it just overrode - printing either
        # would describe a route this run is not taking.
        sup.reason = ""
        level, why_rel = installer.reliability(g, sup.recommended)
    if card:
        # The same warning the install page shows, for the route that will
        # actually be installed. A command-line install on a driver that
        # cannot run any of this used to proceed in silence.
        warn = dlss.driver_warning(sup.recommended, gpu.driver_version(),
                                   offered=sup.options)
        if warn:
            print(f"driver  : {warn}")
        said = community.driver_note(community.cached(), gpu.driver_version() or "",
                                     sup.recommended)
        if said:
            print(f"shared  : {said}")
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
    if sup.reason:
        print(f"          {sup.reason}")
    print(f"outlook : {level} - {why_rel}")
    if use_dxvk:
        print(f"dxvk    : yes - {need + ' closes itself when ReShade hooks it; ' if need else ''}"
              f"the game will render on Vulkan and ReShade loads as a Vulkan "
              f"layer (--no-dxvk to turn this off)")

    if own_fg and sup.recommended != installer.OPTI:
        print("note    : --own-fg applies to --route optiscaler only; ignored here")
        own_fg = ""
    elif own_fg and g.api != "DX12":
        # the window shows the row on D3D12 games only: DLSS frame
        # generation is D3D12 (gate 2.0.7)
        print(f"note    : --own-fg applies to D3D12 games only ({g.api} here); ignored")
        own_fg = ""
    elif own_fg:
        from core import ownfg as _ofg
        print(f"fg files: {_ofg.label(own_fg)} - {', '.join(_ofg.dests(own_fg))} "
              f"({_ofg.UNTRIED})")

    ok, why = installer.check_supported(g)
    if check:
        # The RTX 40 MFG package on another card is refused by install()
        # before a file is written; --check said "ready" over it (gate
        # 2.0.5). The same check, the same words.
        refused = (optiscaler.card_refusal(opti_build, sm)
                   if ok and sup.recommended == installer.OPTI else "")
        if ok and not refused:
            # the card no route runs on (#770), refused by install() too
            from core import gpu as _gpu
            refused = _gpu.no_dlss_here() or ""
        if ok and not refused and own_fg:
            refused = installer._own_fg_refusal(g.install_dir, installer.Options(
                path=sup.recommended, own_fg=own_fg))
        print(f"installed: {'yes' if g.installed else 'no'}")
        print(f"status   : {refused or ('ready' if ok else why)}")
        # The one mode whose whole job is "look, write nothing" was the one
        # that never said the executable is a launcher: the warning reached
        # the command line only in install()'s report, after the files were
        # already written.
        launcher = installer.launcher_warning(g)
        if launcher:
            print(f"warning  : {launcher}")
        local, _ = prefs.find_renodx()
        print(f"renodx   : {local.name if local else 'will download from the mirror'}")
        if ok:
            popt = installer.Options(path=sup.recommended,
                                     native_dlss=sup.native_dlss, dxvk=use_dxvk, vr=vr, opti_build=opti_build,
                                     upscaler=sup.upscaler,
                                     remix_swap=remix_swap, gpu_pref=gpu_pref, own_fg=own_fg)
            print(f"plan     : {' -> '.join(installer.plan(g, popt))}")
            # ...and what the MFG package needs from the game, which only
            # the finished install used to say.
            for w in installer._mfg_warnings(g, popt, g.install_dir):
                print(f"warning  : {w}")
            if popt.opti_build == optiscaler.PRESR_MFG and popt.fg and g.api == "DX12":
                print(f"warning  : {installer.MFG_FSR_FG}")
            gp = installer._gpu_pref_line(g.install_dir, g, popt)
            if gp:
                print(f"outside  : {gp} (--no-gpu-pref to skip)")
        return 1 if refused else 0
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
                                 remix_swap=remix_swap, gpu_pref=gpu_pref, own_fg=own_fg),
            on_log=print,
            on_prog=lambda p, m: print(f"\r  {p:3d}%  {m:<60}", end="", flush=True))
    except installer.InstallError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    if installer.record_lost(rep):
        print(f"\n\nInstalled, but no record was saved - {len(rep.written)} files written.")
    else:
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
    # --remove as administrator with no Vulkan install left also takes off
    # the all-users Vulkan layer an earlier uninstall could not - the
    # clean-up the window does at start. Only on --remove: --check looks
    # and writes nothing.
    if "--remove" in args:
        try:
            from core import prefs, vulkan
            if vulkan.cleanup_machine(prefs.vulkan_games_or_none()):
                _console()
                print("removed: our ReShade Vulkan layer from the all-users "
                      "list (no Vulkan install left)")
        except Exception:
            pass
    route = ""
    if "--route" in args and args.index("--route") + 1 < len(args):
        route = args[args.index("--route") + 1]
        args.remove(route)
    opti_build = ""
    if "--opti-build" in args and args.index("--opti-build") + 1 < len(args):
        _console()      # the exe has no console of its own; errors below need one
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
    own_fg = ""
    if "--own-fg" in args:
        _console()
        if args.index("--own-fg") + 1 >= len(args):
            print("error: --own-fg takes the path of the .dll you downloaded, or a name "
                  "you used before", file=sys.stderr)
            return 2
        picked = args[args.index("--own-fg") + 1]
        args.remove(picked)
        # a set's name in any case (SM86); anything else is a path
        if picked.lower() in __import__("core.ownfg", fromlist=["RECIPES"]).RECIPES:
            picked = picked.lower()
        from core import ownfg as _ofg
        try:
            if picked in _ofg.RECIPES:
                if not _ofg.stored(picked):
                    raise _ofg.OwnFgError(
                        f"no copy of {_ofg.label(picked).split(' (')[0]} is stored yet - give the "
                        f"path of the .dll you downloaded")
                own_fg = picked
            else:
                own_fg, files = _ofg.identify([Path(picked)])
                _ofg.store(own_fg, files)
        except (_ofg.OwnFgError, OSError) as e:
            print(f"error: --own-fg: {e}", file=sys.stderr)
            return 2
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
                   own_fg=own_fg,
                   dxvk=(True if "--dxvk" in args
                         else False if "--no-dxvk" in args else None),
                   remix_swap="--remix-swap" in args,
                   gpu_pref="--no-gpu-pref" not in args)
    if "--help" in args or "-h" in args:
        _console()
        print(__doc__)
        return 0
    if "--version" in args:
        _console()
        print(update.VERSION)
        return 0
    from core.ui import app
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
