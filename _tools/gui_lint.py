r"""Look at every widget on every page the way a person would, and complain.

    python _tools\gui_lint.py

gui_scale_check measures whether each page fits. This looks at what is ON
the page, widget by widget, at 100%, 150% and 200%, on every page and
every route of the install page, and reports:

  clipped    a label or button whose text is wider or taller than the room
             it was given - the text is cut off on screen
  offscreen  a widget whose right edge is past the window's
  colour     a widget painted in a colour that is not in the palette - a
             default grey or white left behind by Tk or ttk, the kind of
             thing that made the scrollbars look broken
  font       a widget whose font family is not the window's own - the
             dropdowns were in Segoe UI inside a monospaced window
  overlap    two mapped widgets drawn on top of each other in one parent

It found nothing is not the goal; it found nothing AND the screenshots look
right is. Run it after any GUI change, before gui_scale_check.
"""
from __future__ import annotations

import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core import dlss, games, gui, prefs   # noqa: E402

prefs.FILE = Path(tempfile.mkdtemp(prefix="lint_prefs_")) / "settings.json"
# A window that finds a saved library from another version rescans the
# disks at start; the one on the machine running this is not the test's.
from core import library as _library_iso  # noqa: E402
_library_iso.FILE = Path(tempfile.mkdtemp(prefix="lib_iso_")) / "library.json"

PALETTE = {c.lower() for c in (
    gui.BG, gui.PANEL, gui.FIELD, gui.LINE, gui.TXT, gui.BODY, gui.DIM,
    gui.FAINT, gui.AMBER, gui.SLIDER_TROUGH, gui.SLIDER_HOT,
    getattr(gui, "EDGE", ""), getattr(gui, "RUST", ""),
    getattr(gui, "GREEN", ""), getattr(gui, "RED", ""),
    getattr(gui, "RAIL", ""), getattr(gui, "HOVER", ""), "#e8bd7a") if c}
FAMILIES = {f.lower() for f in gui.MONO}
ISSUES: list[str] = []


def hexcol(w: tk.Misc, c: str) -> str:
    try:
        r, g, b = (v // 257 for v in w.winfo_rgb(c))
        return f"#{r:02x}{g:02x}{b:02x}"
    except tk.TclError:
        return c.lower()


def label(w: tk.Misc) -> str:
    try:
        t = w.cget("text")
    except tk.TclError:
        t = ""
    name = w.winfo_class()
    return f"{name}({str(t)[:28]!r})" if t else f"{name} {str(w)[-40:]}"


def walk(w: tk.Misc):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


def check(root: tk.Tk, where: str) -> int:
    root.update()
    found = 0
    rw = root.winfo_width()
    for w in walk(root):
        try:
            if not w.winfo_ismapped() or w.winfo_width() <= 1:
                continue
        except tk.TclError:
            continue
        cls = w.winfo_class()
        # clipped text: a label or button asks for more than it got
        if cls in ("Label", "Button", "TButton", "Checkbutton", "TCheckbutton",
                   "Radiobutton", "TRadiobutton"):
            try:
                wrap = int(str(w.cget("wraplength") or 0)) if cls == "Label" else 0
            except (tk.TclError, ValueError):
                wrap = 0
            if not wrap and (w.winfo_reqwidth() > w.winfo_width() + 2
                             or w.winfo_reqheight() > w.winfo_height() + 2):
                ISSUES.append(f"{where}: clipped   {label(w)}  "
                              f"asks {w.winfo_reqwidth()}x{w.winfo_reqheight()}, "
                              f"got {w.winfo_width()}x{w.winfo_height()}")
                found += 1
        # off the right edge of the window
        try:
            right = w.winfo_rootx() + w.winfo_width() - root.winfo_rootx()
            if right > rw + 2 and w.master is not None \
                    and w.master.winfo_class() != "Canvas":
                ISSUES.append(f"{where}: offscreen {label(w)} right edge "
                              f"{right} > window {rw}")
                found += 1
        except tk.TclError:
            pass
        # colours that are not ours - only the ones that can be SEEN. A
        # highlight colour with no highlight thickness, or an "active"
        # colour on a widget that is never active, is Tk's default sitting
        # unused, not a grey patch on the screen.
        has_text = False
        try:
            has_text = bool(str(w.cget("text")).strip())
        except tk.TclError:
            pass
        typed = cls in ("Entry", "Text", "Spinbox", "Listbox")
        opts = ["bg"]
        if has_text or typed:
            opts.append("fg")
        try:
            if int(str(w.cget("highlightthickness") or 0)) > 0:
                opts.append("highlightbackground")
        except (tk.TclError, ValueError):
            pass
        if cls in ("Button", "Checkbutton", "Radiobutton", "Scale", "Spinbox"):
            opts += ["activebackground"]
        if cls in ("Checkbutton", "Radiobutton"):
            opts.append("selectcolor")
        if cls in ("Scale",):
            opts.append("troughcolor")
        if cls == "Spinbox":
            opts.append("buttonbackground")
        if typed:
            opts.append("insertbackground")
        # A disabled or read-only field does not paint with bg/fg at all:
        # it has its own pair, and the fps box went white through exactly
        # that gap while bg and fg both looked right.
        try:
            state = str(w.cget("state"))
        except tk.TclError:
            state = ""
        if state == "disabled" and cls in ("Entry", "Spinbox"):
            opts = [o for o in opts if o not in ("bg", "fg")]
            opts += ["disabledbackground", "disabledforeground"]
        elif state == "readonly" and cls in ("Entry", "Spinbox"):
            opts = [o for o in opts if o != "bg"] + ["readonlybackground"]
        elif state == "disabled" and cls in ("Label", "Button", "Checkbutton",
                                             "Radiobutton"):
            opts = [o for o in opts if o != "fg"] + ["disabledforeground"]
        if cls == "Label":
            try:
                if w.cget("image") and not has_text:
                    opts = []          # an image label shows only its image
            except tk.TclError:
                pass
        for opt in opts:
            try:
                v = w.cget(opt)
            except tk.TclError:
                continue
            if not v:
                continue
            h = hexcol(w, str(v))
            if str(v).lower().startswith("system") or h not in PALETTE:
                ISSUES.append(f"{where}: colour    {label(w)} {opt}={v} "
                              f"({h}) is not in the palette")
                found += 1
        # one font family - for anything that draws text
        if has_text or typed:
            try:
                f = w.cget("font")
            except tk.TclError:
                f = None
            if f:
                try:
                    fam = tk.font.Font(root=root, font=f).actual("family").lower()
                except Exception:
                    fam = ""
                if fam and fam not in FAMILIES:
                    ISSUES.append(f"{where}: font      {label(w)} is {fam!r}")
                    found += 1
    # overlaps between siblings laid out by grid or place
    for w in walk(root):
        kids = []
        for c in w.winfo_children():
            try:
                if c.winfo_ismapped() and c.winfo_width() > 2 \
                        and c.winfo_manager() in ("grid", "place"):
                    kids.append((c, c.winfo_x(), c.winfo_y(),
                                 c.winfo_width(), c.winfo_height()))
            except tk.TclError:
                pass
        def _inside(x, y) -> bool:
            """Is x gridded INTO sibling y (grid's in_)? Then it sits on y
            by design - the games page's filter lines are laid out that way."""
            try:
                return x.winfo_manager() == "grid" and \
                    str(x.grid_info().get("in")) == str(y)
            except tk.TclError:
                return False

        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                a, ax, ay, aw, ah = kids[i]
                b, bx, by, bw, bh = kids[j]
                if _inside(a, b) or _inside(b, a):
                    continue
                if ax < bx + bw - 3 and bx < ax + aw - 3 \
                        and ay < by + bh - 3 and by < ay + ah - 3:
                    ISSUES.append(f"{where}: overlap   {label(a)} and {label(b)}")
                    found += 1
    return found


def run(scale: float, size: str) -> None:
    import tkinter.font  # noqa: F401
    gui.SCALE = scale
    root = tk.Tk()
    root.geometry(f"{size}+30+30")
    app = gui.App(root)
    root.update()

    d = Path(tempfile.mkdtemp(prefix="lint_game_"))
    (d / "Game.exe").write_bytes(b"MZ" + b"\0" * 200)
    (d / "nvngx_dlss.dll").write_bytes(b"MZ")
    (d / "nvngx_dlssd.dll").write_bytes(b"MZ")
    g = games.Game(name="A Game With A Fairly Long Name: Definitive Edition",
                   folder=d, exe=d / "Game.exe", bitness=64, api="DX12",
                   source="Steam")
    app.all_games = [g]
    app._fill()
    tag = f"{int(scale * 100)}% {size}"
    app._show(1); check(root, f"{tag} start")
    app._show(4); check(root, f"{tag} video")
    app._show(5); check(root, f"{tag} rtx remix")
    app._show(2); check(root, f"{tag} library")
    try:
        app.tree.selection_set(app.tree.get_children()[0])
        root.update()
        check(root, f"{tag} library+selected")
    except Exception:
        pass
    app.game = g
    app._show(3); app._enter_install()
    for route in (dlss.FEEDER, dlss.OPTI, dlss.NATIVE, dlss.BRIDGE,
                  dlss.UPSTREAM, dlss.RENODX, dlss.STANDALONE):
        try:
            app._apply_route(route)
        except Exception as e:
            ISSUES.append(f"{tag} install/{route}: route raised {e!r}")
            continue
        check(root, f"{tag} install/{route}")
    try:
        for job in root.tk.call("after", "info"):
            root.after_cancel(job)
    except tk.TclError:
        pass
    root.destroy()


def main() -> int:
    for scale, size in ((1.0, "1280x800"), (1.0, "1920x1080"),
                        (1.5, "1920x1080"), (2.0, "2560x1440")):
        run(scale, size)
    # the same widget reported on every route is one problem, not seven
    seen, unique = set(), []
    for line in ISSUES:
        key = line.split(": ", 1)[1] if ": " in line else line
        if key not in seen:
            seen.add(key)
            unique.append(line)
    if not unique:
        print("nothing clipped, off the window, off the palette, in another "
              "font, or overlapping - on any page, route or scale checked")
        return 0
    print(f"{len(unique)} distinct finding(s) ({len(ISSUES)} in all):\n")
    for line in unique:
        print("  " + line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
