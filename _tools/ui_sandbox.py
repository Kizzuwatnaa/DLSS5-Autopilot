r"""A sandbox for the tools that open the real window: temporary files for
everything it writes, the network switched off, and real-event helpers.

    from ui_sandbox import Sandbox
    sb = Sandbox()                   # before core.ui is imported by anyone
    app = sb.app(scale=1.0, size=(1400, 900))
    sb.click(app.shell.content, tag)

Used by walkthrough.py, gui_lint.py and gui_scale_check.py, so the three of
them isolate the same way: the owner's settings, library, sightings, log and
download cache are never read for writing, and nothing reaches the network or
opens a browser. A check that wrote into the real settings once is a check
nobody may run again on the machine it is meant for.

Clicks, keys and the wheel are sent with event_generate - Tk runs the class
and tag bindings the way it does for a person. Calling a handler by hand
proves the handler, not the behaviour (see the memory note on real events).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import tkinter as tk
import urllib.request
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _no_network(*_a, **_k):
    raise OSError("the network is switched off in this check")


class Sandbox:
    def __init__(self, prefix: str = "ui_check_"):
        self.dir = Path(tempfile.mkdtemp(prefix=prefix))
        self.opened: list[str] = []            # what webbrowser.open was asked for
        self.errors: list[str] = []            # Tk callback exceptions
        self._patches: list = []
        from core import library, log, net, prefs, profiles, sources, watch
        prefs.FILE = self.dir / "settings.json"
        library.FILE = self.dir / "library.json"
        watch.RECORD = self.dir / "sightings.json"
        log.DIR = self.dir
        log.FILE = self.dir / "autopilot.log"
        net.CACHE = self.dir / "cache"
        profiles.DIR = self.dir / "profiles"
        # covers found online and pictures chosen by hand (covers.ROOT is
        # read from prefs.FILE when the module loads, so it is set too)
        from core import covers
        covers.ROOT = self.dir / "art"
        # answered: no lookups, and no first-run question over the page under test
        prefs.set_("online_art", False)
        if hasattr(sources, "_API_CACHE"):
            sources._API_CACHE = self.dir / "api-cache"
        self._start(patch.object(urllib.request, "urlopen", _no_network))
        self._start(patch("webbrowser.open", side_effect=lambda url, *a, **k: self.opened.append(str(url))))
        self._start(patch("subprocess.Popen", side_effect=OSError("no processes from a UI check")))
        from core import community
        self._start(patch.object(community, "fetch", side_effect=OSError("offline")))
        from core.ui import app as _app
        from core.ui.ctl_game import GameControl
        from core.ui.ctl_library import LibraryControl
        from core.lookout import Lookout
        # the jobs that only fetch: no thread, no request
        self._start(patch.object(_app.App, "check_update", lambda self: None))
        self._start(patch.object(LibraryControl, "load_board", lambda self: None))
        self._start(patch.object(LibraryControl, "load_shared", lambda self: None))
        self._start(patch.object(GameControl, "load_catalog", lambda self: None))
        # the watcher never polls the machine's processes from a check
        self._start(patch.object(Lookout, "start", lambda self: None))
        # the dlss read that starts on its own 5 s after opening: a check
        # starts it when it means to, or it lands in the middle of another state
        from core.ui.ctl_dlss import DlssControl
        self._start(patch.object(DlssControl, "dlss_background", lambda self: None))

    def _start(self, p):
        p.start()
        self._patches.append(p)

    def close(self) -> None:
        while self._patches:
            try:
                self._patches.pop().stop()
            except RuntimeError:
                pass

    # ------------------------------------------------------------ games on disk
    def game(self, name: str, api: str = "DX11", bits: int = 64, installed: bool = False,
             route: str = "feeder", extra: tuple = (), source: str = "Manual"):
        """A folder that looks like a game: an MZ executable, optional files,
        and our install record when `installed`."""
        import json
        from core import games
        d = self.dir / "games" / name.replace(":", "")
        d.mkdir(parents=True, exist_ok=True)
        (d / "Game.exe").write_bytes(b"MZ" + b"\0" * 200)
        for n in extra:
            (d / n).parent.mkdir(parents=True, exist_ok=True)
            (d / n).write_bytes(b"MZ")
        if installed:
            files = ["dxgi.dll", "dlss5-feed.addon64", "ReShade.ini"]
            for n in files:
                (d / n).write_bytes(b"MZ")
            (d / "dlss5-autopilot.json").write_text(json.dumps(
                {"version": 1, "complete": True, "exe": "Game.exe", "bitness": bits, "api": api,
                 "proxy": "dxgi.dll", "path": route, "files": files}), encoding="utf8")
        return games.Game(name=name, folder=d, exe=d / "Game.exe", bitness=bits, api=api, source=source)

    # ------------------------------------------------------------ the window
    def app(self, scale: float = 1.0, size=(1400, 900), visible: bool = True, screen=None):
        """The real App. `scale` is theme.SCALE; `screen` (w, h) makes the
        window believe it is on a screen of that size."""
        from core.ui import theme as T
        from core.ui.app import App
        T.set_scale(96 * scale)
        T._measure.clear()
        root = tk.Tk()
        root.tk.call("tk", "scaling", 96 * scale / 72.0)
        root.report_callback_exception = lambda *a: self.errors.append(
            "".join(__import__("traceback").format_exception(*a)))
        if screen is not None:
            root.winfo_screenwidth = lambda: screen[0]
            root.winfo_screenheight = lambda: screen[1]
        if not visible:
            try:
                root.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
        self.asked = {"geometry": [], "minsize": []}
        geo, mins = root.geometry, root.minsize

        def geometry(spec=None):
            if spec is not None:
                self.asked["geometry"].append(spec)
            return geo(spec)

        def minsize(w=None, h=None):
            if w is not None:
                self.asked["minsize"].append((w, h))
            return mins(w, h)
        root.geometry, root.minsize = geometry, minsize
        app = App(root)
        root.state("normal")
        root.geometry(f"{size[0]}x{size[1]}+20+20")
        self.pump(root, 0.6)
        return app

    @staticmethod
    def destroy(root) -> None:
        try:
            for job in root.tk.call("after", "info"):
                root.after_cancel(job)
        except tk.TclError:
            pass
        try:
            root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ time
    @staticmethod
    def pump(root, seconds: float = 0.3) -> None:
        end = time.monotonic() + seconds
        while True:
            root.update()
            if time.monotonic() >= end:
                return
            time.sleep(0.01)

    @staticmethod
    def until(root, cond, seconds: float = 15.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            root.update()
            if cond():
                return True
            time.sleep(0.02)
        return bool(cond())

    # ------------------------------------------------------------ real events
    @staticmethod
    def visible_xy(canvas, tag):
        """The middle of `tag` in widget coordinates, or None when not drawn."""
        if tag is None:
            return None
        box = canvas.bbox(tag)
        if not box:
            return None
        x1, y1, x2, y2 = box
        return int((x1 + x2) / 2 - canvas.canvasx(0)), int((y1 + y2) / 2 - canvas.canvasy(0))

    def reveal(self, shell, tag) -> None:
        """Scroll the page so `tag` is on screen, the way a person scrolls to it."""
        c = shell.content
        if tag is None:
            return
        box = c.bbox(tag)
        if not box:
            return
        top, bottom = c.canvasy(0), c.canvasy(c.winfo_height())
        if box[1] < top or box[3] > bottom:
            shell.scroll_to(max(0, box[1] - 120))
            self.pump(shell.root, 0.05)

    def click(self, canvas, tag, button: int = 1, settle: float = 0.25) -> bool:
        xy = self.visible_xy(canvas, tag)
        if xy is None:
            return False
        self.click_xy(canvas, *xy, button=button, settle=settle)
        return True

    def click_xy(self, canvas, x, y, button: int = 1, settle: float = 0.25) -> None:
        canvas.event_generate("<Motion>", x=x, y=y, when="now")
        canvas.event_generate(f"<Button-{button}>", x=x, y=y, when="now")
        canvas.event_generate(f"<ButtonRelease-{button}>", x=x, y=y, when="now")
        self.pump(canvas.winfo_toplevel(), settle)

    def key(self, widget, keysym: str, settle: float = 0.25) -> None:
        widget.focus_force()
        widget.update()
        widget.event_generate(f"<{keysym}>", when="now")
        self.pump(widget.winfo_toplevel(), settle)

    def wheel(self, widget, x, y, delta: int = -120, settle: float = 0.15) -> None:
        widget.event_generate("<MouseWheel>", delta=delta, x=x, y=y,
                              rootx=widget.winfo_rootx() + x, rooty=widget.winfo_rooty() + y, when="now")
        self.pump(widget.winfo_toplevel(), settle)

    @staticmethod
    def text_tag(canvas, text: str, within: str = "page"):
        """A canvas text item whose words are exactly `text` - for the parts
        of a page that are clickable but not in the kit's registry."""
        for i in canvas.find_withtag(within):
            if canvas.type(i) == "text" and canvas.itemcget(i, "text") == text:
                return i
        return None


def overlaps(canvas, registry_items, shrink: int = 1) -> list[str]:
    """Pairs of controls whose drawn areas cross. A canvas has no layout
    manager to complain, so two rows drawn at the same y just paint over
    each other - the 1.9 grid-cell collision, in its canvas form."""
    boxes = []
    for tag, kind, label in registry_items:
        b = canvas.bbox(tag)
        if b:
            boxes.append((tag, kind, label, (b[0] + shrink, b[1] + shrink, b[2] - shrink, b[3] - shrink)))
    out = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i][3], boxes[j][3]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                out.append(f"{boxes[i][1]} '{boxes[i][2]}' and {boxes[j][1]} '{boxes[j][2]}'")
    return out
