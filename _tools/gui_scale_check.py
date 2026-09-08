r"""Does the window still work on somebody else's display?

The owner's monitor is 1920x1080 at 100%, so every display bug in this tool
has arrived as a bug report with a photograph attached (issue #40, twice).
This builds the REAL window at other scalings and measures what each part
actually got, instead of reasoning about it.

    python _tools\gui_scale_check.py              # the usual set
    python _tools\gui_scale_check.py 1.5          # one scaling
    python _tools\gui_scale_check.py --shot out.png 1.5

READ THIS BEFORE TRUSTING A NUMBER: a 4K screen at 300% has the same room
as a 1280x720 screen at 100% - the fonts are three times the pixels, and so
is the screen. So "4K at 300%" is emulated on a 1080p panel as **SCALE 1.5
in a 1920-wide window**, not SCALE 3.0 in a 1280-wide one. Getting that
backwards makes the test three times harsher than any real machine and
sends you fixing things that are not broken.

    what the user has          emulate here as
    ----------------------     ---------------------------
    1080p at 100%              SCALE 1.0, 1920x1061
    4K at 200% (=1920x1080)    SCALE 1.0, 1920x1061
    4K at 300% (=1280x720)     SCALE 1.5, 1920x1061
    1080p at 150% (=1280x720)  SCALE 1.5, 1920x1061

What it checks, per page:
  - nothing is squeezed to nothing (a widget mapped at 1 pixel that asked
    for more is a widget the person cannot see or press)
  - every action button is inside the window
  - the install page's log keeps at least three readable lines
  - the settings area scrolls when it does not fit, and does NOT show a
    scrollbar when it does
"""
from __future__ import annotations

import sys
import tkinter as tk
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import gui   # noqa: E402

# (scaling, window) pairs: see the table above for what each one stands for.
CASES = ((1.0, 1920, 1061), (1.25, 1920, 1061), (1.5, 1920, 1061),
         (2.0, 1400, 900))


def _pump(root: tk.Tk, n: int = 4) -> None:
    for _ in range(n):
        root.update_idletasks()
        root.update()


def check(scale: float, w: int, h: int, shot: str = "") -> list[str]:
    bad: list[str] = []
    errors: list[str] = []
    gui.SCALE = scale
    root = tk.Tk()
    root.report_callback_exception = lambda *a: errors.append(
        "".join(traceback.format_exception(*a)))
    root.tk.call("tk", "scaling", 96 * scale / 72.0)
    try:
        app = gui.App(root)
        for _ in range(3):
            root.state("normal")
            root.geometry(f"{w}x{h}+0+0")
            _pump(root, 2)
        win_h = root.winfo_height()
        print(f"\n--- scaling {scale:.0%} in {root.winfo_width()}x{win_h} "
              f"(a {int(1920 / scale)}x{int(1061 / scale)} workspace) ---")

        for n, name in ((1, "architecture"), (2, "game list"), (3, "install")):
            app.step = n
            app._show(n)
            _pump(root)
            page = (app.p1, app.p2, app.p3)[n - 1]
            squeezed = [f"{c.winfo_class()}" for c in page.winfo_children()
                        if c.winfo_ismapped() and c.winfo_height() <= 1
                        < c.winfo_reqheight()]
            if squeezed:
                bad.append(f"{scale:.0%} {name}: squeezed to nothing: "
                           f"{', '.join(squeezed)}")
            print(f"  {name:<13} got {page.winfo_height():>4} px"
                  + ("   OK" if not squeezed else "   !! " + ", ".join(squeezed)))

        # the install page in detail - it is the one that has to share
        app.step = 3
        app._show(3)
        _pump(root)
        rows = max(1, int(app.log.cget("height")))
        row_px = max(1.0, app.log.winfo_reqheight() / rows)
        visible = app.log.winfo_height() / row_px
        sc = app.installscroll
        fits = sc.content_height() <= sc.winfo_height() + 2
        barred = bool(sc._bar.winfo_ismapped())
        print(f"  log            {visible:.1f} lines")
        print(f"  settings       {sc.winfo_height()} px of {sc.content_height()} "
              f"needed, scrollbar {'yes' if barred else 'no'}")
        if visible < 3:
            bad.append(f"{scale:.0%}: the log is under three lines ({visible:.1f})")
        if not fits and not barred:
            bad.append(f"{scale:.0%}: the settings do not fit and cannot be scrolled")
        if fits and barred:
            bad.append(f"{scale:.0%}: a scrollbar on settings that already fit")
        for b in (app.btn_diag, app.btn_remove):
            bottom = b.winfo_rooty() + b.winfo_height() - root.winfo_rooty()
            if b.winfo_ismapped() and bottom > win_h:
                bad.append(f"{scale:.0%}: a button is {bottom - win_h} px below "
                           f"the bottom of the window")
        if shot:
            _shot(root, shot)
        if errors:
            bad.append(f"{scale:.0%}: {len(errors)} Tk error(s): "
                       f"{errors[0].strip().splitlines()[-1]}")
    finally:
        root.destroy()
        gui.SCALE = 1.0
    return bad


def _shot(root: tk.Tk, out: str) -> None:
    """A picture of the window, for a report or for the owner."""
    import subprocess
    root.after(300, root.quit)
    root.mainloop()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    subprocess.run(["powershell", "-NoProfile", "-Command", f'''
Add-Type -AssemblyName System.Drawing
$b = New-Object System.Drawing.Bitmap {w}, {h}
[System.Drawing.Graphics]::FromImage($b).CopyFromScreen({x}, {y}, 0, 0, $b.Size)
$b.Save("{out}")'''], check=False)
    print(f"  saved {out}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    shot = ""
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]
        args = [a for a in args if a != shot]
    cases = ([(float(args[0]), 1920, 1061)] if args else list(CASES))
    bad: list[str] = []
    for scale, w, h in cases:
        bad += check(scale, w, h, shot)
    print()
    print("=" * 70)
    if bad:
        print(f"{len(bad)} PROBLEM(S):")
        for b in bad:
            print("  -", b)
        return 1
    print("every page fits at every scaling checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
