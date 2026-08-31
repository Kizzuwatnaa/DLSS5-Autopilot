r"""DLSS 5 Autopilot - entry point.

GUI:            dlss5-autopilot.exe
Command line:   dlss5-autopilot.exe "D:\Games\Game" [--check | --remove]
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import games, installer, prefs, update  # noqa: E402


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


def cli(target: Path, remove: bool, check: bool) -> int:
    g = games.manual(target)
    if not g.exe:
        print(f"error: no executable found in {target}", file=sys.stderr)
        return 1
    level, why_rel = installer.reliability(g)
    print(f"game    : {g.name}")
    print(f"exe     : {g.exe}")
    print(f"arch    : {g.bit_label}   API: {g.api} ({g.api_why})")
    print(f"outlook : {level} - {why_rel}")

    ok, why = installer.check_supported(g)
    if check:
        print(f"installed: {'yes' if g.installed else 'no'}")
        print(f"status   : {'ready' if ok else why}")
        local, _ = prefs.find_renodx()
        print(f"renodx   : {local.name if local else 'will download from the mirror'}")
        if ok:
            print(f"plan     : {' -> '.join(installer.plan(g, installer.Options()))}")
        return 0
    if not ok:
        print(f"error: {why}", file=sys.stderr)
        return 1

    if remove:
        installer.uninstall(g, on_log=print)
        return 0

    try:
        rep = installer.install(
            g, installer.Options(),
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
    positional = [a for a in args if not a.startswith("-")]
    if positional:
        _console()
        return cli(Path(positional[0]),
                   remove="--remove" in args,
                   check="--check" in args)
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
