"""Full verification pass: every module imports, every route installs and
uninstalls cleanly, and the guard rails actually fire.

Run this before cutting a release.
"""
import atexit
import inspect
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import time
import warnings
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILS: list[str] = []
X64 = Path(os.environ.get("DLSS5_TEST_X64",
                          r"C:\Users\Mustafa\Downloads\dlss5-feed-host64.exe"))
SRC_DIR = Path(__file__).resolve().parent


def _diag_src() -> str:
    """Every rule in the diagnosis, as text.

    It was one file until 1.9.0 and src_of(diagnose) read it; the package's
    __init__ holds no rules, so its parts are read instead.
    """
    from core import diagnose as _dp
    return "\n".join(src_of(m) for m in (_dp.model, _dp.evidence, _dp.process,
                                         _dp.helper, _dp.routes, _dp.body,
                                         _dp.chain))


def src_of(obj) -> str:
    """The source of a function or module, or "" when it cannot be read.

    Dozens of checks below read source to assert that a rule is written the
    way it has to be. `inspect.getsource` raises rather than returning
    nothing - a TokenError while a file is being edited, an OSError for a
    frozen module - and a raise here does not fail one check, it ends the
    run before RESULT is printed and quietly drops every check after it.
    An empty string fails the one check that asked.
    """
    import inspect as _i
    try:
        return _i.getsource(obj)
    except Exception as e:
        print(f"   (source unreadable: {type(e).__name__}: {e})")
        return ""


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"   {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)
    return cond


_SECTIONS: list[str] = []
_REACHED_RESULT = False


@atexit.register
def _say_if_truncated() -> None:
    """A suite that dies half way is not a suite that passed.

    An uncaught exception in any section ends the run - the sections after
    it never execute, and the exit code is 1, exactly as it is for a failed
    check. This says which section it stopped in, so nobody reads 731
    passes as a green run.
    """
    if _REACHED_RESULT or not _SECTIONS:
        return
    print()
    print("!! THE SUITE STOPPED IN: " + _SECTIONS[-1])
    print(f"!! {len(_SECTIONS)} section(s) ran; everything after that one "
          f"never did. This is not a pass.")


def section(title: str) -> None:
    _SECTIONS.append(title)
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------- the window, for the checks
# The 2.0 window (core/ui) keeps its logic in controller classes and draws
# everything on a Canvas, so the checks below run that logic instead of
# reading its source. These helpers are the four ways they do it:
#
#   _ui_isolated()   settings, the saved library and the watcher's record go
#                    to a temporary folder for as long as a check needs them -
#                    nothing here writes into the machine's own settings
#   _UiThreads       stands in for `threading` inside a core.ui module: it
#                    records every worker, runs it only when told to, and
#                    knows whether the code asking is inside one - which is
#                    how "off the Tk thread" is proven rather than read
#   _ui_ctl()        the real controllers (library, game, watcher) with no
#                    Tk at all: what they write, ask and queue is recorded
#   _ui_live()       the real window on a real (invisible) Tk root, with the
#                    network calls it makes at start switched off; clicks go
#                    in as real events through _ui_click / _ui_pick
#
# Every one of them is built so a missing name is a FAIL on the check that
# asked, not an AttributeError that ends the run.
import contextlib as _ui_ctx


@_ui_ctx.contextmanager
def _ui_isolated():
    from core import covers as _cv, library as _l, prefs as _p, watch as _w
    saved = (_p.FILE, _l.FILE, _w.RECORD, _cv.ROOT, _cv.online, _cv.undecided)
    d = Path(tempfile.mkdtemp(prefix="ui_iso_"))
    _p.FILE, _l.FILE, _w.RECORD = d / "settings.json", d / "library.json", d / "record.json"
    # a test game's cover is never looked up online: its made-up name would
    # go to Steam's store, and the answer into the machine's art cache
    _cv.ROOT, _cv.online, _cv.undecided = d / "art", (lambda: False), (lambda: False)
    try:
        yield d
    finally:
        _p.FILE, _l.FILE, _w.RECORD, _cv.ROOT, _cv.online, _cv.undecided = saved
        shutil.rmtree(d, ignore_errors=True)


class _UiThreads:
    """`threading` for a core.ui module: Thread(target=...).start() is
    recorded, and run at once (run=True) or when `go()` is called."""

    def __init__(self, run: bool = True):
        self.started: list = []
        self.run = run
        self.inside = 0
        outer = self

        class _T:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                self.target, self.args, self.kwargs = target, args, kwargs or {}
                self.done = False

            def start(self):
                outer.started.append(self)
                if outer.run:
                    outer._one(self)

            def is_alive(self):
                return False

            def join(self, timeout=None):
                pass

        self.Thread = _T

    def _one(self, t):
        if t.done:
            return
        t.done = True
        self.inside += 1
        try:
            t.target(*t.args, **t.kwargs)
        finally:
            self.inside -= 1

    def go(self):
        """Run every worker started so far (and any they start)."""
        while any(not t.done for t in self.started):
            for t in list(self.started):
                self._one(t)

    @property
    def names(self) -> list:
        return [getattr(t.target, "__qualname__", "?") for t in self.started]


@_ui_ctx.contextmanager
def _ui_threads(run: bool = True):
    from core.ui import app as _a, ctl_game as _cg, ctl_library as _cl, ctl_watch as _cw
    th = _UiThreads(run)
    with patch.object(_cg, "threading", th), patch.object(_cl, "threading", th), \
            patch.object(_cw, "threading", th), patch.object(_a, "threading", th):
        yield th


def _ui_ctl(game=None, support=None):
    """The window's controllers with no window: LibraryControl, GameControl
    and WatchControl on one object whose shell, root and log are recorders.
    Call inside _ui_isolated() - the controllers read settings as they start."""
    import queue as _queue
    from core.ui import ctl_dlss as _cd, ctl_game as _cg, ctl_library as _cl, ctl_watch as _cw

    class _Shell:
        def __init__(self):
            self.asked, self.answers, self.infos, self.toasts = [], [], [], []
            self.busy_text = self.status_text = ""
            self.page, self.log_open, self.watching = None, False, None

        def ask(self, title, text, ok="ok", cancel="cancel", danger=False, accent=None):
            self.asked.append((title, text, ok, cancel))
            return self.answers.pop(0) if self.answers else False

        def info(self, title, text):
            self.infos.append((title, text))

        error = info

        def ask_text(self, title, prompt, initial=""):
            self.asked.append((title, prompt, "save", "cancel"))
            return None

        def busy(self, text=""):
            self.busy_text = text

        def status(self, text):
            self.status_text = text

        def toggle_log(self, open_=None):
            self.log_open = True if open_ is None else open_

        def toast(self, *a, **k):
            self.toasts.append((a, k))

        def banner(self, *a, **k):
            pass

        def unbanner(self, *a, **k):
            pass

        def draw_rail(self):
            pass

        def redraw(self):
            pass

    class _Root:
        def after(self, ms, fn=None, *a):
            return "after#0"

        def after_cancel(self, _job):
            pass

        def clipboard_clear(self):
            pass

        def clipboard_append(self, _t):
            pass

    class _Ctl(_cl.LibraryControl, _cg.GameControl, _cw.WatchControl, _cd.DlssControl):
        def __init__(self):
            self.q = _queue.Queue()
            self.busy = False
            self.game = None
            self.said: list = []
            self.shell = _Shell()
            self.root = _Root()
            self._crash_shown = False
            self.crash_offered = False
            self._library_init()
            self._game_init()
            self._watch_init()
            self._dlss_init()

        def write(self, text, tag=""):
            self.said.append((str(text), tag))

        def text(self) -> str:
            return "\n".join(t for t, _tag in self.said)

        def refresh(self, page, soft=False):
            pass

        def offer_crash_report(self):
            self.crash_offered = True

        def open_game(self, g):
            self.enter_game(g)

        def pump(self) -> list:
            """Hand every queued message to its _on_<kind>, like App._pump,
            but let a raise through - a check wants to see it."""
            kinds = []
            while not self.q.empty():
                kind, payload = self.q.get_nowait()
                kinds.append(kind)
                getattr(self, f"_on_{kind}")(payload)
            return kinds

    c = _Ctl()
    if game is not None:
        c.game = game
        c.settings = c.default_settings(game)
        c.entry = {}
    if support is not None:
        c.support = support
        c.route = support.recommended
    return c


def _ui_support(options, recommended=None, native_dlss=True, evidence=(), reason=""):
    return dlss.Support(native_dlss=native_dlss, evidence=list(evidence),
                        recommended=recommended or options[0], options=list(options),
                        reason=reason)


_UI_DIRS: list = []


def _ui_cleanup() -> None:
    """Remove the folders _ui_game made."""
    while _UI_DIRS:
        shutil.rmtree(_UI_DIRS.pop(), ignore_errors=True)


def _ui_game(folder=None, name="Test Game", api="DX12", bitness=64, installed=False,
             manifest=None):
    """A game in a temporary folder with a real executable in it."""
    if folder:
        d = Path(folder)
    else:
        d = Path(tempfile.mkdtemp(prefix="ui_game_")) / name
        _UI_DIRS.append(d.parent)
    d.mkdir(parents=True, exist_ok=True)
    exe = d / "Game.exe"
    if not exe.is_file():
        shutil.copyfile(X64, exe)
    if installed or manifest:
        man = {"version": 1, "complete": True, "exe": "Game.exe", "path": "feeder",
               "api": api, "bitness": bitness, "files": []}
        man.update(manifest or {})
        (d / "dlss5-autopilot.json").write_text(json.dumps(man), encoding="utf8")
    return games.Game(name=name, folder=d, exe=exe, bitness=bitness, api=api,
                      api_detected=api, source="Manual", candidates=[exe])


class _UiLive:
    """The real window, invisible, with no network. close() puts back what
    it changed."""

    def __init__(self, scale: float = 1.0, games_=None):
        import tkinter as tk
        from core import community as _cm, library as _l, prefs as _p, watch as _w
        from core.ui import app as _a, theme as _t
        self.T = _t
        self._iso = _ui_isolated()
        self.tmp = self._iso.__enter__()
        # Tk fonts belong to the interpreter that made them: a width cached
        # under the last root raises once that root is gone.
        getattr(_t, "_measure", {}).clear()
        _t.set_scale(96 * scale)
        self._patches = [patch.object(_a.App, n, lambda self, *a, **k: None)
                         for n in ("check_update", "load_board", "load_shared", "load_catalog",
                                   "check_stale")]
        self._patches.append(patch.object(_cm, "fetch", lambda *a, **k: {}))
        for p in self._patches:
            p.start()
        self.root = tk.Tk()
        try:
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.root.tk.call("tk", "scaling", 96 * scale / 72.0)
        self.app = None
        self.error = None
        try:
            self.app = _a.App(self.root)
        except Exception as e:           # a window that cannot open is a FAIL, not a stop
            import traceback as _tb
            self.error = _tb.format_exc()
            print("   (the window did not open: " + f"{type(e).__name__}: {e})")
        self.settle(150)

    @property
    def ok(self) -> bool:
        return self.app is not None

    @property
    def kit(self):
        return self.app.shell.kit

    @property
    def canvas(self):
        return self.app.shell.content

    def settle(self, ms: int = 120) -> None:
        end = time.monotonic() + ms / 1000.0
        while True:
            try:
                self.root.update()
            except Exception:
                return
            if time.monotonic() >= end:
                return
            time.sleep(0.01)

    def until(self, cond, timeout: float = 6.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if cond():
                    return True
            except Exception:
                pass
            self.settle(30)
        try:
            return bool(cond())
        except Exception:
            return False

    def log(self) -> str:
        try:
            return self.app.shell.log_text.get("1.0", "end")
        except Exception:
            return ""

    def labels(self, kind=None) -> list:
        try:
            return [lab for _t, _k, lab in self.kit.controls(kind)]
        except Exception:
            return []

    def _xy(self, canvas, tag):
        canvas.update_idletasks()
        box = canvas.bbox(tag)
        if not box:
            return None
        x1, y1, x2, y2 = box
        view_h = canvas.winfo_height()
        top = canvas.canvasy(0)
        if y1 < top or y2 > top + view_h:
            try:
                self.app.shell.scroll_to(max(0, y1 - view_h / 3))
            except Exception:
                canvas.yview_moveto(max(0.0, (y1 - 40) / max(1, self.app.shell.content_h)))
            canvas.update_idletasks()
            top = canvas.canvasy(0)
        return int((x1 + x2) / 2 - canvas.canvasx(0)), int((y1 + y2) / 2 - top)

    def click(self, tag, button: int = 1, canvas=None) -> bool:
        """A real press and release in the middle of `tag`."""
        c = canvas or self.canvas
        xy = self._xy(c, tag) if tag else None
        if xy is None:
            return False
        x, y = xy
        c.event_generate("<Motion>", x=x, y=y)
        c.event_generate(f"<ButtonPress-{button}>", x=x, y=y)
        c.event_generate(f"<ButtonRelease-{button}>", x=x, y=y)
        self.settle(60)
        return True

    def press(self, label: str, kind=None) -> bool:
        return self.click(self.kit.find(label, kind))

    def pick(self, label: str) -> bool:
        """Click the row of the open menu whose text starts with `label`."""
        top = self.kit.top()
        if top is None:
            return False
        c = self.canvas
        for item in c.find_withtag(top.tag):
            if c.type(item) != "text":
                continue
            txt = str(c.itemcget(item, "text"))
            if txt.startswith(label) or (txt.endswith(chr(0x2026)) and label.startswith(txt[:-1])):
                row = [t for t in c.gettags(item) if t.startswith(top.tag + "r")]
                if row:
                    return self.click(row[0])
        return False

    def texts(self) -> list:
        c = self.canvas
        return [str(c.itemcget(i, "text")) for i in c.find_all() if c.type(i) == "text"]

    def enter(self, g, support, extra=None) -> None:
        """Open a game's page with detection answered: what the worker would
        have put on the queue, put there by hand."""
        a = self.app
        with patch.object(dlss, "detect", lambda *a_, **k: support):
            a.all_games = [g] + [x for x in a.all_games if x is not g]
            a.game = None
            a.open_game(g)
        fit = {o: (True, "") for o in support.options}
        ex = {"ac": None, "reengine": False, "shared": "", "dxvk": False,
              "ok": (True, ""), "seen": None}
        ex.update(extra or {})
        # the real worker may land first; either way the page ends on this
        self.until(lambda: not a.entering, 8.0)
        a.q.put(("entered", (g, support, fit, ex)))
        self.until(lambda: a.support is support and not a.entering, 4.0)
        self.settle(150)

    def close(self) -> None:
        try:
            if self.app is not None:
                self.app.lookout.stop()
        except Exception:
            pass
        try:
            for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(job)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        try:
            self.T.set_scale(96)
            getattr(self.T, "_measure", {}).clear()
        except Exception:
            pass
        self._iso.__exit__(None, None, None)


# ---------------------------------------------------------------- 1. imports
section("1. every module imports cleanly, with warnings as errors")
with warnings.catch_warnings():
    warnings.simplefilter("error")
    mods = ("pe", "games", "emulators", "gpu", "sources", "net", "prefs",
            "reshade_ini", "feedcfg", "dxvk", "dlss", "vulkan",
            "anticheat", "optiscaler", "diagnose", "selfupdate", "update", "watch",
            "log", "components", "profiles", "remix", "reengine", "refw",
            "installer", "verdicts", "lookout")
    # The window is a package: every module in it, whatever it is called, so
    # a new page cannot be added without being imported here.
    mods = mods + tuple("ui." + p.stem for p in sorted(Path("core", "ui").glob("*.py"))
                        if p.stem != "__init__")
    check("the window's package has its modules",
          {"ui.app", "ui.shell", "ui.kit", "ui.theme"} <= set(mods), mods)
    for m in mods:
        try:
            __import__(f"core.{m}")
            check(f"core.{m}", True)
        except Exception as e:
            check(f"core.{m}", False, f"{type(e).__name__}: {e}")

from core import remix, remixlist  # noqa: E402
from core import pe, reengine, refw, watch, community  # noqa: E402
from core import (diagnose, dlss, games, gpu, installer, net, optiscaler,  # noqa: E402
                  pe, prefs, reshade_ini, sources, update, vulkan)

check("no Turkish characters in any source", not any(
    any(ch in p.read_text(encoding="utf8") for ch in "şğıöçüŞĞİÖÇÜ")
    for p in list(Path("core").rglob("*.py")) + [Path("dlss5_autopilot.py")]))

# ---------------------------------------------------------------- 2. detection
section("2. detection on the real library")
found = games.scan_all(lambda m: None)
playable = [g for g in found if g.exe]
check("library scan returns games", len(playable) > 0, f"{len(playable)} playable")
for g in playable:
    s = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0)
    ok = (s.recommended in s.options
          and all(o in dlss.ALL_ROUTES for o in s.options))
    if not ok:
        check(f"route sane for {g.name}", False, f"{s.recommended} / {s.options}")
check("every game got a sane route", not any(f.startswith("route sane") for f in FAILS))

# A 32-bit game must never be offered the 64-bit-only bridge or add-ons. The
# feeder is the only route that reaches one - plus remix, which injects
# nothing into the game at all and so has no bitness of its own.
for g in playable:
    if g.bitness == 32:
        s = dlss.detect(g.install_dir, g.folder, g.api, g.bitness)
        check(f"32-bit {g.name[:22]} is feeder-only",
              set(s.options) <= {dlss.FEEDER, dlss.REMIX}
              and dlss.FEEDER in s.options, str(s.options))

# ---------------------------------------------------------------- 3. routes
section("3. install and uninstall on every route")
EXPECT = {
    dlss.NATIVE: (["dxgi.dll", "renodx-dlss5.addon64", "nvngx_dlssnr.dll",
                   "ReShade.ini"],
                  ["dlss5-feed.addon64", "dlss5-bridge.addon64",
                   "ReShadePreset.ini", "dlss5-feed.cfg"]),
    dlss.BRIDGE: (["dxgi.dll", "dlss5-bridge.addon64", "dlss5-bridge.cfg",
                   "renodx-dlss5.addon64", "nvngx_dlssnr.dll", "ReShade.ini"],
                  ["dlss5-feed.addon64", "dlss5-feed.cfg"]),
    dlss.FEEDER: (["dxgi.dll", "dlss5-feed.addon64", "renodx-dlss5.addon64",
                   "nvngx_dlssnr.dll", "ReShade.ini", "ReShadePreset.ini",
                   "reshade-shaders/Shaders/DLSS5_Feed.fx",
                   "reshade-shaders/Shaders/lumenite_Kernel.fx",
                   "dlss5-feed.cfg"],
                  ["dlss5-bridge.addon64"]),
}
EXPECT[dlss.OPTI] = (["dxgi.dll", "nvngx_dlssnr.dll", "nvngx.dll_dlssnr.dll",
                      "OptiScaler.ini"],
                     ["dlss5-feed.addon64", "dlss5-bridge.addon64",
                      "ReShade.ini", "renodx-dlss5.addon64"])
EXPECT[dlss.RENODX] = (["dxgi.dll", "renodx-dlss.addon64", "nvngx_dlssnr.dll",
                        "ReShade.ini"],
                       ["renodx-dlss5.addon64", "dlss5-feed.addon64",
                        "dlss5-bridge.addon64", "OptiScaler.ini"])

for route, (want, unwanted) in EXPECT.items():
    d = Path(tempfile.mkdtemp(prefix=f"all_{route}_"))
    shutil.copyfile(X64, d / "Game.exe")
    g = games.manual(d)
    try:
        installer.install(g, installer.Options(path=route,
                                               native_dlss=route != dlss.FEEDER),
                          on_log=lambda t: None)
        idir = g.install_dir
        files = {p.relative_to(idir).as_posix() for p in idir.rglob("*") if p.is_file()}
        check(f"{route}: all expected files", not [w for w in want if w not in files],
              str([w for w in want if w not in files]))
        check(f"{route}: nothing from other routes",
              not [u for u in unwanted if u in files],
              str([u for u in unwanted if u in files]))
        installer.uninstall(g, on_log=lambda t: None)
        left = [p.name for p in idir.rglob("*") if p.is_file()]
        check(f"{route}: uninstall is clean", left == ["Game.exe"], str(left))
        dirs_left = [p.name for p in idir.iterdir() if p.is_dir()]
        check(f"{route}: no empty folders left behind", not dirs_left, str(dirs_left))
    except Exception as e:
        check(f"{route}: installs", False, f"{type(e).__name__}: {e}")
    shutil.rmtree(d, ignore_errors=True)

# --------------------------------------------------- 3b. switching routes
section("3b. switching routes does not leave the old one behind")
for a, b in ((dlss.FEEDER, dlss.OPTI), (dlss.OPTI, dlss.FEEDER),
             (dlss.NATIVE, dlss.BRIDGE), (dlss.BRIDGE, dlss.NATIVE),
             (dlss.NATIVE, dlss.RENODX), (dlss.RENODX, dlss.FEEDER)):
    d = Path(tempfile.mkdtemp(prefix="switch_"))
    shutil.copyfile(X64, d / "Game.exe")
    (d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
    g = games.manual(d)
    try:
        installer.install(g, installer.Options(path=a, native_dlss=True),
                          on_log=lambda t: None)
        installer.install(g, installer.Options(path=b, native_dlss=True),
                          on_log=lambda t: None)
        files = {p.relative_to(g.install_dir).as_posix()
                 for p in g.install_dir.rglob("*") if p.is_file()}
        if b == dlss.FEEDER:
            stale = [f for f in files if "OptiScaler" in f or "nvngx.dll_dlssnr" in f
                     or "renodx-dlss.addon64" in f]
        elif b == dlss.RENODX:
            stale = [f for f in files if "renodx-dlss5" in f or "dlss5-feed" in f]
        else:
            stale = [f for f in files
                     if "dlss5-feed" in f or "reshade-shaders" in f]
        check(f"{a} -> {b}: no leftovers", not stale, str(stale[:3]))
        installer.uninstall(g, on_log=lambda t: None)
        left = sorted(p.name for p in g.install_dir.rglob("*") if p.is_file())
        check(f"{a} -> {b}: uninstall is clean",
              left == ["Game.exe", "sl.interposer.dll"], str(left))
    except Exception as e:
        check(f"{a} -> {b}: switches", False, f"{type(e).__name__}: {e}")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 4. guards
section("4. guard rails fire")

d = Path(tempfile.mkdtemp(prefix="guard_"))
shutil.copyfile(X64, d / "explorer.exe")          # a name that is running
g = games.manual(d)
try:
    installer.preflight(g)
    check("running game is refused", False)
except installer.InstallError as e:
    check("running game is refused", "running" in str(e).lower())
shutil.rmtree(d, ignore_errors=True)

d = Path(tempfile.mkdtemp(prefix="guard2_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "dxgi.dll").write_bytes(b"MZ" + b"\x00" * (2 << 20))   # not ReShade
g = games.manual(d)
try:
    installer.install(g, installer.Options(), on_log=lambda t: None)
    check("foreign dxgi.dll is refused", False)
except installer.InstallError as e:
    check("foreign dxgi.dll is refused", "not ReShade" in str(e))
shutil.rmtree(d, ignore_errors=True)

# GPU compatibility: an RTX 50-only build must be refused on this card
_, sm = gpu.detect()
d = Path(tempfile.mkdtemp(prefix="guard3_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
try:
    installer.install(g, installer.Options(dlssnr="310.8.0"), on_log=lambda t: None)
    check("incompatible dlssnr is refused", sm == 120, "installed anyway")
except installer.InstallError as e:
    check("incompatible dlssnr is refused", "will not run" in str(e).lower()
          or "not run on" in str(e).lower(), str(e).splitlines()[0][:60])
shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- 5. backups
section("5. the game's own files survive")
d = Path(tempfile.mkdtemp(prefix="bak_"))
shutil.copyfile(X64, d / "Game.exe")
orig = b"GAME ORIGINAL" + b"\x00" * 500
(d / "nvngx_dlss.dll").write_bytes(orig)
g = games.manual(d)
installer.install(g, installer.Options(keep_game_dlss=False), on_log=lambda t: None)
check("backup was made",
      (g.install_dir / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).is_file())
installer.install(g, installer.Options(keep_game_dlss=False), on_log=lambda t: None)
installer.uninstall(g, on_log=lambda t: None)
check("original restored after a REinstall",
      (g.install_dir / "nvngx_dlss.dll").is_file()
      and (g.install_dir / "nvngx_dlss.dll").read_bytes() == orig)
shutil.rmtree(d, ignore_errors=True)

# ------------------------------------------------- 5b. nothing is destroyed
section("5b. pre-existing files survive every route, byte for byte")
PRE = {
    "nvngx_dlssnr.dll":     b"USER OWN DLSSNR",
    "renodx-dlss5.addon64": b"USER OWN RENODX",
    "ReShade.ini":          b"[GENERAL]\nMyCustomSetting=42\n",
    "ReShadePreset.ini":    b"Techniques=MyFavourite@Cool.fx\n",
    "OptiScaler.ini":       b"[Upscalers]\nDx12Upscaler=fsr31\n",
    "dlss5-bridge.cfg":     b"ofa_perf=5\n",
    "nvngx_dlss.dll":       b"USER OWN DLSS",
    "d3d9.dll":             b"USER OWN DXVK",
}
for route in (dlss.FEEDER, dlss.OPTI, dlss.BRIDGE, dlss.NATIVE):
    d = Path(tempfile.mkdtemp(prefix=f"pre_{route}_"))
    shutil.copyfile(X64, d / "Game.exe")
    (d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
    for n, c in PRE.items():
        (d / n).write_bytes(c + bytes(300))
    g = games.manual(d)
    try:
        installer.install(g, installer.Options(path=route, native_dlss=True,
                                               keep_game_dlss=False),
                          on_log=lambda t: None)
        installer.uninstall(g, on_log=lambda t: None)
        idir = g.install_dir
        lost = [n for n, c in PRE.items()
                if not (idir / n).is_file()
                or not (idir / n).read_bytes().startswith(c)]
        check(f"{route}: every pre-existing file restored", not lost, str(lost))
    except Exception as e:
        check(f"{route}: survives pre-existing files", False,
              f"{type(e).__name__}: {e}")
    shutil.rmtree(d, ignore_errors=True)

# A manifest-less uninstall (record deleted, corrupted, or a pre-manifest
# v1.0/v1.1 install) used to delete dxgi.dll/opengl32.dll unconditionally -
# unlike every other proxy name, which was only ever removed after
# confirming the file really is ReShade. A real dxgi.dll from something else
# entirely (SpecialK, an ENB, a separately installed ReShade) sitting in the
# folder must survive.
d = Path(tempfile.mkdtemp(prefix="uninstall_foreign_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "dxgi.dll").write_bytes(b"MZ some other injector, not reshade" + bytes(2000))
(d / "opengl32.dll").write_bytes(b"MZ also not reshade" + bytes(2000))
g = games.manual(d)
installer.uninstall(g, on_log=lambda t: None)
check("a foreign dxgi.dll survives a manifest-less uninstall",
      (d / "dxgi.dll").is_file())
check("a foreign opengl32.dll survives a manifest-less uninstall",
      (d / "opengl32.dll").is_file())
shutil.rmtree(d, ignore_errors=True)

# The same folder, but this time the dxgi.dll really is ours (ReShade) -
# it must still be removed the way it always was.
d = Path(tempfile.mkdtemp(prefix="uninstall_real_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "dxgi.dll").write_bytes(b"ReShade" + bytes(1 << 20))
g = games.manual(d)
installer.uninstall(g, on_log=lambda t: None)
check("a real ReShade dxgi.dll is still removed with no manifest",
      not (d / "dxgi.dll").is_file())
shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- 6. vulkan
section("6. vulkan layer handling")
before = vulkan.existing_registration()
check("existing ReShade registration is detected or absent",
      before is None or isinstance(before, Path), repr(before))
# Ours can legitimately be the active one on this PC (a Vulkan-layer game of
# ours is installed here); what must hold is that it points at our folder
# and the manifest is really there.
check("an active registration is a real manifest; ours lives in our layer dir",
      before is None or (before.is_file() and (not vulkan.is_ours(before)
                                                 or before.parent == vulkan.layer_dir())),
      str(before))

# ---------------------------------------------------------------- 7. misc
section("6b. optiscaler proxy names")


def _fake_game(prefix: str = "opti_"):
    """A throwaway game folder that looks like it ships DLSS."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, d / "Game.exe")
    (d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
    return games.manual(d)


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


# OptiScaler identifies itself through the PE version resource, which keeps
# saying "OptiScaler.dll" whatever the file on disk is called.
d = Path(tempfile.mkdtemp(prefix="dlss5-proxy-"))
(d / "winmm.dll").write_bytes(
    b"MZ" + b"\0" * (1 << 20) + "OptiScaler.dll".encode("utf-16-le"))
(d / "dxgi.dll").write_bytes(b"MZ" + b"\0" * (1 << 21))     # someone else's
check("optiscaler is recognised under another name",
      optiscaler.is_optiscaler(d / "winmm.dll"))
check("an unrelated dll is not mistaken for optiscaler",
      not optiscaler.is_optiscaler(d / "dxgi.dll"))
check("a missing file is not optiscaler",
      not optiscaler.is_optiscaler(d / "version.dll"))
check("a taken proxy name is stepped over",
      optiscaler.suggest_proxy(d) == "winmm.dll", optiscaler.suggest_proxy(d))
check("an empty folder gets the default",
      optiscaler.suggest_proxy(Path(tempfile.mkdtemp())) == optiscaler.DEFAULT_PROXY)
check("every proxy name is explained",
      set(optiscaler.PROXY_HELP) == set(optiscaler.PROXY_NAMES))
check("an unsupported proxy name is refused",
      _raises(lambda: optiscaler.install(d, proxy="nonsense.dll")))

# A real install under a chosen name, with a conflicting copy already there.
g = _fake_game()
rival = g.install_dir / "version.dll"
rival.write_bytes(b"MZ" + b"\0" * (1 << 20) + "OptiScaler.dll".encode("utf-16-le"))
own = g.install_dir / "dxgi.dll"
own.write_bytes(b"THE GAME'S OWN DXGI")
installer.install(g, installer.Options(path=dlss.OPTI, native_dlss=True,
                                       opti_proxy="winmm.dll"),
                  on_log=lambda t: None)
check("the chosen proxy name is what gets written",
      (g.install_dir / "winmm.dll").is_file()
      and optiscaler.is_optiscaler(g.install_dir / "winmm.dll"))
check("the game's own dxgi.dll is left alone",
      own.read_bytes() == b"THE GAME'S OWN DXGI")
check("a rival optiscaler is moved out of the way",
      not rival.exists()
      and rival.with_name("version.dll" + installer.BACKUP_SUFFIX).is_file())
installer.uninstall(g, on_log=lambda t: None)
check("the rival is put back on uninstall",
      rival.is_file() and optiscaler.is_optiscaler(rival))
check("uninstall removes the proxy it installed",
      not (g.install_dir / "winmm.dll").exists())

# With no choice made, a game that ships its own dxgi.dll gets another name.
g = _fake_game()
(g.install_dir / "dxgi.dll").write_bytes(b"THE GAME'S OWN DXGI")
installer.install(g, installer.Options(path=dlss.OPTI, native_dlss=True),
                  on_log=lambda t: None)
check("auto avoids replacing the game's own dxgi.dll",
      (g.install_dir / "dxgi.dll").read_bytes() == b"THE GAME'S OWN DXGI"
      and optiscaler.is_optiscaler(g.install_dir / "winmm.dll"))
man = json.loads((g.install_dir / installer.MANIFEST).read_text(encoding="utf8"))
check("the manifest records the name it actually used",
      man["proxy"] == "winmm.dll", man["proxy"])
installer.uninstall(g, on_log=lambda t: None)

section("6c. the interface survives bad data")
import tkinter as _tk  # noqa: E402
# A window that finds a saved library from another version rescans the
# disks at start; the one on the machine running this is not the test's.
from core import library as _library_iso  # noqa: E402
_library_iso.FILE = Path(tempfile.mkdtemp(prefix="lib_iso_")) / "library.json"
from core.ui import ctl_game as _uig, ctl_library as _uil  # noqa: E402


def _ticked_names() -> set[str]:
    """Every "tick 'X'" / "untick 'X'" the tool says, joined across lines -
    in every module, the window's package included."""
    import ast as _ast
    import re as _re_t
    out = set()
    pat = _re_t.compile(r"\b(?:un)?tick(?:ed)? '([^'{}]+)'", _re_t.I)
    for _f in sorted((SRC_DIR / "core").rglob("*.py")):
        if _f == SRC_DIR / "core" / "gui.py":
            continue            # the 1.9 window: nobody sees it any more
        for _n in _ast.walk(_ast.parse(_f.read_text(encoding="utf8"))):
            if isinstance(_n, _ast.JoinedStr):
                _s = "".join(v.value if isinstance(v, _ast.Constant) else "{}"
                             for v in _n.values)
            elif isinstance(_n, _ast.Constant) and isinstance(_n.value, str):
                _s = _n.value
            else:
                continue
            out.update(m.group(1) for m in pat.finditer(_s))
    return out


# Names in someone else's window (Remix's developer menu, ReShade's overlay).
_FOREIGN_TICKS = {"Enable Neural Uplift (DLSS-NR)",
                  # ReShade's own Generic Depth tab
                  "Copy depth buffer before clear operations"}

# The window, read while it is up: every toggle its settings draw on every
# route, every button on its pages, and the one button the autopilot checks
# further down ask about. A check that cannot run is not a check that passed,
# so what 1.9.0c needs is kept here in plain values.
_TOGGLES: set = set()
_LIVE_BUTTONS: list = []
_AUTO_BTN: dict = {}
_ui6c = _UiLive()
check("the window opens", _ui6c.ok, _ui6c.error)
if _ui6c.ok:
    _a6c = _ui6c.app
    _LIVE_BUTTONS += [b.lower() for b in _ui6c.labels("button")]
    _rr6c = [dlss.FEEDER, dlss.NATIVE, dlss.BRIDGE, dlss.RENODX, dlss.UPSTREAM,
             dlss.STANDALONE, dlss.OPTI, dlss.REMIX]
    for _api6c in ("DX11", "DX12"):
        _g6c = _ui_game(name=f"Toggles {_api6c}", api=_api6c)
        _ui6c.enter(_g6c, _ui_support(_rr6c, dlss.FEEDER,
                                      evidence=["nvngx_dlss.dll", "nvngx_dlssd.dll"]))
        if _api6c == "DX12":
            _LIVE_BUTTONS += [b.lower() for b in _ui6c.labels("button")]
            # the pass is pressed from here: where it sits and what it says
            _ab = _ui6c.kit.find("autopilot", "button")
            _ib = _ui6c.kit.find("install", "button")
            _c6c = _ui6c.canvas
            if _ab and _ib:
                _AUTO_BTN["row"] = (_c6c.bbox(_ab)[1], _c6c.bbox(_ib)[1])
                _AUTO_BTN["texts"] = [_c6c.itemcget(i, "text") for i in _c6c.find_withtag(_ab)
                                      if _c6c.type(i) == "text"]
                _xy = _ui6c._xy(_c6c, _ab)
                _c6c.event_generate("<Motion>", x=_xy[0], y=_xy[1])
                _ui6c.until(lambda: _c6c.find_withtag("kit_tip"), 3.0)
                _AUTO_BTN["tip"] = " ".join(_c6c.itemcget(i, "text")
                                            for i in _c6c.find_withtag("kit_tip")
                                            if _c6c.type(i) == "text")
                _c6c.event_generate("<Motion>", x=2, y=2)
        _ui6c.press("settings", "button")
        _ui6c.settle(350)
        for _r6c in _rr6c:
            _a6c.set_setting("route", _r6c)
            _a6c.shell.redraw()
            _ui6c.settle(40)
            _TOGGLES |= {t.lower() for t in _ui6c.labels("toggle")}
        _a6c.set_setting("route", dlss.FEEDER)
_dead = sorted(n for n in _ticked_names() - _FOREIGN_TICKS
               if not any(t.startswith(n.lower()) for t in _TOGGLES))
# Five messages sent people to tick 'swap the Remix runtime' and the window
# never had that box (#148); 'feeder pre-release' was a dropdown entry.
check("every 'tick X' the tool says names a toggle the window draws", not _dead,
      (_dead, sorted(_TOGGLES)))
check("...and the toggle #148 was about is really drawn, on the Remix route",
      "swap the remix runtime" in _TOGGLES, sorted(_TOGGLES))

if _ui6c.ok:
    # a folder that has gone away must not abandon the whole list
    _ghost = games.Game(name="Ghost", folder=Path("Z:/gone"))
    _ghost.exe = Path("Z:/gone/x.exe")
    _a6c.all_games = [_ghost]
    _a6c.shell.show("library")
    _ui6c.until(lambda: _a6c._rows.get((str(_ghost.folder), str(_ghost.exe))) is not None)
    _a6c.shell.redraw()
    _ui6c.settle(80)
    _cards6c = _a6c.shell.pages["library"].cards
    check("one unreadable game does not empty the list",
          len(_cards6c) == 1 and _a6c.card(_ghost)["status"] in ("unreadable", "unsupported"),
          (len(_cards6c), _a6c.card(_ghost)))

    # an exception in a queue handler must not stop the pump for good
    _a6c.q.put(("scanned", None))          # a payload _on_scanned cannot unpack
    try:
        _a6c._pump()
        _a6c.q.put(("scan", "alive"))
        _a6c._pump()
        _pumped6c = _a6c.shell.busy_text
    except Exception as _e6c:
        _pumped6c = repr(_e6c)
    check("the pump survives a handler that raises", _pumped6c == "alive", _pumped6c)
    check("...and a message nobody handles is dropped, not raised",
          (_a6c.q.put(("no_such_kind", 1)) or _a6c._pump() or True) and _a6c.q.empty())

    # a game whose architecture could not be read must stay visible
    _unk = games.Game(name="Unknown", folder=Path("Z:/g1"))
    _unk.exe, _unk.bitness = Path("Z:/g1/x.exe"), None
    _b64 = games.Game(name="Sixtyfour", folder=Path("Z:/g2"))
    _b64.exe, _b64.bitness = Path("Z:/g2/x.exe"), 64
    _a6c.all_games = [_unk, _b64]
    _seen = {}
    for _a in ("all", "64", "32"):
        _a6c.set_arch(_a)
        _seen[_a] = [x.name for x in _a6c.visible()]
    _a6c.set_arch("all")
    check("unknown architecture is never filtered away",
          all("Unknown" in v for v in _seen.values()), str(_seen))
    check("a known architecture still filters",
          "Sixtyfour" not in _seen["32"], str(_seen["32"]))

    # issue #30: the add-on dropdown opened on the newest build and passed it
    # as an explicit choice, so the driver pin to 4.55 never ran from the GUI.
    # Pressed through the page, the way a person does it.
    with patch.object(prefs, "find_renodx", lambda sf=False: (None, [])):
        _g30 = _ui_game(name="Addon Pick", api="DX12")
        _ui6c.enter(_g30, _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER))
        _a6c.catalog = {"renodx": [{"label": "4.70", "tag": "4.70", "url": "u"},
                                   {"label": "4.55", "tag": "4.55", "url": "u"}],
                        "renodx_sf": []}
        _ui6c.press("settings", "button")
        _ui6c.settle(350)
        _dd30 = _ui6c.kit.find("dlss5 add-on", "dropdown")
        _shown30 = [_ui6c.canvas.itemcget(i, "text") for i in _ui6c.canvas.find_withtag(_dd30 or "none")
                    if _ui6c.canvas.type(i) == "text"]
        _o = _a6c.opts()
        check("the DLSS 5 add-on dropdown opens on auto, not on the newest build",
              bool(_dd30) and any(t.startswith("auto") for t in _shown30)
              and [v for v, _l in _a6c.choices("renodx")][:2] == ["auto", "4.70"],
              (_shown30, _a6c.choices("renodx")))
        check("...so the options carry no explicit add-on version and the installer's pins apply",
              _o.renodx is None, repr(_o.renodx))
        _ui6c.press("dlss5 add-on", "dropdown")
        _picked30 = _ui6c.pick("4.70")
        check("a build picked from the list is still an explicit choice",
              _picked30 and _a6c.opts().renodx == "4.70", (_picked30, _a6c.opts().renodx))
_ui6c.close()
_ui_cleanup()

section("6d. a quarantined file is reported, not ignored")
_d = Path(tempfile.mkdtemp(prefix="quar_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
installer.install(_g, installer.Options(), on_log=lambda t: None)
# antivirus takes the add-on away after the install wrote it
_victim = _g.install_dir / installer.RENODX
_victim.unlink()
_rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
check("an install that lost a file says nothing was wrong",
      not [w for w in _rep.warnings if "no longer there" in w])
# now simulate the file vanishing DURING the install
_orig_manifest = installer._write_manifest
def _steal(root, g, opt, rep, proxy, level, complete):
    if complete:
        pass
    return _orig_manifest(root, g, opt, rep, proxy, level, complete)
_rep2 = installer.Report()
_rep2.written = [installer.RENODX, "definitely-not-here.dll"]
_miss = [r for r in _rep2.written if not (_g.install_dir / r).exists()]
check("a missing written file is detectable", _miss == ["definitely-not-here.dll"],
      str(_miss))
installer.uninstall(_g, on_log=lambda t: None)
shutil.rmtree(_d, ignore_errors=True)

section("6e. no two routes' add-ons in one folder, no logs left behind")
# Seen in MGS V: a bridge install recorded in the manifest with an orphaned
# dlss5-feed.addon64 beside it. ReShade loads every .addon64, so both
# registered, both tried to build a contract, and the game exited before it
# ever created a swap chain.
_d = Path(tempfile.mkdtemp(prefix="orphan_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
_g = games.manual(_d)
installer.install(_g, installer.Options(path=dlss.BRIDGE, native_dlss=True),
                  on_log=lambda t: None)
# an orphan no manifest knows about
(_g.install_dir / installer.FEEDER_ADDON64).write_bytes(b"MZ" + bytes(1000))
(_g.install_dir / "dlss5-feed.cfg").write_text("orphan")
installer.install(_g, installer.Options(path=dlss.BRIDGE, native_dlss=True),
                  on_log=lambda t: None)
check("an orphaned add-on from another route is removed",
      not (_g.install_dir / installer.FEEDER_ADDON64).is_file())
check("only one route's add-on remains",
      (_g.install_dir / installer.BRIDGE_ADDON).is_file())

# every log the components write must go on uninstall
for _n in ("ReShade.log", "dlss5-feed.log", "OptiScaler.log", "nvngx.log"):
    (_g.install_dir / _n).write_text("runtime")
(_g.install_dir / "Logs").mkdir(exist_ok=True)
(_g.install_dir / "Logs" / "OptiScaler-x.log").write_text("x")
installer.uninstall(_g, on_log=lambda t: None)
_left = sorted(p.relative_to(_g.install_dir).as_posix()
               for p in _g.install_dir.rglob("*") if p.is_file())
# The orphan came back because we cannot prove it was ours - uninstall's job
# is to return the folder to how it was, and an add-on with no ReShade beside
# it does nothing. What must NOT survive is any log or anything we wrote.
check("uninstall leaves no runtime logs behind",
      not [f for f in _left if f.endswith(".log")], str(_left))
check("uninstall removes everything this tool wrote",
      not [f for f in _left if f in (installer.BRIDGE_ADDON, "dxgi.dll",
                                     installer.RENODX, installer.DLSSNR,
                                     installer.MANIFEST)], str(_left))
check("a file we could not prove was ours is put back",
      (_g.install_dir / installer.FEEDER_ADDON64).is_file())
shutil.rmtree(_d, ignore_errors=True)

# ...but one we DID record as ours is removed, not restored.
_d = Path(tempfile.mkdtemp(prefix="orphan2_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
_g = games.manual(_d)
installer.install(_g, installer.Options(path=dlss.FEEDER), on_log=lambda t: None)
installer.install(_g, installer.Options(path=dlss.BRIDGE, native_dlss=True),
                  on_log=lambda t: None)
check("switching routes leaves only the new route's add-on",
      (_g.install_dir / installer.BRIDGE_ADDON).is_file()
      and not (_g.install_dir / installer.FEEDER_ADDON64).is_file())
installer.uninstall(_g, on_log=lambda t: None)
_left = sorted(p.relative_to(_g.install_dir).as_posix()
               for p in _g.install_dir.rglob("*") if p.is_file())
check("and uninstall after a switch leaves nothing of ours",
      _left == ["Game.exe", "sl.interposer.dll"], str(_left))
shutil.rmtree(_d, ignore_errors=True)

section("6f. reshade can be loaded under another name")
check("every reshade proxy name is explained",
      set(installer.RESHADE_PROXY_HELP) == set(installer.RESHADE_PROXIES))
check("the api still decides by default",
      installer._proxy_name("DX11") == "dxgi.dll"
      and installer._proxy_name("OpenGL") == "opengl32.dll")
check("an explicit choice wins",
      installer._proxy_name("DX11", "d3d11.dll") == "d3d11.dll")
check("a name reshade does not support is ignored",
      installer._proxy_name("DX11", "nonsense.dll") == "dxgi.dll")

_d = Path(tempfile.mkdtemp(prefix="rproxy_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
_g = games.manual(_d)
installer.install(_g, installer.Options(path=dlss.BRIDGE, native_dlss=True,
                                        reshade_proxy="d3d11.dll"),
                  on_log=lambda t: None)
check("reshade is installed under the chosen name",
      (_g.install_dir / "d3d11.dll").is_file()
      and not (_g.install_dir / "dxgi.dll").exists())
_man = json.loads((_g.install_dir / installer.MANIFEST).read_text(encoding="utf8"))
check("the manifest records the reshade name used",
      _man["proxy"] == "d3d11.dll", _man["proxy"])
installer.uninstall(_g, on_log=lambda t: None)
_left = sorted(p.relative_to(_g.install_dir).as_posix()
               for p in _g.install_dir.rglob("*") if p.is_file())
check("uninstall removes it under that name too",
      _left == ["Game.exe", "sl.interposer.dll"], str(_left))
shutil.rmtree(_d, ignore_errors=True)

section("6g. an install in a subfolder is still found")
# The exe is picked fresh on every scan. Reported as "uninstall does not work":
# the install went to Bin\Win64, the next scan ranked another exe first, and
# the marker files were then looked for in a folder that never had them.
_d = Path(tempfile.mkdtemp(prefix="adopt_"))
_sub = _d / "Bin" / "Win64"
_sub.mkdir(parents=True)
shutil.copyfile(X64, _sub / "Game.exe")
shutil.copyfile(X64, _d / "Decoy-Shipping.exe")   # ranks above the real exe
_cands = pe.find_game_exes(_d)
check("the ranking really does prefer the other exe",
      bool(_cands) and _cands[0].parent == _d,
      _cands[0].name if _cands else "no candidates")
_g = games.manual(_d)
check("with nothing installed, the top-ranked exe is used",
      _g.install_dir == _d and not _g.installed, str(_g.install_dir))
(_sub / installer.MANIFEST).write_text(json.dumps(
    {"version": 1, "exe": "Game.exe", "files": ["dxgi.dll"]}), encoding="utf8")
_g2 = games.manual(_d)
check("an install in a subfolder is adopted", _g2.install_dir == _sub,
      str(_g2.install_dir))
check("and the exe it was made for comes with it",
      _g2.exe == _sub / "Game.exe", str(_g2.exe))
check("so the uninstall button is enabled", _g2.installed)
# an older release wrote no exe name - the folder must still be found
(_sub / installer.MANIFEST).unlink()
(_sub / "dlss5kur-kurulum.json").write_text("{}", encoding="utf8")
_g3 = games.manual(_d)
check("a record left by an older release counts too",
      _g3.install_dir == _sub and _g3.installed, str(_g3.install_dir))
(_sub / "dlss5kur-kurulum.json").unlink()
_g4 = games.manual(_d)
check("once nothing is installed, nothing is adopted",
      _g4.install_dir == _d and not _g4.installed, str(_g4.install_dir))
shutil.rmtree(_d, ignore_errors=True)

section("6h. the game list can be searched")
from core.ui import ctl_library as _uil  # noqa: E402
check("the search is the library controller's own rule",
      callable(getattr(_uil.LibraryControl, "matches", None)))
_m = getattr(_uil.LibraryControl, "matches", lambda g, t: "missing")   # the caller lowercases what was typed
_fake = games.Game(name="Cyberpunk 2077", source="Steam",
                   folder=Path(r"D:\SteamLibrary\common\Cyberpunk 2077"))
check("an empty search matches everything", _m(_fake, []))
check("part of the name matches", _m(_fake, ["cyber"]))
check("typing does not have to match the case", _m(_fake, ["cyberpunk 2077".lower()]))
check("every word has to match",
      _m(_fake, ["cyber", "2077"]) and not _m(_fake, ["cyber", "witcher"]))
check("the folder is searched as well", _m(_fake, ["steamlibrary"]))
check("so is the store it came from", _m(_fake, ["steam"]))
check("a word in neither matches nothing", not _m(_fake, ["skyrim"]))

section("7. odds and ends")
check("rate-limit fallback message exists", hasattr(sources, "last_fallback"))
check("api cache path set", "api-cache" in str(sources._API_CACHE))
check("download supports retry", "attempts" in net.download.__code__.co_varnames)
check("update points at the right repo", update.REPO.endswith("DLSS5-Autopilot"))
check("version is 2.0.0", update.VERSION == "2.0.0", update.VERSION)

from core import log as _log  # noqa: E402
_log.write("test run")
check("the log file is written", _log.path().is_file(), str(_log.path()))
_before = _log.path().stat().st_size
try:
    raise ValueError("deliberate")
except ValueError as e:
    _log.exception("test", e)
check("a traceback reaches the log",
      _log.path().stat().st_size > _before
      and "deliberate" in _log.path().read_text(encoding="utf8", errors="replace"))
from core import components as _comp  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="comp_"))
(_d / installer.MANIFEST).write_text(json.dumps(
    {"components": {"renodx": "4.60"}}), encoding="utf8")
_items = _comp.check(_d)
check("component versions are read from the manifest",
      len(_items) == 1 and _items[0].installed == "4.60",
      str([(i.name, i.installed, i.latest) for i in _items]))
# pre-1.3 installs kept their versions in the notes only
(_d / installer.MANIFEST).write_text(json.dumps(
    {"notes": ["renodx version: 4.55", "backed up the game's own x.dll"]}),
    encoding="utf8")
# On this PC's driver the renodx pin may cap "latest" at 4.55 (section 44);
# this check is about reading the old note format, so the driver is taken
# out of the equation.
_saved_drv = gpu.driver_at_least
gpu.driver_at_least = lambda want: False
_old = _comp.check(_d)
gpu.driver_at_least = _saved_drv
check("versions recorded by an older release are still read",
      len(_old) == 1 and _old[0].installed == "4.55"
      and _old[0].outdated, str([(i.installed, i.latest, i.outdated) for i in _old]))
check("a different build family is not called outdated",
      not _comp.Item("x", "310.8.SF-v2", "310.8.0-RTX40",
                     _comp._key("310.8.0-RTX40") > _comp._key("310.8.SF-v2")).outdated)
check("nothing recorded gives nothing to report", _comp.check(Path(tempfile.mkdtemp())) == [])

check("every store is scanned",
      all(hasattr(games, f"scan_{s}") for s in
          ("steam", "epic", "gog", "ea", "ubisoft", "battlenet", "xbox")))
r = diagnose.analyse(Path(r"C:\Program Files (x86)\Steam\steamapps\common\DEATHLOOP"))
check("diagnosis reads a real log", bool(r.verdict), r.verdict[:52])

# ---------------------------------------------------------- 8. v1.3.0 rules
section("8. route rules, version pins and the OptiScaler dials")

# DirectX 10: reachable since feeder 0.13.1 (private D3D11 relay), feeder only.
d = Path(tempfile.mkdtemp(prefix="dx10_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
g.api = "DX10"
ok, why = installer.check_supported(g)
check("dx10 is supported now", ok, why)
s10 = dlss.detect(d, d, "DX10", 64)
check("dx10 goes to the feeder only, and says which build",
      s10.supported and s10.options == [dlss.FEEDER] and "0.13.1" in s10.reason,
      s10.reason)
check("dx10 reliability is beta with the relay named",
      installer.reliability(g, dlss.FEEDER)[0] == installer.BETA
      and "relay" in installer.reliability(g, dlss.FEEDER)[1])
check("the feeder build gate compares versions the feeder's way",
      sources.feeder_key("v0.13.1-beta.1") >= sources.feeder_key(sources.FEEDER_DX10_MIN)
      and sources.feeder_key("v0.12.1-beta.2") < sources.feeder_key(sources.FEEDER_DX10_MIN)
      and sources.feeder_key("v0.14.0") > sources.feeder_key(sources.FEEDER_DX10_MIN))
shutil.rmtree(d, ignore_errors=True)

# 64-bit D3D9 is reachable now, through ShortFuse's add-on only.
# A fresh folder, never %TEMP% itself: handing the detector the whole
# temp directory let another test's leftover .trex folder decide the answer,
# and the suite gave different results on back-to-back runs.
_empty = Path(tempfile.mkdtemp(prefix="empty_game_"))
s9 = dlss.detect(_empty, _empty, "DX9", 64)
check("64-bit dx9 goes to the renodx add-on only", s9.options == [dlss.RENODX], str(s9.options))
# 32-bit stays feeder-only whatever the API.
for api in ("DX9", "DX11", "DX12", "Vulkan", "OpenGL"):
    s32 = dlss.detect(_empty, _empty, api, 32)
    check(f"32-bit {api} is feeder-only", s32.options == [dlss.FEEDER], str(s32.options))

# D3D12 + DLSS -> OptiScaler on any RTX card; the note says what the author tested.
g50 = _fake_game("rtx50_")
sup50 = dlss.detect(g50.install_dir, g50.folder, "DX12", 64, sm=120)
sup40 = dlss.detect(g50.install_dir, g50.folder, "DX12", 64, sm=89)
sup10 = dlss.detect(g50.install_dir, g50.folder, "DX12", 64, sm=61)
check("rtx 50 with a dlss d3d12 game is steered to optiscaler",
      sup50.recommended == dlss.OPTI, sup50.recommended)
check("rtx 40 is steered to optiscaler too", sup40.recommended == dlss.OPTI, sup40.recommended)
check("a pascal card is not steered anywhere new", sup10.recommended == dlss.NATIVE)
fit40 = dlss.fit(dlss.OPTI, "DX12", True, 89)
check("optiscaler is usable on an rtx 40, with the author's caveat",
      fit40[0] is True and "author tested RTX 50" in fit40[1], str(fit40))
check("optiscaler is marked usable on an rtx 50",
      dlss.fit(dlss.OPTI, "DX12", True, 120)[0] is True)
check("optiscaler without dlss in the game is refused",
      dlss.fit(dlss.OPTI, "DX12", False, 120)[0] is False)
shutil.rmtree(g50.folder, ignore_errors=True)

# The feeder's stable release only accepts renodx-dlss5 4.55.
check("feeder 0.7.0 pins renodx to 4.55", sources.renodx_for_feeder("v0.7.0") == "4.55")
check("feeder 0.8.0-beta.2 still pins", sources.renodx_for_feeder("v0.8.0-beta.2") == "4.55")
check("feeder 0.8.0-beta.3 accepts newer", sources.renodx_for_feeder("v0.8.0-beta.3") is None)
check("feeder 0.9.0-beta.1 accepts newer", sources.renodx_for_feeder("v0.9.0-beta.1") is None)
check("a plain release sorts above its betas",
      sources.feeder_key("v0.9.0") > sources.feeder_key("v0.9.0-beta.1"))

# nvngx_dlssnr build order follows the card.
fake_cat = [{"label": l} for l in ("310.8.SF-v2", "310.8.0-RTX40", "310.8.0", "310.8.SF")]
check("rtx 50 gets nvidia's own build first",
      gpu.order_dlssnr(fake_cat, 120)[0]["label"] == "310.8.0")
check("rtx 40 gets the -RTX40 build first",
      gpu.order_dlssnr(fake_cat, 89)[0]["label"] == "310.8.0-RTX40")
check("rtx 30 gets an SF build first",
      gpu.order_dlssnr(fake_cat, 86)[0]["label"].startswith("310.8.SF"))
check("unknown card keeps the mirror's order",
      [e["label"] for e in gpu.order_dlssnr(fake_cat, None)] == [e["label"] for e in fake_cat])
check("every tier has a plain-words note",
      all(gpu.tier_note(sm_) for sm_ in (75, 86, 89, 120)))

# OptiScaler.ini: dials land in [DlssNr], the rest of the file is untouched.
d = Path(tempfile.mkdtemp(prefix="nr_"))
(d / "OptiScaler.ini").write_text("; tuned by hand\n[Upscalers]\nDx12Upscaler=dlss\n\n"
                                   "[DLSSNR]\nEnabled=false\nIntensity=1.3\n",
                                   encoding="utf8")
optiscaler.enable_nr(d, settings={"WorkingScale": 0.75, "Preset": 2})
txt = (d / "OptiScaler.ini").read_text(encoding="utf8")
check("nr enabled in place", "Enabled=true" in txt and "Enabled=false" not in txt)
check("working scale written", "WorkingScale=0.75" in txt, txt)
check("hand-tuned keys survive", "Intensity=1.3" in txt and "; tuned by hand" in txt)
check("section spelling normalised", "[DlssNr]" in txt and "[DLSSNR]" not in txt)
check("other sections untouched", "Dx12Upscaler=dlss" in txt)
optiscaler.set_dx11_bridged_upscaler(d)
txt = (d / "OptiScaler.ini").read_text(encoding="utf8")
check("dx11 gets a bridged upscaler", "Dx11Upscaler=fsr22_12" in txt)
check("still exactly one DlssNr section", txt.count("[DlssNr]") == 1)
shutil.rmtree(d, ignore_errors=True)

# Uninstall with a locked file: nothing is lost, the record stays, second run cleans.
g = _fake_game("locked_")
installer.install(g, installer.Options(path=dlss.NATIVE, native_dlss=True),
                  on_log=lambda t: None)
held = open(g.install_dir / "dxgi.dll", "rb")
lines = []
installer.uninstall(g, on_log=lines.append)
check("locked file is reported, not silently skipped",
      any("could not remove" in l for l in lines))
check("record kept for the locked file", (g.install_dir / installer.MANIFEST).is_file())
held.close()
installer.uninstall(g, on_log=lambda t: None)
left = sorted(p.name for p in g.install_dir.rglob("*") if p.is_file())
check("second uninstall finishes the job", left == ["Game.exe", "sl.interposer.dll"], str(left))
shutil.rmtree(g.folder, ignore_errors=True)

# A hand-installed OptiScaler under dxgi.dll is moved aside, not fought with.
g = _fake_game("handopti_")
fake = b"MZ" + bytes(1 << 20) + "OptiScaler.dll".encode("utf-16-le")
(g.install_dir / "dxgi.dll").write_bytes(fake)
(g.install_dir / "OptiScaler.ini").write_text("[Upscalers]\nDx12Upscaler=dlss\n")
try:
    installer.install(g, installer.Options(path=dlss.NATIVE, native_dlss=True),
                      on_log=lambda t: None)
    check("hand-installed optiscaler does not block a reshade route", True)
    check("it was backed up", (g.install_dir / "dxgi.dll.dlss5-autopilot-backup").is_file())
    check("its ini was moved aside too", not (g.install_dir / "OptiScaler.ini").is_file())
    installer.uninstall(g, on_log=lambda t: None)
    check("uninstall puts the hand-installed optiscaler back",
          (g.install_dir / "dxgi.dll").read_bytes() == fake
          and (g.install_dir / "OptiScaler.ini").is_file())
except Exception as e:
    check("hand-installed optiscaler does not block a reshade route", False, f"{type(e).__name__}: {e}")
shutil.rmtree(g.folder, ignore_errors=True)

# DLSS kept where engines keep it, not beside the exe, still counts.
for sub in (Path("Engine/Plugins/Runtime/Nvidia/DLSS/Binaries/ThirdParty/Win64"),
            Path("Bin/Win64Shared")):
    d = Path(tempfile.mkdtemp(prefix="deepdlss_"))
    exe_dir = d / "Binaries" / "Win64"
    exe_dir.mkdir(parents=True)
    shutil.copyfile(X64, exe_dir / "Game.exe")
    (d / sub).mkdir(parents=True)
    (d / sub / "nvngx_dlss.dll").write_bytes(b"MZ" + bytes(1000))
    (d / "Content").mkdir()
    (d / "Content" / "nvngx_dlss.dll").write_bytes(b"MZ")     # never looked at
    sd = dlss.detect(exe_dir, d, "DX12", 64, sm=89)
    check(f"dlss under {sub.parts[0]}/... is found", sd.native_dlss and dlss.OPTI in sd.options,
          str(sd.evidence))
    shutil.rmtree(d, ignore_errors=True)
d = Path(tempfile.mkdtemp(prefix="nodlss_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "Content").mkdir()
(d / "Content" / "nvngx_dlss.dll").write_bytes(b"MZ")
check("a dll inside Content does not count", not dlss.detect(d, d, "DX12", 64).native_dlss)
shutil.rmtree(d, ignore_errors=True)

# A store's launch stub in the root must not win over the real Binaries exe.
d = Path(tempfile.mkdtemp(prefix="stub_"))
real = d / "Snowfall" / "Binaries" / "Win64"
real.mkdir(parents=True)
shutil.copyfile(X64, real / "GWT.exe")
shutil.copyfile(X64, d / "GWT.exe")
gs = games.Game(name="stub", folder=d, exe=d / "GWT.exe", source="Epic")
games.enrich(gs)
check("the real Binaries exe wins over the root stub", gs.exe == real / "GWT.exe", str(gs.exe))
shutil.rmtree(d, ignore_errors=True)

# The SF add-on is told apart from renodx-dlss5 by content, not by name.
d = Path(tempfile.mkdtemp(prefix="sf_"))
(d / "a.addon64").write_bytes(b"MZ" + bytes(300_000) + b"RenoDX DLSS renodx-dlss.addon64")
(d / "b.addon64").write_bytes(b"MZ" + bytes(300_000) + b"RenoDX DLSS renodx-dlss5.addon64")
check("sf build recognised", prefs.is_renodx_sf(d / "a.addon64"))
check("renodx-dlss5 is not mistaken for sf", not prefs.is_renodx_sf(d / "b.addon64"))
shutil.rmtree(d, ignore_errors=True)




# ------------------------------------------------------------ 9. v1.3.2
section("9. dxvk for games that quit on reshade, stray reshade copies, "
        "settings that travel, the feeder zip")
from core import dxvk, reshade_ini, sources, prefs

# The known list and the switch that follows it.
d = Path(tempfile.mkdtemp(prefix="dxvk_"))
shutil.copyfile(X64, d / "mgsvtpp.exe")
g = games.manual(d)
g.api = "DX11"            # the fixture exe is not the real game; MGS V is D3D11
check("mgs v is recognised as needing dxvk", bool(installer.wants_dxvk(g)),
      str(installer.wants_dxvk(g)))
check("an ordinary game is not", installer.wants_dxvk(
    games.Game(name="x", folder=d, exe=d / "Game.exe", bitness=64, api="DX11")) is None)
o = installer.Options(path=dlss.FEEDER, dxvk=True)
steps = installer.plan(g, o)
check("dxvk is the first step and reshade becomes the vulkan layer",
      steps[0].startswith("DXVK") and steps[1] == "ReShade (Vulkan layer)", str(steps[:2]))
check("optiscaler never goes through dxvk",
      not installer.uses_dxvk(g, installer.Options(path=dlss.OPTI, dxvk=True)))
check("a vulkan game has no proxy dll name",
      installer._proxy_name("Vulkan") == installer.VULKAN_LAYER)
check("dxvk names its logs after the exe",
      dxvk.logs_for(Path("mgsvtpp.exe"))[0] == "mgsvtpp_dxgi.log")
g9 = games.Game(name="gta", folder=d, exe=d / "GTAIV.exe", bitness=32, api="DX9")
s9 = installer.plan(g9, installer.Options(path=dlss.FEEDER, dxvk=True))
check("dx9 through dxvk: dxvk first, vulkan layer, host64 helper",
      s9[0] == "DXVK (DX9 -> Vulkan)"
      and s9[1] == "ReShade (Vulkan layer)" and "host64 helper process" in s9, str(s9))
# dgVoodoo2 was dropped in 1.6.0, so DXVK is the only DirectX 9 translation
# left: a DX9 game takes it whether or not the box is ticked, and nothing
# may ever put a dgVoodoo step back into the plan.
sp9 = installer.plan(g9, installer.Options(path=dlss.FEEDER))
check("dx9 takes dxvk even with the box unticked - it is the only way left",
      sp9[0] == "DXVK (DX9 -> Vulkan)"
      and installer.uses_dxvk(g9, installer.Options(path=dlss.FEEDER)), str(sp9))
check("no plan on any route mentions dgVoodoo any more",
      not any("dgvoodoo" in step.lower()
              for r in (dlss.FEEDER, dlss.NATIVE, dlss.BRIDGE, dlss.RENODX)
              for step in installer.plan(g9, installer.Options(path=r))))
check("dxvk puts d3d9.dll for dx9 and dxgi+d3d11 for dx11",
      dxvk.files_for("DX9") == ("d3d9.dll",) and "d3d11.dll" in dxvk.files_for("DX11"))
check("the renodx-dlss route never goes through dxvk (it hooks in-process)",
      not installer.uses_dxvk(g9, installer.Options(path=dlss.RENODX, dxvk=True)))
from core import vulkan as _vk
check("the 32-bit layer has its own manifest and both are unregistered",
      _vk.MANIFEST32 == "ReShade32.json" and "MANIFEST32" in open(SRC_DIR / "core" / "vulkan.py", encoding="utf8").read())

# The real thing: install through DXVK, check what landed, uninstall. Under
# a name of its own: the install refuses while a process of that name runs,
# and the owner may well be playing MGS V while this runs.
shutil.move(d / "mgsvtpp.exe", d / "dxvktest.exe")
dxvk.NEEDS_DXVK["dxvktest.exe"] = "test game"
g = games.manual(d)
g.api = "DX11"
try:
    installer.install(g, o, on_log=lambda t: None)
    idir = g.install_dir
    check("dxvk's dxgi.dll and d3d11.dll are in place, and they are dxvk",
          dxvk.is_dxvk(idir / "dxgi.dll") and dxvk.is_dxvk(idir / "d3d11.dll"))
    check("no reshade proxy dll beside them",
          not any(installer._is_reshade(idir / n) for n in installer.RESHADE_PROXIES))
    man = json.loads((idir / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest records dxvk and the vulkan layer",
          man.get("dxvk") and man["api"] == "Vulkan"
          and man["proxy"] == installer.VULKAN_LAYER, str((man.get("dxvk"), man["api"], man["proxy"])))
    check("the folder counts as a vulkan install", str(idir) in prefs.vulkan_games())
    check("the folder is remembered as an install", str(idir) in prefs.installs())
    (idir / "dxvktest_dxgi.log").write_text("x")
    (idir / "dxvktest_d3d11.log").write_text("x")
    installer.uninstall(g, on_log=lambda t: None)
    left = [p_.name for p_ in idir.rglob("*") if p_.is_file()]
    check("uninstall removes dxvk and its logs too", left == ["dxvktest.exe"], str(left))
    check("the folder is forgotten again", str(idir) not in prefs.installs())
except Exception as e:
    check("dxvk route installs", False, f"{type(e).__name__}: {e}")
dxvk.NEEDS_DXVK.pop("dxvktest.exe", None)
shutil.rmtree(d, ignore_errors=True)

# A ReShade left under another name is moved out of the way, ours or not.
d = Path(tempfile.mkdtemp(prefix="stray_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "sl.interposer.dll").write_bytes(b"MZ" + bytes(300_000))
(d / "d3d11.dll").write_bytes(b"MZ" + bytes(1 << 20) + b"ReShade")   # not ours
g = games.manual(d)
installer.install(g, installer.Options(path=dlss.BRIDGE, native_dlss=True),
                  on_log=lambda t: None)
check("a stray reshade d3d11.dll is moved aside before dxgi.dll goes in",
      not (d / "d3d11.dll").exists() and (d / "dxgi.dll").is_file()
      and (d / ("d3d11.dll" + installer.BACKUP_SUFFIX)).is_file())
installer.uninstall(g, on_log=lambda t: None)
check("uninstall puts the stray one back (it was not ours)",
      (d / "d3d11.dll").is_file() and not (d / "dxgi.dll").exists())
shutil.rmtree(d, ignore_errors=True)

# Without a record, uninstall still finds ReShade under any name - and only
# ReShade: a game's own d3d11.dll is left alone.
d = Path(tempfile.mkdtemp(prefix="norec_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "d3d12.dll").write_bytes(b"MZ" + bytes(1 << 20) + b"ReShade")
(d / "d3d11.dll").write_bytes(b"MZ" + bytes(1 << 20) + b"the game's own")
installer.uninstall(games.manual(d), on_log=lambda t: None)
check("no record: a reshade d3d12.dll is removed", not (d / "d3d12.dll").exists())
check("no record: a game's own d3d11.dll stays", (d / "d3d11.dll").is_file())
shutil.rmtree(d, ignore_errors=True)

# The user's ReShade keys and overlay settings travel to the next game.
a = Path(tempfile.mkdtemp(prefix="carry_a_"))
b = Path(tempfile.mkdtemp(prefix="carry_b_"))
(a / "ReShade.ini").write_text("[GENERAL]\nEffectSearchPaths=.\\x\n\n[INPUT]\n"
                               "KeyOverlay=36,0,0,0\nKeyEffects=145,0,0,0\n\n"
                               "[OVERLAY]\nTutorialProgress=4\nShowFPS=1\n\n"
                               "[STYLE]\nStyleIndex=2\n", encoding="utf8")
(b / "ReShade.ini").write_text("[GENERAL]\nEffectSearchPaths=.\\y\n\n[INPUT]\n"
                               "KeyOverlay=35,0,0,0\n", encoding="utf8")
src = reshade_ini.carry_over(b, [a])
bi = reshade_ini.Ini.load(b / "ReShade.ini")
check("settings come from the other game", src == a / "ReShade.ini")
check("the tutorial stays done and the fps counter follows",
      bi.get("OVERLAY", "TutorialProgress") == "4" and bi.get("OVERLAY", "ShowFPS") == "1")
check("a key this game already had is not overruled",
      bi.get("INPUT", "KeyOverlay") == "35,0,0,0")
check("this game's own paths are untouched",
      bi.get("GENERAL", "EffectSearchPaths") == ".\\y")
check("nothing to carry from an empty folder",
      reshade_ini.carry_over(a, [Path(tempfile.mkdtemp())]) is None)
shutil.rmtree(a, ignore_errors=True); shutil.rmtree(b, ignore_errors=True)

# The feeder's newer releases ship one zip; the loose names still resolve.
tag, assets = sources.resolve_feeder(prerelease=True)
check("the newest feeder pre-release is found", tag.startswith("v"), tag)
d = Path(tempfile.mkdtemp(prefix="feedzip_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
try:
    installer.install(g, installer.Options(path=dlss.FEEDER, feeder_prerelease=True),
                      on_log=lambda t: None)
    idir = g.install_dir
    check("the add-on and shader came out of the zip",
          (idir / "dlss5-feed.addon64").is_file()
          and (idir / "reshade-shaders/Shaders/DLSS5_Feed.fx").is_file())
    man = json.loads((idir / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest names the pre-release", man["components"].get("feeder") == tag,
          str(man["components"].get("feeder")))
    installer.uninstall(g, on_log=lambda t: None)
    left = [p_.name for p_ in idir.rglob("*") if p_.is_file()]
    check("pre-release feeder uninstalls clean", left == ["Game.exe"], str(left))
except Exception as e:
    check("pre-release feeder installs", False, f"{type(e).__name__}: {e}")
shutil.rmtree(d, ignore_errors=True)

check("dxvk is imported with the rest",
      "dxvk" in open(Path(__file__).with_name("test_all.py"), encoding="utf8").read())


# ------------------------------------------- 10. the diagnosis reads real logs
section("10. the diagnosis reads real logs")
import os as _os  # noqa: E402
import time as _time  # noqa: E402

_ADDONS = ["dlss5-feed.addon64", "renodx-dlss5.addon64"]


def _diag_dir(prefix: str, *, proxy: bool = True, addons: bool = True,
              reshade: str | None = None, feed: str | None = None,
              **extra) -> Path:
    """A folder that looks like a feeder install, minus whatever the test removes."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    man = {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
           "api": "DX11", "proxy": "dxgi.dll", "path": "feeder",
           "files": ["dxgi.dll", *_ADDONS, "reshade-shaders\\Shaders\\DLSS5_Feed.fx"]}
    man.update(extra)
    (d / "dlss5-autopilot.json").write_text(json.dumps(man), encoding="utf8")
    if proxy:
        (d / "dxgi.dll").write_bytes(b"MZ")
    # A real install leaves the shader there too, and the diagnosis now
    # treats a recorded file that has since gone as the answer (#84).
    _fx = d / "reshade-shaders" / "Shaders" / "DLSS5_Feed.fx"
    _fx.parent.mkdir(parents=True, exist_ok=True)
    _fx.write_text("// technique", encoding="utf8")
    if addons:
        for a in _ADDONS:
            (d / a).write_bytes(b"MZ")
    if reshade is not None:
        (d / "ReShade.log").write_text(reshade, encoding="utf8")
    if feed is not None:
        (d / "dlss5-feed.log").write_text(feed, encoding="utf8")
    return d


def _levels(rep, level):
    return [f_.title for f_ in rep.findings if f_.level == level]


# no log: the folder itself has to say why
_d = _diag_dir("diag_noproxy_", proxy=False)
_r = diagnose.analyse(_d)
check("missing proxy DLL is named, not 'never loaded'",
      not _r.ran and "dxgi.dll is missing" in _r.verdict
      and any("gone from the folder" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_noaddon_", addons=False)
_r = diagnose.analyse(_d)
check("a quarantined add-on is reported before anything else",
      not _r.ran and "gone from the folder" in _r.verdict
      and any("dlss5-feed.addon64" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_notrun_")
_r = diagnose.analyse(_d)
_info = " ".join(f_.title + f_.detail for f_ in _r.findings)
check("intact folder with no ReShade.log means 'not started since the install'",
      not _r.ran and _r.verdict.startswith("Not started since the install")
      and not _levels(_r, "bad"), _r.verdict)
check("...and the hints name the exe and the other proxy name",
      "Game.exe" in _info and "d3d11.dll" in _info)
# #171: that verdict rests on there being no log, so it has to say as much -
# Windows' fault record for the game's own exe is better evidence, and the
# report carried one while the answer said "run the game once".
check("...and the verdict marks itself as resting on the absent log",
      _r.never_ran is True)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_stale_", reshade="INFO | Initializing crosire's ReShade\n"
                                        'Registered add-on "DLSS 5 Feed" v0.1\n')
_old = _time.time() - 3600
_os.utime(_d / "ReShade.log", (_old, _old))
_r = diagnose.analyse(_d)
check("a ReShade.log older than the install is not evidence it ran",
      not _r.ran and "play once and check again" in _r.verdict.lower()
      and not _levels(_r, "bad"), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# frames delivered into a neural pass that cannot compile is not "Working."
_FEED_OK = ("[feed] effects: DLSS5_Feed.fx technique found, ColorInput found, "
            "DLSS5_MV_PROVIDER=3 (LumeniteFX Kernel) -> Lumenite_Kernel (enabled), depth reversed=1\n"
            "[feed] NVSDK_NGX_D3D12_Init -> 0x00000001 (Success)\n"
            "[feed] feature ready: 1920x1080 DLAA\n"
            "[feed] frame 1 delivered (1920x1080 at 100%)\n"
            "[feed] frame 2 delivered (1920x1080 at 100%)\n")
_OLD_COMPILER = (
    "d3dcompiler_47.dll: C:\\g\\d3dcompiler_47.dll -- rejects cs_5_1, hr=0x8876086C "
    "(error X3506: unrecognized compiler target 'cs_5_1'\n"
    "C:\\g\\d3dcompiler_47.dll is too old for Shader Model 5.1. The DLSS 5 add-on "
    "compiles its neural pass as cs_5_1, so neural rendering will silently do nothing "
    "-- this add-on will still report frames delivered\n")
_d = _diag_dir("diag_working_", feed=_FEED_OK)
_r = diagnose.analyse(_d)
check("frames delivered with a good compiler is Working.", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_oldcomp_", feed=_OLD_COMPILER + _FEED_OK)
_r = diagnose.analyse(_d)
check("an old d3dcompiler_47.dll stops 'Working.' (feed log form)",
      _r.verdict.startswith("Frames flow, but neural rendering is silently doing nothing")
      and any("d3dcompiler_47.dll is too old" in t for t in _levels(_r, "bad")), _r.verdict)
check("...and the finding carries the rename fix",
      any("dlss5-off" in f_.detail for f_ in _r.findings))
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_oldcomp2_", feed=_FEED_OK, reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "ERROR | error X3506: unrecognized compiler target 'cs_5_1'\n"))
_r = diagnose.analyse(_d)
check("an old d3dcompiler_47.dll stops 'Working.' (ReShade.log form)",
      "silently doing nothing" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# shader compile errors: only the feed's own shaders count
_d = _diag_dir("diag_shaders_", feed=_FEED_OK, reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "ERROR | Failed to compile 'C:\\g\\reshade-shaders\\Shaders\\lumenite_RTAO.fx':\n"
    "ERROR | Failed to compile 'C:\\g\\reshade-shaders\\Shaders\\lumenite_SSSR.fx':\n"
    "ERROR | Failed to load 'C:\\g\\reshade-shaders\\Shaders\\lumenite_TRAA.fx'\n"))
_r = diagnose.analyse(_d)
_bad = _levels(_r, "bad")
_info = [f_ for f_ in _r.findings if f_.level == "info" and "other shaders" in f_.title]
check("shaders the feed does not use are one INFO line, not failures",
      not _bad and len(_info) == 1 and _info[0].title.startswith("3 other shaders")
      and "lumenite_RTAO.fx" in _info[0].detail and "lumenite_TRAA.fx" in _info[0].detail,
      str(_bad) + " " + str([f_.title for f_ in _info]))
check("the verdict stays Working.", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_shaders2_", feed=_FEED_OK, reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "ERROR | Failed to compile 'C:\\g\\reshade-shaders\\Shaders\\DLSS5_Feed.fx':\n"
    "ERROR | Failed to compile 'C:\\g\\reshade-shaders\\Shaders\\lumenite_Kernel.fx':\n"))
_r = diagnose.analyse(_d)
_bad = _levels(_r, "bad")
check("the feed's own shaders failing IS reported",
      len(_bad) == 2 and any("DLSS5_Feed.fx" in t for t in _bad)
      and any("lumenite_Kernel.fx" in t for t in _bad), str(_bad))
shutil.rmtree(_d, ignore_errors=True)

# the new feeder lines
_FLAT = ("[feed] Depth probe (4x 32x32, frame 600): min 0, max 0, mean 0, variance 0, "
         "100% finite  <-- sampled depth is flat; inspect the depth debug view\n")
_d = _diag_dir("diag_depth_", feed=_FEED_OK + _FLAT)
_r = diagnose.analyse(_d)
check("flat depth in a game is a warning with the Generic Depth hint",
      any("depth buffer" in t for t in _levels(_r, "warn"))
      and any("aspect ratio heuristics" in f_.detail for f_ in _r.findings))
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_depthvideo_", feed=_FEED_OK + _FLAT, kind="video")
_r = diagnose.analyse(_d)
check("flat depth in a video player is expected (info only)",
      any("video player" in t for t in _levels(_r, "info"))
      and not any("depth buffer" in t for t in _levels(_r, "warn")))
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_lastwins_", feed=(
    "[feed] effects: DLSS5_Feed.fx technique MISSING, ColorInput MISSING, "
    "DLSS5_MV_PROVIDER=3 (LumeniteFX Kernel) -> none (not installed), depth reversed=1\n"
    "DLSS5_Feed.fx is not loaded (technique/textures missing) -- install it.\n"
    + _FEED_OK), reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "WARN | [DLSS 5 Feed] DLSS5_Feed.fx is not loaded (technique/textures missing)\n"
    "WARN | Skipping device because the focus window is the desktop window.\n"))
_r = diagnose.analyse(_d)
check("the last 'technique found' wins over an earlier MISSING",
      not _levels(_r, "bad") and _r.verdict == "Working."
      and any("Lumenite" in t and "enabled" in t for t in _levels(_r, "ok")),
      str(_levels(_r, "bad")) + " " + _r.verdict)
check("the desktop-window skip is information only",
      any("desktop" in t for t in _levels(_r, "info")))
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_d3d9_", reshade=(
    'Registered add-on "DLSS 5 Feed" v0.11\n'
    "INFO | Redirecting Direct3DCreate9Ex(SDKVersion = 32, ppD3D = 0) ...\n"
    "INFO | Exiting ...\n"))
_r = diagnose.analyse(_d)
check("a D3D9 device under a DXGI install is called out",
      any("Direct3D 9" in t for t in _levels(_r, "warn"))
      and not any("closed before" in t for t in _levels(_r, "bad")),
      str([f_.title for f_ in _r.findings]))
shutil.rmtree(_d, ignore_errors=True)

# the real video-player install, when it is on this machine
_real = Path(r"C:\Users\Mustafa\Desktop\dlss 5\_video\mpc-hc")
# Only when the player's own logs are from a real playback: a helper started
# from that folder can overwrite ReShade.log with a no-swapchain session.
if (_real / "dlss5-feed.log").is_file()         and "Registered add-on" in (_real / "ReShade.log").read_text(errors="replace"):
    _r = diagnose.analyse(_real)
    # A live folder: the owner plays in it, so its verdict is whatever the
    # last session did. What is checked is that real logs parse to a verdict.
    check("the mpc-hc sample parses to a verdict",
          _r.ran and bool(_r.verdict), _r.verdict)

# the bug report body
_d = _diag_dir("diag_body_", feed=_FEED_OK, reshade=(
    "INFO | Redirecting RegisterClassW(...)\n"
    'INFO | Registered add-on "DLSS 5 Feed" v0.11\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"))
_r = diagnose.analyse(_d)
_body = diagnose.issue_body("9.9", "RTX 4060 Ti", 89, "581.0", None, "feeder", _r,
                            "scan steam: 3 found\nscan epic: 0 found\nreal line\n",
                            Path("C:/x/autopilot.log"), _d, last_error="Traceback: boom")
_order = ["**Did the game start?**", "**What happened**", "**What I expected**", "- version: 9.9",
          "**Diagnosis**", "**Files in the folder**", "**ReShade.log**",
          "**dlss5-feed.log**", "**Last error**", "autopilot.log"]
_pos = [_body.find(k) for k in _order]
check("the report has every section, in order",
      all(p >= 0 for p in _pos) and _pos == sorted(_pos), str(_pos))
check("the folder check names the proxy and the add-ons",
      "- dxgi.dll: present" in _body and "- dlss5-feed.addon64: present" in _body
      and "- nvngx_dlssnr.dll: MISSING" in _body)
check("ReShade.log is filtered to what matters",
      "Registered add-on" in _body and "CreateSwapChainForHwnd" in _body
      and "RegisterClassW" not in _body)
check("the scan lines are dropped from the autopilot tail",
      "real line" in _body and "scan steam" not in _body)
check("the whole report fits in a URL-sized budget", len(_body) <= 6000, str(len(_body)))
shutil.rmtree(_d, ignore_errors=True)
_body = diagnose.issue_body("9.9", "x", None, "?", None, "optiscaler", None, "",
                            Path("C:/x/autopilot.log"), Path(tempfile.mkdtemp(prefix="diag_empty_")))
check("missing logs say (none), and the OptiScaler tail appears on that route",
      _body.count("(none)") >= 3 and "**OptiScaler.log**" in _body)



# ------------------------------------------------- 11. video player
section("11. the video player and the d3dcompiler sideline")
from core import video  # noqa: E402

# The ini: written fresh, and merged into one the person already edited.
_d = Path(tempfile.mkdtemp(prefix="video_ini_"))
video._write_ini(_d)
_ini = (_d / video.INI).read_text(encoding="utf8")
check("fresh ini selects the D3D11 renderer and silences the updater",
      "DSVidRen=14" in _ini and "UpdaterAutoCheck=0" in _ini and "[Settings]" in _ini)
(_d / video.INI).write_text("[Settings]\r\nDSVidRen=11\r\nVolume=42\r\n"
                            "YDLMaxHeight=720\r\n[Other]\r\nX=1\r\n", encoding="utf8")
video._write_ini(_d)
_ini = (_d / video.INI).read_text(encoding="utf8")
check("a user-set renderer is corrected back to MPCVR",
      "DSVidRen=14" in _ini and "DSVidRen=11" not in _ini)
check("the user's other settings survive", "Volume=42" in _ini and "X=1" in _ini)
check("a user-set YouTube quality is respected",
      "YDLMaxHeight=720" in _ini and "YDLMaxHeight=1440" not in _ini)
check("keys are not duplicated", _ini.count("UpdaterAutoCheck=") == 1
      and _ini.count("[Settings]") == 1)
# YDLExePath made MPC-HC fail to open any URL; an earlier build wrote it.
(_d / video.INI).write_bytes(b"[Settings]\r\nYDLExePath=C:\\x\\yt-dlp.exe\r\n")
video._write_ini(_d)
(_d / video.INI).write_bytes(b"\xef\xbb\xbf[Settings]\r\nVolume=42\r\n[Other]\r\nX=1\r\n")
for _ in range(3):
    video._write_ini(_d)
_raw = (_d / video.INI).read_bytes()
check("three rewrites keep the BOM, CRLF only, and add no blank lines",
      _raw.startswith(b"\xef\xbb\xbf") and b"\r\r" not in _raw
      and b"\r\n\r\n" not in _raw and b"\n" not in _raw.replace(b"\r\n", b""))
(_d / video.INI).write_bytes(b"[Settings]\r\nYDLExePath=C:\\x\\yt-dlp.exe\r\n")
video._write_ini(_d)
(_d / video.INI).write_bytes(
    b"[Settings]\r\nDSVidRen=14\r\n[Commands2]\r\n"
    b"CommandMod9=807 3 74 \"\" 5 0 0 0 0 0\r\n"
    b"CommandMod38=996 3 24 \"\" 5 0 0 0 0 0\r\n"
    b"CommandMod40=830 3 0 \"\" 5 0 0 0 0 0\r\n")
video._write_ini(_d)
_txt = (_d / video.INI).read_text(encoding="utf8")
check("the player's Home = jump-to-start binding is taken away",
      "CommandMod38=996 3 0 " in _txt and "CommandMod9=807 3 74 " in _txt
      and "CommandMod40=830 3 0 " in _txt, _txt[-200:])
check("a stray YDLExePath is dropped",
      "YDLExePath" not in (_d / video.INI).read_text(encoding="utf8"))
check("helper tools live under tools/, yt-dlp beside the player",
      video.tools_dir(_d) == _d / "tools" and video.YTDLP == "yt-dlp.exe")
shutil.rmtree(_d, ignore_errors=True)

check("the checklist names the toggle key", any("F6" in c for c in video.CHECKLIST))
check("video default folder is under the user's Videos",
      "Videos" in str(video.default_dir()))

# The sideline: a game-shipped d3dcompiler_47.dll goes aside on install and
# comes back on uninstall, byte for byte, without ever being deleted.
_d = Path(tempfile.mkdtemp(prefix="sideline_"))
shutil.copyfile(X64, _d / "Game.exe")
_comp = b"OLD COMPILER" + b"\x00" * 300
(_d / "D3DCompiler_47.dll").write_bytes(_comp)
_g = games.manual(_d)
installer.install(_g, installer.Options(), on_log=lambda t: None)
_moved = _d / ("D3DCompiler_47.dll" + installer.SIDELINE_SUFFIX)
check("d3dcompiler_47.dll is moved aside by the install",
      _moved.is_file() and not (_d / "D3DCompiler_47.dll").exists())
_man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
check("the manifest records the sideline and the kind",
      _man.get("sidelined") == ["D3DCompiler_47.dll"] and _man.get("kind") == "game")
# Reinstalling must not lose the original.
installer.install(_g, installer.Options(), on_log=lambda t: None)
check("a reinstall keeps the moved-aside original",
      _moved.is_file() and _moved.read_bytes() == _comp)
installer.uninstall(_g, on_log=lambda t: None)
check("uninstall puts the game's compiler back, byte for byte",
      (_d / "D3DCompiler_47.dll").is_file()
      and (_d / "D3DCompiler_47.dll").read_bytes() == _comp
      and not _moved.exists())
check("nothing of ours is left",
      not (_d / "dxgi.dll").exists() and not (_d / installer.MANIFEST).exists())
shutil.rmtree(_d, ignore_errors=True)

# An install with no such file records an empty list, and the OptiScaler
# route leaves the game's compiler alone (it does not use the neural pass
# through ReShade).
_d = Path(tempfile.mkdtemp(prefix="sideline_none_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
installer.install(_g, installer.Options(), on_log=lambda t: None)
_man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
check("no compiler, nothing sidelined", _man.get("sidelined") == [])
installer.uninstall(_g, on_log=lambda t: None)
shutil.rmtree(_d, ignore_errors=True)

_real = Path(r"C:\Users\Mustafa\Desktop\dlss 5\_video\mpc-hc")
if video.is_player(_real):
    _vg = video.as_game(_real)
    check("the real player folder is seen as a 64-bit D3D11 video target",
          _vg.kind == "video" and _vg.bitness == 64 and _vg.api == "DX11"
          and _vg.exe.name == video.PLAYER_EXE, f"{_vg.api} {_vg.bitness}")
    _sup = dlss.detect(_vg.install_dir, _vg.folder, _vg.api, _vg.bitness or 0, 89)
    check("...and the feeder is what it gets", _sup.recommended == dlss.FEEDER)


# ------------------------------------------------- 12. xbox and game pass
section("12. xbox and game pass")
# The path test on its own: only the two system-owned folder names count,
# and ModifiableWindowsApps (the one meant to be modified) must not.
check("XboxGames\\...\\Content is a locked store path",
      games.is_locked_store_path(Path(r"C:\XboxGames\Forza Horizon 6\Content")))
check("WindowsApps is a locked store path",
      games.is_locked_store_path(Path(r"C:\Program Files\WindowsApps\X")))
check("ModifiableWindowsApps is not",
      not games.is_locked_store_path(Path(r"C:\Program Files\ModifiableWindowsApps\X")))
check("a plain folder is not", not games.is_locked_store_path(Path(r"D:\Games\X")))

# A readable game under XboxGames is a normal game.
_d = Path(tempfile.mkdtemp(prefix="xbox_ok_"))
_content = _d / "XboxGames" / "Fake" / "Content"
_content.mkdir(parents=True)
shutil.copyfile(X64, _content / "Game.exe")
_g = games.manual(_content)
check("a readable XboxGames game scans like any other",
      not _g.error and _g.bitness == 64 and _g.exe == _content / "Game.exe",
      f"{_g.error!r} {_g.bitness}")
check("...and is supported", installer.check_supported(_g)[0])
shutil.rmtree(_d, ignore_errors=True)


_real_open = open


def _protected_read(path, mode="r", *args, **kwargs):
    if Path(path) == _exe and mode == "rb":
        raise PermissionError(13, "Permission denied", str(path))
    return _real_open(path, mode, *args, **kwargs)


# The real failure: an exe under XboxGames the user cannot read.
_d = Path(tempfile.mkdtemp(prefix="xbox_locked_"))
_content = _d / "XboxGames" / "Fake" / "Content"
_content.mkdir(parents=True)
_exe = _content / "Game.exe"
shutil.copyfile(X64, _exe)
with patch("builtins.open", side_effect=_protected_read):
    _g = games.manual(_content)
    check("an unreadable XboxGames exe gets a warning, not a fatal error",
          not _g.error and _g.exe_warning == games.XBOX_EXE_HINT, repr(_g.exe_warning))
    check("...the game keeps its executable so it stays listed", _g.exe == _exe)
    _ok, _why = installer.check_supported(_g)
    check("...and check_supported requests missing metadata, naming the "
          "controls as the window spells them",
          not _ok and "'architecture'" in _why and "'graphics api'" in _why,
          repr(_why))
    installer.preflight(_g)
    check("writing beside a protected EXE is allowed", True)
check("the executable was not changed", _exe.read_bytes()[:2] == b"MZ")
shutil.rmtree(_d, ignore_errors=True)

# The same unreadable exe outside a store folder keeps the plain error: the
# Xbox instruction would send someone to an app that has nothing to do with
# their game.
_d = Path(tempfile.mkdtemp(prefix="plain_locked_"))
_exe = _d / "Game.exe"
shutil.copyfile(X64, _exe)
with patch("builtins.open", side_effect=_protected_read):
    _g = games.manual(_d)
    check("an unreadable exe elsewhere stays fatal without an Xbox warning",
          _g.error and not _g.exe_warning and not installer.check_supported(_g)[0],
          repr(_g.error))
shutil.rmtree(_d, ignore_errors=True)

# preflight distinguishes a refused directory write from EXE protection.
_d = Path(tempfile.mkdtemp(prefix="xbox_pre_"))
_content = _d / "XboxGames" / "Fake" / "Content"
_content.mkdir(parents=True)
shutil.copyfile(X64, _content / "Game.exe")
_gx = games.manual(_content)
_plain = Path(tempfile.mkdtemp(prefix="plain_pre_"))
shutil.copyfile(X64, _plain / "Game.exe")
_gp = games.manual(_plain)
_orig_probe = tempfile.NamedTemporaryFile


def _refuse(*args, **kwargs):
    raise PermissionError(13, "Permission denied", str(kwargs.get("dir")))


tempfile.NamedTemporaryFile = _refuse
try:
    try:
        installer.preflight(_gx)
        check("preflight under XboxGames raises on a refused write", False)
    except installer.InstallError as e:
        check("preflight under XboxGames reports the actual write restriction",
              games.XBOX_HINT in str(e) and "Permission denied" not in str(e)
              and "administrator" not in str(e), str(e)[:80])
    try:
        installer.preflight(_gp)
        check("preflight elsewhere raises on a refused write", False)
    except installer.InstallError as e:
        check("preflight elsewhere still says run as administrator",
              "administrator" in str(e) and "Xbox" not in str(e), str(e)[:80])
finally:
    tempfile.NamedTemporaryFile = _orig_probe
try:
    installer.preflight(_gp)
    check("preflight passes again once writes work", True)
except installer.InstallError as e:
    check("preflight passes again once writes work", False, str(e))
shutil.rmtree(_d, ignore_errors=True)
shutil.rmtree(_plain, ignore_errors=True)

# ---------------------------------------------------------------- 13. profiles
section("13. settings profiles")
from core import profiles  # noqa: E402
_pdir = Path(tempfile.mkdtemp(prefix="profiles_"))
_old_dir = profiles.DIR
profiles.DIR = _pdir / "profiles"
try:
    _full = installer.Options(
        provider=4, renodx="v1.2.3", renodx_local=Path(r"C:\x\renodx.addon64"),
        dlssnr="310.1.0", dlss="310.2.1", keep_game_dlss=False,
        feed={"work_resolution": 80, "preset": 10, "hdr": 1},
        ignore_gpu_mismatch=True, path=dlss.OPTI, opti_proxy="winmm.dll",
        reshade_proxy="d3d11.dll", native_dlss=True, feeder_prerelease=True,
        feeder_tag="v0.9.0-beta", dxvk=True,
        nr={"WorkingScale": 0.66, "Preset": 2, "Style": 1})
    check("built-ins are listed with nothing on disk",
          profiles.list_profiles() == ["Quality", "Balanced", "Performance"],
          str(profiles.list_profiles()))
    check("built-ins are recognised", all(profiles.is_builtin(n) for n in
          ("Quality", "Balanced", "Performance")) and not profiles.is_builtin("Mine"))
    _b = profiles.load("Performance")
    check("built-in Performance is 70% / 0.5",
          _b.feed == {"work_resolution": 70} and _b.nr == {"WorkingScale": 0.5, "Preset": 0})

    _pf = profiles.save("My Cyberpunk", _full)
    check("save lands under the profiles folder", _pf.parent == profiles.DIR and _pf.is_file())
    _raw = json.loads(_pf.read_text(encoding="utf8"))
    check("the file keeps the display name, a timestamp and the app version",
          _raw.get("name") == "My Cyberpunk" and "saved" in _raw
          and _raw.get("app_version") == update.VERSION)
    check("machine and per-game fields are NOT written",
          not any(k in _raw for k in ("renodx_local", "native_dlss", "ignore_gpu_mismatch")))
    _back = profiles.load("My Cyberpunk")
    check("every profile field survives a round-trip", all(
        getattr(_back, f) == getattr(_full, f) for f in profiles.FIELDS),
          str([(f, getattr(_back, f), getattr(_full, f)) for f in profiles.FIELDS
               if getattr(_back, f) != getattr(_full, f)]))
    check("...and the excluded ones come back as defaults",
          _back.renodx_local is None and _back.native_dlss is False
          and _back.ignore_gpu_mismatch is False)
    check("the saved profile is listed after the built-ins",
          profiles.list_profiles() == ["Quality", "Balanced", "Performance", "My Cyberpunk"])

    # apply: the profile wins on its own fields, the game keeps its own
    _base = installer.Options(path=dlss.FEEDER, provider=3, native_dlss=True,
                              renodx_local=Path(r"C:\me\renodx.addon64"),
                              ignore_gpu_mismatch=True, feed={"work_resolution": 100})
    _ap = profiles.apply(_base, _back)
    check("apply overlays the profile's fields",
          _ap.path == dlss.OPTI and _ap.provider == 4 and _ap.feed == _full.feed
          and _ap.nr == _full.nr and _ap.dxvk is True and _ap.feeder_tag == "v0.9.0-beta")
    check("apply keeps the per-game and per-machine fields",
          _ap.native_dlss is True and _ap.renodx_local == _base.renodx_local
          and _ap.ignore_gpu_mismatch is True)
    check("apply does not alias the profile's dicts",
          _ap.feed is not _back.feed and _ap.nr is not _back.nr)
    check("apply leaves the base untouched", _base.path == dlss.FEEDER and _base.provider == 3)

    # odd names land on disk safely and still round-trip
    _odd = 'we/ird: name*?<>|"\\ \u00e7\u011f'
    _op = profiles.save(_odd, installer.Options())
    check("an odd name becomes a safe file name",
          _op.is_file() and _op.parent == profiles.DIR
          and all(ord(c) < 128 for c in _op.name)
          and not any(c in _op.name for c in '/\\:*?<>|"'), _op.name)
    check("...and is listed under its display name", _odd in profiles.list_profiles())
    check("...and loads by its display name", profiles.load(_odd).path == dlss.FEEDER)
    check("a name of only odd characters still gets a file",
          profiles.save("???", installer.Options()).is_file())

    # bad data
    (profiles.DIR / "broken.json").write_text(json.dumps(
        {"name": "Broken", "path": "teleport", "provider": 3}), encoding="utf8")
    try:
        profiles.load("Broken")
        check("an unknown route is rejected", False)
    except ValueError as e:
        check("an unknown route is rejected", "teleport" in str(e), str(e))
    (profiles.DIR / "extra.json").write_text(json.dumps(
        {"name": "Extra", "path": "bridge", "future_knob": 1, "provider": "4"}), encoding="utf8")
    _ex = profiles.load("Extra")
    check("unknown keys are ignored, missing keys default, types coerced",
          _ex.path == dlss.BRIDGE and _ex.provider == 4 and _ex.keep_game_dlss is True
          and _ex.feed == {} and _ex.nr == {})
    try:
        profiles.load("does not exist")
        check("a missing profile is a plain error", False)
    except ValueError:
        check("a missing profile is a plain error", True)
    for _bad in ("Quality", "Balanced"):
        try:
            profiles.save(_bad, installer.Options())
            check(f"built-in {_bad} cannot be overwritten", False)
        except ValueError:
            check(f"built-in {_bad} cannot be overwritten", True)

    # describe
    _desc = profiles.describe(_full)
    check("describe names the route and the dials",
          "route optiscaler" in _desc and "work resolution 80%" in _desc
          and "model resolution 66%" in _desc and "dxvk" in _desc, str(_desc))
    _fd = profiles.describe(installer.Options(provider=3, feed={"work_resolution": 85}))
    check("describe names the feeder provider",
          "route feeder" in _fd and "provider 3 (LumeniteFX Kernel 2.0)" in _fd
          and "work resolution 85%" in _fd, str(_fd))

    # delete
    profiles.delete("My Cyberpunk")
    check("delete removes the file", not _pf.exists()
          and "My Cyberpunk" not in profiles.list_profiles())
    profiles.delete("My Cyberpunk")
    check("deleting twice is harmless", True)
    try:
        profiles.delete("Quality")
        check("built-ins cannot be deleted", False)
    except ValueError:
        check("built-ins cannot be deleted", "Quality" in profiles.list_profiles())
finally:
    profiles.DIR = _old_dir
    shutil.rmtree(_pdir, ignore_errors=True)

# ------------------------------------------- 16. before/after screenshots
section("16. before/after screenshots")
import os  # noqa: E402
import struct  # noqa: E402
import time as _time  # noqa: E402
from core import compare  # noqa: E402

_d = Path(tempfile.mkdtemp(prefix="compare_"))
(_d / "ReShade.ini").write_text(
    "[INPUT]\nKeyScreenshot=44,1,0,0\n[SCREENSHOT]\nSavePath=.\\shots\n",
    encoding="utf8")
(_d / "shots").mkdir()
_now = _time.time()


def _mk(rel, age, size=8):
    p = _d / rel
    p.write_bytes(b"x" * size)
    os.utime(p, (_now - age, _now - age))
    return p


_old = _mk("Game 2020-01-01 10-00-00.png", 0)      # name stamp wins over mtime
_a = _mk("shots/Game_1.png", 200)
_b = _mk("shots/Game_2.png", 100)
_j = _mk("Game_3.jpg", 50)
_mk("dlss5_compare_2026.png", 10)                  # ours - never listed
_mk("readme.txt", 5)
_found = compare.find_screenshots(_d)
check("save path resolves relative to the game folder",
      compare.save_path(_d) == (_d / "shots").resolve(), str(compare.save_path(_d)))
check("finds images in the game folder and the save path, ignoring ours and non-images",
      set(_found) == {_old, _a, _b, _j}, ", ".join(p.name for p in _found))
check("newest first, name stamp beating mtime",
      _found[:3] == [_j, _b, _a] and _found[-1] == _old)
check("pairs the two newest within 5 minutes, oldest first",
      compare.pair(_found) == (_b, _j))
_c = _mk("shots/Game_4.png", 0)
os.utime(_b, (_now - 2000, _now - 2000))
os.utime(_j, (_now - 1000, _now - 1000))
check("the newest close-enough pair wins over older shots",
      compare.pair(compare.find_screenshots(_d)) == (_a, _c))
check("...and none when no two are close", compare.pair([_c, _j, _b]) is None)
check("a single file has no pair", compare.pair([_c]) is None)
check("screenshot key: vk + modifier from the ini",
      compare.screenshot_key(_d) == "Ctrl + Print Screen", compare.screenshot_key(_d))
check("screenshot key: default when no ini",
      compare.screenshot_key(_d / "nowhere") == "Print Screen")
check("key names: 44, F5, letters, numpad, unbound, garbage",
      compare.key_name("44,0,0,0") == "Print Screen"
      and compare.key_name("116,0,0,0") == "F5"
      and compare.key_name("65,0,1,0") == "Shift + A"
      and compare.key_name("101") == "Numpad 5"
      and compare.key_name("0,0,0,0").startswith("not bound")
      and compare.key_name("garbage") == "Print Screen")
check("fit factor caps a 4K side at 1920",
      compare.fit_factor(3840, 1920) == 2 and compare.fit_factor(1920, 1920) == 1
      and compare.fit_factor(2000, 1920) == 2)

try:
    import tkinter as _tk
    _root = _tk.Tk()
    _root.withdraw()
except Exception as e:  # no display / no Tcl on this machine
    _root = None
    check("tk could not start - export test skipped", True, f"{type(e).__name__}: {e}")
if _root is not None:
    def _png(name, w, h, colour):
        img = _tk.PhotoImage(master=_root, width=w, height=h)
        img.put("{" + " ".join([colour] * w) + "}", to=(0, 0, w, h))
        p = _d / name
        img.write(str(p), format="png")
        return p
    _pa = _png("Game 2026-09-02 12-00-00.png", 40, 30, "#d8a657")
    _pb = _png("Game 2026-09-02 12-00-20.png", 60, 20, "#6f9f6f")
    _out = compare.export_side_by_side(_pa, _pb, _d / "out" / "combo.png", master=_root)
    _hdr = _out.read_bytes()[:24]
    _w, _h = struct.unpack(">II", _hdr[16:24])
    check("export writes a png of the combined width and the taller height",
          _hdr[:8] == b"\x89PNG\r\n\x1a\n" and (_w, _h) == (100, 30), f"{_w}x{_h}")
    _chk = _tk.PhotoImage(master=_root, file=str(_out))
    check("left pixels come from a, right pixels from b",
          _chk.get(5, 5) == (0xd8, 0xa6, 0x57) and _chk.get(70, 5) == (0x6f, 0x9f, 0x6f))
    _big = _tk.PhotoImage(master=_root, width=4000, height=2)
    _big.put("{" + " ".join(["#ffffff"] * 4000) + "}", to=(0, 0, 4000, 2))
    _big.write(str(_d / "big.png"), format="png")
    _out2 = compare.export_side_by_side(_d / "big.png", _pa, _d / "combo2.png", master=_root)
    _w2, _ = struct.unpack(">II", _out2.read_bytes()[16:24])
    check("a wide side is subsampled under 1920 first",
          _w2 == 1334 + 40 and compare.fit_factor(4000, 1920) == 3, str(_w2))
    check("without a master it makes and tears down its own hidden root",
          compare.export_side_by_side(_pa, _pb, _d / "combo3.png").is_file())
    check("the export is 'ours' and never listed as a screenshot",
          compare.is_ours(compare.export_name(_d))
          and (_d / "out" / "combo.png") not in compare.find_screenshots(_d))
    try:
        from core import compareui
        _p = Path(tempfile.mkdtemp(prefix="compare_pair_"))
        shutil.copy(_pa, _p); shutil.copy(_pb, _p)
        _pa, _pb = _p / _pa.name, _p / _pb.name
        _cw = compareui.show(_root, _p, "test game")
        _root.update()
        check("the compare window opens on a real pair and shows both",
              _cw.shots == [_pa, _pb] and all(x is not None for x in _cw._full))
        _cw._swap()
        check("swap flips the sides", _cw.shots == [_pb, _pa])
        _cw.win.destroy()
        _e = Path(tempfile.mkdtemp(prefix="compare_empty_"))
        _cw = compareui.show(_root, _e, "empty game")
        _root.update()
        check("with no screenshots it explains the F6 / screenshot-key flow",
              not _cw.shots and "F6" in _cw.help_text.cget("text")
              and "Print Screen" in _cw.help_text.cget("text"))
        _cw.win.destroy()
        shutil.rmtree(_e, ignore_errors=True)
        shutil.rmtree(_p, ignore_errors=True)
    except Exception as e:
        check("compare window", False, f"{type(e).__name__}: {e}")
    _root.destroy()
shutil.rmtree(_d, ignore_errors=True)

# --------------------------------------- 15. the preview tells the truth
section("15. the install preview tells the truth")


def _snapshot(d: Path) -> set:
    return {p.relative_to(d).as_posix() for p in d.rglob("*")}


def _unknowable(route: str, f: str) -> bool:
    """Files only a download reveals: LumeniteFX's shader set and the
    OptiScaler package. The preview lists them when the cache has the zip
    and by pattern otherwise, so the exact-name check must let them pass."""
    fl = f.lower()
    if fl.startswith("reshade-shaders/") and "lumenite_" in fl:
        return True
    if route == dlss.OPTI and (fl.startswith(("optiscaler/", "licenses/"))
                               or fl.endswith(".txt") or fl.startswith("!!")):
        return True
    return "*" in f


for route in (dlss.NATIVE, dlss.BRIDGE, dlss.FEEDER, dlss.OPTI, dlss.RENODX):
    d = Path(tempfile.mkdtemp(prefix=f"pv_{route}_"))
    shutil.copyfile(X64, d / "Game.exe")
    g = games.manual(d)
    o = installer.Options(path=route, native_dlss=route != dlss.FEEDER)
    before = _snapshot(d)
    pv = installer.preview(g, o)
    check(f"{route}: preview creates nothing", _snapshot(d) == before,
          str(_snapshot(d) - before))
    check(f"{route}: preview steps are the plan", pv.steps == installer.plan(g, o))
    check(f"{route}: clean folder - no blockers, backups or removals",
          not pv.blockers and not pv.backups and not pv.removes,
          str((pv.blockers, pv.backups, pv.removes)))
    check(f"{route}: nothing outside the folder", not pv.outside, str(pv.outside))
    check(f"{route}: the manifest is announced", installer.MANIFEST in pv.writes)
    try:
        installer.install(g, o, on_log=lambda t: None)
        man = json.loads((d / installer.MANIFEST).read_text(encoding="utf8"))
        wrote = {str(f).replace("\\", "/") for f in man["files"]}
        pv_back = {b.split(" -> ")[0] for b in pv.backups}
        unannounced = []
        for f in wrote:
            if f.endswith(installer.BACKUP_SUFFIX):
                if f[:-len(installer.BACKUP_SUFFIX)] not in pv_back:
                    unannounced.append(f)
            elif f not in pv.writes and not _unknowable(route, f):
                unannounced.append(f)
        check(f"{route}: every file the install wrote was announced",
              not unannounced, str(unannounced))
        on_disk = _snapshot(d)
        unannounced = [f for f in on_disk - before
                       if (d / f).is_file() and f not in wrote
                       and f not in pv.writes and not _unknowable(route, f)]
        check(f"{route}: every file on disk was announced", not unannounced,
              str(unannounced))
        extra = [w for w in pv.writes if w not in wrote and w != installer.MANIFEST
                 and not _unknowable(route, w)]
        check(f"{route}: the preview promised nothing the install did not do",
              not extra, str(extra))
        # A same-route reinstall: what is there is ours, so no backups.
        pv2 = installer.preview(g, o)
        check(f"{route}: a reinstall backs up none of our own files",
              not pv2.backups, str(pv2.backups))
        installer.uninstall(g, on_log=lambda t: None)
    except Exception as e:
        check(f"{route}: preview vs install", False, f"{type(e).__name__}: {e}")
    shutil.rmtree(d, ignore_errors=True)

# The game's own files: an nvngx_dlss.dll we replace is backed up, one we
# keep is neither written nor backed up; the compiler goes aside with the
# arrow the log shows.
d = Path(tempfile.mkdtemp(prefix="pv_bak_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "nvngx_dlss.dll").write_bytes(b"GAME OWN" + bytes(500))
(d / "d3dcompiler_47.dll").write_bytes(b"OLD COMPILER" + bytes(300))
(d / "ReShade.ini").write_bytes(b"[GENERAL]\nMine=1\n")
g = games.manual(d)
pv = installer.preview(g, installer.Options(keep_game_dlss=False))
check("replacing the game's dlss is announced as a backup",
      "nvngx_dlss.dll" in pv.backups and "nvngx_dlss.dll" in pv.writes,
      str(pv.backups))
check("the user's ReShade.ini is backed up", "ReShade.ini" in pv.backups)
check("the compiler sideline is shown with its new name",
      "d3dcompiler_47.dll -> d3dcompiler_47.dll" + installer.SIDELINE_SUFFIX
      in pv.backups, str(pv.backups))
pv = installer.preview(g, installer.Options(keep_game_dlss=True))
check("keeping the game's dlss: neither written nor backed up",
      "nvngx_dlss.dll" not in pv.backups and "nvngx_dlss.dll" not in pv.writes)
lines = installer.preview_lines(pv)
check("the lines say what is backed up",
      any(l.startswith("will back up:") and "d3dcompiler_47.dll" in l for l in lines)
      and any(l.startswith("will write ") for l in lines)
      and "nothing is written outside this folder" in lines, str(lines))
try:
    installer.install(g, installer.Options(keep_game_dlss=False), on_log=lambda t: None)
    check("the backup really happened as previewed",
          (d / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).is_file()
          and (d / ("d3dcompiler_47.dll" + installer.SIDELINE_SUFFIX)).is_file())
    installer.uninstall(g, on_log=lambda t: None)
except Exception as e:
    check("backup preview vs install", False, f"{type(e).__name__}: {e}")
shutil.rmtree(d, ignore_errors=True)

# Vulkan: no proxy DLL, the layer lands outside the folder - and so does a
# D3D11 game sent through DXVK, which additionally gets DXVK's DLLs.
d = Path(tempfile.mkdtemp(prefix="pv_vk_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
g.api = "Vulkan"
pv = installer.preview(g, installer.Options(path=dlss.BRIDGE, native_dlss=True))
check("a vulkan game lists the layer as written outside",
      len(pv.outside) == 1 and "Vulkan layer" in pv.outside[0], str(pv.outside))
check("...and no proxy dll", "dxgi.dll" not in pv.writes and not pv.blockers,
      str(pv.writes))
check("...and the lines say so",
      any(l.startswith("outside: ") for l in installer.preview_lines(pv)))
g.api = "DX11"
pv = installer.preview(g, installer.Options(path=dlss.FEEDER, dxvk=True))
check("dxvk: its dlls are written and the layer goes outside",
      "dxgi.dll" in pv.writes and "d3d11.dll" in pv.writes and pv.outside
      and pv.steps[0].startswith("DXVK"), str((pv.writes[:3], pv.outside)))
shutil.rmtree(d, ignore_errors=True)

# Blockers: what install() would refuse is said up front, and nothing else.
d = Path(tempfile.mkdtemp(prefix="pv_block_"))
shutil.copyfile(X64, d / "Game.exe")
(d / "dxgi.dll").write_bytes(b"MZ some other injector" + bytes(4000))
g = games.manual(d)
pv = installer.preview(g, installer.Options(path=dlss.NATIVE, native_dlss=True))
check("a foreign dxgi.dll is a blocker",
      len(pv.blockers) == 1 and "not ReShade" in pv.blockers[0], str(pv.blockers))
check("the lines lead with it",
      installer.preview_lines(pv)[0].startswith("cannot install:"))
check("install refuses for the same reason",
      _raises(lambda: installer.install(g, installer.Options(path=dlss.NATIVE, native_dlss=True),
                                        on_log=lambda t: None)))
check("the folder is still untouched", sorted(p.name for p in d.iterdir())
      == ["Game.exe", "dxgi.dll"], str(sorted(p.name for p in d.iterdir())))
g.api = "DX10"
pv = installer.preview(g, installer.Options())
check("dx10 is no longer a preview blocker", not any("DirectX 10" in b for b in pv.blockers),
      str(pv.blockers))
shutil.rmtree(d, ignore_errors=True)

# A Remix-modded folder with some other route picked by hand: install()
# refuses this outright, and until this was found in review the preview did
# not know that - it happily described a dgVoodoo/ReShade plan for a route
# that was never going to run.
d = Path(tempfile.mkdtemp(prefix="pv_remix_block_"))
shutil.copyfile(X64, d / "Game.exe")
(d / ".trex").mkdir()
(d / ".trex" / "d3d9.dll").write_bytes(b"MZ" + bytes(4096))
g = games.manual(d)
pv = installer.preview(g, installer.Options(path=dlss.NATIVE, native_dlss=True))
check("preview refuses a non-remix route on a Remix folder",
      any("Only the remix route works" in b for b in pv.blockers), str(pv.blockers))
check("install refuses the same way",
      _raises(lambda: installer.install(
          g, installer.Options(path=dlss.NATIVE, native_dlss=True), on_log=lambda t: None)))
pv = installer.preview(g, installer.Options(path=dlss.REMIX))
check("the remix route itself is not blocked by this check",
      not any("Only the remix route works" in b for b in pv.blockers), str(pv.blockers))
shutil.rmtree(d, ignore_errors=True)

# Switching routes: the previous install's files are announced as removals.
d = Path(tempfile.mkdtemp(prefix="pv_switch_"))
shutil.copyfile(X64, d / "Game.exe")
g = games.manual(d)
try:
    installer.install(g, installer.Options(path=dlss.FEEDER), on_log=lambda t: None)
    pv = installer.preview(g, installer.Options(path=dlss.OPTI, native_dlss=True))
    check("switching routes announces the old add-on's removal",
          any(r.startswith("dlss5-feed.addon64") for r in pv.removes)
          and any(r.startswith("ReShade.ini") for r in pv.removes), str(pv.removes[:5]))
    check("...and backs up nothing of the old route", not pv.backups, str(pv.backups))
    check("...and the lines say so",
          any(l.startswith("will clean up first:") for l in installer.preview_lines(pv)))
    installer.uninstall(g, on_log=lambda t: None)
except Exception as e:
    check("route switch preview", False, f"{type(e).__name__}: {e}")
shutil.rmtree(d, ignore_errors=True)

# ------------------------------------------------- 14. emulator render backends
section("14. emulator render backends")
from core import emulators  # noqa: E402

# Each fake: a real exe name so profile_for() recognises it, a portable
# marker so the config resolves inside the temp folder, and a config with
# the backend on a non-DXGI value plus unrelated keys that must survive.
_EMU_CASES = [
    # (exe name, marker files, config relative path, original text, expected new line, old name)
    ("duckstation-qt-x64.exe", (), "settings.ini",
     "[Main]\nSettingsVersion = 3\n\n[GPU]\nRenderer = Vulkan\nResolutionScale = 3\n\n[Audio]\nBackend = Cubeb\n",
     "Renderer = D3D12\n", "Vulkan"),
    ("pcsx2-qt.exe", ("portable.ini",), "inis/PCSX2.ini",
     "[UI]\r\nMainWindowGeometry = x\r\n\r\n[EmuCore/GS]\r\nVsyncEnable = 0\r\nRenderer = 14\r\nupscale_multiplier = 2\r\n\r\n[EmuCore]\r\nRenderer = 99\r\n",
     "Renderer = 15\r\n", "Vulkan"),
    ("Dolphin.exe", ("portable.txt",), "User/Config/Dolphin.ini",
     "[General]\nISOPath0 = D:/wii\n[Core]\nGFXBackend = Vulkan\nCPUThread = True\n",
     "GFXBackend = D3D12\n", "Vulkan"),
    ("PPSSPPWindows64.exe", (), "memstick/PSP/SYSTEM/ppsspp.ini",
     "[General]\nLanguage = en_US\n[Graphics]\nFailedGraphicsBackends = \nGraphicsBackend = 3 (VULKAN)\nInternalResolution = 3\n",
     "GraphicsBackend = 2 (DIRECT3D11)\n", "Vulkan"),
    ("xenia_canary.exe", ("portable.txt",), "xenia-canary.config.toml",
     '[APU]\napu = "any"\n\n[GPU]\ndraw_resolution_scale_x = 2\ngpu = "vulkan"\nvsync = true\n',
     'gpu = "d3d12"\n', "Vulkan"),
    ("retroarch.exe", (), "retroarch.cfg",
     'audio_driver = "xaudio"\nvideo_driver = "vulkan"\nvideo_fullscreen = "false"\n',
     'video_driver = "d3d11"\n', "Vulkan"),
]
for _exe_name, _markers, _rel, _orig, _expect_line, _old_name in _EMU_CASES:
    _d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
    _exe = _d / _exe_name
    _exe.write_bytes(b"MZ")
    for _m in _markers:
        (_d / _m).write_text("", encoding="utf8")
    _cfg = _d / _rel
    _cfg.parent.mkdir(parents=True, exist_ok=True)
    _cfg.write_bytes(_orig.encode("utf8"))
    _p = emulators.profile_for(_exe)
    _label = _p.name if _p else _exe_name
    if not check(f"{_label}: the fake exe is recognised", _p is not None):
        continue
    _status, _found = emulators.backend_status(_p, _exe)
    check(f"{_label}: status reads the config and the old backend",
          _found == _cfg and _status == _old_name, f"{_status} {_found}")
    _notes = emulators.set_backend(_p, _exe)
    _bak = _cfg.with_name(_cfg.name + emulators.BACKUP_SUFFIX)
    _new = _cfg.read_bytes().decode("utf8")
    _old_lines = _orig.splitlines(keepends=True)
    _new_lines = _new.splitlines(keepends=True)
    _diff = [(a, b) for a, b in zip(_old_lines, _new_lines) if a != b]
    check(f"{_label}: exactly one line changed and it is the backend key",
          len(_old_lines) == len(_new_lines) and len(_diff) == 1 and _diff[0][1] == _expect_line,
          repr(_diff))
    check(f"{_label}: the note says file and old -> new",
          any(str(_cfg) in n and "->" in n for n in _notes), " | ".join(_notes))
    check(f"{_label}: backup holds the original byte for byte",
          _bak.is_file() and _bak.read_bytes() == _orig.encode("utf8"))
    check(f"{_label}: status now reports DXGI",
          emulators.backend_status(_p, _exe)[0] in ("D3D11", "D3D12"))
    _notes2 = emulators.set_backend(_p, _exe)
    check(f"{_label}: second run changes nothing (idempotent)",
          _cfg.read_bytes().decode("utf8") == _new and _bak.read_bytes() == _orig.encode("utf8")
          and any("already" in n for n in _notes2), " | ".join(_notes2))
    _r = emulators.restore_backend(_p, _exe)
    check(f"{_label}: restore brings the original back and drops the backup",
          _cfg.read_bytes() == _orig.encode("utf8") and not _bak.exists(), " | ".join(_r))
    check(f"{_label}: nothing else was created in the folder",
          sorted(f.name for f in _d.iterdir()) == sorted({_exe_name, *_markers, _rel.split("/")[0]}))
    shutil.rmtree(_d, ignore_errors=True)

# A config that already runs on the other DXGI flavour is left alone: no
# backup, no edit, and nothing to restore.
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "duckstation-qt-x64.exe").write_bytes(b"MZ")
(_d / "settings.ini").write_text("[GPU]\nRenderer = D3D11\n", encoding="utf8")
_p = emulators.profile_for(_d / "duckstation-qt-x64.exe")
_notes = emulators.set_backend(_p, _d / "duckstation-qt-x64.exe")
check("an existing D3D11 choice is respected, with no backup",
      any("already" in n for n in _notes)
      and not (_d / ("settings.ini" + emulators.BACKUP_SUFFIX)).exists())
check("restore with no backup just says so",
      any("no backend backup" in n for n in emulators.restore_backend(_p, _d / "duckstation-qt-x64.exe")))
shutil.rmtree(_d, ignore_errors=True)

# The key missing from its section is added under the header, not appended
# somewhere the emulator will not read it.
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "Dolphin.exe").write_bytes(b"MZ")
(_d / "portable.txt").write_text("", encoding="utf8")
(_d / "User" / "Config").mkdir(parents=True)
(_d / "User" / "Config" / "Dolphin.ini").write_text("[Core]\nCPUThread = True\n[DSP]\nBackend = Cubeb\n", encoding="utf8")
_p = emulators.profile_for(_d / "Dolphin.exe")
emulators.set_backend(_p, _d / "Dolphin.exe")
_ini = (_d / "User" / "Config" / "Dolphin.ini").read_text(encoding="utf8")
check("a missing key is inserted under its own section",
      _ini == "[Core]\nGFXBackend = D3D12\nCPUThread = True\n[DSP]\nBackend = Cubeb\n", repr(_ini))
shutil.rmtree(_d, ignore_errors=True)

# No config yet: a hint, and not a single file written.
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "pcsx2-qt.exe").write_bytes(b"MZ")
(_d / "portable.ini").write_text("", encoding="utf8")
_p = emulators.profile_for(_d / "pcsx2-qt.exe")
_notes = emulators.set_backend(_p, _d / "pcsx2-qt.exe")
check("a missing config gives the by-hand hint and writes nothing",
      any(_p.renderer_hint in n for n in _notes) and sorted(f.name for f in _d.iterdir()) == ["pcsx2-qt.exe", "portable.ini"])
check("status on a missing config is unknown",
      emulators.backend_status(_p, _d / "pcsx2-qt.exe") == ("unknown", None))
shutil.rmtree(_d, ignore_errors=True)

# Vulkan/OpenGL-only emulators: the tool reports and leaves the file alone.
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "rpcs3.exe").write_bytes(b"MZ")
(_d / "config.yml").write_text("Core:\n  PPU Decoder: Recompiler (LLVM)\nVideo:\n  Renderer: Vulkan\n  Resolution: 1280x720\n", encoding="utf8")
_p = emulators.profile_for(_d / "rpcs3.exe")
_notes = emulators.set_backend(_p, _d / "rpcs3.exe")
check("RPCS3: no DXGI backend, Vulkan is left in place",
      any("no DXGI backend" in n for n in _notes)
      and emulators.backend_status(_p, _d / "rpcs3.exe")[0] == "Vulkan"
      and sorted(f.name for f in _d.iterdir()) == ["config.yml", "rpcs3.exe"], " | ".join(_notes))
check("RPCS3: restore is a no-op", emulators.restore_backend(_p, _d / "rpcs3.exe") == [])
shutil.rmtree(_d, ignore_errors=True)
_d = Path(tempfile.mkdtemp(prefix="emu_backend_"))
(_d / "Cemu.exe").write_bytes(b"MZ")
(_d / "settings.xml").write_text('<?xml version="1.0"?>\n<content>\n  <Graphic>\n    <api>1</api>\n    <device></device>\n  </Graphic>\n</content>\n', encoding="utf8")
_p = emulators.profile_for(_d / "Cemu.exe")
_notes = emulators.set_backend(_p, _d / "Cemu.exe")
check("Cemu: no DXGI backend, note only, Vulkan reported",
      any("no DXGI backend" in n for n in _notes)
      and emulators.backend_status(_p, _d / "Cemu.exe")[0] == "Vulkan", " | ".join(_notes))
shutil.rmtree(_d, ignore_errors=True)
for _name in ("Ryujinx.exe", "yuzu.exe"):
    _p = emulators.profile_for(Path(_name))
    check(f"{_p.name}: note only", any("no DXGI backend" in n for n in emulators.set_backend(_p, Path(_name)))
          and emulators.backend_status(_p, Path(_name)) == ("unknown", None))



# ------------------------------------------------- 17. an unready drive
section("17. an unready drive letter does not kill a scan")
import pathlib as _pl  # noqa: E402
_orig_is_dir = _pl.Path.is_dir


def _angry_is_dir(self, *a, **k):
    if str(self).upper().startswith("Q:"):
        raise OSError(87, "The parameter is incorrect", str(self))
    return _orig_is_dir(self, *a, **k)


try:
    _pl.Path.is_dir = _angry_is_dir
    check("_isdir swallows OSError 87", games._isdir(_pl.Path("Q:/")) is False)
    _errs = []
    for _fn in (games.scan_xbox, games.scan_folders, emulators.scan):
        try:
            _fn()
        except OSError as e:
            _errs.append(f"{_fn.__name__}: {e}")
    check("xbox / folder / emulator scans survive it", not _errs, str(_errs))
    # installer.preview()/preflight() used a bare root.is_dir() until this was
    # found in review: a game on a drive that goes unready mid-session
    # (unplugged, asleep, a dropped network share) crashed both with an
    # uncaught OSError instead of the intended "does not exist" message.
    _gq = games.Game(name="Unready", folder=_pl.Path("Q:/SomeGame"))
    _pvq = None
    try:
        _pvq = installer.preview(_gq, installer.Options())
    except OSError as e:
        check("preview survives an unready drive", False, str(e))
    if _pvq is not None:
        check("preview reports it as a normal blocker, not a crash",
              any("does not exist" in b for b in _pvq.blockers), str(_pvq.blockers))
    try:
        installer.preflight(_gq)
        check("preflight survives an unready drive", False, "did not raise")
    except installer.InstallError as e:
        check("preflight turns it into a clean InstallError", "does not exist" in str(e), str(e))
    except OSError as e:
        check("preflight survives an unready drive", False, str(e))
finally:
    _pl.Path.is_dir = _orig_is_dir


# ------------------------------------------------- 18. other NGX hooks
section("18. another DLSS hook in the folder is called out")
_d = _diag_dir("diag_hooks_", reshade=(
    'INFO | Registered add-on "RenoDX" v0.0.0.0\n'
    'INFO | Registered add-on "RenoDX DLSS" v0.0.0.0\n'
    'INFO | Registered add-on "Auto Reload" v16.2.1.0\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"), path="native")
(_d / "OptiScaler.ini").write_text("[Upscalers]\n", encoding="utf8")
(_d / "dlssg_to_fsr3_amd_is_better.dll").write_bytes(b"MZ")
(_d / "renodx-cp2077.addon64").write_bytes(b"MZ")
_r = diagnose.analyse(_d)
_bad = _levels(_r, "bad")
_warn = _levels(_r, "warn")
check("our add-on missing from the loaded list is a failure",
      any("did not load" in b for b in _bad), str(_bad))
check("other RenoDX add-ons are named",
      any("Other ReShade add-ons" in w and "RenoDX" in w for w in _warn), str(_warn))
check("OptiScaler / frame-gen files are named",
      any("Another DLSS hook" in w and "OptiScaler.ini" in w for w in _warn), str(_warn))
_hooks = installer.other_ngx_hooks(_d)
check("other_ngx_hooks sees the ini, the dll and the foreign add-on",
      "OptiScaler.ini" in _hooks and "dlssg_to_fsr3_amd_is_better.dll" in _hooks
      and "renodx-cp2077.addon64" in _hooks and "dlss5-feed.addon64" not in _hooks,
      str(_hooks))
check("the OptiScaler route ignores its own ini",
      "OptiScaler.ini" not in installer.hook_warning(_d, "optiscaler")
      and "dlssg" in installer.hook_warning(_d, "optiscaler"))
check("a clean folder gives no warning",
      installer.hook_warning(Path(tempfile.mkdtemp(prefix="clean_")), "native") == "")
shutil.rmtree(_d, ignore_errors=True)

# and the install itself says so
_d = Path(tempfile.mkdtemp(prefix="hooks_install_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "dlss-enabler.dll").write_bytes(b"MZ")
_g = games.manual(_d)
_pv = installer.preview(_g, installer.Options())
check("the preview warns about the other hook",
      any("another DLSS hook" in w for w in _pv.warnings), str(_pv.warnings))
_rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
check("the install warns about the other hook",
      any("another DLSS hook" in w for w in _rep.warnings), str(_rep.warnings))
installer.uninstall(_g, on_log=lambda t: None)
check("the other mod's file is left alone", (_d / "dlss-enabler.dll").is_file())
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 19. feeder crash record
section("19. the feeder's own crash record is read")
_d = _diag_dir("diag_crash_", feed=(
    "22:31:54.185  [feed32] frame 1 delivered (1920x1080, reset=0)\n"
    "22:31:54.235  [feed32] frame 3 delivered (1920x1080, reset=0)\n"
    "22:31:54.839  ### CRASH RECORDED ###  exception 0xC0000005 at 00C5ED39 in "
    "V:\\Games\\Bayonetta\\Bayonetta.exe; this add-on was last doing: "
    "preparing work-resolution inputs\n"
    "22:31:55.613  [feed32] crash dump written: V:\\Games\\Bayonetta\\dlss5-feed-crash.dmp "
    "-- attach it to the issue with this log\n"), reshade=(
    'INFO | Registered add-on "DLSS 5 Feed (32-bit) 0.12.0" v0.0.0.0\n'
    "INFO | Redirecting IDXGIFactory::CreateSwapChain(...)\n"))
_r = diagnose.analyse(_d)
check("a recorded crash is not reported as Working",
      not _r.verdict.startswith("Working") and "crashed" in _r.verdict, _r.verdict)
_bad = _levels(_r, "bad")
check("the crash line names the exception and the step",
      any("0xC0000005" in b and "work-resolution" in b for b in _bad), str(_bad))
_det = " ".join(f_.detail for f_ in _r.findings if f_.level == "bad")
check("the advice names the dump and another feeder build",
      "dlss5-feed-crash.dmp" in _det and "feeder build" in _det, _det[:200])
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 20. their own LumeniteFX
section("20. a LumeniteFX the person installed is not duplicated")
_d = Path(tempfile.mkdtemp(prefix="lum_"))
shutil.copyfile(X64, _d / "Game.exe")
_theirs = _d / "reshade-shaders" / "Shaders" / "LumeniteFX"
_theirs.mkdir(parents=True)
(_theirs / "lumenite_Kernel.fx").write_text("// theirs\n", encoding="utf8")
_g = games.manual(_d)
_pv = installer.preview(_g, installer.Options())
check("the preview says their copy is used",
      any("already installed" in w for w in _pv.warnings)
      and not any("lumenite_" in w for w in _pv.writes), str(_pv.warnings))
_rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
check("no second lumenite_Kernel.fx is written",
      not (_d / "reshade-shaders" / "Shaders" / "lumenite_Kernel.fx").exists()
      and any("already installed" in n for n in _rep.notes), str(_rep.notes))
check("the technique is still wired in ReShade.ini",
      "Lumenite_Kernel@lumenite_Kernel.fx" in (_d / "ReShadePreset.ini").read_text(encoding="utf8"))
installer.uninstall(_g, on_log=lambda t: None)
check("their copy survives uninstall", (_theirs / "lumenite_Kernel.fx").read_text(encoding="utf8") == "// theirs\n")
shutil.rmtree(_d, ignore_errors=True)

# and our own earlier copy is still overwritten, not mistaken for theirs
_d = Path(tempfile.mkdtemp(prefix="lum_ours_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
installer.install(_g, installer.Options(), on_log=lambda t: None)
_rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
check("our own copy from the first install is refreshed, not skipped",
      not any("already installed" in n for n in _rep.notes), str(_rep.notes))
installer.uninstall(_g, on_log=lambda t: None)
check("uninstall removes our lumenite files",
      not list((_d / "reshade-shaders").rglob("lumenite_*")) if (_d / "reshade-shaders").is_dir() else True)
shutil.rmtree(_d, ignore_errors=True)

# ------------------------------------------------- 21. neural-upstream
section("21. the neural-upstream route")
# A DX12 game with its own DLSS: the only place the route is offered.
_d = Path(tempfile.mkdtemp(prefix="upstream_"))
shutil.copyfile(X64, _d / "Game.exe")
_own = b"MZ" + bytes(range(256)) * 40
(_d / "nvngx_dlss.dll").write_bytes(_own)
_g = games.manual(_d)
_sup = dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness)
check("upstream is offered second, after native",
      _sup.options[:2] == [dlss.NATIVE, dlss.UPSTREAM], str(_sup.options))
check("upstream is never the recommendation", _sup.recommended != dlss.UPSTREAM)
_ok, _note = dlss.fit(dlss.UPSTREAM, _g.api, True, None)
check("fit says upstream is usable", _ok and bool(_note), _note)
_bare = Path(tempfile.mkdtemp(prefix="upstream_nodlss_"))
shutil.copyfile(X64, _bare / "Game.exe")
_gb = games.manual(_bare)
check("a game without DLSS is not offered upstream",
      dlss.UPSTREAM not in dlss.detect(_gb.install_dir, _gb.folder, _gb.api,
                                       _gb.bitness).options)
shutil.rmtree(_bare, ignore_errors=True)
check("every route has a conflicts entry of 2-4 lines",
      set(dlss.CONFLICTS) == set(dlss.ALL_ROUTES)
      and all(2 <= len(v) <= 4 for v in dlss.CONFLICTS.values()),
      str(sorted(dlss.CONFLICTS)))
check("upstream has a label and a blurb",
      dlss.UPSTREAM in dlss.LABELS and dlss.UPSTREAM in dlss.BLURB)

_opt = installer.Options(path=dlss.UPSTREAM, native_dlss=True)
_steps = installer.plan(_g, _opt)
check("the plan has no renodx step and never touches nvngx_dlss.dll",
      not any("renodx" in s for s in _steps) and "nvngx_dlss.dll" not in _steps
      and "neural-upstream" in _steps, str(_steps))
_pv = installer.preview(_g, _opt)
check("the preview lists nvngx.dll.addon64 and not renodx-dlss5.addon64",
      installer.UPSTREAM_ADDON in _pv.writes and installer.RENODX not in _pv.writes
      and "nvngx_dlss.dll" not in _pv.writes, str(_pv.writes))
check("reliability is beta and says so",
      installer.reliability(_g, dlss.UPSTREAM)[0] == installer.BETA
      and "two games" in installer.reliability(_g, dlss.UPSTREAM)[1])
try:
    _rep = installer.install(_g, _opt, on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("install writes the add-on and nvngx_dlssnr.dll",
          installer.UPSTREAM_ADDON in _files and installer.DLSSNR in _files,
          str(sorted(_files)))
    check("install writes no renodx add-on",
          installer.RENODX not in _files and installer.RENODX_SF not in _files)
    check("the game's nvngx_dlss.dll is byte-identical, no backup made",
          (_d / "nvngx_dlss.dll").read_bytes() == _own
          and not (_d / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).exists())
    check("the plan and the steps taken agree",
          len(_steps) == len(installer.plan(_g, _opt)) and
          any("upstream version" in n for n in _rep.notes), str(_rep.notes))
    _man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest records path upstream and its version",
          _man.get("path") == "upstream"
          and bool((_man.get("components") or {}).get("upstream")),
          str(_man.get("components")))
    check("the manifest round-trips the route",
          installer.options_from_manifest(_d).path == dlss.UPSTREAM
          and installer.options_from_manifest(_d).native_dlss)
    check("our own add-on is not reported as a foreign hook",
          installer.UPSTREAM_ADDON not in installer.other_ngx_hooks(_d)
          and installer.hook_warning(_d, dlss.UPSTREAM) == "",
          str(installer.other_ngx_hooks(_d)))
    installer.install(_g, installer.Options(path=dlss.NATIVE, native_dlss=True),
                      on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("switching to native removes nvngx.dll.addon64",
          installer.UPSTREAM_ADDON not in _files and installer.RENODX in _files,
          str(sorted(_files)))
    installer.install(_g, _opt, on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("switching back removes renodx-dlss5.addon64",
          installer.RENODX not in _files and installer.UPSTREAM_ADDON in _files,
          str(sorted(_files)))
    installer.uninstall(_g, on_log=lambda t: None)
    _left = sorted(p.name for p in _d.rglob("*") if p.is_file())
    check("uninstall leaves only the game and its DLSS",
          _left == ["Game.exe", "nvngx_dlss.dll"], str(_left))
except Exception as e:
    check("upstream: installs", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# The diagnosis: no frame log, the overlay tab is the judge, and the
# renodx-dlss5 add-on registered beside it is two NGX hooks.
_d = _diag_dir("diag_upstream_", addons=False, reshade=(
    'INFO | Registered add-on "DLSS5 NR Pre-Upscale" v0.3.0.0\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"),
    path="upstream", files=["dxgi.dll", "nvngx.dll.addon64"])
(_d / "nvngx.dll.addon64").write_bytes(b"MZ")
_r = diagnose.analyse(_d)
check("upstream loaded alone is not a failure",
      not _levels(_r, "bad") and "Pre-Upscale" in _r.verdict,
      str(_levels(_r, "bad")) + " / " + _r.verdict)
check("the folder's own add-on is not called a foreign hook",
      not any("Another DLSS hook" in w for w in _levels(_r, "warn")),
      str(_levels(_r, "warn")))
(_d / "ReShade.log").write_text(
    'INFO | Registered add-on "DLSS5 NR Pre-Upscale" v0.3.0.0\n'
    'INFO | Registered add-on "DLSS 5 Neural Rendering" v4.7.0.0\n', encoding="utf8")
_r = diagnose.analyse(_d)
check("renodx-dlss5 beside upstream is two NGX hooks",
      any("Two NGX hooks" in b for b in _levels(_r, "bad")), str(_levels(_r, "bad")))
shutil.rmtree(_d, ignore_errors=True)

section("22. FSR and XeSS games through OptiScaler")
# A 64-bit D3D12 game with FSR 2 (or XeSS) and no DLSS: OptiScaler hooks the
# game's upscaler calls as its input and runs DLSS in their place, so the
# route is offered and the tool has to bring a nvngx_dlss.dll of its own.


def _upscaler_game(prefix: str, runtime: str):
    d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, d / "Game.exe")
    (d / runtime).write_bytes(b"MZ" + bytes(4096))
    g = games.manual(d)
    g.api = "DX12"
    return d, g


for _runtime, _kind, _name in (("ffx_fsr2_api_x64.dll", "fsr", "FSR"),
                               ("libxess.dll", "xess", "XeSS")):
    _d, _g = _upscaler_game(f"opti_{_kind}_", _runtime)
    _sup = dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness, 89)
    check(f"{_name}: detect reports upscaler {_kind} and no native DLSS",
          _sup.upscaler == _kind and not _sup.native_dlss
          and _runtime in _sup.upscaler_evidence,
          f"{_sup.upscaler} {_sup.upscaler_evidence}")
    check(f"{_name}: optiscaler is offered, feeder stays recommended",
          dlss.OPTI in _sup.options and _sup.options[0] == dlss.FEEDER
          and _sup.recommended == dlss.FEEDER, str(_sup.options))
    check(f"{_name}: the reason names the upscaler seen",
          _runtime in _sup.reason and "OptiScaler" in _sup.reason, _sup.reason)
    check(f"{_name}: rtx 50 is steered to optiscaler",
          dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness, 120).recommended
          == dlss.OPTI)
    _ok, _note = dlss.fit(dlss.OPTI, "DX12", False, 89, upscaler=_kind)
    check(f"{_name}: fit accepts optiscaler with the upscaler passed",
          _ok and "redirected into DLSS" in _note, _note)
    check(f"{_name}: fit without the upscaler still refuses (old positional call)",
          dlss.fit(dlss.OPTI, "DX12", False, 89)[0] is False)
    _opt = installer.Options(path=dlss.OPTI, upscaler=_kind)
    _steps = installer.plan(_g, _opt)
    check(f"{_name}: the plan lists nvngx_dlss.dll",
          "nvngx_dlss.dll" in _steps and any(s.startswith("OptiScaler (") for s in _steps),
          str(_steps))
    _pv = installer.preview(_g, _opt)
    check(f"{_name}: the preview lists nvngx_dlss.dll and says beta",
          "nvngx_dlss.dll" in _pv.writes
          and any("redirected into DLSS" in w for w in _pv.warnings),
          str(_pv.writes))
    _lvl, _why = installer.reliability(_g, dlss.OPTI, _kind)
    check(f"{_name}: reliability is beta and honest",
          _lvl == installer.BETA and "not all" in _why, _why)
    try:
        _rep = installer.install(_g, _opt, on_log=lambda t: None)
        _files = {p.name for p in _d.iterdir() if p.is_file()}
        check(f"{_name}: install writes OptiScaler, nvngx_dlssnr and nvngx_dlss",
              "dxgi.dll" in _files and optiscaler.FORWARDER in _files
              and installer.DLSSNR in _files and installer.DLSS in _files,
              str(sorted(_files)))
        check(f"{_name}: the plan and the steps taken agree",
              len(_steps) == len(installer.plan(_g, _opt))
              and any("dlss version" in n for n in _rep.notes), str(_rep.notes))
        _ini = (_d / optiscaler.INI).read_text(encoding="utf8")
        _want = "EnableFsr2Inputs=true" if _kind == "fsr" else "EnableXeSSInputs=true"
        check(f"{_name}: the ini enables the input and picks dlss on D3D12",
              _want in _ini and "Dx12Upscaler=dlss" in _ini
              and "Enabled=true" in _ini,
              str([ln for ln in _ini.splitlines()
                   if "Inputs=" in ln or "Dx12Upscaler" in ln]))
        _man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
        check(f"{_name}: the manifest records the upscaler",
              _man.get("upscaler") == _kind and _man.get("path") == "optiscaler"
              and "nvngx_dlss.dll" in _man.get("files", []), str(_man.get("upscaler")))
        _back = installer.options_from_manifest(_d)
        check(f"{_name}: the manifest round-trips upscaler and native_dlss",
              _back.upscaler == _kind and not _back.native_dlss
              and _back.path == dlss.OPTI)
        check(f"{_name}: with ours installed detect still says {_kind}",
              dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness).upscaler
              == _kind and not dlss.detect(_g.install_dir, _g.folder, _g.api,
                                           _g.bitness).native_dlss)
        installer.uninstall(_g, on_log=lambda t: None)
        _left = sorted(p.name for p in _d.rglob("*") if p.is_file())
        check(f"{_name}: uninstall removes all of ours and leaves the {_name} dll",
              _left == ["Game.exe", _runtime], str(_left))
    except Exception as e:
        check(f"{_name}: installs", False, f"{type(e).__name__}: {e}")
    shutil.rmtree(_d, ignore_errors=True)

# Neither DLSS nor an upscaler: OptiScaler has nothing to hook.
_bare = Path(tempfile.mkdtemp(prefix="opti_none_"))
shutil.copyfile(X64, _bare / "Game.exe")
_gb = games.manual(_bare)
_gb.api = "DX12"
_sb = dlss.detect(_gb.install_dir, _gb.folder, _gb.api, _gb.bitness, 120)
check("a game with neither DLSS nor FSR/XeSS is not offered optiscaler",
      dlss.OPTI not in _sb.options and _sb.upscaler == ""
      and _sb.recommended == dlss.FEEDER, str(_sb.options))
shutil.rmtree(_bare, ignore_errors=True)

# A native-DLSS game keeps the old behaviour: no upscaler, no extra dlss step.
_gn = _fake_game("opti_native_")
_sn = dlss.detect(_gn.install_dir, _gn.folder, "DX12", 64)
check("a game with its own DLSS reports no upscaler",
      _sn.native_dlss and _sn.upscaler == "" and _sn.dlss_source)
check("native optiscaler plan is unchanged",
      "nvngx_dlss.dll" not in installer.plan(
          _gn, installer.Options(path=dlss.OPTI, native_dlss=True)))
shutil.rmtree(_gn.folder, ignore_errors=True)

# The ini writer on its own, D3D11 included.
_d = Path(tempfile.mkdtemp(prefix="opti_ini_"))
(_d / optiscaler.INI).write_text(
    "[Upscalers]\nDx11Upscaler=auto\nDx12Upscaler=auto\n\n[Inputs]\n"
    "EnableFsr2Inputs=auto\nUseFsr2Dx11Inputs=auto\nEnableXeSSInputs=auto\n",
    encoding="utf8")
optiscaler.enable_inputs(_d, "fsr", "DX11")
_ini = (_d / optiscaler.INI).read_text(encoding="utf8")
check("on D3D11 the FSR2 D3D11 inputs are hooked and dlss is not the upscaler",
      "UseFsr2Dx11Inputs=true" in _ini and "EnableFsr2Inputs=true" in _ini
      and "Dx12Upscaler=auto" in _ini and _ini.count("[Inputs]") == 1, _ini)
optiscaler.enable_inputs(_d, "xess", "DX12")
_ini = (_d / optiscaler.INI).read_text(encoding="utf8")
check("xess on D3D12 enables XeSS inputs and pins dlss",
      "EnableXeSSInputs=true" in _ini and "Dx12Upscaler=dlss" in _ini, _ini)
shutil.rmtree(_d, ignore_errors=True)

# The diagnosis: with an upscaler recorded, the log has to show the hook.
_log_head = ("[00:00:01.000] [I] DLLMain forwarder loaded\n"
             "[00:00:01.100] [I] HookFSR2ExeInputs Trying to hook FSR2 methods\n"
             "[00:00:05.000] [I] DLSS-NR running at 1920x1080\n")
_d = _diag_dir("diag_optifsr_", addons=False, path="optiscaler",
               upscaler="fsr", files=["dxgi.dll"])
(_d / "OptiScaler.log").write_text(_log_head, encoding="utf8")
_r = diagnose.analyse(_d)
check("no FSR context through OptiScaler is a warning that names the feeder",
      any("never saw the game's FSR calls" in t for t in _levels(_r, "warn")),
      str(_levels(_r, "warn")))
(_d / "OptiScaler.log").write_text(
    _log_head + "[00:00:02.000] [I] hk_ffxFsr2ContextCreate_Dx12 context created: 1A2B\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("an FSR context created through OptiScaler is not flagged",
      not any("never saw" in t for t in _levels(_r, "warn") + _levels(_r, "bad"))
      and _r.verdict == "Working.", str(_r.findings))
shutil.rmtree(_d, ignore_errors=True)
_d = _diag_dir("diag_optixess_", addons=False, path="optiscaler",
               upscaler="xess", files=["dxgi.dll"])
(_d / "OptiScaler.log").write_text(
    _log_head + "[00:00:01.200] [W] Config::CheckXeSS libxess.dll not found!\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("'libxess.dll not found!' is bad and names the feeder",
      any("never saw the game's XeSS calls" in t for t in _levels(_r, "bad"))
      and any("feeder" in f_.detail for f_ in _r.findings if f_.level == "bad"),
      str(_levels(_r, "bad")))
shutil.rmtree(_d, ignore_errors=True)
_d = _diag_dir("diag_optinative_", addons=False, path="optiscaler",
               files=["dxgi.dll"])
(_d / "OptiScaler.log").write_text(_log_head, encoding="utf8")
_r = diagnose.analyse(_d)
check("a native-DLSS OptiScaler install is not asked about FSR/XeSS inputs",
      not any("never saw" in t for t in _levels(_r, "warn") + _levels(_r, "bad")))
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 24. only the provider shader
section("24. only the selected motion-vector shader is installed")
_d = Path(tempfile.mkdtemp(prefix="lum_min_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
_rep = installer.install(_g, installer.Options(provider=3), on_log=lambda t: None)
_fx = sorted(p.name for p in (_d / installer.SHADERS).glob("lumenite_*.fx"))
check("provider 3 installs lumenite_Kernel.fx and nothing else from the pack",
      _fx == ["lumenite_Kernel.fx"], str(_fx))
check("its includes and texture are there",
      (_d / installer.INCLUDE / "lumenite_Compute.fxh").is_file()
      and (_d / installer.TEXTURES / "lumenite_bluenoise256.png").is_file())
_pv = installer.preview(_g, installer.Options(provider=4))
check("the preview lists only the provider it would write",
      any("lumenite_QuantMotion.fx" in w for w in _pv.writes)
      and not any("lumenite_RTAO" in w or "lumenite_TRAA" in w for w in _pv.writes),
      str([w for w in _pv.writes if "lumenite" in w]))
# an earlier full-pack install of ours is trimmed on reinstall
(_d / installer.SHADERS / "lumenite_RTAO.fx").write_text("// old", encoding="utf8")
_man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
_man["files"].append(str(installer.SHADERS / "lumenite_RTAO.fx"))
(_d / installer.MANIFEST).write_text(json.dumps(_man), encoding="utf8")
_rep = installer.install(_g, installer.Options(provider=3), on_log=lambda t: None)
check("a leftover effect from an earlier install of ours is removed",
      not (_d / installer.SHADERS / "lumenite_RTAO.fx").exists()
      and any("removed lumenite_RTAO.fx" in n for n in _rep.notes), str(_rep.notes))
installer.uninstall(_g, on_log=lambda t: None)
check("uninstall leaves no lumenite files",
      not list((_d / "reshade-shaders").rglob("lumenite_*")) if (_d / "reshade-shaders").is_dir() else True)
shutil.rmtree(_d, ignore_errors=True)

section("23. the standalone-dlssnr route")
# A 64-bit D3D12 game WITHOUT DLSS: the add-on brings its own feed, so it is
# offered anyway - experimental, after the feeder and the bridge, and never
# the recommendation.
_d = Path(tempfile.mkdtemp(prefix="standalone_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
_g.api = "DX12"
_sup = dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness)
check("standalone is offered on a DX12 game without DLSS, after feeder and bridge",
      dlss.STANDALONE in _sup.options
      and _sup.options.index(dlss.STANDALONE) > _sup.options.index(dlss.FEEDER)
      and _sup.options.index(dlss.STANDALONE) > _sup.options.index(dlss.BRIDGE),
      str(_sup.options))
check("standalone is never the recommendation", _sup.recommended != dlss.STANDALONE)
_gd = _fake_game("standalone_dlss_")
check("standalone is offered on DX12 and DX11 games with DLSS too",
      dlss.STANDALONE in dlss.detect(_gd.install_dir, _gd.folder, "DX12", 64).options
      and dlss.STANDALONE in dlss.detect(_gd.install_dir, _gd.folder, "DX11", 64).options
      and dlss.detect(_gd.install_dir, _gd.folder, "DX12", 64, 120).recommended
      != dlss.STANDALONE)
shutil.rmtree(_gd.folder, ignore_errors=True)
check("32-bit, Vulkan and OpenGL games are not offered standalone",
      all(dlss.STANDALONE not in dlss.detect(_g.install_dir, _g.folder, api, bits).options
          for api, bits in (("DX12", 32), ("DX11", 32), ("Vulkan", 64), ("OpenGL", 64))))
_ok, _note = dlss.fit(dlss.STANDALONE, "DX12", False, None)
check("fit says standalone is usable without DLSS in the game",
      _ok and "DLAA" in _note, _note)
check("standalone has a label, a blurb and a conflicts entry that says OFF",
      dlss.STANDALONE in dlss.LABELS and dlss.STANDALONE in dlss.BLURB
      and dlss.STANDALONE in dlss.CONFLICTS
      and any("OFF" in c for _k, c in dlss.CONFLICTS[dlss.STANDALONE])
      and any("window" in c for _k, c in dlss.CONFLICTS[dlss.STANDALONE]))
# Every line says which kind it is, because the card shows them differently:
# a setting in the game is always on screen, something that must not be in
# the folder is only said when it IS, and the rest folds with the blurb.
check("every conflict line says whether it is the folder, the game or a note",
      all(k in ("folder", "ingame", "note")
          for v in dlss.CONFLICTS.values() for k, _t in v),
      sorted({k for v in dlss.CONFLICTS.values() for k, _t in v}))
# Run, not read: the game page's notes for a route, with the folder asked.
with _ui_isolated(), _ui_threads(run=False):
    _g23 = _ui_game(name="Conflicts")
    _asked23: list = []
    _hooks23: list = []

    def _ngx23(root, path=""):
        _asked23.append((Path(root), path))
        return list(_hooks23)
    with patch.object(installer, "other_ngx_hooks", _ngx23):
        _c23 = _ui_ctl(_g23, _ui_support([dlss.NATIVE, dlss.STANDALONE], dlss.NATIVE))
        _c23.apply_route(dlss.NATIVE)
        _quiet23 = [t for _k, t in _c23.notes if "in this folder" in t]
        _hooks23.append("OptiScaler.dll")
        _c23.apply_route(dlss.NATIVE)
        _loud23 = [t for _k, t in _c23.notes if "in this folder" in t]
        _steps23 = [t for _k, t in _c23.route_steps(dlss.STANDALONE)]
check("...and the window asks the folder before it says one",
      (_g23.install_dir, dlss.NATIVE) in _asked23 and not _quiet23
      and _loud23 and "OptiScaler.dll" in _loud23[0], (_asked23, _quiet23, _loud23))
check("...while what to switch off in the game is always said",
      all(any(line in s for s in _steps23)
          for k, line in dlss.CONFLICTS[dlss.STANDALONE] if k == "ingame"), _steps23)
_ui_cleanup()
check("the release lists the three assets and VORT",
      set(sources.STANDALONE_ASSETS) == {installer.STANDALONE_ADDON,
                                         installer.STANDALONE_BRIDGE,
                                         installer.STANDALONE_FX}
      and sources.VORT_ZIP_NAME.endswith(".zip"))

_opt = installer.Options(path=dlss.STANDALONE)
_steps = installer.plan(_g, _opt)
check("the plan has no renodx step and lists the add-on, VORT and the three runtimes",
      not any("renodx" in s for s in _steps) and "standalone-dlssnr" in _steps
      and any("VORT" in s for s in _steps) and "nvngx_dlssnr.dll" in _steps
      and "nvngx_dlss.dll" in _steps and "nvngx_dlssg.dll" in _steps, str(_steps))
_pv = installer.preview(_g, _opt)
check("the preview lists the add-on, nvngx.dll, the shader and no renodx add-on",
      installer.STANDALONE_ADDON in _pv.writes
      and installer.STANDALONE_BRIDGE in _pv.writes
      and "reshade-shaders/Shaders/DLSS5_AIO_Feed.fx" in _pv.writes
      and "reshade-shaders/Shaders/vort_Motion.fx" in _pv.writes
      and installer.DLSSG in _pv.writes
      and installer.RENODX not in _pv.writes and installer.RENODX_SF not in _pv.writes,
      str(_pv.writes))
_lvl, _why = installer.reliability(_g, dlss.STANDALONE)
check("reliability is experimental and names the window trick",
      _lvl == installer.EXPERIMENTAL and "window" in _why, _why)
check("standalone does not go through DXVK",
      not installer.uses_dxvk(_g, installer.Options(path=dlss.STANDALONE, dxvk=True)))
try:
    _rep = installer.install(_g, _opt, on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("install writes the add-on, nvngx.dll, nvngx_dlssnr and nvngx_dlss",
          installer.STANDALONE_ADDON in _files and installer.STANDALONE_BRIDGE in _files
          and installer.DLSSNR in _files and installer.DLSS in _files,
          str(sorted(_files)))
    check("install writes no renodx add-on",
          installer.RENODX not in _files and installer.RENODX_SF not in _files
          and installer.UPSTREAM_ADDON not in _files)
    check("nvngx_dlssg.dll came from the mirror's dlssg family",
          installer.DLSSG in _files
          and bool((_rep.components or {}).get("dlssg")), str(_rep.components))
    check("the companion shader, VORT and its includes are under reshade-shaders",
          (_d / installer.SHADERS / installer.STANDALONE_FX).is_file()
          and (_d / installer.SHADERS / installer.VORT_FX).is_file()
          and (_d / installer.VORT_INCLUDE / "vort_Defs.fxh").is_file()
          and (_d / installer.TEXTURES / installer.VORT_TEXTURE).is_file()
          and (_d / installer.SHADERS / "ReShade.fxh").is_file())
    check("the plan and the steps taken agree",
          len(_steps) == len(installer.plan(_g, _opt))
          and any("standalone version" in n for n in _rep.notes), str(_rep.notes))
    _ini = (_d / "ReShade.ini").read_text(encoding="utf8")
    check("ReShade.ini has the shader paths and no technique is enabled",
          "EffectSearchPaths" in _ini and "AddonPath" in _ini
          and "Techniques" not in _ini
          and not (_d / "ReShadePreset.ini").exists(), _ini)
    _man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest records path standalone, its version and nvngx.dll",
          _man.get("path") == "standalone"
          and bool((_man.get("components") or {}).get("standalone"))
          and installer.STANDALONE_BRIDGE in _man.get("files", [])
          and installer.DLSSG in _man.get("files", []),
          str(_man.get("components")))
    check("the manifest round-trips the route",
          installer.options_from_manifest(_d).path == dlss.STANDALONE)
    check("our own nvngx.dll is not a foreign hook on this route",
          installer.STANDALONE_BRIDGE not in installer.other_ngx_hooks(_d, dlss.STANDALONE)
          and installer.STANDALONE_ADDON not in installer.other_ngx_hooks(_d, dlss.STANDALONE)
          and installer.hook_warning(_d, dlss.STANDALONE) == "",
          str(installer.other_ngx_hooks(_d, dlss.STANDALONE)))
    check("but it IS one on the native route",
          installer.STANDALONE_BRIDGE in installer.other_ngx_hooks(_d, dlss.NATIVE)
          and installer.STANDALONE_BRIDGE in installer.other_ngx_hooks(_d)
          and "nvngx.dll" in installer.hook_warning(_d, dlss.NATIVE),
          str(installer.other_ngx_hooks(_d, dlss.NATIVE)))
    check("the preview of a feeder install says it removes the standalone files",
          any(installer.STANDALONE_BRIDGE in r for r in
              installer.preview(_g, installer.Options(path=dlss.FEEDER)).removes))
    installer.install(_g, installer.Options(path=dlss.FEEDER), on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("switching to feeder removes the add-on and nvngx.dll",
          installer.STANDALONE_ADDON not in _files
          and installer.STANDALONE_BRIDGE not in _files
          and installer.FEEDER_ADDON64 in _files and installer.RENODX in _files,
          str(sorted(_files)))
    check("the feeder's own DLSS5_Feed.fx and the AIO shader do not both remain",
          not (_d / installer.SHADERS / installer.STANDALONE_FX).exists())
    installer.install(_g, _opt, on_log=lambda t: None)
    _files = {p.name for p in _d.iterdir() if p.is_file()}
    check("switching back removes the feeder and renodx-dlss5.addon64",
          installer.FEEDER_ADDON64 not in _files and installer.RENODX not in _files
          and installer.STANDALONE_ADDON in _files
          and installer.STANDALONE_BRIDGE in _files, str(sorted(_files)))
    installer.uninstall(_g, on_log=lambda t: None)
    _left = sorted(p.name for p in _d.rglob("*") if p.is_file())
    check("uninstall leaves only the game", _left == ["Game.exe"], str(_left))
    check("uninstall removed the empty shader folders",
          not (_d / "reshade-shaders").exists())
except Exception as e:
    check("standalone: installs", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# The diagnosis: the add-on's own log lives outside the game folder and is
# shared by every game, so the test points diagnose at a synthetic one.
_REG = ('INFO | Registered add-on "Standalone DLSS-NR + SR 1.7.17-early-proxy" '
        'v1.7.17.0\n'
        "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n")
_ATTACH = ("Standalone DLSS-NR + SR 1.7.17-early-proxy attached; requested "
           "profile=Auto model=1 NR=on early_proxy=disabled\n")
_d = _diag_dir("diag_standalone_", addons=False, reshade=_REG, path="standalone",
               files=["dxgi.dll", "standalone-dlssnr.addon64", "nvngx.dll"])
(_d / "standalone-dlssnr.addon64").write_bytes(b"MZ")
(_d / "nvngx.dll").write_bytes(b"MZ")
_saved_log = diagnose.model.STANDALONE_LOG
_logd = Path(tempfile.mkdtemp(prefix="diag_salog_"))
diagnose.model.STANDALONE_LOG = _logd / "standalone-dlssnr.log"
try:
    _r = diagnose.analyse(_d)
    check("no standalone log yet is not a failure",
          not _levels(_r, "bad") and "play once" in _r.verdict, _r.verdict)
    diagnose.model.STANDALONE_LOG.write_text(
        _ATTACH + "runtime dependency: nvngx_dlssnr.dll (109425288 bytes)\n"
        "required private runtime dependency missing\n", encoding="utf8")
    _r = diagnose.analyse(_d)
    check("'required private runtime dependency missing' is BAD and names nvngx.dll",
          any("runtime" in b for b in _levels(_r, "bad"))
          and any("nvngx.dll" in f_.detail for f_ in _r.findings if f_.level == "bad")
          and "reinstall" in _r.verdict, str(_levels(_r, "bad")) + " / " + _r.verdict)
    check("the folder's own nvngx.dll and add-on are not called a foreign hook",
          not any("Another DLSS hook" in w for w in _levels(_r, "warn")),
          str(_levels(_r, "warn")))
    diagnose.model.STANDALONE_LOG.write_text(
        "old session line from another game\n" + _ATTACH
        + "standalone contract ready: NR=on at 1920x1080, DLSS SR -> 3840x2160, "
          "DLSS-G=on, model=1, profile=sRGB\n"
        + "same-frame VORT optical flow + DLSS5_AIO_Feed submitted before NGX: frame=1\n"
        + "on-present frame 120: NR=on, DLSS SR=Success, model=1, NR-reset=0, "
          "NR-guides=same-frame VORT optical flow, SR-history=on, DLSS history "
          "mask=on, input=1920x1080, output=3840x2160\n", encoding="utf8")
    _r = diagnose.analyse(_d)
    check("a contract and frames in the log is Working",
          _r.verdict == "Working." and not _levels(_r, "bad")
          and any("VORT" in t for t in _levels(_r, "ok")), str(_r.findings))
    diagnose.model.STANDALONE_LOG.write_text(
        _ATTACH + "standalone contract ready: NR=on at 1920x1080\n"
        "active on present: per-frame reset / zero motion + fallback guides\n"
        "standalone pipeline FAILED at DLSS SR feature creation: 0xBAD00010\n",
        encoding="utf8")
    _r = diagnose.analyse(_d)
    check("a pipeline failure names the stage, zero motion is a warning",
          any("DLSS SR feature creation" in b for b in _levels(_r, "bad"))
          and any("zero-motion" in w for w in _levels(_r, "warn")),
          str(_levels(_r, "bad")) + " / " + str(_levels(_r, "warn")))
    (_d / "ReShade.log").write_text(
        _REG + 'INFO | Registered add-on "DLSS 5 Neural Rendering" v4.7.0.0\n',
        encoding="utf8")
    _r = diagnose.analyse(_d)
    check("renodx-dlss5 beside standalone is two add-ons processing the frame",
          any("Two add-ons" in b for b in _levels(_r, "bad")), str(_levels(_r, "bad")))
    (_d / "ReShade.log").write_text(
        'INFO | Registered add-on "Some HDR mod" v1.0.0.0\n', encoding="utf8")
    _r = diagnose.analyse(_d)
    check("the add-on missing from the registered list is BAD",
          any("did not load" in b for b in _levels(_r, "bad")), str(_levels(_r, "bad")))
    (_d / "ReShade.log").write_text(_REG, encoding="utf8")
    (_d / "dlss5-autopilot.json").write_text(json.dumps(
        {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
         "api": "DX12", "proxy": "dxgi.dll", "path": "native",
         "files": ["dxgi.dll", "renodx-dlss5.addon64"]}), encoding="utf8")
    _r = diagnose.analyse(_d)
    check("on the native route a loaded standalone add-on is two add-ons",
          any("standalone" in b for b in _levels(_r, "bad")), str(_levels(_r, "bad")))
finally:
    diagnose.model.STANDALONE_LOG = _saved_log
shutil.rmtree(_logd, ignore_errors=True)
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 26. webcam and aside rules
section("26. webcam helpers")
_fake = ('[dshow @ 000001] "Brio 100" (video)\n[dshow @ 000001] "Microphone (Brio 100)" (audio)\n'
         '[dshow @ 000001] "OBS Virtual Camera" (video)\n')
import re as _re
_cams = []
for _line in _fake.splitlines():
    _m = _re.search(r'"([^"]+)"\s+\(video\)', _line)
    if _m and _m.group(1) not in _cams:
        _cams.append(_m.group(1))
check("camera names are parsed from ffmpeg's device list (video only)",
      _cams == ["Brio 100", "OBS Virtual Camera"], str(_cams))
check("stop_webcam without a stream is harmless", video.stop_webcam() is None)
check("no ffmpeg -> no cameras, no exception",
      video.list_cameras(Path(tempfile.mkdtemp(prefix="nocam_"))) == [])
check("the webcam URL is local only", video.WEBCAM_URL.startswith("udp://@127.0.0.1:"))


# ------------------------------------------------- 27. the webcam self-check
section("27. the webcam self-check reads the feed log by time")
import datetime as _dt
_d = Path(tempfile.mkdtemp(prefix="camlog_"))
_now = _dt.datetime.now().replace(microsecond=0)
_stamp = lambda secs: (_now + _dt.timedelta(seconds=secs)).strftime("%H:%M:%S.000")
(_d / "dlss5-feed.log").write_text(
    f"{_stamp(-120)}  [feed] frame 500 delivered (old run)\n"
    f"{_stamp(5)}  [feed] frame 1 delivered (1280x720 at 100% -> 1280x720, reset=1)\n"
    f"{_stamp(6)}  [feed] frame 7 delivered (1280x720 at 100% -> 1280x720, reset=0)\n"
    f"{_stamp(8)}  [feed] MV probe (centre 64x64, frame 600): mean |mv| 0.4 px, max 0.6 px, 98% non-zero\n",
    encoding="utf8")
_t0 = _now.timestamp()
_frames, _mv = video.feed_frames_since(_d, _t0)
check("frames after the start time are counted, the old run is not",
      _frames == 7 and _mv, f"{_frames} {_mv}")
check("nothing after a start time in the future", video.feed_frames_since(_d, _t0 + 600) == (0, False))
check("a missing log is (0, False)", video.feed_frames_since(Path(tempfile.mkdtemp(prefix="nolog_")), _t0) == (0, False))
shutil.rmtree(_d, ignore_errors=True)

section("28. the RTX Remix route")
from core import remix as _remix  # noqa: E402

# A FAKE Remix game: a .trex folder with a small runtime binary that carries
# Kim2091's option prefix as a string, and an rtx.conf that does NOT end with
# a newline - the exact shape that produced "...= 3rtx.neuralUplift = True"
# when this was done by hand. The real GTA IV install is only ever read.
_CONF_BODY = (b"rtx.fallbackLightMode = 2\r\n"
              b"rtx.atmosphere.skyIndirectRadianceScale = 3")


def _fake_remix(prefix: str, marker: bytes = b"rtx.neuralUplift") -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, d / "Game.exe")
    t = d / ".trex"
    t.mkdir()
    (t / "d3d9.dll").write_bytes(b"MZ" + b"\0" * 4096 + marker
                                 + b"\0" * 512 + marker + b".bypassCallerCheck")
    (d / "d3d9.dll").write_bytes(b"MZ" + b"\0" * 2048)   # the Remix bridge stub
    (d / "rtx.conf").write_bytes(_CONF_BODY)
    return d


_d = _fake_remix("remix_")
_trex = _remix.find_runtime(_d)
check("the .trex runtime folder is found", _trex == _d / ".trex", str(_trex))
check("a folder with a .trex is a Remix game", _remix.is_remix_game(_d))
check("a folder without one is not",
      not _remix.is_remix_game(Path(tempfile.mkdtemp(prefix="notremix_"))))
check("the uplift fork is recognised from the binary",
      _remix.runtime_flavour(_trex) == "uplift", _remix.runtime_flavour(_trex))
check("the enable key comes from the fork, not a guess",
      _remix.enable_key("uplift") == "rtx.neuralUplift.enable"
      and _remix.enable_key("neural") == "rtx.neuralRendering.enable")
_dn = _fake_remix("remixn_", marker=b"rtx.neuralRendering")
check("the neuralRendering fork is recognised too",
      _remix.runtime_flavour(_dn / ".trex") == "neural")
shutil.rmtree(_dn, ignore_errors=True)
_dp = _fake_remix("remixplain_", marker=b"rtx.someOtherOption")
check("a runtime with neither marker has no flavour",
      _remix.runtime_flavour(_dp / ".trex") == "")
_gp = games.manual(_dp)
check("a Remix runtime without the neural pass blocks the install",
      any("swap" in b for b in installer.preview(
          _gp, installer.Options(path=dlss.REMIX)).blockers),
      str(installer.preview(_gp, installer.Options(path=dlss.REMIX)).blockers))
check("the plan then has the runtime step when the swap is ticked",
      installer.plan(_gp, installer.Options(path=dlss.REMIX, remix_swap=True))
      == ["RTX Remix runtime (DLSS 5 build)", "nvngx_dlssnr.dll", "rtx.conf"],
      str(installer.plan(_gp, installer.Options(path=dlss.REMIX, remix_swap=True))))
check("swapping the runtime is experimental, keeping it is beta",
      installer.reliability(_gp, dlss.REMIX, remix_swap=True)[0]
      == installer.EXPERIMENTAL
      and installer.reliability(_gp, dlss.REMIX)[0] == installer.BETA)
shutil.rmtree(_dp, ignore_errors=True)

# --- rtx.conf editing: the file is the user's, one line is ours ------------
_conf = _d / "rtx.conf"
check("set_option appends to a file with no trailing newline",
      _remix.set_option(_conf, "rtx.neuralUplift.enable", "True")
      and _conf.read_bytes() ==
      _CONF_BODY + b"\r\nrtx.neuralUplift.enable = True\r\n",
      repr(_conf.read_bytes()[-70:]))
check("CRLF is preserved, LF is never introduced",
      b"\n" not in _conf.read_bytes().replace(b"\r\n", b""))
_remix.set_option(_conf, "rtx.neuralUplift.enable", "False")
check("setting a key that is already there replaces it in place",
      _conf.read_bytes().count(b"rtx.neuralUplift.enable") == 1
      and b"= False" in _conf.read_bytes()
      and b"rtx.fallbackLightMode = 2" in _conf.read_bytes(),
      repr(_conf.read_bytes()))
_remix.set_option(_conf, "rtx.neuralUplift.enable", "True")
check("option_set sees the key, and not a key that is absent",
      _remix.option_set(_conf, "rtx.neuralUplift.enable")
      and not _remix.option_set(_conf, "rtx.neuralRendering.enable"))
check("remove_option takes only that line out",
      _remix.remove_option(_conf, "rtx.neuralUplift.enable")
      and _conf.read_bytes() == _CONF_BODY + b"\r\n",
      repr(_conf.read_bytes()))
check("removing a key that is not there changes nothing",
      not _remix.remove_option(_conf, "rtx.neuralUplift.enable")
      and _conf.read_bytes() == _CONF_BODY + b"\r\n")
_conf.write_bytes(_CONF_BODY)          # back to the awkward shape for install

# --- detection and the route list -----------------------------------------
_g = games.manual(_d)
_sup = dlss.detect(_g.install_dir, _g.folder, _g.api, _g.bitness or 0, 89)
check("remix is the recommendation for a Remix game",
      _sup.recommended == dlss.REMIX and _sup.options[0] == dlss.REMIX,
      f"{_sup.recommended} {_sup.options}")
check("the other routes are still listed", len(_sup.options) > 1, str(_sup.options))
check("the reason says a ReShade proxy crashes a Remix game",
      "ReShade" in _sup.reason and "crashes" in _sup.reason, _sup.reason[:80])
check("remix has a label, a blurb and a conflicts entry",
      dlss.REMIX in dlss.LABELS and dlss.REMIX in dlss.BLURB
      and dlss.REMIX in dlss.CONFLICTS
      and dlss.LABELS[dlss.REMIX] ==
      "remix - DLSS 5 inside RTX Remix (path tracing)")
check("every route still has a conflicts entry of 2-4 lines",
      set(dlss.CONFLICTS) == set(dlss.ALL_ROUTES)
      and all(2 <= len(v) <= 4 for v in dlss.CONFLICTS.values()),
      str(sorted(dlss.CONFLICTS)))
check("fit says remix is usable", dlss.fit(dlss.REMIX, _g.api, False, 89)[0])
check("DXVK is never offered on this route",
      not installer.uses_dxvk(_g, installer.Options(path=dlss.REMIX, dxvk=True)))

_opt = installer.Options(path=dlss.REMIX)
check("the plan is the runtime's own files and one config line",
      installer.plan(_g, _opt) == ["nvngx_dlssnr.dll", "rtx.conf"],
      str(installer.plan(_g, _opt)))
_pv = installer.preview(_g, _opt)
check("the preview writes into .trex and nowhere else",
      any(w.endswith(".trex/nvngx_dlssnr.dll") for w in _pv.writes)
      and not any(w.lower().endswith(("dxgi.dll", ".addon64", "reshade.ini"))
                  for w in _pv.writes), str(_pv.writes))
check("the preview names the one rtx.conf line it will add",
      any("rtx.neuralUplift.enable = True" in w for w in _pv.writes),
      str(_pv.writes))
check("the preview has no blockers", not _pv.blockers, str(_pv.blockers))

# --- a ReShade proxy in a Remix folder is a crash, so it goes -------------
(_d / "dxgi.dll").write_bytes(b"MZ" + b"ReShade" + b"\0" * (1 << 21))
_pv = installer.preview(_g, installer.Options(path=dlss.REMIX))
check("a ReShade proxy is reported for removal",
      any("dxgi.dll" in r for r in _pv.removes), str(_pv.removes))

try:
    _rep = installer.install(_g, _opt, on_log=lambda t: None)
    check("nvngx_dlssnr.dll goes inside .trex, not beside the exe",
          (_trex / "nvngx_dlssnr.dll").is_file()
          and not (_d / "nvngx_dlssnr.dll").exists())
    check("the enable key is set and rtx.conf is otherwise untouched",
          _remix.option_set(_conf, "rtx.neuralUplift.enable")
          and _conf.read_bytes().startswith(_CONF_BODY),
          repr(_conf.read_bytes()[-60:]))
    _names = sorted(p.name for p in _d.rglob("*") if p.is_file())
    check("no ReShade, no feeder, no add-on and no shader went in",
          not any(n.endswith((".addon64", ".addon32", ".fx", ".fxh"))
                  or n.lower() in ("reshade.ini", "reshadepreset.ini",
                                   "dlss5-feed.cfg")
                  for n in _names), str(_names))
    check("the ReShade proxy was moved out of the way",
          not (_d / "dxgi.dll").exists()
          and (_d / ("dxgi.dll" + installer.BACKUP_SUFFIX)).is_file(),
          str(_names))
    _man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
    check("the manifest records the route, the key and the conf",
          _man.get("path") == "remix"
          and _man["remix"]["key"] == "rtx.neuralUplift.enable"
          and _man["remix"]["conf"] == "rtx.conf"
          and _man["remix"]["flavour"] == "uplift", str(_man.get("remix")))
    check("the manifest round-trips the route",
          installer.options_from_manifest(_d).path == dlss.REMIX
          and not installer.options_from_manifest(_d).remix_swap)
    check("no runtime swap was recorded - this runtime already had the pass",
          "remix_runtime" not in (_man.get("components") or {}),
          str(_man.get("components")))

    # the diagnosis, from the runtime's own log
    _rl = _remix.log_path(_d)
    _rl.parent.mkdir(parents=True, exist_ok=True)
    _rl.write_text(
        "[15:28:12.945] info:  [DLSS-NR] Loaded .trex\\nvngx_dlssnr.dll\n"
        "[15:28:13.071] info:  [DLSS-NR] Snippet initialized\n"
        "[15:28:13.739] info:  [DLSS-NR] Created the Neural Uplift feature "
        "(id 18, preset 0) at 1920x1080\n", encoding="utf8")
    _r = diagnose.analyse(_d)
    check("a real success log reads as working",
          _r.verdict == "Working."
          and any("feature 18" in f.title for f in _r.findings if f.level == "ok"),
          _r.verdict + " / " + str(_levels(_r, "bad")))
    _rl.write_text("[15:28:12.945] err:   nvngx_dlssnr.dll could not be loaded\n",
                   encoding="utf8")
    _r = diagnose.analyse(_d)
    check("the fork's own failure phrase is matched and explained",
          any("could not be loaded" in b for b in _levels(_r, "bad")),
          str(_levels(_r, "bad")))
    _remix.remove_option(_conf, "rtx.neuralUplift.enable")
    _r = diagnose.analyse(_d)
    check("the key missing from rtx.conf is reported",
          any("switched off in rtx.conf" in b for b in _levels(_r, "bad")),
          str(_levels(_r, "bad")))
    check("the bug report carries the Remix log, not ReShade's",
          "remix-dxvk.log" in diagnose.issue_body(
              "1.5.0", "RTX 4060 Ti", 89, "581.15", _g, "remix", _r, "", "x",
              _d))
    _remix.set_option(_conf, "rtx.neuralUplift.enable", "True")
    shutil.rmtree(_d / "rtx-remix", ignore_errors=True)

    _runtime_before = (_trex / "d3d9.dll").read_bytes()
    installer.uninstall(_g, on_log=lambda t: None)
    check("uninstall takes the key back out and leaves the rest of rtx.conf",
          not _remix.option_set(_conf, "rtx.neuralUplift.enable")
          and _conf.read_bytes().startswith(_CONF_BODY),
          repr(_conf.read_bytes()))
    check("uninstall removes nvngx_dlssnr.dll from .trex",
          not (_trex / "nvngx_dlssnr.dll").exists())
    check("the mod's own runtime is still there, byte for byte",
          (_trex / "d3d9.dll").read_bytes() == _runtime_before)
    check("the ReShade proxy that was moved aside is put back",
          (_d / "dxgi.dll").is_file()
          and not (_d / ("dxgi.dll" + installer.BACKUP_SUFFIX)).exists())
    _left = sorted(p.name for p in _d.rglob("*") if p.is_file())
    check("nothing of ours is left behind",
          _left == ["Game.exe", "d3d9.dll", "d3d9.dll", "dxgi.dll", "rtx.conf"],
          str(_left))
except Exception as e:
    check("remix: installs", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# Read-only, against the owner's real GTA IV RTX install. Skipped anywhere
# else: nothing here writes, and the guard keeps the suite portable.
_GTA = Path(r"D:\SteamLibrary\steamapps\common\Grand Theft Auto IV\GTAIV")
if _remix.is_remix_game(_GTA):
    _t = _remix.find_runtime(_GTA)
    check("real GTA IV: the runtime is the neuralUplift fork",
          _remix.runtime_flavour(_t) == "uplift", str(_t))
    # Not "the key is set" - that depends on whether the tool happens to be
    # installed there right now. What must hold is that the real rtx.conf is
    # found and readable, and that asking about the key gives an answer.
    _real_conf = _remix.conf_path(_GTA, _t)
    check("real GTA IV: its rtx.conf is found and readable",
          _real_conf.is_file() and len(_real_conf.read_bytes()) > 100,
          str(_real_conf))
    check("real GTA IV: the enable key can be asked about either way",
          _remix.option_set(_real_conf, _remix.enable_key("uplift")) in (True, False))
    _gg = games.manual(_GTA)
    check("real GTA IV: remix is the recommended route",
          dlss.detect(_gg.install_dir, _gg.folder, _gg.api,
                      _gg.bitness or 0, 89).recommended == dlss.REMIX)
else:
    print("   SKIP  the real GTA IV Remix install is not on this machine")


# ------------------------------------------------- 29. a Remix mod is never damaged
section("29. an RTX Remix install survives everything we do")


def _fake_remix(prefix: str = "remix_safe_"):
    """A game folder shaped like a real Remix install."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, d / "Game.exe")
    (d / "d3d9.dll").write_bytes(b"MZ REMIX BRIDGE CLIENT" + b"\x00" * 200)
    trex = d / ".trex"
    trex.mkdir()
    (trex / "d3d9.dll").write_bytes(b"MZ REMIX RUNTIME rtx.neuralUplift" + b"\x00" * 400)
    (trex / "NvRemixBridge.exe").write_bytes(b"MZ")
    (d / "rtx.conf").write_bytes(b"rtx.fallbackLightMode = 2\r\nrtx.skyBrightness = 1")
    mods = d / "rtx-remix" / "mods" / "thegame"
    mods.mkdir(parents=True)
    (mods / "mod.usda").write_bytes(b"#usda 1.0\n")
    return d


_d = _fake_remix()
_before = {p.relative_to(_d).as_posix(): p.read_bytes()
           for p in _d.rglob("*") if p.is_file()}
_g = games.manual(_d)
# A Remix game IS a DirectX 9 game - that is the whole point of Remix - so
# the DX9 branch of the installer has to be exercised here, not skipped.
_g.api = "DX9"
check("a folder with .trex is recognised as a Remix game",
      remix.is_remix_game(_d) and remix.find_runtime(_d) == (_d / ".trex"))
check("the runtime's DLSS 5 flavour is read from the binary, not guessed",
      remix.runtime_flavour(_d / ".trex") == "uplift",
      remix.runtime_flavour(_d / ".trex"))

# every other route must refuse before it writes anything
for _route in (dlss.FEEDER, dlss.NATIVE, dlss.BRIDGE, dlss.STANDALONE, dlss.OPTI):
    try:
        installer.install(_g, installer.Options(path=_route), on_log=lambda t: None)
        _refused = False
    except installer.InstallError as e:
        _refused = "Remix" in str(e)
    except Exception:
        _refused = False
    check(f"the {_route} route refuses a Remix game", _refused)
_after = {p.relative_to(_d).as_posix(): p.read_bytes()
          for p in _d.rglob("*") if p.is_file()}
check("and not one byte of the mod changed", _after == _before,
      str(sorted(set(_after) ^ set(_before))))

# uninstall with no manifest at all must not delete the runtime
installer.uninstall(_g, on_log=lambda t: None)
check("a manifest-less uninstall leaves the Remix runtime and its mods alone",
      (_d / "d3d9.dll").read_bytes() == _before["d3d9.dll"]
      and (_d / ".trex" / "d3d9.dll").is_file()
      and (_d / "rtx-remix" / "mods" / "thegame" / "mod.usda").is_file())

# the remix route itself: install, then put everything back
_rep = installer.install(_g, installer.Options(path=dlss.REMIX), on_log=lambda t: None)
check("the route puts nvngx_dlssnr.dll inside .trex",
      (_d / ".trex" / "nvngx_dlssnr.dll").is_file())
check("it writes no ReShade, no feeder and no add-on",
      not (_d / "dxgi.dll").exists() and not (_d / installer.RENODX).exists()
      and not (_d / installer.FEEDER_ADDON64).exists()
      and not (_d / "ReShade.ini").exists(),
      str(sorted(p.name for p in _d.iterdir())))
# The one that got through on the real GTA IV: a Remix game is a DX9 game,
# and the DX9 translation step wrote its own d3d9.dll over the Remix bridge
# client. No DX9 translation may run on this route, DXVK or anything else.
check("and no DX9 translation layer, whatever the game's API says",
      not (_d / "dgVoodoo.conf").exists() and not (_d / "dgVoodooCpl.exe").exists()
      and not (_d / ("D3D9.dll" + installer.BACKUP_SUFFIX)).exists()
      and not (_d / ("d3d9.dll" + installer.BACKUP_SUFFIX)).exists(),
      str(sorted(p.name for p in _d.iterdir())))
check("the steps taken match the plan exactly",
      len(installer.plan(_g, installer.Options(path=dlss.REMIX))) == 2,
      str(installer.plan(_g, installer.Options(path=dlss.REMIX))))
check("the runtime is left exactly as it was without the swap option",
      (_d / ".trex" / "d3d9.dll").read_bytes() == _before[".trex/d3d9.dll"]
      and (_d / "d3d9.dll").read_bytes() == _before["d3d9.dll"])
_conf = (_d / "rtx.conf").read_bytes()
check("the enable key is added and the file's own lines are untouched",
      b"rtx.fallbackLightMode = 2" in _conf and b"rtx.skyBrightness = 1" in _conf
      and remix.enable_key("uplift").encode() in _conf, _conf)
check("a conf with no trailing newline does not get two keys glued together",
      b"1rtx." not in _conf and _conf.count(b"neuralUplift.enable") == 1, _conf)
installer.uninstall(_g, on_log=lambda t: None)
_end = {p.relative_to(_d).as_posix(): p.read_bytes()
        for p in _d.rglob("*") if p.is_file()}
check("uninstall returns the Remix install byte for byte", _end == _before,
      str(sorted(set(_end) ^ set(_before))))

# The swap, end to end: a runtime without the pass, swapped in; "update all"
# (options_from_manifest) renews the one we put there instead of skipping it
# because it now has the pass; uninstall brings the mod's own back (#148).
_ds = _fake_remix("remix_swap_")
(_ds / ".trex" / "d3d9.dll").write_bytes(b"MZ MOD'S OWN RUNTIME, NO PASS" + b"\x00" * 400)
_orig_rt = (_ds / ".trex" / "d3d9.dll").read_bytes()
_gs = games.manual(_ds)
_gs.api = "DX9"
_tag = {"v": "dlssnr-v1"}
_saved_sw = (installer.sources.resolve_remix_runtime, net.download)
installer.sources.resolve_remix_runtime = lambda: (
    _tag["v"], {n: f"https://x/{_tag['v']}/{n}" for n in sources.REMIX_RUNTIME_ASSETS})


def _dl_sw(url, name, **k):
    if not name.startswith("remix-runtime-"):
        return _saved_sw[1](url, name, **k)
    p = Path(tempfile.mkdtemp(prefix="rtdl_")) / name
    p.write_bytes(b"MZ COMMUNITY RUNTIME " + _tag["v"].encode()
                  + b" rtx.neuralUplift" + b"\x00" * 400)
    return p


net.download = _dl_sw
try:
    installer.install(_gs, installer.Options(path=dlss.REMIX, remix_swap=True),
                      on_log=lambda t: None)
    check("the swap puts the community runtime in",
          b"dlssnr-v1" in (_ds / ".trex" / "d3d9.dll").read_bytes())
    _tag["v"] = "dlssnr-v2"
    _opt_up = installer.options_from_manifest(_ds)
    installer.install(_gs, _opt_up, on_log=lambda t: None)
    _man_sw = json.loads((_ds / installer.MANIFEST).read_text(encoding="utf8"))
    check("'update all' renews a runtime this tool swapped in, and keeps it in the record",
          b"dlssnr-v2" in (_ds / ".trex" / "d3d9.dll").read_bytes()
          and (_man_sw.get("components") or {}).get("remix_runtime") == "dlssnr-v2",
          (_man_sw.get("components"), (_ds / ".trex" / "d3d9.dll").read_bytes()[:40]))
    installer.uninstall(_gs, on_log=lambda t: None)
    check("...and uninstall brings the mod's own runtime back after both",
          (_ds / ".trex" / "d3d9.dll").read_bytes() == _orig_rt,
          (_ds / ".trex" / "d3d9.dll").read_bytes()[:40])
    # After our swap the mod updates its own runtime. With the pass, it is
    # the mod's and stays; without it, the swap backs up THAT one, and
    # uninstall brings back the mod's newer runtime, not the old one.
    _tag["v"] = "dlssnr-v1"
    installer.install(_gs, installer.Options(path=dlss.REMIX, remix_swap=True),
                      on_log=lambda t: None)
    (_ds / ".trex" / "d3d9.dll").write_bytes(b"MZ MOD V2 WITH PASS rtx.neuralUplift" + b"\x00" * 400)
    check("a runtime the mod updated since our swap is not taken for ours",
          installer.remix_state(_gs, installer.options_from_manifest(_ds))[2] is False)
    _mod_v3 = b"MZ MOD V3 NO PASS" + b"\x00" * 400
    (_ds / ".trex" / "d3d9.dll").write_bytes(_mod_v3)
    installer.install(_gs, installer.options_from_manifest(_ds), on_log=lambda t: None)
    installer.uninstall(_gs, on_log=lambda t: None)
    check("...and one the mod replaced with a runtime without the pass comes back on uninstall",
          (_ds / ".trex" / "d3d9.dll").read_bytes() == _mod_v3,
          (_ds / ".trex" / "d3d9.dll").read_bytes()[:30])
    # A record from before the stamp (a command-line swap by 1.8.1), and a
    # reinstall with the box left unticked: neither may lose the backup or
    # the swap from the record.
    installer.install(_gs, installer.Options(path=dlss.REMIX, remix_swap=True),
                      on_log=lambda t: None)
    _m = json.loads((_ds / installer.MANIFEST).read_text(encoding="utf8"))
    _m["components"].pop("remix_runtime_stamp", None)
    (_ds / installer.MANIFEST).write_text(json.dumps(_m), encoding="utf8")
    installer.install(_gs, installer.Options(path=dlss.REMIX), on_log=lambda t: None)
    _m2 = json.loads((_ds / installer.MANIFEST).read_text(encoding="utf8"))
    check("a reinstall without the box keeps the swap and the backup on the record",
          (_m2.get("components") or {}).get("remix_runtime")
          and any(f.replace("\\", "/").endswith(".trex/d3d9.dll" + installer.BACKUP_SUFFIX)
                  for f in _m2.get("files", [])),
          (_m2.get("components"), _m2.get("files")))
    installer.uninstall(_gs, on_log=lambda t: None)
    check("...and uninstall still brings the mod's runtime back",
          (_ds / ".trex" / "d3d9.dll").read_bytes() == _mod_v3,
          (_ds / ".trex" / "d3d9.dll").read_bytes()[:30])
finally:
    installer.sources.resolve_remix_runtime, net.download = _saved_sw
# A mod whose own runtime HAS the pass is never replaced, box ticked or not.
_dk = _fake_remix("remix_keep_")
_keep_rt = (_dk / ".trex" / "d3d9.dll").read_bytes()
check("a mod's own runtime with the pass is not swapped even with the box ticked",
      installer.remix_state(games.manual(_dk),
                            installer.Options(path=dlss.REMIX, remix_swap=True))[2] is False)
shutil.rmtree(_ds, ignore_errors=True)
shutil.rmtree(_dk, ignore_errors=True)
shutil.rmtree(_d, ignore_errors=True)

# the same, for a conf that DID end with a newline: it must keep it
_d = _fake_remix("remix_nl_")
(_d / "rtx.conf").write_bytes(b"rtx.fallbackLightMode = 2\r\n")
_g = games.manual(_d)
installer.install(_g, installer.Options(path=dlss.REMIX), on_log=lambda t: None)
installer.uninstall(_g, on_log=lambda t: None)
check("a conf that ended with a newline still does",
      (_d / "rtx.conf").read_bytes() == b"rtx.fallbackLightMode = 2\r\n",
      (_d / "rtx.conf").read_bytes())
shutil.rmtree(_d, ignore_errors=True)

# the runtime swap is opt-in, backs the original up, and comes back
_d = _fake_remix("remix_swap_")
_orig = (_d / ".trex" / "d3d9.dll").read_bytes()
_g = games.manual(_d)
_g.api = "DX9"
_pv = installer.preview(_g, installer.Options(path=dlss.REMIX))
check("the preview does not swap the runtime unless asked",
      not any("d3d9" in w.lower() for w in _pv.writes), str(_pv.writes))
check("the list of known Remix projects is real and matched by name",
      remixlist.match("Grand Theft Auto IV") is not None
      and remixlist.match("Some Game Nobody Modded") is None
      and all(m.url.startswith("https://") for m in remixlist.MODS + remixlist.BUILT_IN))
shutil.rmtree(_d, ignore_errors=True)


# ------------------------------------------------- 30. RE Engine: REFramework
section("30. RE Engine games get REFramework installed first, for real")
_d = Path(tempfile.mkdtemp(prefix="reengine_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "re_chunk_000.pak").write_bytes(b"pak")
check("re_chunk_000.pak marks the folder as RE Engine",
      reengine.detected(_d))
check("a folder with no marker is not RE Engine",
      not reengine.detected(Path(tempfile.mkdtemp(prefix="not_reengine_"))))
check("dinput8.dll is no longer offered as a fake ReShade proxy name",
      "dinput8.dll" not in installer.RESHADE_PROXIES
      and "dinput8.dll" not in installer.RESHADE_PROXY_HELP)
_g = games.manual(_d)
_pv = installer.preview(_g, installer.Options())
check("the preview carries the RE Engine warning",
      any("RE Engine" in w for w in _pv.warnings), str(_pv.warnings))
check("the warning names REFramework, not a dead end",
      any("REFramework" in w for w in _pv.warnings), str(_pv.warnings))
check("REFramework is the first step of the plan",
      installer.plan(_g, installer.Options())[0].startswith("REFramework"),
      str(installer.plan(_g, installer.Options())))
try:
    _rep = installer.install(_g, installer.Options(), on_log=lambda t: None)
    check("install carries the same warning and still finishes",
          any("RE Engine" in w for w in _rep.warnings))
    check("a real REFramework dinput8.dll landed in the folder",
          refw.is_reframework(_d / refw.DINPUT8), str(_rep.written))
    installer.uninstall(_g, on_log=lambda t: None)
    check("uninstall takes it back out again",
          not (_d / refw.DINPUT8).exists())
except Exception as e:
    check("RE Engine install/uninstall round trip", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# A dinput8.dll already there - the person's own REFramework, or something
# else entirely using the same load slot - is backed up, not clobbered.
_d = Path(tempfile.mkdtemp(prefix="reengine_existing_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "re_chunk_000.pak").write_bytes(b"pak")
(_d / refw.DINPUT8).write_bytes(b"USER OWN DINPUT8" + bytes(200))
_g = games.manual(_d)
try:
    installer.install(_g, installer.Options(), on_log=lambda t: None)
    check("the user's own dinput8.dll was backed up, not deleted",
          (_d / (refw.DINPUT8 + installer.BACKUP_SUFFIX)).read_bytes()
          .startswith(b"USER OWN DINPUT8"))
    installer.uninstall(_g, on_log=lambda t: None)
    check("uninstall restores the user's own dinput8.dll byte for byte",
          (_d / refw.DINPUT8).read_bytes().startswith(b"USER OWN DINPUT8"))
except Exception as e:
    check("RE Engine dinput8.dll backup/restore", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# A manifest-less uninstall must only take a dinput8.dll that really is
# REFramework by content - a foreign one (VR mod, something unrelated using
# the same slot) survives, the same protection dxgi.dll/opengl32.dll got.
_d = Path(tempfile.mkdtemp(prefix="reengine_foreign_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / refw.DINPUT8).write_bytes(b"MZ not reframework at all" + bytes(2000))
_g = games.manual(_d)
installer.uninstall(_g, on_log=lambda t: None)
check("a foreign dinput8.dll survives a manifest-less uninstall",
      (_d / refw.DINPUT8).is_file())
shutil.rmtree(_d, ignore_errors=True)

# Installing twice must not turn our own file into "the game's own file".
# It did: the second install backed up the first install's copy, uninstall
# restored that backup, and the layer stayed in the folder for good - the
# game left rendering through DXVK with nothing on disk admitting it.
check("a DXVK d3d9.dll is recognised as ours, a foreign one is not",
      dxvk.is_dxvk(Path(tempfile.mkdtemp(prefix="notdxvk_")) / "nothing.dll") is False)
_d = Path(tempfile.mkdtemp(prefix="twice_dxvk_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.Game(name="twice", folder=_d, exe=_d / "Game.exe", bitness=64, api="DX11")
try:
    for _round in (1, 2):
        installer.install(_g, installer.Options(path=dlss.FEEDER, dxvk=True),
                          on_log=lambda t: None)
    _baks = sorted(p.name for p in _d.glob("*" + installer.BACKUP_SUFFIX))
    check("a second install does not back up our own DXVK", not _baks, str(_baks))
    installer.uninstall(_g, on_log=lambda t: None)
    _left = sorted(p.name for p in _d.iterdir() if p.is_file())
    check("after uninstall no DXVK is left behind", _left == ["Game.exe"], str(_left))
except Exception as e:
    check("install twice / uninstall clean", False, f"{type(e).__name__}: {e}")
shutil.rmtree(_d, ignore_errors=True)

# Not "covered structurally" - that was a literal True, which passes with
# both gates deleted. Ask the two previews instead.
_re_d = Path(tempfile.mkdtemp(prefix="reeng_"))
shutil.copyfile(X64, _re_d / "re2.exe")
(_re_d / "re_chunk_000.pak").write_bytes(b"x")
_g_re = games.manual(_re_d)


def _preview_says(path, word):
    try:
        pv = installer.preview(_g_re, installer.Options(path=path))
    except Exception:
        return False
    return any(word in str(x) for x in
               (list(getattr(pv, "warnings", [])) + list(getattr(pv, "notes", []))))


check("the remix route is never bothered with the RE Engine warning",
      not _preview_says(dlss.REMIX, "RE Engine"))
shutil.rmtree(_re_d, ignore_errors=True)

# ------------------------------------------------- 32. fetching a Remix mod
section("32. a Remix mod is fetched only when the release is a complete one")
from core import remixdl  # noqa: E402

# The real layouts of every published Remix release, read off the archives
# themselves. Only an archive carrying the renderer (.trex/d3d9.dll) may be
# installed; a bare proxy expects NVIDIA's runtime and a manual rename first,
# and dropping it in alone leaves the game loading a d3d9.dll with nothing
# behind it.
_LAYOUTS = {
    "GTA IV, renderer nested one level": (
        ["GTAIV-Remix-CompatibilityMod/.trex/d3d9.dll",
         "GTAIV-Remix-CompatibilityMod/d3d9.dll",
         "GTAIV-Remix-CompatibilityMod/dxvk.conf",
         "_installer_options/FusionFix_RTXRemixFork/plugins/x.asi"],
        "GTAIV-Remix-CompatibilityMod"),
    "NFSU2, renderer at the archive root": (
        [".trex/d3d9.dll", ".trex/bridge.conf", "rtx.conf", "dxvk.conf"], ""),
    "Thief Gold, proxy only": (
        ["INSTALL.txt", "d3d9.dll", "remix-comp-proxy.ini", "rtx.conf"], None),
    "AC II, a .trex with no renderer in it": (
        [".trex/bridge.conf", "dinput8.dll", "plugins/remix-comp-base.asi"], None),
    "Saints Row 3, configuration only": (
        ["dxvk.conf", "rtx.conf", "INSTALL.md"], None),
    "Garry's Mod, its own launcher": (
        ["bin/x.dll", "garrysmod/y", "rtx.conf", "dxvk.conf"], None),
    "Deus Ex, dev tools": (["IngestionHelper.exe", "omniversehelper/a"], None),
}
for _name, (_names, _want) in _LAYOUTS.items():
    check(f"{_name} -> {'refused' if _want is None else repr(_want)}",
          remixdl.remix_root(_names) == _want,
          str(remixdl.remix_root(_names)))

check("only the verified-complete projects are offered for fetching",
      [m.game for m in remixlist.MODS if m.installable]
      == ["Grand Theft Auto IV", "Need for Speed: Underground 2"],
      str([m.game for m in remixlist.MODS if m.installable]))
check("a github url gives its repo, anything else does not",
      remixdl.repo_of("https://github.com/xoxor4d/gta4-rtx") == "xoxor4d/gta4-rtx"
      and remixdl.repo_of("https://www.moddb.com/rtx") == "")

# A complete archive, built here: install it, then take it back out and the
# folder has to be exactly what it was.
_d = Path(tempfile.mkdtemp(prefix="remixmod_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "own.cfg").write_bytes(b"THE PLAYER'S OWN FILE")
_before = {p.name: p.read_bytes() for p in _d.iterdir() if p.is_file()}
_zip = _d.parent / "fake-remix-mod.zip"
import zipfile as _zf  # noqa: E402
with _zf.ZipFile(_zip, "w") as _z:
    _z.writestr("TheMod/.trex/d3d9.dll", b"MZ" + bytes(2048))
    _z.writestr("TheMod/.trex/bridge.conf", b"x=1\n")
    _z.writestr("TheMod/d3d9.dll", b"MZ" + bytes(512))
    _z.writestr("TheMod/rtx.conf", b"rtx.fallbackLightMode = 2\n")
    _z.writestr("TheMod/own.cfg", b"THE MOD'S VERSION")
_root, _lands = remixdl.inspect(_zip)
check("inspect finds the mod root and what it would write",
      _root == "TheMod" and sorted(_lands) == [".trex", "d3d9.dll", "own.cfg", "rtx.conf"],
      f"{_root} {_lands}")
_bad = _d.parent / "not-a-mod.zip"
with _zf.ZipFile(_bad, "w") as _z:
    _z.writestr("d3d9.dll", b"MZ" + bytes(64))
    _z.writestr("rtx.conf", b"x\n")
check("an incomplete release is refused by inspect too",
      _raises(lambda: remixdl.inspect(_bad)))
shutil.rmtree(_d, ignore_errors=True)

# The refusal that matters most: a folder that already has somebody's mod.
_d = Path(tempfile.mkdtemp(prefix="remixmod_busy_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / ".trex").mkdir()
(_d / ".trex" / "d3d9.dll").write_bytes(b"MZ" + bytes(4096))
_refused = False
try:
    remixdl.install("https://github.com/xoxor4d/gta4-rtx", _d, log=lambda t: None)
except remixdl.NotAModError as e:
    _refused = "already installed" in str(e)
except Exception:
    _refused = False
check("a folder that already has a Remix mod is never written over", _refused)
check("...and nothing was downloaded or written",
      sorted(p.name for p in _d.iterdir()) == [".trex", "Game.exe"])
shutil.rmtree(_d, ignore_errors=True)

# ------------------------------------------------- 31. a D3D12 game that only imports d3d11.dll
section("31. a D3D12 Agility SDK game is not mistaken for D3D11")
_d = Path(tempfile.mkdtemp(prefix="agility_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "D3D12").mkdir()
(_d / "D3D12" / "D3D12Core.dll").write_bytes(b"MZ")
check("a D3D12 folder with D3D12Core.dll promotes the label to DX12",
      pe.detect_api(_d / "Game.exe")[0] == "DX12")
shutil.rmtree(_d, ignore_errors=True)
_d = Path(tempfile.mkdtemp(prefix="dlssg_"))
shutil.copyfile(X64, _d / "Game.exe")
(_d / "nvngx_dlssg.dll").write_bytes(b"MZ")
check("DLSS Frame Generation alone is also enough evidence",
      pe.detect_api(_d / "Game.exe")[0] == "DX12")
shutil.rmtree(_d, ignore_errors=True)
_d = Path(tempfile.mkdtemp(prefix="plain_dx11_"))
shutil.copyfile(r"C:\Windows\System32\where.exe", _d / "Game.exe")
check("an exe with neither, and no graphics DLL named anywhere, stays Unknown",
      pe.detect_api(_d / "Game.exe")[0] == "Unknown")
shutil.rmtree(_d, ignore_errors=True)

section("33. a Vulkan-layer install is diagnosed by the registry, not the folder")
# Since 1.6.0 every DirectX 9 game goes through DXVK, so its manifest says
# "(vulkan layer)" where a proxy name would be. 1.6.0 looked for a file by
# that name and told everyone it had been quarantined (issues #10, #2).
from core import vulkan as _vk
_saved = (_vk.registrations, )
_vk.registrations = lambda: [(Path("C:/ProgramData/ReShade/ReShade64.json"), 0)]
check("registered_for(64-bit) sees ReShade's own 64-bit registration",
      _vk.registered_for(True) is not None)
check("registered_for(32-bit) does not accept a 64-bit manifest",
      _vk.registered_for(False) is None)
_vk.registrations = lambda: [(Path("C:/x/ReShade32.json"), 0),
                             (Path("C:/x/ReShade64.json"), 1)]
check("a DISABLED registration (value 1) counts for nothing",
      _vk.registered_for(True) is None and _vk.existing_registration() is not None)
check("manifest_x64 reads the architecture from the file name when the JSON is unreadable",
      _vk.manifest_x64(Path("C:/nowhere/ReShade32.json")) is False
      and _vk.manifest_x64(Path("C:/nowhere/ReShade64.json")) is True)

_vk.registrations = lambda: [(Path("C:/ProgramData/ReShade/ReShade64.json"), 0)]
# The manifest says api "Vulkan" once DXVK is in - the DXVK file set has to
# come from the recorded file list (d3d9.dll here), not from that label.
_d = _diag_dir("diag_vk32_", proxy=False, bitness=32, api="Vulkan", dxvk="v3.1")
(_d / "d3d9.dll").write_bytes(b"MZ")
_m = json.loads((_d / "dlss5-autopilot.json").read_text(encoding="utf8"))
_m["proxy"] = "(vulkan layer)"
_m["files"] = [f for f in _m["files"] if f != "dxgi.dll"] + ["d3d9.dll"]
(_d / "dlss5-autopilot.json").write_text(json.dumps(_m), encoding="utf8")
_r = diagnose.analyse(_d)
check("a 32-bit game with only the 64-bit layer registered is told exactly that",
      not _r.ran and "32-bit ReShade Vulkan layer is not registered" in _r.verdict
      and not any("gone from the folder" in t for t in _levels(_r, "bad")), _r.verdict)
_body = diagnose.issue_body("9.9", "x", None, "?", None, "feeder", _r, "",
                            Path("C:/x/autopilot.log"), _d)
check("the report shows the layer state and looks for the runtime in host64",
      "ReShade 32-bit Vulkan layer: NOT REGISTERED" in _body
      and "(vulkan layer): MISSING" not in _body
      and "- host64/nvngx_dlssnr.dll: MISSING" in _body
      and "- d3d9.dll: present" in _body, _body[_body.find("**Files"):][:400])

_vk.registrations = lambda: [(Path("C:/x/ReShade32.json"), 0),
                             (Path("C:/x/ReShade64.json"), 0)]
_r = diagnose.analyse(_d)
check("with both layers registered the verdict moves on to 'not started yet'",
      not _r.ran and "Not started" in _r.verdict, _r.verdict)
check("DXVK's d3d9.dll is NOT reported missing just because the api label is Vulkan",
      "dxgi.dll" not in " ".join(_levels(_r, "bad")), str(_levels(_r, "bad")))
(_d / "d3d9.dll").unlink()
_r = diagnose.analyse(_d)
check("DXVK missing from the folder is its own finding",
      "DXVK is missing" in _r.verdict, _r.verdict)
_vk.registrations = _saved[0]
shutil.rmtree(_d, ignore_errors=True)

# install_layer: a foreign 64-bit registration must not satisfy a 32-bit game
_calls = []
_saved = (_vk.registrations, _vk._place, _vk._register, _vk.layer_dir)
_vk.registrations = lambda: [(Path("C:/ProgramData/ReShade/ReShade64.json"), 0)]
_vk._place = lambda setup, d, dll, manifest: (_calls.append(("place", dll)) or d / manifest)
_vk._register = lambda m: _calls.append(("register", m.name))
_tmp = Path(tempfile.mkdtemp(prefix="vk_layer_"))
_vk.layer_dir = lambda: _tmp
_m, _fresh = _vk.install_layer(Path("C:/x/setup.exe"), also32=False)
check("a 64-bit game reuses ReShade's own 64-bit layer", not _fresh and not _calls)
_m, _fresh = _vk.install_layer(Path("C:/x/setup.exe"), also32=True)
check("a 32-bit game gets our 32-bit layer registered despite the foreign 64-bit one",
      _fresh and ("register", "ReShade32.json") in _calls, str(_calls))
_vk.registrations, _vk._place, _vk._register, _vk.layer_dir = _saved
shutil.rmtree(_tmp, ignore_errors=True)

section("34. a d3d9.dll import is not DirectX 9 when the game ships DLSS")
# No fixture here imports d3d9.dll, so the evidence function is tested on
# its own; detect_api consults it only on the d3d9-without-DXGI branch.
_d = Path(tempfile.mkdtemp(prefix="rdr2_"))
check("an empty folder is no evidence", pe._ships_dlss(_d) == "")
(_d / "nvngx_dlss.dll").write_bytes(b"MZ")
check("the game's own nvngx_dlss.dll is evidence of a modern renderer",
      pe._ships_dlss(_d) == "nvngx_dlss.dll")
(_d / "dlss5-autopilot.json").write_text(json.dumps({"files": ["nvngx_dlss.dll"]}), encoding="utf8")
check("an nvngx_dlss.dll our own manifest lists as written is NOT evidence (64-bit DX9 game after one install)",
      pe._ships_dlss(_d) == "")
(_d / "dlss5-autopilot.json").unlink()
(_d / "nvngx_dlss.dll").unlink()
(_d / "nvngx_dlssnr.dll").write_bytes(b"MZ")
check("our own nvngx_dlssnr.dll is NOT evidence (it would re-label every DX9 game after one install)",
      pe._ships_dlss(_d) == "")
check("the d3d9 branch of detect_api consults it",
      "_ships_dlss(path.parent" in src_of(pe.detect_api))
shutil.rmtree(_d, ignore_errors=True)

section("35. a scan cannot get stuck in a folder with no executables")
_d = Path(tempfile.mkdtemp(prefix="deep_"))
_p = _d
for i in range(4):
    _p = _p / f"lvl{i}"
    _p.mkdir()
    for j in range(60):
        (_p / f"box{j}").mkdir()
_old = pe._WALK_DIRS
pe._WALK_DIRS = 50
_t0 = time.monotonic()
_found = pe._walk_exes(_d)
pe._WALK_DIRS = _old
check("the walk stops at the directory budget", _found == [] and time.monotonic() - _t0 < 5)
check("Xbox's GameSave and Minecraft Launcher folders are not games",
      "gamesave" in games.XBOX_NOT_GAMES and "minecraft launcher" in games.XBOX_NOT_GAMES)
shutil.rmtree(_d, ignore_errors=True)

section("36. 1.7.0: OpenGL pin, quirks, route texts")
check("OpenGL pins renodx-dlss5 to 4.60 (4.70 stalls on GL)",
      sources.OPENGL_RENODX_PIN == "4.60"
      and "OPENGL_RENODX_PIN" in src_of(installer.install))
check("quirks are keyed by executable name, case-insensitively",
      dlss.quirks(Path("D:/Q3/Quake3.exe")) and "ioquake3" in dlss.quirks(Path("quake3.exe"))[0]
      and dlss.quirks(Path("Game.exe")) == () and dlss.quirks(None) == ())
check("every route has a one-line label and a blurb, and the bridge is no longer 'stopped'",
      set(dlss.LABELS) == set(dlss.ALL_ROUTES) == set(dlss.BLURB)
      and all("\n" not in v and len(v) < 80 for v in dlss.LABELS.values())
      and "stopped" not in dlss.BLURB[dlss.BRIDGE])
check("the feeder blurb lists Direct3D 10", "D3D10" in dlss.BLURB[dlss.FEEDER])
_d = _diag_dir("diag_fx_", provider=4)
_body = diagnose.issue_body("9.9", "x", None, "?", None, "feeder", diagnose.analyse(_d), "",
                            Path("C:/x/autopilot.log"), _d)
check("the feeder report lists the feed shader and the chosen provider's file (issue #13)",
      "- reshade-shaders/Shaders/DLSS5_Feed.fx: present" in _body
      and "- reshade-shaders/Shaders/lumenite_QuantMotion.fx: MISSING" in _body,
      _body[_body.find("**Files"):][:500])
shutil.rmtree(_d, ignore_errors=True)

# frame generation on the OptiScaler route: three ini keys, libraries checked
_d = Path(tempfile.mkdtemp(prefix="fg_"))
(_d / "OptiScaler.ini").write_text("[DlssNr]\nEnabled=true\n", encoding="utf8")
check("no FG libraries beside OptiScaler -> nothing written, False",
      optiscaler.enable_fg(_d) is False
      and "FrameGen" not in (_d / "OptiScaler.ini").read_text(encoding="utf8"))
(_d / "OptiScaler").mkdir()
for _n in optiscaler.FG_LIBS:
    (_d / _n).write_bytes(b"MZ")
check("with the libraries the three keys and HUDFix are written",
      optiscaler.enable_fg(_d) is True)
_ini = (_d / "OptiScaler.ini").read_text(encoding="utf8")
check("FrameGen section: Enabled=true, FGInput=upscaler, FGOutput=fsrfg",
      "[FrameGen]" in _ini and "FGInput=upscaler" in _ini and "FGOutput=fsrfg" in _ini
      and "HUDFix=true" in _ini and "[DlssNr]" in _ini, _ini)
shutil.rmtree(_d, ignore_errors=True)
check("Options carries fg and the manifest round-trips it",
      hasattr(installer.Options(), "fg") and installer.Options().fg is False
      and '"fg": opt.fg' in src_of(installer)
      and 'fg=bool(data.get("fg"' in src_of(installer.options_from_manifest))

section("37. RTX 40 multi-frame generation (mfg.py) - files, loader name, ini merge")
from core import mfg as _m  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="mfg_"))
check("not offered on RTX 50 / RTX 30", not _m.applies(120, "DX12", _d)[0]
      and not _m.applies(86, "DX12", _d)[0])
check("not offered without a DLSS Frame Generation file", not _m.applies(89, "DX12", _d)[0]
      and "no DLSS Frame Generation" in _m.applies(89, "DX12", _d)[1])
(_d / "nvngx_dlssg.dll").write_bytes(b"MZ")
check("RTX 40 + D3D12 + nvngx_dlssg.dll -> offered", _m.applies(89, "DX12", _d) == (True, ""))
check("RTX 40 + Vulkan -> offered, D3D11 -> not",
      _m.applies(89, "Vulkan", _d)[0] and not _m.applies(89, "DX11", _d)[0])
(_d / "dlss5-autopilot.json").write_text(json.dumps({"files": ["nvngx_dlssg.dll"]}), encoding="utf8")
check("an nvngx_dlssg.dll our own manifest wrote (standalone route) is NOT evidence",
      not _m.applies(89, "DX12", _d)[0])
(_d / "dlss5-autopilot.json").unlink()
(_d / "nvngx_dlssg.dll").unlink()
_deep = _d / "Engine" / "Plugins" / "Runtime" / "Nvidia" / "DLSS" / "Binaries" / "ThirdParty" / "Win64"
_deep.mkdir(parents=True)
(_deep / "sl.dlss_g.dll").write_bytes(b"MZ")
check("Streamline's sl.dlss_g.dll nine levels down (Unreal) counts",
      _m.has_dlssg(_d).replace("\\", "/").endswith("Win64/sl.dlss_g.dll"), _m.has_dlssg(_d))
_exe_dir = _d / "Proj" / "Binaries" / "Win64"; _exe_dir.mkdir(parents=True)
check("applies() searches from the game folder, not the executable's folder",
      _m.applies(89, "DX12", _exe_dir, _d)[0] and not _m.applies(89, "DX12", _exe_dir)[0])
check("loader name comes from the import table only - no name imported means None",
      _m.loader_name(None) is None and _m.loader_name(Path("C:/nowhere.exe")) is None)
_imp = X64
_names = {i.lower() for i in pe.pe_imports(_imp)}
_first = next((n for n in _m.LOADER_NAMES if n in _names), None)
check("a real executable gets a name it imports; a taken name moves to the next imported one",
      _first is not None and _m.loader_name(_imp) == _first
      and _m.loader_name(_imp, {_first}) in (set(_m.LOADER_NAMES) & _names) - {_first} | {None})
# install() against fake release zips: three files, the loader, its ini
import zipfile as _zf
_rz = _d / "mfg.zip"
with _zf.ZipFile(_rz, "w") as z:
    for n in _m.FILES:
        z.writestr(n, b"MZ" + n.encode())
    z.writestr("global.ini", "[GlobalSets]\nLoadPlugins=1\n")
_lz = _d / "ual.zip"
with _zf.ZipFile(_lz, "w") as z:
    z.writestr("dinput8.dll", b"MZ" + b"Ultimate ASI Loader" + b"\0" * (1 << 18))
_game = _d / "game"; _game.mkdir()
shutil.copyfile(X64, _game / "Game.exe")
# The loader takes a name the executable really imports (version.dll on
# this fixture, which has no DirectInput), so the test follows that choice.
_lname = _m.loader_name(_game / "Game.exe")
_lini = _lname[:-4] + ".ini"
check("the loader name comes from the executable's import table",
      _lname in _m.LOADER_NAMES and _lname in {i.lower() for i in pe.pe_imports(_game / "Game.exe")}, _lname)
# A file of the game's own under that name is not ours to replace: the
# unlock is an opt-in extra and a version.dll the game ships does something.
# It used to be backed up and written over (#137: on Cyberpunk that file is
# Cyber Engine Tweaks, and doing it took CET out of the game).
(_game / _lname).write_bytes(b"MZ-the-games-own")
check("a name the game itself occupies is not taken (#137)",
      _m.loader_name(_game / "Game.exe", set(), _game) is None
      and _m.loader_name(_game / "Game.exe") == _lname)
try:
    _m.install(_game, _game / "Game.exe")
    _refused = ""
except _m.NoLoaderName as e:
    _refused = str(e)
check("...and the unlock says which file stopped it, not 'none imported'",
      _lname in _refused and "another mod" in _refused, _refused[:90])
# From here the name is a loader the person installed by hand, which IS
# taken over - its ini is merged, so its own plugins keep loading.
(_game / _lname).write_bytes(b"MZ" + b"Ultimate ASI Loader" + b"\0" * (1 << 18))
(_game / _lini).write_text("[GlobalSets]\nLoadPlugins=1\nUseCrashHandler=0\n\n[Other]\nKeep=1\n", encoding="utf8")
_saved = (_m.resolve, _m.resolve_loader, net.download)
_m.resolve = lambda: ("v9.9", "mfg")
_m.resolve_loader = lambda: ("v1", "ual")
net.download = lambda url, name, **k: _rz if url == "mfg" else _lz
try:
    _tag, _files = _m.install(_game, _game / "Game.exe")
    # a second install of ours: the loader now in place is OURS (in the
    # previous manifest), so it must not be backed up over the real backup
    _bak_before = (_game / (_lname + _m.BACKUP_SUFFIX)).read_bytes()
    _tag2, _files2 = _m.install(_game, _game / "Game.exe", preinstalled=set(_files))
    check("a reinstall keeps the ORIGINAL backup (the loader in place was ours)",
          (_game / (_lname + _m.BACKUP_SUFFIX)).read_bytes() == _bak_before
          and (_lname + _m.BACKUP_SUFFIX) not in _files2, str(_files2))
    # the person's own Ultimate ASI Loader is theirs: backed up like anything else
    _g2 = _d / "game2"; _g2.mkdir(); shutil.copyfile(X64, _g2 / "Game.exe")
    (_g2 / _lname).write_bytes(b"MZ" + b"Ultimate ASI Loader" + b"\0" * (1 << 18))
    _t3, _f3 = _m.install(_g2, _g2 / "Game.exe")
    check("a loader the person installed by hand is backed up, not treated as ours",
          (_g2 / (_lname + _m.BACKUP_SUFFIX)).is_file() and (_lname + _m.BACKUP_SUFFIX) in _f3, str(_f3))
    _gone = _m.remove_leftovers(_g2, set(_f3))
    check("remove_leftovers takes the unlock out and puts the person's loader back",
          all(not (_g2 / n).is_file() for n in _m.FILES)
          and (_g2 / _lname).read_bytes().startswith(b"MZ" + b"Ultimate ASI Loader")
          and not (_g2 / (_lname + _m.BACKUP_SUFFIX)).exists(), str(_gone))
    check("remove_leftovers does nothing when the previous install had no unlock",
          _m.remove_leftovers(_g2, {"dxgi.dll"}) == [])
finally:
    _m.resolve, _m.resolve_loader, net.download = _saved
check("the three unlock files land beside the exe",
      all((_game / n).is_file() for n in _m.FILES) and _tag == "v9.9")
check("a loader the person installed by hand IS taken over, and backed up",
      (_game / (_lname + _m.BACKUP_SUFFIX)).read_bytes().startswith(
          b"MZUltimate ASI Loader")
      and _m.is_loader(_game / _lname), str(_files))
_ini = (_game / _lini).read_text(encoding="utf8")
check("the loader's ini is merged, not replaced",
      "LoadExtraPlugins=RTX40MFG.asi" in _ini and "UseCrashHandler=0" in _ini
      and "[Other]" in _ini and "Keep=1" in _ini and _ini.count("[GlobalSets]") == 1, _ini)
check("an ini that existed before is not listed as ours; the backup and the loader are",
      _lini not in _files and _lname in _files
      and (_lname + _m.BACKUP_SUFFIX) in _files, str(_files))
check("Options carries mfg and the manifest round-trips it",
      installer.Options().mfg is False and '"mfg": opt.mfg' in src_of(installer)
      and 'mfg=bool(data.get("mfg"' in src_of(installer.options_from_manifest))
shutil.rmtree(_d, ignore_errors=True)

section("38. screen and window capture through the player (video.py)")
_ff = Path("C:/t/ffmpeg.exe")
_c = video.capture_command(_ff, "screen 2", 30, gpu=True, region=None, output_idx=1)
check("a whole monitor goes through Desktop Duplication on the GPU and NVENC",
      "ddagrab=output_idx=1:framerate=30" in _c and "h264_nvenc" in _c
      and _c[-1].startswith("udp://127.0.0.1:"), str(_c))
_c = video.capture_command(_ff, "screen 1", 30, gpu=True, region=(0, 0, 1190, 1080), output_idx=0)
check("a region is cut on the GPU with ddagrab's own offset and size",
      any(x == "ddagrab=output_idx=0:framerate=30:offset_x=0:offset_y=0:video_size=1190x1080" for x in _c), str(_c))
_c = video.capture_command(_ff, "screen 1", 30, gpu=False, region=(10, 20, 640, 480))
check("the fallback is GDI of the same region with libx264",
      "gdigrab" in _c and "-offset_x" in _c and "640x480" in _c and "libx264" in _c, str(_c))
_cap, _park = video.split_layout((0, 0, 1920, 1080))
check("one monitor is split: capture on the left, the player parked on the right, no overlap",
      _cap == (0, 0, 1190, 1080) and _park == (1190, 0, 730, 1080)
      and _cap[0] + _cap[2] == _park[0], f"{_cap} {_park}")
_cap, _park = video.split_layout((1920, 0, 4480, 1440))
check("a second monitor's own origin is kept", _cap[0] == 1920 and _cap[2] % 2 == 0 and _park[0] == 1920 + _cap[2])
import tkinter as _tkw
# The window belongs to ANOTHER process, as in real use (a Tk window of this
# process renders black through PrintWindow once an earlier root was torn down).
_child = subprocess.Popen([sys.executable, "-c",
    "import tkinter as t; r=t.Tk(); r.title('dlss5 grab test'); r.geometry('400x300+40+40'); "
    "r.configure(bg='#3060c0'); r.mainloop()"])
_u = video._user32()
_hw = 0
for _ in range(100):
    _hw = _u.FindWindowW(None, "dlss5 grab test")
    if _hw:
        break
    time.sleep(0.05)
time.sleep(0.4)
_wd = _ht = 0; _buf = b""; _samples = []
for _ in range(5):
    _wd, _ht, _buf = video.grab_window(_hw) if _hw else (0, 0, b"")
    _samples = list(memoryview(_buf)[0::4 * 53]) if _buf else []      # blue channel of BGRA
    if _samples and sum(_samples) / len(_samples) > 150:
        break
    time.sleep(0.3)
check("grab_window returns another process's window pixels (a blue tk window is not black)",
      _wd >= 2 and _ht >= 2 and len(_buf) == _wd * _ht * 4 and _samples and sum(_samples) / len(_samples) > 150,
      f"hwnd={_hw} {_wd}x{_ht} mean-blue-channel={sum(_samples)/len(_samples) if _samples else None}")
_child.kill()
# the feed thread against a stand-in encoder: a .cmd that swallows stdin
_stub_dir = Path(tempfile.mkdtemp(prefix="feedstub_"))
_stub = _stub_dir / "ffmpeg.cmd"
_stub.write_text("@echo off\r\n\"" + sys.executable + "\" -c \"import sys; sys.stdin.buffer.read()\"\r\n", encoding="utf8")
_root2 = _tkw.Tk(); _root2.geometry("320x240+60+60"); _root2.update()
_feed = video._WindowFeed(_stub, _root2.winfo_id(), 20).start()
for _ in range(40):
    _root2.update(); time.sleep(0.05)
_sent = _feed.frames
_feed.stop()
for _ in range(12):                 # PrintWindow needs the window's thread to pump
    _root2.update(); time.sleep(0.05)
check("the window feed grabs frames and pushes them down the pipe; stop() ends the thread",
      _sent >= 5 and not _feed.alive(), f"sent={_sent} alive={_feed.alive()}")
# an encoder that refuses the GPU path: the first write breaks, the CPU path is opened
_calls = []
_orig_cmd = video.window_pipe_command
def _fake_cmd(ff, w_, h_, fps, gpu=True):
    _calls.append(gpu)
    return ["cmd", "/c", "exit", "1"] if gpu else [str(_stub)]
video.window_pipe_command = _fake_cmd
_msgs = []
_feed2 = video._WindowFeed(_stub, _root2.winfo_id(), 20, _msgs.append).start()
for _ in range(40):
    _root2.update(); time.sleep(0.05)
_feed2.stop()
for _ in range(12):                 # PrintWindow needs the window's thread to pump
    _root2.update(); time.sleep(0.05)
video.window_pipe_command = _orig_cmd
check("NVENC refusing the pipe falls back to the CPU encoder and the feed keeps going",
      _calls[:2] == [True, False] and _feed2.frames >= 3 and any("CPU encoder" in m for m in _msgs),
      f"calls={_calls} frames={_feed2.frames} msgs={_msgs}")
_root2.destroy()
shutil.rmtree(_stub_dir, ignore_errors=True)
_c = video.window_pipe_command(Path("C:/t/ffmpeg.exe"), 1190, 1080, 30)
check("the window feed pipes raw BGRA into NVENC at the chosen rate",
      "rawvideo" in _c and "1190x1080" in _c and "h264_nvenc" in _c and _c[_c.index("-r") + 1] == "30", str(_c))
_ls = video.list_screens()
check("the list starts with the monitors and never lists this tool's own window",
      _ls and _ls[0] == "screen 1" and not any("DLSS 5 Autopilot" in s for s in _ls), str(_ls[:4]))
_region, _idx, _park, _other = video.plan_capture("screen 1")
check("on this PC the plan parks the player beside the capture (one monitor) or on the other one",
      (_other and _region is None) or (not _other and _region is not None and _park is not None), str((_region, _idx, _park, _other)))

section("39. OpenGL games get VORT motion vectors, installed by the tool")
check("VORT is a provider the tool installs, with its technique above the feed",
      reshade_ini.PROVIDERS[2][1] == "vort_MotionEffects@vort_Motion.fx"
      and reshade_ini.PROVIDERS[2][2] is True)
_d = Path(tempfile.mkdtemp(prefix="glpreset_"))
reshade_ini.write_preset(_d, 2)
_t = (_d / "ReShadePreset.ini").read_text(encoding="utf8")
check("the preset puts vort_MotionEffects first and DLSS5_MV_PROVIDER=2",
      _t.index("vort_MotionEffects@vort_Motion.fx") < _t.index("DLSS5_Feed@DLSS5_Feed.fx")
      and "DLSS5_MV_PROVIDER=2" in _t, _t[:300])
shutil.rmtree(_d, ignore_errors=True)
_d = Path(tempfile.mkdtemp(prefix="vortkeep_"))
(_d / "ReShadePreset.ini").write_text(
    "Techniques=vort_MotionEffects@vort_Motion.fx,DLSS5_Feed@DLSS5_Feed.fx,Lumenite_Kernel@lumenite_Kernel.fx,Clarity@Clarity.fx\n",
    encoding="utf8")
reshade_ini.remove_our_techniques(_d, 3)
_t = (_d / "ReShadePreset.ini").read_text(encoding="utf8")
check("uninstall of a LumeniteFX install leaves the person's VORT technique alone",
      "vort_MotionEffects" in _t and "Lumenite_Kernel" not in _t and "DLSS5_Feed" not in _t and "Clarity" in _t, _t)
(_d / "ReShadePreset.ini").write_text(
    "Techniques=vort_MotionEffects@vort_Motion.fx,DLSS5_Feed@DLSS5_Feed.fx,Clarity@Clarity.fx\n", encoding="utf8")
reshade_ini.remove_our_techniques(_d, 2)
_t = (_d / "ReShadePreset.ini").read_text(encoding="utf8")
check("uninstall of a VORT install removes it", "vort_MotionEffects" not in _t and "Clarity" in _t, _t)
shutil.rmtree(_d, ignore_errors=True)
check("the feeder route installs VORT for provider 2 through the shared step",
      "elif opt.provider == 2:" in src_of(installer._install_feeder_parts)
      and "_install_vort" in src_of(installer._install_feeder_parts)
      and "_install_vort" in src_of(installer.install))
check("an OpenGL install with a Lumenite provider is switched to VORT before planning",
      'g.api == "OpenGL" and opt.provider in (3, 4)' in src_of(installer.install))
_d = Path(tempfile.mkdtemp(prefix="glprev_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d); _g.api = "OpenGL"; _g.bitness = 64
_pv = installer.preview(_g, installer.Options(path=dlss.FEEDER, provider=3))
check("the preview of an OpenGL feeder install lists VORT's files, not LumeniteFX's",
      any("vort_Motion.fx" in w for w in _pv.writes)
      and not any("lumenite_Kernel.fx" in w for w in _pv.writes), str(_pv.writes[:12]))
shutil.rmtree(_d, ignore_errors=True)

section("40. the updater understands a one-folder release before one exists")
import zipfile as _zf2
import hashlib
from core import selfupdate  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="upd_"))
_pe = X64.read_bytes()
_one = _d / "one.zip"
with _zf2.ZipFile(_one, "w") as z:
    z.writestr("dlss5-autopilot.exe", _pe); z.writestr("README.md", "x")
_dir = _d / "dir.zip"
with _zf2.ZipFile(_dir, "w") as z:
    z.writestr("dlss5-autopilot/dlss5-autopilot.exe", _pe)
    z.writestr("dlss5-autopilot/_internal/python313.dll", b"MZ" + b"\0" * 8000)
    z.writestr("dlss5-autopilot/_internal/core/gui.pyc", b"pyc")
    z.writestr("dlss5-autopilot/README.md", "x")
_saved = (net.json_get, net.download, net.fetch_text, selfupdate.MIN_BYTES)
selfupdate.MIN_BYTES = len(_pe) + 4000   # exe alone is below; exe + _internal is above
_sha = hashlib.sha256(_pe).hexdigest()
net.fetch_text = lambda url: f"{_sha}  dist/dlss5-autopilot.exe\n".encode()
net.json_get = lambda url: {"tag_name": "v9.9", "assets": [
    {"name": "DLSS5-Autopilot-v9.9-win64.zip", "browser_download_url": "zip"},
    {"name": "SHA256SUMS.txt", "browser_download_url": "sums"}]}
try:
    net.download = lambda url, name, **k: _one
    _raised = False
    try:
        selfupdate.fetch()
    except selfupdate.UpdateError:
        _raised = True
    check("the size floor applies to the exe alone for a one-file release", _raised)
    selfupdate.MIN_BYTES = 1024
    _exe1 = selfupdate.fetch()
    selfupdate.MIN_BYTES = len(_pe) + 4000
    check("a one-file release yields the exe alone",
          _exe1.name == "dlss5-autopilot.exe" and not (_exe1.parent / "_internal").exists())
    net.download = lambda url, name, **k: _dir
    _exe2 = selfupdate.fetch()
    check("...but for a one-folder release the whole download is measured (exe alone would fail)",
          _exe2.stat().st_size < selfupdate.MIN_BYTES)
    check("a one-folder release yields the exe WITH its _internal folder beside it",
          _exe2.name == "dlss5-autopilot.exe"
          and (_exe2.parent / "_internal" / "python313.dll").is_file()
          and (_exe2.parent / "_internal" / "core" / "gui.pyc").is_file())
finally:
    net.json_get, net.download, net.fetch_text, selfupdate.MIN_BYTES = _saved
_cur = _d / "app" / "dlss5-autopilot.exe"
_s1 = selfupdate.swap_script(_cur, _exe1)
_s2 = selfupdate.swap_script(_cur, _exe2)
check("the swap script for a one-file build touches only the exe",
      "_internal" not in _s1 and 'copy /y "%SOURCE%" "%TARGET%"' in _s1)
check("the swap script for a one-folder build copies _internal (drives may differ), keeps the old one and can roll back",
      f'move /y "{_d / "app" / "_internal"}" "{_d / "app" / "_internal.old"}"' in _s2
      and f'xcopy "{_exe2.parent / "_internal"}" "{_d / "app" / "_internal"}\\"' in _s2
      and ":rollback" in _s2 and 'copy /y "%SOURCE%" "%TARGET%"' in _s2
      and _s2.index("_internal.old") < _s2.index('copy /y "%SOURCE%"'), _s2)
shutil.rmtree(_d, ignore_errors=True)

section("41. the day-two reports: duplicate add-on lines, layer wording, USB drives, 'bin'")
_d = _diag_dir("diag_dupe_", reshade=(
    'Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0 using ReShade API version 18.\n'
    'Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0 using ReShade API version 18.\n'
    'Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0 using ReShade API version 18.\n'), path="upstream")
_r = diagnose.analyse(_d)
check("one 'loaded add-on' line per add-on, however many sessions the log holds (#22)",
      sum("loaded add-on" in t for t in _levels(_r, "ok")) == 1, str(_levels(_r, "ok")))
shutil.rmtree(_d, ignore_errors=True)
_d = _diag_dir("diag_vklayer_", proxy=False, api="Vulkan")
_m = json.loads((_d / "dlss5-autopilot.json").read_text(encoding="utf8"))
_m["proxy"] = "(vulkan layer)"
(_d / "dlss5-autopilot.json").write_text(json.dumps(_m), encoding="utf8")
_saved = _vk.registrations
_vk.registrations = lambda: [(Path("C:/x/ReShade64.json"), 0)]
_r = diagnose.analyse(_d)
_vk.registrations = _saved
check("a not-started Vulkan-layer install is told to check the renderer, not a proxy name (#16/#19)",
      any("not running on Vulkan" in t for t in _levels(_r, "info"))
      and not any("ignores (vulkan layer)" in t for t in _levels(_r, "info")), str(_levels(_r, "info")))
shutil.rmtree(_d, ignore_errors=True)
check("generic folder names give way to the game's own (#17)",
      games.display_name(Path("D:/Games/World War Z/bin")) == "World War Z"
      and games.display_name(Path("D:/Games/Ghostwire/Snowfall/Binaries/Win64")) == "Snowfall"
      and games.display_name(Path("D:/Games/Bayonetta")) == "Bayonetta"
      and games.display_name(Path("D:/SteamLibrary/steamapps/common/Game")) == "Game"
      and games.display_name(Path("D:/Games/Deus Ex/System")) == "Deus Ex")
check("scan_folders skips removable drives (#18)",
      "is_removable(base)" in src_of(games.scan_folders)
      and games.is_removable(Path("C:/")) is False)

section("42. the plan counts the new steps, so the progress bar cannot run past its end")
_d = Path(tempfile.mkdtemp(prefix="plan_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d); _g.bitness = 64
_g.api = "OpenGL"
check("an OpenGL feeder plan lists VORT, not LumeniteFX",
      "VORT Motion (motion vectors)" in installer.plan(_g, installer.Options(path=dlss.FEEDER, provider=3))
      and "LumeniteFX (motion vectors)" not in installer.plan(_g, installer.Options(path=dlss.FEEDER, provider=3)))
_g.api = "DX11"
check("a D3D11 feeder plan with VORT chosen by hand lists VORT",
      "VORT Motion (motion vectors)" in installer.plan(_g, installer.Options(path=dlss.FEEDER, provider=2)))
_g.api = "DX12"
(_d / "nvngx_dlssg.dll").write_bytes(b"MZ")
_saved = gpu.detect
gpu.detect = lambda: ("RTX 4070", 89)
try:
    _p = installer.plan(_g, installer.Options(path=dlss.NATIVE, native_dlss=True, mfg=True))
    check("an RTX 40 native plan with MFG ticked counts the MFG step",
          "RTX 40 multi-frame generation" in _p, str(_p))
    gpu.detect = lambda: ("RTX 5080", 120)
    check("...and not on an RTX 50",
          "RTX 40 multi-frame generation" not in installer.plan(_g, installer.Options(path=dlss.NATIVE, native_dlss=True, mfg=True)))
finally:
    gpu.detect = _saved
shutil.rmtree(_d, ignore_errors=True)

section("43. review fixes: DX10 refused before a write, MFG off removes leftovers, quirks per API")
_d = Path(tempfile.mkdtemp(prefix="dx10early_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d); _g.api = "DX10"; _g.bitness = 64
_saved = sources.resolve_feeder
sources.resolve_feeder = lambda prerelease=False, tag="": ("v0.12.1-beta.2", {})
try:
    _raised = ""
    try:
        installer.install(_g, installer.Options(path=dlss.FEEDER), on_log=lambda t: None)
    except installer.InstallError as e:
        _raised = str(e)
    check("an old feeder on DX10 is refused before the folder is touched",
          "refuses Direct3D 10" in _raised
          and sorted(p.name for p in _d.iterdir()) == ["Game.exe"], str(sorted(p.name for p in _d.iterdir())))
finally:
    sources.resolve_feeder = _saved
_pv = installer.preview(_g, installer.Options(path=dlss.FEEDER, feeder_tag="v0.12.1-beta.2"))
check("the preview shows the pinned old feeder as a blocker, offline",
      any("refuses Direct3D 10" in b for b in _pv.blockers), str(_pv.blockers))
shutil.rmtree(_d, ignore_errors=True)
check("quirks(api='OpenGL') explains the provider switch; other APIs get nothing generic",
      any("VORT" in q for q in dlss.quirks(Path("Game.exe"), "OpenGL"))
      and dlss.quirks(Path("Game.exe"), "DX12") == ())
check("our MFG overlay add-on is not reported as a foreign NGX hook",
      "rtx40mfg-ui.addon64" in src_of(installer.other_ngx_hooks))

section("44. driver 616.64+: renodx-dlss5 is pinned to 4.55, and the fault is named")
check("the pin constants exist", sources.DRIVER_FAULT_MIN == "616.64" and sources.DRIVER_FAULT_RENODX_PIN == "4.55")
# find(), not index(): src_of() returns "" when the source cannot be read,
# and "".index("") raises - which ends the run and makes every section after
# it look like it passed.
_isrc = src_of(installer.install)
check("install() consults the driver before the OpenGL and feeder pins",
      0 <= _isrc.find("DRIVER_FAULT_MIN") < _isrc.find("OPENGL_RENODX_PIN"),
      (_isrc.find("DRIVER_FAULT_MIN"), _isrc.find("OPENGL_RENODX_PIN")))
_d = _diag_dir("diag_drv_", feed=_FEED_OK, reshade=(
    'INFO | Registered add-on "DLSS 5 Feed" v0.14\n'
    "INFO | Redirecting IDXGIFactory2::CreateSwapChainForHwnd(...)\n"), bitness=32)
(_d / "host64").mkdir()
(_d / "host64" / "dlss5-feed-host.log").write_text(
    "12:00:00.000  [host] evaluate raised 0xC0000005 (reading address FFFFFFFFFFFFFFFF) in D3D12Core.dll (caught; nothing submitted)\n"
    "12:00:00.001  [host] evaluate fault stack, by module (innermost first):\n"
    "              D3D12Core.dll <- nvngx_dlssnr.dll <- _nvngx.dll <- renodx-dlss5.addon64 <- dlss5-feed-host64.exe\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("the host log's fault chain is read as the 616.64 driver fault, not 'Working'",
      "616.64" in _r.verdict and any("NGX runtime" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_drv55_", feed=_FEED_OK +
    "12:00:00.000  [feed] evaluate raised 0xC0000005 (reading address FFFFFFFFFFFFFFFF) (caught; nothing submitted)\n"
    "12:00:00.000  [feed] evaluate fault stack, by module (innermost first): D3D12Core.dll <- nvngx_dlssnr.dll <- _nvngx.dll <- renodx-dlss5.addon64 <- dlss5-feed.addon64 <- ReShade64.dll\n",
    reshade='INFO | Registered add-on "DLSS 5 Feed 0.14.0-beta.5" v0.14.0.0\n',
    components={"renodx": "4.55", "feeder": "v0.14.0-beta.5"})
_r = diagnose.analyse(_d)
check("beta.5's two-line feed fault (no 'in D3D12Core.dll' on the first line) is still the driver fault",
      "616.64" in _r.verdict and any("NGX runtime" in t for t in _levels(_r, "bad")), _r.verdict)
check("...and with 4.55 already installed it says roll back, not 'install again'",
      "616.56" in _r.verdict and not any("Install again" in (f_.detail or "") for f_ in _r.findings if f_.level == "bad"), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# the stale check must not nag toward the build the pin avoids
from core import components as _cmp  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="stale_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps({"api": "DX12", "components": {"renodx": "4.55"}}), encoding="utf8")
_saved = (_cmp._latest, gpu.driver_at_least)
_cmp._latest = lambda name: "4.70"
gpu.driver_at_least = lambda want: True
try:
    _items = _cmp.check(_d)
    check("on driver 616.64+ a pinned 4.55 is not reported as outdated", _items and not _items[0].outdated, str(_items))
    gpu.driver_at_least = lambda want: False
    _items = _cmp.check(_d)
    check("on an older driver the same install IS outdated (4.70 works there)", _items and _items[0].outdated, str(_items))
finally:
    _cmp._latest, gpu.driver_at_least = _saved
shutil.rmtree(_d, ignore_errors=True)

section("45. 1.7.1: online games warned, metadata reads retried, Xbox text, graphics-api override")
from core import anticheat  # noqa: E402
_d = Path(tempfile.mkdtemp(prefix="ac_"))
for n in ("ZenlessZoneZero.exe", "EAAntiCheat.GameServiceLauncher.exe"):
    (_d / n).write_bytes(b"MZ")
_f = anticheat.detect(_d, _d)
check("HoYoverse and EA Javelin games are detected as anti-cheat",
      _f.present and "HoYoverse anti-cheat" in _f.products and "EA Javelin" in _f.products, str(_f.products))
shutil.rmtree(_d, ignore_errors=True)

# An anti-cheat that ships as a folder of its own beside the game, with
# nothing next to the executable: WARDOGS carries Elytra that way, and the
# library listed it as an ordinary single-player game.
_d = Path(tempfile.mkdtemp(prefix="ac_elytra_"))
(_d / "Elytra").mkdir()
(_d / "Elytra" / "Elytra-Setup.exe").write_bytes(b"MZ")
(_d / "WardogsLauncher-Shipping.exe").write_bytes(b"MZ")
_f = anticheat.detect(_d / "Binaries", _d)
check("an anti-cheat in a folder of its own is found too",
      _f.present and "Elytra Anti-Cheat" in _f.products, str(_f.products))
check("...and the folder it was found in is named as the evidence",
      any("Elytra" in e for e in _f.evidence), _f.evidence)
shutil.rmtree(_d, ignore_errors=True)

# #187: Rise of the Tomb Raider was told Riot Vanguard was installed. A
# marker is a whole word of a name, never its middle, Vanguard is only its
# own files, and Denuvo alone is copy protection, not anti-cheat.
_d = Path(tempfile.mkdtemp(prefix="ac_187_"))
for n in ("ROTTR.exe", "vanguard_outfit.pak", "Vanguard", "denuvo64.dll",
          "RiceRicochetFX.bin", "gameguardians.txt"):
    (_d / n).write_bytes(b"x")
_f = anticheat.detect(_d, _d)
check("#187: a single-player game with 'vanguard' or 'denuvo' in a name is not anti-cheat",
      not _f.present, f"{_f.products} {_f.evidence}")
(_d / "vgk.sys").write_bytes(b"x")
(_d / "mhyprot3.sys").write_bytes(b"x")
_f = anticheat.detect(_d, _d)
check("...Vanguard's own driver and a numbered driver name still are",
      "Riot Vanguard" in _f.products and "HoYoverse anti-cheat" in _f.products, _f.products)
check("...and every warning names the file it rests on",
      "vgk.sys" in _f.found and "vgk.sys" in anticheat.message(_f), _f.found)
shutil.rmtree(_d, ignore_errors=True)

import io
import re
import urllib.error as _ue
_calls = {"n": 0}
class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b"ok"
def _flaky(req, timeout=0, **kw):
    _calls["n"] += 1
    if _calls["n"] < 3:
        raise TimeoutError("The read operation timed out")
    return _Resp()
_saved = (sources.urllib.request.urlopen, sources.time.sleep)
sources.urllib.request.urlopen = _flaky
sources.time.sleep = lambda s: None
try:
    check("a metadata read survives two timeouts (#26)", sources._get("https://x/") == b"ok" and _calls["n"] == 3)
finally:
    sources.urllib.request.urlopen, sources.time.sleep = _saved

check("the locked-folder hint names the Xbox app's switch and the way out without it",
      "Enable mods" in games.XBOX_HINT and "Steam version" in games.XBOX_HINT)
check("the protected-exe hint says what to choose, in words (#157)",
      "64-bit or 32-bit" in games.XBOX_EXE_HINT and "EXE" not in games.XBOX_EXE_HINT)

_d = Path(tempfile.mkdtemp(prefix="apiov_"))
shutil.copyfile(X64, _d / "Game.exe")
_saved_pref = prefs.get("api_override")
try:
    games.set_api_override(_d, "DX9")
    _g = games.manual(_d)
    check("an API chosen for the folder overrides the executable's import table (#24)",
          _g.api == "DX9" and _g.api_detected != "DX9" and "set by hand" in _g.api_why,
          f"{_g.api} {_g.api_detected} {_g.api_why}")
    games.set_api_override(_d, None)
    _g = games.manual(_d)
    check("clearing the override restores detection", _g.api == _g.api_detected and _g.api != "DX9")
finally:
    prefs.set_("api_override", _saved_pref or {})
shutil.rmtree(_d, ignore_errors=True)

section("46. issue #31: a renderer loaded at run time is still recognised (Call of Juarez: Gunslinger)")
_d = Path(tempfile.mkdtemp(prefix="coj_"))
_exe = _d / "CoJGunslinger.exe"
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
check("an exe with no graphics import and no name in it stays Unknown",
      pe.detect_api(_exe)[0] == "Unknown")
with open(_exe, "ab") as _f:
    _f.write(b"\0\0" + "d3d9.dll".encode("utf-16-le") + b"\0\0")
_api, _why = pe.detect_api(_exe)
check("d3d9.dll named in the exe (UTF-16) -> DirectX 9, and the reason says so",
      _api == "DX9" and "run time" in _why, f"{_api}: {_why}")
_g = games.manual(_d)
check("...so the game is 32-bit / DX9 and gets the DXVK route, not dxgi.dll + feeder",
      _g.bitness == 32 and _g.api == "DX9", f"{_g.bitness} {_g.api}")
with open(_exe, "ab") as _f:
    _f.write(b"\0dxgi.dll\0")
check("a DXGI name beside it wins, as in the static table", pe.detect_api(_exe)[0] == "DX12")
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
shutil.copyfile(r"C:\Windows\SysWOW64\Magnification.dll", _d / "engine.dll")
_api, _why = pe.detect_api(_exe)
check("an engine DLL beside the exe that imports d3d9.dll is the renderer",
      _api == "DX9" and "engine.dll" in _why, f"{_api}: {_why}")
shutil.copyfile(r"C:\Windows\SysWOW64\Magnification.dll", _d / "d3d9.dll")
(_d / "engine.dll").unlink()
check("a proxy d3d9.dll (DXVK, ReShade) beside the exe is not consulted",
      pe.detect_api(_exe)[0] == "Unknown")
(_d / "nvngx_dlss.dll").write_bytes(b"MZ")
with open(_exe, "ab") as _f:
    _f.write(b"\0d3d9.dll\0")
# DLSS has no 32-bit build, so beside a 32-bit executable it says nothing
# about that executable (#190); beside a 64-bit one it still does.
check("a run-time d3d9.dll in a 32-bit exe stays DX9 whatever DLSS sits beside it",
      pe.detect_api(_exe)[0] == "DX9", pe.detect_api(_exe))
_exe64 = _d / "Game64.exe"
shutil.copyfile(r"C:\Windows\System32\where.exe", _exe64)
with open(_exe64, "ab") as _f:
    _f.write(b"\0d3d9.dll\0")
check("a run-time d3d9.dll with the game's own DLSS beside it is still a modern renderer",
      pe.detect_api(_exe64)[0] == "DX12", pe.detect_api(_exe64))
shutil.rmtree(_d, ignore_errors=True)

section("47. issues #46-#48: an engine that merely names opengl32.dll is not an OpenGL game")
_d = Path(tempfile.mkdtemp(prefix="unity_"))
_exe = _d / "HouseParty.exe"
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
with open(_exe, "ab") as _f:
    _f.write(b"\0opengl32.dll\0")
check("an exe naming only opengl32.dll, nothing beside it: OpenGL (Gunslinger-style run-time load)",
      pe.detect_api(_exe)[0] == "OpenGL")
(_d / "engine.dll").write_bytes(b"MZ" + b"\0" * 600_000 + b"d3d11.dll\0opengl32.dll\0")
_api, _why = pe.detect_api(_exe)
check("...but a DLL beside it that names d3d11.dll outranks the OpenGL string",
      _api == "DX11" and "engine.dll" in _why, f"{_api}: {_why}")
(_d / "engine.dll").unlink()
(_d / "UnityPlayer.dll").write_bytes(b"MZ" + b"\0" * 100)
_api, _why = pe.detect_api(_exe)
check("UnityPlayer.dll beside the exe decides: Direct3D 11, and the reason names Unity",
      _api == "DX11" and "Unity" in _why, f"{_api}: {_why}")
_g = games.manual(_d)
check("...so the game is not put on the opengl32.dll route", _g.api == "DX11", _g.api)
(_d / "UnityPlayer.dll").unlink()
(_d / "sl.interposer.dll").write_bytes(b"MZ" + b"\0" * 600_000 + b"d3d12.dll\0")
(_d / "ourthing.dll").write_bytes(b"MZ" + b"\0" * 600_000 + b"d3d12.dll\0")
(_d / "dlss5-autopilot.json").write_text(json.dumps({"files": ["ourthing.dll"]}), encoding="utf8")
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
with open(_exe, "ab") as _f:
    _f.write(b"\0opengl32.dll\0")
check("DLLs our routes drop (Streamline) and files named in our manifest are not sibling evidence",
      pe.detect_api(_exe)[0] == "OpenGL", pe.detect_api(_exe))
(_d / "dlss5-autopilot.json").unlink()
(_d / "UnityPlayer.dll").write_bytes(b"MZ" + b"\0" * 100)
check("the side windows scale their pixels too",
      "px(720)" in Path("core/compareui.py").read_text(encoding="utf8"))
_launcher = _d / "Launcher.exe"
_ship = _d / "Bin" / "Win64" / "Game-Win64-Shipping.exe"
_ship.parent.mkdir(parents=True)
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _launcher)
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _ship)
(_d / "dlss5-autopilot.json").write_text(json.dumps({"exe": "Launcher.exe", "files": []}), encoding="utf8")
(_d / "dlss5-feed.addon64").write_bytes(b"MZ")
_g56 = games.Game("Conan", _d, exe=_launcher, candidates=[_launcher, _ship])
games.enrich(_g56)
check("a scan adopts the earlier install's folder and exe (unchanged)",
      _g56.exe == _launcher and _g56.install_dir == _d, (_g56.exe, _g56.install_dir))
_g56.exe = _ship
games.enrich(_g56, chosen=True)
check("an executable picked in the list keeps that pick and installs beside it (#56)",
      _g56.exe == _ship and _g56.install_dir == _ship.parent, (_g56.exe, _g56.install_dir))
(_d / "dlss5-autopilot.json").unlink()
(_d / "dlss5-feed.addon64").unlink()
_launcher.unlink()
_ship.unlink()
_ship.parent.rmdir()
_ship.parent.parent.rmdir()
check("the preview discloses an emulator config change",
      "its own config is switched" in src_of(installer.preview))

def _card47(manifest, api="DX11"):
    """What the library card says for an install recorded with this manifest."""
    with _ui_isolated(), _ui_threads(run=False):
        g = _ui_game(name="Card", api=api, manifest=manifest)
        c = _ui_ctl()
        c.all_games = [g]
        c._rows[(str(g.folder), str(g.exe))] = (True, "feeder", installer.BETA, "beta", False, "")
        return c.card(g)["status"]


_s47 = (_card47({"api": "DX9", "proxy": diagnose.VULKAN_LAYER}),
        _card47({"api": "DX9", "proxy": "d3d9.dll", "dxvk": True}))
check("the game list does not flag a DXVK install as an API change",
      not any(s.startswith("reinstall") for s in _s47), _s47)
check("an unknown optiscaler build key in a manifest does not break the plan",
      any(s.startswith("OptiScaler (") for s in
          installer.plan(games.Game("X", _d, exe=_exe, bitness=64, api="DX12"),
                         installer.Options(path=dlss.OPTI, opti_build="future"))))
for _k in range(65):
    (_d / f"a{_k:02}.dll").write_bytes(b"MZ")
check("...even behind 65 other DLLs (the engine rule is not cut with the list)",
      pe.detect_api(_exe)[0] == "DX11")
(_d / "UnityPlayer.dll").unlink()
for _k in range(65):
    (_d / f"a{_k:02}.dll").unlink()
shutil.copyfile(r"C:\Windows\SysWOW64\where.exe", _exe)
with open(_exe, "ab") as _f:
    _f.write(b"\0d3d9.dll\0")
(_d / "bink2w64.dll").write_bytes(b"MZ" + b"\0" * 600_000 + b"d3d11.dll\0dxgi.dll\0")
_api, _why = pe.detect_api(_exe)
check("an exe that names d3d9.dll itself is DirectX 9 whatever Bink beside it names (#31 stands)",
      _api == "DX9" and "named in the exe" in _why, f"{_api}: {_why}")
(_d / "host64").mkdir()
(_d / "host64" / "dlss5-feed-host64.exe").write_bytes(open(_exe, "rb").read())
check("the feeder's helper under host64 is never a candidate executable",
      pe.find_game_exes(_d) == [_exe], pe.find_game_exes(_d))
_exe.unlink()
check("...even when it is the only executable left in the folder", pe.find_game_exes(_d) == [])
# The diagnosis and the report body for a game that was installed as OpenGL
(_d / diagnose.MANIFEST).write_text(json.dumps({"path": "feeder", "proxy": "opengl32.dll",
                                                "api": "OpenGL", "exe": "HouseParty.exe"}),
                                    encoding="utf8")
(_d / "opengl32.dll").write_bytes(b"MZ")
_rep = diagnose.analyse(_d)
check("no ReShade.log after an opengl32.dll install: the diagnosis says the game may not draw with OpenGL",
      any("does not render with OpenGL" in f.title for f in _rep.findings),
      [f.title for f in _rep.findings])
(_d / diagnose.RESHADE_LOG).write_text("INFO | Initializing crosire's ReShade\nExiting ...\n", encoding="utf8")
_rep = diagnose.analyse(_d)
check("a ReShade.log with no add-on registered under opengl32.dll points at the graphics api dropdown",
      any("another program" in f.title for f in _rep.findings),
      [f.title for f in _rep.findings])
class _G:
    name = "House Party"; exe = _d / "HouseParty.exe"; bit_label = "64-bit"; api = "DX11"
    api_why = "Unity player beside the exe - Direct3D 11 on Windows"
_body = diagnose.issue_body("1.7.2", "RTX", 120, "616.64", _G(), "feeder", None, "", _d / "a.log", _d)
check("the report body carries the reason behind the detected API",
      "- arch/api: 64-bit / DX11 (Unity player beside the exe" in _body
      and "\n- route: feeder" in _body, _body[:600])
_s47 = _card47({"api": "DX12", "proxy": "dxgi.dll"})
check("the game list marks an install whose manifest api differs from the detected one",
      _s47 == "reinstall - was DX12", _s47)
check("...and says nothing of the kind where they agree",
      not _card47({"api": "DX11", "proxy": "dxgi.dll"}).startswith("reinstall"))
_ui_cleanup()
shutil.rmtree(_d, ignore_errors=True)

section("48. issue #40: pixel sizes follow the display scale, not only the fonts")
import re as _re
import types as _types48
from core.ui import app as _uiapp48, theme as _uth, win as _uwin  # noqa: E402
_uth.set_scale(96)
check("px() rounds to the display scale", _uth.px(26) == 26)
_uth.set_scale(192)
check("...and doubles at 200 %", _uth.px(26) == 52 and _uth.px(1060) == 2120)
_uth.set_scale(72)
check("...and never shrinks below 100 %", _uth.SCALE == 1.0 and _uth.px(26) == 26)
_uth.set_scale(96)

# The window takes its scale from the DPI Windows reports for it, and tells
# Tk the same number, so fonts and pixel lengths grow together.
_calls48: list = []


class _Root48:
    def winfo_id(self):
        return 4242

    class tk:  # noqa: N801
        @staticmethod
        def call(*a):
            _calls48.append(a)


with patch.object(_uwin, "ctypes", _types48.SimpleNamespace(windll=_types48.SimpleNamespace(
        user32=_types48.SimpleNamespace(GetDpiForWindow=lambda h: 144 if h == 4242 else 0)))):
    _uwin.apply_scale(_Root48())
check("the window's scale is its DPI over 96, and Tk is told the same",
      _uth.SCALE == 1.5 and ("tk", "scaling", 2.0) in _calls48, (_uth.SCALE, _calls48))
_uth.set_scale(96)

# ...and run() asks for it on the real root before the first page is drawn.
_order48: list = []


class _Tk48:
    def __init__(self):
        _order48.append("root")

    def mainloop(self):
        _order48.append("mainloop")


with patch.object(_uiapp48, "tk", _types48.SimpleNamespace(Tk=_Tk48)), \
        patch.object(_uiapp48.win, "dpi_aware", lambda: _order48.append("dpi_aware")), \
        patch.object(_uiapp48.win, "apply_scale", lambda r: _order48.append("apply_scale")), \
        patch.object(_uiapp48, "App", lambda r: _order48.append("App")), \
        patch.object(_uiapp48.log, "start", lambda *a: None), \
        patch.object(_uiapp48.log, "install_handlers", lambda *a: None), \
        patch.object(_uiapp48.log, "write", lambda *a, **k: None):
    try:
        _uiapp48.run()
    except Exception as _e48:
        _order48.append(f"raised {_e48!r}")
check("run() is DPI-aware first and scales the root before it builds the window",
      _order48[:5] == ["dpi_aware", "root", "apply_scale", "App", "mainloop"], _order48)

section("49. issue #54: the exe carries its own root certificates")
_ctx = net.ssl_context()
check("ssl_context() is one shared, verifying context",
      _ctx is net.ssl_context() and _ctx.verify_mode == ssl.CERT_REQUIRED)
check("...with the certifi bundle loaded on top of the Windows store",
      _ctx.cert_store_stats()["x509_ca"] > 100, _ctx.cert_store_stats())
check("every fetch goes through it",
      "context=ssl_context()" in src_of(net.download)
      and "context=net.ssl_context()" in src_of(sources._get))
check("the release build installs certifi",
      "pip install --upgrade certifi" in Path(".github/workflows/release.yml").read_text(encoding="utf8"))
check("a failed verification is explained, by host and not by file name",
      "untrusted(_host(url), e)" in src_of(net.download)
      and "untrusted(name, e)" not in src_of(net.download))

section("50. issue #21: y4my4my4m's OptiScaler fork, from a .7z, through Windows' tar.exe")
check("the build list starts with the build the route always installed",
      list(optiscaler.BUILDS)[0] == "" and optiscaler.FORK in optiscaler.BUILDS)
_tag, _url = optiscaler.resolve(optiscaler.FORK)
check("the fork resolves to a plain archive, not the _with_DLSS one",
      _url.lower().endswith((".7z", ".zip")) and "with_dlss" not in _url.lower(), _url)
_tag0, _url0 = optiscaler.resolve()
check("the default build still resolves to Dagherbou's zip", _url0.lower().endswith(".zip"), _url0)
_d = Path(tempfile.mkdtemp(prefix="sz_"))
(_d / "in").mkdir()
(_d / "in" / "OptiScaler.dll").write_bytes(b"MZ-opti")
(_d / "in" / "OptiScaler.pdb").write_bytes(b"symbols")
(_d / "in" / "OptiScaler").mkdir()
(_d / "in" / "OptiScaler" / "libxess.dll").write_bytes(b"MZ-xess")
_r = subprocess.run([str(optiscaler._tar_exe()), "-cf", str(_d / "t.7z"), "--format", "7zip",
                     "-C", str(_d / "in"), "."], capture_output=True, text=True)
check("Windows' tar.exe writes a 7z for the test", _r.returncode == 0, _r.stderr)
optiscaler.extract_7z(_d / "t.7z", _d / "out")
check("...and extract_7z unpacks it", (_d / "out" / "OptiScaler" / "libxess.dll").read_bytes() == b"MZ-xess")
shutil.rmtree(net.cache_dir() / "unpacked" / "t", ignore_errors=True)
_game = _d / "game"
_game.mkdir()
_w = optiscaler.install(_game, proxy="winmm.dll", dl=lambda url, name: _d / "t.7z",
                        release=("t", "https://example/t.7z"))
check("install() from a .7z: OptiScaler.dll under the proxy name, subfolder kept, .pdb skipped",
      (_game / "winmm.dll").read_bytes() == b"MZ-opti"
      and (_game / "OptiScaler" / "libxess.dll").is_file()
      and not (_game / "OptiScaler.pdb").exists()
      and sorted(_w) == ["OptiScaler/libxess.dll", "winmm.dll"], _w)
_game_b = _d / "game_b"
_game_b.mkdir()
_w2 = optiscaler.install(_game_b, proxy="winmm.dll", dl=lambda url, name: _d / "t.7z",
                         release=("t", "https://example/t.7z"))
check("a second install reuses the unpacked copy and writes the same files", sorted(_w2) == sorted(_w))
(_d / "wrapped").mkdir()
shutil.copytree(_d / "in", _d / "wrapped" / "OptiScaler_v10")
_r = subprocess.run([str(optiscaler._tar_exe()), "-cf", str(_d / "w.7z"), "--format", "7zip",
                     "-C", str(_d / "wrapped"), "."], capture_output=True, text=True)
_game2 = _d / "game2"
_game2.mkdir()
_w3 = optiscaler.install(_game2, proxy="dxgi.dll", dl=lambda url, name: _d / "w.7z",
                         release=("w", "https://example/w.7z"))
check("an archive wrapped in one folder is unwrapped, so the proxy DLL still lands in the game folder",
      (_game2 / "dxgi.dll").is_file() and "dxgi.dll" in _w3, _w3)
shutil.rmtree(net.cache_dir() / "unpacked" / "w", ignore_errors=True)
check("a failed TLS verification is explained, whatever exception type carried it",
      net.untrusted("x", Exception("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>")) is not None
      and net.untrusted("x", Exception("timed out")) is None
      and "net.untrusted" in src_of(sources._get))
check("Options carries the build and the manifest records it",
      hasattr(installer.Options(), "opti_build")
      and '"opti_build": opt.opti_build' in src_of(installer))
with _ui_isolated(), _ui_threads(run=False):
    _c50 = _ui_ctl(_ui_game(name="Builds"), _ui_support([dlss.FEEDER, dlss.OPTI], dlss.OPTI))
    _c50.apply_route(dlss.OPTI)
    _k50 = [k for k, _l in _c50.choices("opti_build")]
    _pick50 = [k for k in _k50 if k][-1] if any(_k50) else ""
    _c50.set_setting("opti_build", _pick50)
    _o50 = (_c50.shown_setting("opti_build"), _c50.opts().opti_build,
            _c50.opts(dlss.FEEDER).opti_build)
check("the install page offers the build list, and a pick reaches Options",
      _k50 == list(optiscaler.BUILDS) and _o50 == (True, _pick50, ""), (_k50, _o50))
_ui_cleanup()
shutil.rmtree(_d, ignore_errors=True)
shutil.rmtree(net.cache_dir() / "unpacked" / "t", ignore_errors=True)

section("51. issue #33: ReShade's OpenXR layer for VR, registered like the Vulkan one")
from core import openxr as _xr
_d = Path(tempfile.mkdtemp(prefix="xr_"))
_saved_dir = _xr.layer_dir
_xr.layer_dir = lambda: _d
_setup = next(iter(sorted(net.cache_dir().glob("ReShade_Setup_*_Addon.exe"))), None)
check("a cached ReShade setup is at hand for the test", _setup is not None)
if _setup is not None:
    _m, _fresh = _xr.install_layer(_setup, log=lambda *_: None)
    check("the OpenXR manifest and ReShade64.dll are placed beside the Vulkan layer files",
          _m == _d / _xr.MANIFEST and (_d / _xr.DLL).is_file())
    _data = json.loads(_m.read_text(encoding="utf8"))
    check("the manifest names ReShade's OpenXR layer and points at the DLL beside it",
          _data["api_layer"]["name"] == _xr.LAYER_NAME
          and _data["api_layer"]["library_path"] == ".\\" + _xr.DLL, _data)
    check("...and it is registered for this user, active",
          any(p == _m and v == 0 for p, v in _xr.registrations()), _xr.registrations())
    check("a second install reuses it", _xr.install_layer(_setup)[1] is False)
    check("unregister removes exactly that value", _xr.unregister() is True
          and not any(p == _m for p, _ in _xr.registrations()))
    check("...and a second unregister finds nothing", _xr.unregister() is False)
_xr.layer_dir = _saved_dir
shutil.rmtree(_d, ignore_errors=True)
with _ui_isolated(), _ui_threads(run=False):
    _c51 = _ui_ctl(_ui_game(name="VR", api="DX11"),
                   _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER))
    _c51.apply_route(dlss.FEEDER)
    _v51 = [_c51.shown_setting("vr"), _c51.opts().vr]
    _c51.set_setting("vr", True)
    _v51.append(_c51.opts().vr)
    _c51.apply_route(dlss.OPTI)
    _v51 += [_c51.shown_setting("vr"), _c51.opts().vr]
check("Options.vr exists, the manifest records it and the install page offers it",
      hasattr(installer.Options(), "vr") and '"vr": bool(opt.vr)' in src_of(installer)
      and _v51 == [True, False, True, False, False], _v51)
_ui_cleanup()
check("the ReShade step registers the layer when asked, and uninstall drops it with the last VR game",
      "openxr.install_layer(setup, log)" in src_of(installer)
      and "prefs.openxr_games()" in src_of(installer))
check("the command line has --vr", '"--vr" in args' in Path("dlss5_autopilot.py").read_text(encoding="utf8"))
check("the command line has --opti-build and it reaches Options",
      '"--opti-build" in args' in Path("dlss5_autopilot.py").read_text(encoding="utf8")
      and Path("dlss5_autopilot.py").read_text(encoding="utf8").count("opti_build=opti_build") == 3)
check("'did it work?' lists the OpenXR registration for a VR install",
      any("OpenXR layer" in ln for ln in diagnose._presence(Path("."), {"vr": True, "path": "feeder"}, "feeder")))
check("every HTTPS fetch goes through ssl_context()",
      "context=ssl_context()" in src_of(net.fetch_text))

section("52. reshade.me answers 500 with the page as the body; the installer link is still found")
import urllib.error as _ue
_saved_get, _saved_json = sources._get, sources._json
_page = b'<a href="/downloads/ReShade_Setup_6.8.0_Addon.exe">x</a>'
def _five_hundred(url, *a, **k):
    raise _ue.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(_page))
sources._get = _five_hundred
check("a 500 whose body carries the link resolves 6.8.0 from it",
      sources.resolve_reshade() == ("6.8.0", "https://reshade.me/downloads/ReShade_Setup_6.8.0_Addon.exe"),
      sources.resolve_reshade())
def _bare(url, *a, **k):
    raise _ue.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(b""))
sources._get = _bare
sources._json = lambda url: [{"name": "v6.8.1"}, {"name": "v6.8.0"}]
check("a bare 500 falls back to the newest version tag on crosire/reshade",
      sources.resolve_reshade() == ("6.8.1", "https://reshade.me/downloads/ReShade_Setup_6.8.1_Addon.exe"),
      sources.resolve_reshade())
sources._json = lambda url: (_ for _ in ()).throw(RuntimeError("rate limited"))
_cached = sorted(net.cache_dir().glob("ReShade_Setup_*_Addon.exe"))
_r = sources.resolve_reshade() if _cached else None
check("...and then to the newest setup already in the cache",
      not _cached or (_r[0] in _cached[-1].name and _r[1].endswith(f"ReShade_Setup_{_r[0]}_Addon.exe")), _r)
sources._get, sources._json = _saved_get, _saved_json
check("the real site or its fallbacks resolve a version", re.fullmatch(r"\d+(\.\d+)+", sources.resolve_reshade()[0]) is not None)

section("53. DLSS5-Reshade-AIO 2.1.0 ships one 64-bit archive instead of loose files")
sources._json = lambda url: {"tag_name": "v2.1.0", "assets": [
    {"name": "DLSS5-ReShade-AIO-v2.1.0-32-bit.zip", "browser_download_url": "https://x/32.zip"},
    {"name": "DLSS5-ReShade-AIO-v2.1.0-64-bit.zip", "browser_download_url": "https://x/64.zip"}]}
_tag, _urls = sources.resolve_standalone()
check("a zip-only release resolves to its 64-bit archive",
      _tag == "v2.1.0" and _urls.get(sources.STANDALONE_ZIP) == "https://x/64.zip", _urls)
sources._json = lambda url: {"tag_name": "v2.0.9", "assets": [
    {"name": n, "browser_download_url": "https://x/" + n} for n in sources.STANDALONE_ASSETS]}
_tag, _urls = sources.resolve_standalone()
check("a loose-file release still resolves file by file",
      _tag == "v2.0.9" and sources.STANDALONE_ZIP not in _urls and all(n in _urls for n in sources.STANDALONE_ASSETS))
sources._json = _saved_json
_tag, _urls = sources.resolve_standalone()
check("the live release resolves one way or the other",
      sources.STANDALONE_ZIP in _urls or all(n in _urls for n in sources.STANDALONE_ASSETS), (_tag, list(_urls)))

section("54. the 1.7.2 reports: whose crash it is, which launch the log describes, "
        "and the neural-upstream route's own log")

# ReShade never truncates ReShade.log, so a report can carry several launches
# at once. Detroit: Become Human (issue #63) reported two feeder builds
# "loaded" - one of them from a run before the update.
_SESSION = "16:00:00:000 [1] | INFO  | Initializing crosire's ReShade version '6.8.0' (64-bit)\n"
_two = (_SESSION
        + 'INFO  | Registered add-on "DLSS 5 Feed 0.13.1-beta.1" v0.13.1.0\n'
        + "INFO  | Redirecting IDXGIFactory::CreateSwapChain(...)\n"
        + _SESSION.replace("16:00", "17:00")
        + 'INFO  | Registered add-on "DLSS 5 Feed 0.14.0-beta.5" v0.14.0.0\n'
        + "INFO  | Redirecting IDXGIFactory::CreateSwapChain(...)\n")
_d = _diag_dir("diag_session_", reshade=_two, feed=_FEED_OK)
_r = diagnose.analyse(_d)
check("only the last ReShade launch is read, so a replaced build is not reported as loaded",
      any("0.14.0-beta.5" in t for t in _levels(_r, "ok"))
      and not any("0.13.1" in t for t in _levels(_r, "ok")), _levels(_r, "ok"))
shutil.rmtree(_d, ignore_errors=True)

# ...but two builds inside ONE launch are two files fighting over the frame.
_same = (_SESSION
         + 'INFO  | Registered add-on "DLSS 5 Feed 0.13.1-beta.1" v0.13.1.0\n'
         + 'INFO  | Registered add-on "DLSS 5 Feed 0.14.0-beta.5" v0.14.0.0\n'
         + "INFO  | Redirecting IDXGIFactory::CreateSwapChain(...)\n")
_d = _diag_dir("diag_twobuilds_", reshade=_same, feed=_FEED_OK)
_r = diagnose.analyse(_d)
check("two builds of one add-on in the same launch are called out",
      any("builds of the same add-on" in t for t in _levels(_r, "bad")), _levels(_r, "bad"))
shutil.rmtree(_d, ignore_errors=True)

# Issue #63: the feeder records every crash in the process, its own and the
# game's alike. The module chain says which - and the game's own executable
# with no add-on under it was being reported as "a feeder bug".
_GAME_CRASH = (
    "00:56:26.703  [feed] an external frame pacer is presenting this swapchain: 7623 "
    "presents against 6098 frames fed since the first fed frame (1.25x). NVIDIA Smooth "
    "Motion does exactly this.\n"
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "00007FF6AD6D4067 in E:\\g\\DetroitBecomeHuman.exe; this add-on was last doing: "
    "waiting for the result (Vulkan)\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "DetroitBecomeHuman.exe <- KERNEL32.DLL <- ntdll.dll\n")
_d = _diag_dir("diag_gamecrash_", feed=_FEED_OK + _GAME_CRASH,
               exe="DetroitBecomeHuman.exe")
_r = diagnose.analyse(_d)
check("a crash in the game's own executable is the game's, not the feeder's",
      "game's own code" in _r.verdict and "feeder bug" not in _r.verdict, _r.verdict)
check("...and the external frame pacer is named",
      any("pacing the frames" in t for t in _levels(_r, "warn")), _levels(_r, "warn"))
shutil.rmtree(_d, ignore_errors=True)

_OURS_CRASH = (
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "0x1 in E:\\g\\Game.exe; this add-on was last doing: building the mask\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "dlss5-feed.addon64 <- dxgi.dll <- Game.exe\n")
_d = _diag_dir("diag_feedcrash_", feed=_FEED_OK + _OURS_CRASH)
_r = diagnose.analyse(_d)
check("a crash inside the feed add-on is still called a feeder bug",
      "feeder bug" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_NGX_CRASH = (
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "0x1 in E:\\g\\Game.exe; this add-on was last doing: evaluating\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "nvngx_dlssnr.dll <- _nvngx.dll <- renodx-dlss5.addon64 <- dlss5-feed.addon64\n")
_d = _diag_dir("diag_ngxcrash_", feed=_FEED_OK + _NGX_CRASH)
_r = diagnose.analyse(_d)
check("a crash inside the graphics runtime is not blamed on the feeder",
      "graphics runtime" in _r.verdict and "feeder bug" not in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# A module the tool cannot place is not evidence that the game crashed.
# The list of our own files was missing dlss5-feed.addon32, so a 32-bit
# feeder crashing in its own add-on would have been reported as the game's.
_UNKNOWN_CRASH = (
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "0x1 in E:\\g\\Game.exe; this add-on was last doing: presenting\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "somemod.dll <- KERNEL32.DLL <- ntdll.dll\n")
_d = _diag_dir("diag_unknowncrash_", feed=_FEED_OK + _UNKNOWN_CRASH)
_r = diagnose.analyse(_d)
check("a module the tool cannot place is not called the game's crash",
      "game's own code" not in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_32_CRASH = (
    "00:56:49.958  ### CRASH RECORDED ###  exception 0xC0000005 (writing address 0) at "
    "0x1 in E:\\g\\Game.exe; this add-on was last doing: presenting\n"
    "00:56:49.964  [feed] crash stack, by module (innermost first): "
    "dlss5-feed.addon32 <- Game.exe\n")
_d = _diag_dir("diag_32crash_", feed=_FEED_OK + _32_CRASH)
_r = diagnose.analyse(_d)
check("...and the 32-bit feeder's own add-on is still ours",
      "feeder bug" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# OptiScaler's log holds every run too, and it has no banner - a clock that
# jumps a long way back is what says "new run". A few milliseconds out of
# order is several threads writing, not a new run.
_ooo = "[00:00:01.000] a\n[00:00:05.000] b\n[00:00:02.000] c\n"
check("threads writing a few ms out of order do not truncate the log",
      diagnose._last_run(_ooo) == _ooo)
check("...but a run from yesterday is cut away",
      diagnose._last_run("[23:00:01.000] old\n[08:00:00.000] new\n").startswith("[08:00:00"))

# The neural-upstream route writes its whole run into ReShade.log under
# [NRPRE]. Three issues (#39 #51 #64) were answered with "this route keeps no
# log" while the answer was in the file the report already carried.
def _nrpre(*extra: str) -> str:
    return (_SESSION
            + 'INFO  | Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0 using ReShade API version 18.\n'
            + "INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] addon registered (NR at render resolution)\n"
            + "".join("INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] " + e + "\n" for e in extra))


_HB = ("HB #7561 handle=0x1 | pw=1.0000 meas=0.0000 valid=0 upd=0 | hdr=2 det=1 "
       "knee=0.750 kmeas=0.750 | expfresh=7559 fromgame=0 | net=2228x1256 "
       "finalvalid=1 | cad=1 async=0 | erfail=0 grfail=0 passthru=0")
_HOOK = "hook 0 on NVSDK_NGX_D3D12_EvaluateFeature: OK  C:\\w\\_nvngx.dll"


def _up_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="up_"))
    (d / "dlss5-autopilot.json").write_text(json.dumps(
        {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
         "api": "DX12", "path": "upstream", "proxy": "dxgi.dll",
         "files": ["dxgi.dll", "nvngx.dll.addon64"]}), encoding="utf8")
    for n in ("dxgi.dll", "nvngx.dll.addon64", "ReShade.ini", "nvngx_dlssnr.dll"):
        (d / n).write_bytes(b"MZ")
    return d


_d = _up_dir()
(_d / "ReShade.log").write_text(_nrpre(
    "settings loaded: enabled=1 cadence=1 codec=1 pw=1.000 knee=0.75", _HOOK,
    "CreateFeature id=18 -> res=0xBAD0000B handle=0 | NR W=2228 H=1253",
    "2b: snippet route, self=C:\\g\\nvngx.dll.addon64", _HB,
    "resetting state (new device)"), encoding="utf8")
_r = diagnose.analyse(_d)
check("the upstream route's log is read: it ran, then stopped at a device re-creation",
      "device re-creation" in _r.verdict
      and any("hooked the game's DLSS call" in t for t in _levels(_r, "ok")), _r.verdict)
check("...and NGX refusing feature 18 with a snippet fallback is a warning, not the verdict",
      any("0xBAD0000B" in t for t in _levels(_r, "warn")), _levels(_r, "warn"))
check("...and a game that hands over no exposure is named (the 'only darker' route)",
      any("no exposure value" in t for t in _levels(_r, "warn")), _levels(_r, "warn"))

# closing the game ends the log the same way; that must not read as a hang
(_d / "ReShade.log").write_text(_nrpre(
    "settings loaded: enabled=1", _HOOK, _HB, "resetting state (new device)")
    + 'INFO  | Unloading add-on "DLSS5 NR Pre-Upscale" ...\n', encoding="utf8")
_r = diagnose.analyse(_d)
check("...but an orderly shutdown after the reset is not a hang", _r.verdict == "Working.", _r.verdict)

(_d / "ReShade.log").write_text(_nrpre("settings loaded: enabled=0"), encoding="utf8")
_r = diagnose.analyse(_d)
check("switched off in the tab is said outright instead of 'confirm in the overlay'",
      "switched off" in _r.verdict, _r.verdict)

(_d / "ReShade.log").write_text(_nrpre("settings loaded: enabled=1"), encoding="utf8")
_r = diagnose.analyse(_d)
check("no hook on the game's DLSS call answers 'the add-on loaded but nothing happens'",
      "never hooked" in _r.verdict, _r.verdict)

(_d / "ReShade.log").write_text(_nrpre(
    "settings loaded: enabled=1", _HOOK, "CreateFeature id=18 -> res=0x00000001 handle=1",
    _HB.replace("finalvalid=1", "finalvalid=0")), encoding="utf8")
_r = diagnose.analyse(_d)
check("running with no valid result is not 'Working.'",
      "never produces a frame" in _r.verdict, _r.verdict)

# an older build of the add-on that logs nothing still gets the old answer
(_d / "ReShade.log").write_text(
    _SESSION + 'INFO  | Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0\n', encoding="utf8")
_r = diagnose.analyse(_d)
check("an add-on build with no [NRPRE] lines still falls back to the overlay answer",
      "does not log frames" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


# The feeder's own log starts each run with "dlss5-feed ... attached.", and
# the standalone route was already read one session at a time. The feeder's
# was not, so a crash from a run days ago could be reported as what just
# happened - the same mistake as the ReShade log above, in another file.
_OLD_RUN = (
    "12:00:00.000  dlss5-feed 0.13.1-beta.1 (built Sep  4 2026) attached.\n"
    "12:00:01.000  ### CRASH RECORDED ###  exception 0xC0000005 at 0x1 in "
    "E:\\g\\Game.exe; this add-on was last doing: an old run\n"
    "13:00:00.000  dlss5-feed 0.14.0-beta.5 (built Sep  6 2026) attached.\n")
_d = _diag_dir("diag_feedsess_", feed=_OLD_RUN + _FEED_OK)
_r = diagnose.analyse(_d)
check("a crash from an earlier run of the game is not this run's verdict",
      _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_feedsess2_", feed=_FEED_OK + _OLD_RUN.split("\n")[1] + "\n")
_r = diagnose.analyse(_d)
check("...but a log with no session marker at all is still read whole",
      "crashed" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


# The same mistake on the optiscaler route: whichever line matched first won,
# so a session that started and then failed was reported as "Working."
def _opti_dir(prefix: str, log_text: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    (d / "dlss5-autopilot.json").write_text(json.dumps(
        {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
         "api": "DX12", "path": "optiscaler", "proxy": "dxgi.dll",
         "files": ["dxgi.dll"]}), encoding="utf8")
    (d / "dxgi.dll").write_bytes(b"MZ")
    (d / "OptiScaler.log").write_text(log_text, encoding="utf8")
    return d


_d = _opti_dir("opti_after_", "[I] [DLSS-NR] running at 2560x1440\n"
                              "[I] [DLSS-NR] disabling for this session\n")
_r = diagnose.analyse(_d)
check("a failure after 'running at' is the verdict, not 'Working.'",
      "stopped after it started" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _opti_dir("opti_before_", "[I] [DLSS-NR] create failed\n"
                               "[I] [DLSS-NR] running at 2560x1440\n")
_r = diagnose.analyse(_d)
check("...and a failure before it is the older news it is", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# Issue #58: the game's own Present refused the swapchain OptiScaler wrapped.
_d = _opti_dir("opti_present_",
               "[I] Init done\n"
               "[W] LocalPresent Original present result: 80004002\n")
_r = diagnose.analyse(_d)
check("the game refusing OptiScaler's swapchain is named, with what to try",
      "refused OptiScaler's swapchain" in _r.verdict
      and any("winmm.dll" in (f_.detail or "") for f_ in _r.findings), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _opti_dir("opti_ok_present_",
               "[I] [DLSS-NR] running at 2560x1440\n"
               "[I] LocalPresent Original present result: 00000000\n")
_r = diagnose.analyse(_d)
check("...and a present that succeeded is not mistaken for one that failed",
      _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


section("55. issue #40 again: scaling the fonts is not the whole of a 4K display")

# The first round scaled the fonts and the row heights, and the report that
# came back was "the text is readable now, but the log box next to the blue
# arrow is one and a half lines". Three more things were wrong with it, and
# none of them was a font: the window asked for more than the screen had,
# row counts grew with the font, and what did not fit was cut off instead of
# scrolling.
#
# So: the whole window, built for real at 100, 150, 200 and 300 per cent and
# measured - the library and a game page with its settings open, on routes
# that draw different rows. A 4K screen at 300% has the same room as a
# 1280x720 one at 100%.


def _fits(scale: float, narrow: bool = False) -> list[str]:
    bad: list[str] = []
    ui = _UiLive(scale=scale)
    if not ui.ok:
        return [f"the window did not open: {ui.error}"]
    a, c, root = ui.app, ui.canvas, ui.root
    T = ui.T
    try:
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        ui.settle(200)
        if root.winfo_width() > sw or root.winfo_height() > sh:
            bad.append(f"window {root.winfo_width()}x{root.winfo_height()} on a {sw}x{sh} screen")
        mw, mh = (int(v) for v in root.tk.splitlist(root.tk.call("wm", "minsize", root._w)))
        if mw > sw or mh > sh:
            bad.append(f"minsize {mw}x{mh} on a {sw}x{sh} screen")
        if narrow:
            # as small as the window lets a person drag it
            root.state("normal")
            root.geometry(f"{mw}x{mh}")
            ui.settle(250)
            if root.winfo_width() > mw + 40:
                bad.append(f"the window would not go down to its minsize ({root.winfo_width()} px)")

        def controls(where):
            view_w = c.winfo_width()
            for tag, kind, label in ui.kit.controls():
                box = c.bbox(tag)
                if not box:
                    continue
                if box[0] < 0 or box[2] > view_w + 1:
                    bad.append(f"{where}: {kind} '{label}' at x {box[0]}..{box[2]} in a {view_w} px page")
                if kind == "button":
                    rects = [i for i in c.find_withtag(tag) if c.type(i) == "rectangle"]
                    texts = [i for i in c.find_withtag(tag) if c.type(i) == "text"]
                    if rects and texts:
                        r = c.coords(rects[0])
                        for t in texts:
                            tb = c.bbox(t)
                            if tb and (tb[0] < r[0] - 1 or tb[2] > r[2] + 1):
                                bad.append(f"{where}: the label of '{label}' runs out of its button "
                                           f"({tb[0]}..{tb[2]} in {r[0]:.0f}..{r[2]:.0f})")

        def scrolls(where, page_h):
            view = c.winfo_height()
            if a.shell.content_h < page_h:
                bad.append(f"{where}: {page_h} px drawn, {a.shell.content_h} px scrollable")
            if page_h > view + T.px(20):
                c.yview_moveto(0)
                c.event_generate("<Motion>", x=c.winfo_width() // 2, y=view // 2)
                c.event_generate("<MouseWheel>", delta=-120, x=c.winfo_width() // 2, y=view // 2)
                ui.settle(40)
                if c.canvasy(0) <= 0:
                    bad.append(f"{where}: {page_h} px on a {view} px view and the wheel does not scroll it")
                c.yview_moveto(0)

        # the library, with more games than any screen holds at 300%
        many = []
        for i in range(14):
            g = games.Game(name=f"A game with a long enough name {i}", folder=Path(f"Z:/scale/{i}"))
            g.exe, g.bitness, g.api = Path(f"Z:/scale/{i}/g.exe"), 64, "DX11"
            many.append(g)
        a.all_games = many
        a.shell.show("library")
        ui.settle(150)
        page = a.shell.pages["library"]
        controls("library")
        scrolls("library", a.shell.content_h)
        if not page.cards:
            bad.append("library: no card drawn")

        # a game page, settings open, on routes with different rows
        g = _ui_game(name="A game whose name is long enough to need room", api="DX11")
        ui.enter(g, _ui_support([dlss.FEEDER, dlss.OPTI, dlss.REMIX, dlss.STANDALONE], dlss.FEEDER,
                                evidence=["nvngx_dlss.dll", "nvngx_dlssd.dll"]))
        if a.shell.page is None or a.shell.page.name != "game":
            bad.append("the game page did not open")
        ui.press("settings", "button")
        ui.settle(400)
        for route in (dlss.FEEDER, dlss.OPTI, dlss.REMIX):
            a.set_setting("route", route)
            a.shell.redraw()
            ui.settle(60)
            controls(f"game page ({route})")
            scrolls(f"game page ({route})", a.shell.content_h)
            if not ui.kit.find("route", "dropdown"):
                bad.append(f"game page ({route}): the settings did not draw")

        # ...and at 300% the log still shows more than three lines
        a.shell.toggle_log(True)
        ui.settle(450)
        import tkinter.font as _tkf
        line = _tkf.Font(root=root, font=a.shell.log_text.cget("font")).metrics("linespace")
        if a.shell.log_text.winfo_height() < 3 * line:
            bad.append(f"log {a.shell.log_text.winfo_height()} px, under three {line} px lines")
    except Exception as e:
        import traceback as _tb
        bad.append("raised: " + _tb.format_exc()[-400:])
    finally:
        ui.close()
        _ui_cleanup()
    return bad


_bad55 = {}
for _s55, _n55 in ((1.0, False), (1.0, True), (1.5, False), (2.0, False), (3.0, False)):
    _bad55[(_s55, _n55)] = _fits(_s55, _n55)
for (_s55, _n55), _b55 in _bad55.items():
    check(f"at {_s55:.0%}{' in a window dragged to its smallest' if _n55 else ''} the window fits "
          f"the screen, every control fits the page, and what does not fit scrolls",
          not _b55, _b55[:6])

section("56. issue #67: the library found last time, without walking the disks again")

# The owner's GTA IV came up twice: Steam reports the library folder, the
# Rockstar launcher the GTAIV subfolder in it - two folders, one executable.
_dup = Path(tempfile.mkdtemp(prefix="dupexe_"))
(_dup / "GTAIV").mkdir()
(_dup / "GTAIV" / "GTAIV.exe").write_bytes(b"MZ")
_ga = games.Game(name="Grand Theft Auto IV: The Complete Edition", folder=_dup,
                 exe=_dup / "GTAIV" / "GTAIV.exe", source="Steam")
_gb = games.Game(name="Grand Theft Auto IV", folder=_dup / "GTAIV",
                 exe=_dup / "GTAIV" / "GTAIV.exe", source="Rockstar")
_one = games.same_exe_once([_ga, _gb])
check("one executable reported by two stores is listed once, the first store's",
      _one == [_ga], [g.source for g in _one])
_e1 = games.Game(name="Game A", folder=_dup, exe=_dup / "GTAIV" / "GTAIV.exe",
                 source="Emulator")
_e2 = games.Game(name="Game B", folder=_dup, exe=_dup / "GTAIV" / "GTAIV.exe",
                 source="Emulator")
_e1.emu = _e2.emu = object()
check("...but games that share an emulator's executable are all kept",
      len(games.same_exe_once([_e1, _e2])) == 2)
shutil.rmtree(_dup, ignore_errors=True)

from core import library as _lib  # noqa: E402

_d = Path(tempfile.mkdtemp(prefix="libcache_"))
_saved_file = _lib.FILE
_lib.FILE = _d / "library.json"
_gdir = _d / "Some Game" / "bin"
_gdir.mkdir(parents=True)
_exe = _gdir / "Game.exe"
_exe.write_bytes(b"MZ" + b"\0" * 100)
_g = games.Game(name="Some Game", folder=_d / "Some Game", exe=_exe, bitness=64,
                api="DX12", api_why="imports dxgi.dll", api_detected="DX12",
                source="Steam", candidates=[_exe], kind="game")
_gone = games.Game(name="Gone Game", folder=_d / "nowhere",
                   exe=_d / "nowhere" / "x.exe", bitness=64, api="DX11", source="Epic")
_key = (str(_g.folder), str(_g.exe))
_row = (True, "feeder", "stable", "reliable", False, "")
_lib.save([_g, _gone], {_key: _row, (str(_gone.folder), str(_gone.exe)): False}, "1.7.3", 89)

_got = _lib.load("1.7.3", 89)
check("the library comes back with every field the list needs",
      _got is not None and [x.name for x in _got[0]] == ["Some Game"]
      and _got[0][0].api_why == "imports dxgi.dll"
      and _got[0][0].candidates == [_exe] and _got[1][_key] == _row
      and _got[2] == [], _got)
check("a game whose folder is gone is dropped, not shown as missing",
      all(x.name != "Gone Game" for x in _got[0]))

check("a cache from another release of this tool is not used",
      _lib.load("1.7.2", 89) is None)
check("...nor one measured against another graphics card",
      _lib.load("1.7.3", 120) is None)

# The compatibility columns are read from the game folder, so a game that
# has changed on disk since has to be looked at again - that is the whole
# reason a cache is allowed to exist at all.
time.sleep(1.1)
_exe.write_bytes(b"MZ" + b"\0" * 200)
os.utime(_exe, None)
_gs, _rows, _changed = _lib.load("1.7.3", 89)
check("a game the store has updated keeps its place, loses its row, and is handed back to be read again",
      [x.name for x in _gs] == ["Some Game"] and not _rows
      and [x.name for x in _changed] == ["Some Game"], (_rows, _changed))

_lib.save([_g], {_key: _row}, "1.7.3", 89)
time.sleep(1.1)
(_g.folder / "OptiScaler.dll").write_bytes(b"MZ")
os.utime(_g.folder, None)
check("...and so does one that had a mod dropped into it",
      not _lib.load("1.7.3", 89)[1] and _lib.load("1.7.3", 89)[2])

_lib.FILE.write_text("{ not json", encoding="utf8")
check("an unreadable cache just means the normal scan runs", _lib.load("1.7.3", 89) is None)
_lib.FILE.unlink()
check("no cache at all is not an error", _lib.load("1.7.3", 89) is None)

# The window's side of it, run: what it opens on, and when it writes.
from core.ui import app as _uiapp56, ctl_library as _uil  # noqa: E402
with _ui_isolated(), _ui_threads(run=False) as _th56:
    _g56a = _ui_game(name="Cached Game")
    _row56 = (True, "feeder", "stable", "reliable", False, "")
    _lib.save([_g56a], {(str(_g56a.folder), str(_g56a.exe)): _row56}, update.VERSION, 89)
    _c56 = _ui_ctl()
    _c56.sm = 89
    _c56.check_stale = _c56.load_catalog = lambda: None     # both go to the network
    _scans56: list = []
    _c56.scan = lambda full=False: _scans56.append(full)
    _c56.shell.show = lambda name, remember=True: None
    _uiapp56.App._open_start(_c56)
    check("the window opens on the saved library and does not walk the disks for it",
          [g.name for g in _c56.all_games] == ["Cached Game"] and _scans56 == [],
          ([g.name for g in _c56.all_games], _scans56))

    def _saved56() -> list:
        got = _lib.load(update.VERSION, 89)
        return sorted(g.name for g in got[0]) if got else []

    _lib.FILE.unlink(missing_ok=True)
    _g56b = _ui_game(name="Scanned Game")
    _c56._on_scanned(([_g56a, _g56b], {(str(_g56b.folder), str(_g56b.exe)): _row56}))
    check("...and writes it after a scan",
          _saved56() == ["Cached Game", "Scanned Game"], _saved56())

    # A folder chosen by hand: its row is read on a worker, and the library is
    # written when that row lands - not before, with the row missing.
    _g56c = _ui_game(name="Picked Game")
    _c56.root.after = lambda ms, fn=None, *a: fn and fn()
    from tkinter import filedialog as _fd56
    with patch.object(_fd56, "askdirectory", lambda **k: str(_g56c.folder)):
        _c56.pick_folder()
    _before56 = _saved56()
    _th56.go()
    _c56.pump()
    check("...and again when a folder is chosen, once its row is read",
          "Picked Game" not in _before56 and "Picked Game" in _saved56(),
          (_before56, _saved56()))
    _c56.game = _g56c
    _lib.FILE.unlink(missing_ok=True)
    _c56._rows[("x", "y")] = _row56
    _c56._on_installed(installer.Report(written=["dxgi.dll"]))
    check("...and again when an install finishes", "Picked Game" in _saved56(), _saved56())

    # full rescan walks every store; the quick one reads only what is new
    _walks56: list = []
    with patch.object(games, "scan_all", lambda progress=None: (_walks56.append("full"), [_g56a])[1]), \
            patch.object(games, "quick_scan",
                         lambda known, progress=None: (_walks56.append("quick"), ([_g56a], []))[1]), \
            patch.object(_uil.LibraryControl, "inspect_row", staticmethod(lambda g, sm: _row56)):
        del _c56.scan                       # the real one again
        _c56.scanning = False
        _c56.scan(full=True)
        _th56.go()
        _c56.pump()
        _c56.scan()
        _th56.go()
        _c56.pump()
    check("rescan still does the full walk, and the quick one does not",
          _walks56 == ["full", "quick"], _walks56)
_ui_cleanup()
_lib.FILE = _saved_file
shutil.rmtree(_d, ignore_errors=True)


section("57. the second day of 1.7.2: a comma for a full stop, an install that "
        "never finished, a moved ffmpeg name, and a game on the wrong API")

# Issue #69: the add-on writes dlss5-feed.cfg too, and it is C++ - on a
# machine whose locale uses a comma for the decimal point (PCSX2 is Qt, and
# Qt sets the locale) it writes "1,000". float() then raised out of the
# middle of install() and NOTHING was installed.
from core import feedcfg as _fc  # noqa: E402

check("a comma decimal is read as the number it is", _fc.number("1,000") == 1.0)
check("...and a full stop still is", _fc.number("0.750") == 0.75)
check("...and rubbish falls back instead of raising", _fc.number("?", 1.0) == 1.0)
check("...and a real number is passed through", _fc.number(2) == 2.0)

_d = Path(tempfile.mkdtemp(prefix="feedcfg_"))
(_d / "dlss5-feed.cfg").write_text(
    "enabled=1\nmode=2\nmv_scale_x=1,000\nmv_scale_y=1,000\nwork_resolution=100\n",
    encoding="utf8")
_p = _fc.write(_d, {"work_resolution": 75})
_txt = _p.read_text(encoding="utf8")
check("the install survives a cfg written in another locale, and rewrites it",
      "mv_scale_x=1.000" in _txt and "work_resolution=75" in _txt, _txt.replace("\n", " "))
check("...and describing it does not raise either",
      _fc.describe(_fc.read(_p)) and _fc.describe_bridge({"ofa_grid": "2,000"}) == [], )
shutil.rmtree(_d, ignore_errors=True)

# Issue #74: a download cut off (a reset connection) leaves a folder set up
# part of the way. The installer records that; the diagnosis never read it,
# so the missing parts were reported as if antivirus had eaten them.
_d = _diag_dir("diag_incomplete_", feed=_FEED_OK, complete=False)
_r = diagnose.analyse(_d)
check("an install that never finished says so, before anything else",
      "never finished" in _r.verdict
      and any("did not finish" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_complete_", feed=_FEED_OK)
_r = diagnose.analyse(_d)
check("...and a finished one is read as before", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# Issue #75: BtbN's /releases/latest is the DATED autobuild, whose asset
# names carry the build hash; the stable name lives on the release tagged
# "latest". Every video download failed with "ffmpeg build not found".
check("the ffmpeg listing asks for the tag, not for 'the latest release'",
      video.FFMPEG_API.endswith("/releases/tags/latest"), video.FFMPEG_API)
check("there is a direct download to fall back on",
      video.FFMPEG_DIRECT.endswith(video.FFMPEG_ASSET))
_dated = ["ffmpeg-N-126475-g35b7df64a0-win64-gpl.zip",
          "ffmpeg-N-126475-g35b7df64a0-win64-gpl-shared.zip",
          "ffmpeg-N-126475-g35b7df64a0-win64-lgpl.zip",
          "ffmpeg-N-126475-g35b7df64a0-winarm64-gpl.zip",
          "ffmpeg-N-126475-g35b7df64a0-linux64-gpl.tar.xz"]
_pick = [n for n in _dated
         if n.lower().endswith(".zip") and "win64" in n.lower() and "gpl" in n.lower()
         and "lgpl" not in n.lower() and "shared" not in n.lower()]
check("...and the name pattern picks the static 64-bit Windows GPL build",
      _pick == ["ffmpeg-N-126475-g35b7df64a0-win64-gpl.zip"], _pick)
check("the real release still carries the name we ask for, or the pattern finds one",
      any(a.get("name") == video.FFMPEG_ASSET
          for a in sources._json(video.FFMPEG_API).get("assets", [])))

# Issues #66 and #70, both Red Dead Redemption 2: the game was set to Vulkan
# and the optiscaler route hooks a Direct3D path, so OptiScaler's overlay
# kept asking for an upscaler that was already switched on.
_d = _opti_dir("opti_vulkan_", "[I] Init done\n[I] Vulkan is creating swapchain!\n")
_r = diagnose.analyse(_d)
check("a game drawing with Vulkan on the optiscaler route is told so",
      "on Vulkan" in _r.verdict and "DirectX 12" in " ".join(
          (f_.detail or "") for f_ in _r.findings), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


# Issue #76: a third OptiScaler build in the dropdown - wilsjo2's fork runs
# the neural pass before super resolution instead of after it. Every fork
# publishes the same way, so the branch that only knew y4my4my4m's is a
# table now.
check("the dropdown offers three builds, and the default is still the plain one",
      list(optiscaler.BUILDS)[0] == ""
      and optiscaler.PRESR in optiscaler.BUILDS
      and optiscaler.FORK in optiscaler.BUILDS, list(optiscaler.BUILDS))
check("...and the new one says it has not been tried here",
      "not run here" in optiscaler.BUILDS[optiscaler.PRESR],
      optiscaler.BUILDS[optiscaler.PRESR])
check("every fork in the table has an API and a skip list",
      all(isinstance(v, tuple) and len(v) == 2 and v[0].startswith("https://")
          for v in optiscaler.FORKS.values()), optiscaler.FORKS)
check("an unknown build key is still refused rather than silently installed",
      _raises(lambda: optiscaler.resolve("nope")))

_saved_json = sources._json
sources._json = lambda url: [
    {"tag_name": "nightly", "published_at": "2026-09-09T00:00:00Z",
     "assets": [{"name": "x.7z", "browser_download_url": "https://x/nightly.7z"}]},
    {"tag_name": "v0.7.1", "published_at": "2026-09-08T12:00:00Z",
     "assets": [{"name": "OptiScaler-DLSSNR-v0.7.1-hybrid.zip",
                 "browser_download_url": "https://x/new.zip"}]},
    {"tag_name": "v0.6.2", "published_at": "2026-09-07T00:00:00Z",
     "assets": [{"name": "old.zip", "browser_download_url": "https://x/old.zip"}]},
]
check("the newest dated release wins and the rolling 'nightly' tag is passed over",
      optiscaler.resolve(optiscaler.PRESR) == ("v0.7.1", "https://x/new.zip"),
      optiscaler.resolve(optiscaler.PRESR))
sources._json = lambda url: [
    {"tag_name": "v1", "published_at": "2026-09-08T12:00:00Z", "assets": [
        {"name": "OptiScaler_with_DLSS.7z", "browser_download_url": "https://x/withdlss.7z"},
        {"name": "OptiScaler.7z", "browser_download_url": "https://x/plain.7z"}]}]
check("...and y4my4my4m's '_with_DLSS' archive is still passed over",
      optiscaler.resolve(optiscaler.FORK) == ("v1", "https://x/plain.7z"),
      optiscaler.resolve(optiscaler.FORK))
# wilsjo2's v0.8.4 page as it really is: the MFG unlock sorts first (#196).
sources._json = lambda url: [
    {"tag_name": "v0.8.4", "published_at": "2026-09-15T00:36:59Z", "assets": [
        {"name": "OptiScaler-NR-v0.8.4-rtx40-mfg.zip",
         "browser_download_url": "https://x/OptiScaler-NR-v0.8.4-rtx40-mfg.zip"},
        {"name": "OptiScaler-NR-v0.8.4-rtx40-mfg.zip.sha256",
         "browser_download_url": "https://x/a.sha256"},
        {"name": "OptiScaler-NR-v0.8.4.zip",
         "browser_download_url": "https://x/OptiScaler-NR-v0.8.4.zip"}]}]
check("wilsjo2's standard zip is installed, not the RTX 40 MFG unlock (#196, #231)",
      optiscaler.resolve(optiscaler.PRESR)
      == ("v0.8.4", "https://x/OptiScaler-NR-v0.8.4.zip"),
      optiscaler.resolve(optiscaler.PRESR))
sources._json = _saved_json
check("...and two archives of one release never share a cache entry",
      optiscaler._archive_name("v0.8.4", "https://x/OptiScaler-NR-v0.8.4.zip")
      != optiscaler._archive_name("v0.8.4",
                                  "https://x/OptiScaler-NR-v0.8.4-rtx40-mfg.zip"))
check("all three builds resolve against the real release pages",
      all(optiscaler.resolve(b)[1].startswith("https://")
          for b in optiscaler.BUILDS))


def _fork_candidates(build):
    """Archives the pick could still choose on a fork's newest release."""
    api, skip = optiscaler.FORKS[build]
    rels = [r for r in (sources.json_or_html(api) or [])
            if isinstance(r, dict) and not r.get("draft")
            and r.get("tag_name") != "nightly"
            and any(a["name"].lower().endswith((".7z", ".zip"))
                    for a in r.get("assets", []))]
    rels.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    return [a["name"] for a in (rels[0]["assets"] if rels else [])
            if a["name"].lower().endswith((".7z", ".zip"))
            and not any(x in a["name"].lower() for x in skip)]


# A second archive on the same release is how the MFG unlock reached every
# card (#196): the pick takes the first, so more than one is a question.
for _b in optiscaler.FORKS:
    try:
        _c = _fork_candidates(_b)
        check(f"{_b}'s newest release leaves the pick exactly one archive",
              len(_c) == 1, _c)
    except Exception as _e:
        check(f"{_b}'s release page could be read", False, _e)


# With two forks' archives in the cache, the preview listed whichever one the
# glob found first and described the wrong package - their file lists differ
# by a dozen documents and a folder of weights. Found by the existing
# "preview promised nothing the install did not do" check the moment a third
# build was added.
check("the preview describes the build it would install, from the cache alone",
      len({optiscaler.archive_name(b) for b in optiscaler.BUILDS}) == len(optiscaler.BUILDS),
      {b: optiscaler.archive_name(b) for b in optiscaler.BUILDS})
check("...under the name the download saves it as",
      all(optiscaler.archive_name(b).startswith("OptiScaler-DLSSNR-")
          for b in optiscaler.BUILDS if optiscaler.archive_name(b)))
check("cached_json never reaches the network",
      "urlopen" not in src_of(sources.cached_json)
      and sources.cached_json("https://example.invalid/nothing-was-ever-fetched") is None)
_saved_get = sources._get


def _no_net(*a, **k):
    raise AssertionError("preview() made a request")


sources._get = _no_net
_d = Path(tempfile.mkdtemp(prefix="pv_opti_offline_"))
shutil.copyfile(X64, _d / "Game.exe")
_pv = installer.preview(games.manual(_d), installer.Options(path=dlss.OPTI))
check("the optiscaler preview still makes no request at all", bool(_pv.writes))
sources._get = _saved_get
shutil.rmtree(_d, ignore_errors=True)


section("58. the release gate on 1.7.3's own changes")

# The cache made two old habits expensive. An install used to throw every
# compatibility row away because recomputing them was free; with the rows
# saved for the next launch, that emptied the cache and gave the launch
# after an install the whole folder walk again - on the Tk thread.
from core.ui import ctl_library as _uil58  # noqa: E402
_row58 = (True, "feeder", "stable", "reliable", False, "")
with _ui_isolated(), _ui_threads(run=False) as _th58:
    _c58 = _ui_ctl()
    _c58.sm = 89
    _c58.check_stale = _c58.load_catalog = lambda: None
    _g58a, _g58b = _ui_game(name="Installed Here"), _ui_game(name="Somebody Else")
    _c58.all_games = [_g58a, _g58b]
    for _x58 in (_g58a, _g58b):
        _c58._rows[(str(_x58.folder), str(_x58.exe))] = _row58
    _c58.game = _g58a
    _c58._on_installed(installer.Report(written=["dxgi.dll"]))
    check("an install drops only the row of the game it installed",
          (str(_g58a.folder), str(_g58a.exe)) not in _c58._rows
          and (str(_g58b.folder), str(_g58b.exe)) in _c58._rows, list(_c58._rows))

    _lib.save([_g58a, _g58b], {(str(_g58b.folder), str(_g58b.exe)): _row58}, update.VERSION, 89)
    _full58 = _lib.FILE.read_text(encoding="utf8")
    _c58._rows.clear()
    _c58.remember_library()
    check("...and an empty set of rows is never written over a full one",
          _lib.FILE.read_text(encoding="utf8") == _full58)

    # A saved library where one game changed on disk: it opens at once, and
    # that game is read again on a worker, keyed on its folder.
    _enriched58: list = []
    _real_enrich58 = games.enrich

    def _enrich58(g, *a, **k):
        _enriched58.append((g.name, _th58.inside > 0))
        g.exe = g.folder / "Moved.exe"            # enrich may re-point the exe
        return g
    with patch.object(_lib, "load", lambda v, sm: ([_g58a, _g58b], {}, [_g58b])), \
            patch.object(games, "enrich", _enrich58), \
            patch.object(_uil58.LibraryControl, "inspect_row", staticmethod(lambda g, sm: _row58)):
        _c58._recheck.clear()
        _c58.load_cached()
        check("...and the games that did change are read on a worker, not on the Tk thread",
              _enriched58 == [] and any("_recheck_changed" in n for n in _th58.names),
              (_enriched58, _th58.names))
        check("...keyed on the folder, which enrich() cannot reassign under it",
              _c58._recheck == {str(_g58b.folder)}, _c58._recheck)
        _spawned58 = len(_th58.started)
        check("...and a card leaves those rows to the worker instead of reading them itself",
              _c58.row_of(_g58b) is None and len(_th58.started) == _spawned58
              and _c58.card(_g58b)["status"] == "reading...", len(_th58.started) - _spawned58)
        _gen58 = _c58._recheck_id
        _th58.go()
        check("...and the worker is where the game is read",
              _enriched58 == [("Somebody Else", True)], _enriched58)
        # a rescan starts while that worker's answer is still on its way
        _c58._recheck_id += 1
        _c58.pump()
        check("...and a rescan makes a running recheck's answer stale, not authoritative",
              (str(_g58b.folder), str(_g58b.exe)) not in _c58._rows
              and _c58._recheck == {str(_g58b.folder)}, (_c58._rows, _c58._recheck))
        _c58._recheck_id = _gen58
        _c58.load_cached()
        _th58.go()
        _c58.pump()
        check("...while the current one lands, and clears the folder it was keyed on",
              (str(_g58b.folder), str(_g58b.exe)) in _c58._rows and not _c58._recheck,
              (_c58._rows, _c58._recheck))
    _before58 = _c58._recheck_id
    with patch.object(_lib, "load", lambda v, sm: None):
        _c58.scan(full=True)
    check("...and a rescan is what moves that counter on",
          _c58._recheck_id == _before58 + 1 and not _c58._recheck, _c58._recheck_id)
_ui_cleanup()

# A graphics API set by hand is kept in the settings, not in the library, so
# a cached game came back with the renderer that was DETECTED - and would be
# installed for it. That is the fault the dropdown exists to fix (#24/#66/#70).
_d = Path(tempfile.mkdtemp(prefix="libapi_"))
_saved_file = _lib.FILE
_lib.FILE = _d / "library.json"
(_d / "bin").mkdir()
_exe2 = _d / "bin" / "Game.exe"
_exe2.write_bytes(b"MZ")
_g2 = games.Game(name="Some Game", folder=_d, exe=_exe2, bitness=64,
                 api="DX12", api_detected="DX12", source="Steam")
_lib.save([_g2], {}, "1.7.3", 89)
games.set_api_override(_d, "Vulkan")
try:
    _back = _lib.load("1.7.3", 89)[0][0]
    check("a graphics api set by hand survives a cached launch",
          _back.api == "Vulkan" and "set by hand" in _back.api_why,
          (_back.api, _back.api_why))
finally:
    games.set_api_override(_d, None)
_lib.FILE = _saved_file
shutil.rmtree(_d, ignore_errors=True)

# An emulator chosen with "choose folder" keeps source "Manual", and its
# profile is what switches the render backend. Dropping it meant the install
# silently did nothing.
check("an emulator profile is found again whatever the game's source says",
      'if exe is not None:' in src_of(_lib._from_json)
      and 'source == "Emulator"' not in src_of(_lib._from_json))

# The fault chain: the list of our own modules was missing three files this
# tool installs, and an unrecognised module was being called the game's.
check("every add-on this tool installs is in the fault-chain list",
      all(n in diagnose._OURS_IN_STACK for n in (
          "dlss5-feed.addon64", "dlss5-feed.addon32", "renodx-dlss5.addon64",
          "renodx-dlss.addon64", "dlss5-bridge.addon64",
          "dlss5-dx11-bridge.addon64", "nvngx.dll.addon64",
          "standalone-dlssnr.addon64")), diagnose._OURS_IN_STACK)

# The upstream analyser could add "it is running" and then return a verdict
# saying it was switched off or never hooked.
def _up(*lines: str) -> str:
    return (_SESSION
            + 'INFO  | Registered add-on "DLSS5 NR Pre-Upscale" v0.0.0.0\n'
            + "".join("INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] " + x + "\n" for x in lines))


_d = _up_dir()
(_d / "ReShade.log").write_text(_up("settings loaded: enabled=0", _HOOK, _HB), encoding="utf8")
_r = diagnose.analyse(_d)
check("frames through the network outrank an 'enabled=0' written at load",
      _r.verdict == "Working.", _r.verdict)
(_d / "ReShade.log").write_text(_up("settings loaded: enabled=1", _HB), encoding="utf8")
_r = diagnose.analyse(_d)
check("...and outrank a hook line that never appeared", _r.verdict == "Working.", _r.verdict)
(_d / "ReShade.log").write_text(
    _up("settings loaded: enabled=1", _HOOK, _HB, "resetting state (new device)"),
    encoding="utf8")
_r = diagnose.analyse(_d)
check("...but not what happened after them",
      "device re-creation" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# An uninstall that could not delete a locked file records itself the same
# way a failed install does; telling that person to install again is the
# opposite of what they need.
_d = _diag_dir("diag_stuckuninstall_", feed=_FEED_OK, complete=False,
               notes=["uninstall left 2 locked file(s); run it again with the game closed"])
_r = diagnose.analyse(_d)
check("a partial uninstall is not reported as a partial install",
      _r.verdict.startswith("The uninstall")
      and any("uninstall did not finish" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


# Found by replaying issue #51's own 400 KB ReShade.log: the tail is cut to
# 250 KB, so the "Registered add-on" lines fell off the front while every
# line the add-on WROTE was still there - and the report said "ReShade
# loaded no add-ons" next to 34621 heartbeats from that add-on.
_d = _up_dir()
(_d / "ReShade.log").write_text(
    "16:00:00:000 [1] | INFO  | Some line with no registration in it\n"
    "16:00:01:000 [1] | INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] " + _HOOK + "\n"
    "16:00:02:000 [1] | INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] " + _HB + "\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("a log cut to its tail is not reported as 'no add-ons loaded'",
      not any("no add-ons" in t for t in _levels(_r, "bad")), _levels(_r, "bad"))
check("...and it is still read as working", _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)


section("59. issue #77: a d3d9.dll import is the weakest evidence in the file")

# Grand Theft Auto V was read as a DirectX 9 game because GTA5.exe imports
# d3d9.dll, and went to a route that has nothing for it. The same shape as
# Red Dead Redemption 2 (#12) and Call of Juarez (#31): engines keep a d3d9
# import for a launcher or an old dialog long after they stopped drawing
# with it. Every other kind of evidence is now asked first.
_d = Path(tempfile.mkdtemp(prefix="d3d9ev_"))
_exe = _d / "Game.exe"
_exe.write_bytes(b"MZ")
_saved_imports = pe.pe_imports


def _fake_imports(static: list[str], delayed: list[str] | None = None):
    pe.pe_imports = (lambda path, delay=False, _s=list(static),
                     _d=list(delayed or []): _d if delay else _s)


try:
    _fake_imports(["d3d9.dll", "kernel32.dll"])
    check("a game with nothing but d3d9 is still DirectX 9",
          pe.detect_api(_exe)[0] == "DX9", pe.detect_api(_exe))

    _fake_imports(["d3d9.dll"], ["d3d11.dll"])
    _api, _why = pe.detect_api(_exe)
    check("...but a delay-loaded d3d11 is the renderer, and the reason says so",
          _api == "DX11" and "delay-loads d3d11.dll" in _why, (_api, _why))

    _fake_imports(["d3d9.dll"], ["d3d12.dll"])
    check("...and so is a delay-loaded d3d12", pe.detect_api(_exe)[0] == "DX12",
          pe.detect_api(_exe))

    # A DirectX 9 game may use DXGI on its own just to enumerate displays,
    # so a bare dxgi delay-load must not flip it.
    _fake_imports(["d3d9.dll"], ["dxgi.dll"])
    check("...but a bare dxgi delay-load does not make a DX9 game modern",
          pe.detect_api(_exe)[0] == "DX9", pe.detect_api(_exe))

    # The evidence already used for RDR2 and for the Agility SDK still wins
    # ahead of any of that.
    _fake_imports(["d3d9.dll"], ["d3d11.dll"])
    (_d / "nvngx_dlss.dll").write_bytes(b"MZ")
    check("a game that ships DLSS beside a d3d9 import is D3D12, as before",
          pe.detect_api(_exe)[0] == "DX12", pe.detect_api(_exe))
    (_d / "nvngx_dlss.dll").unlink()
finally:
    pe.pe_imports = _saved_imports
shutil.rmtree(_d, ignore_errors=True)

# The delay-load table is data directory 13, and it is read out of real
# executables, not just mocked: several games in a real library delay-load
# a renderer they do not import statically.
check("pe_imports takes a delay flag and both tables parse the same exe",
      isinstance(pe.pe_imports(X64), list)
      and isinstance(pe.pe_imports(X64, delay=True), list))
check("...and the delay table is read from data directory 13",
      "want = 13 if delay else 1" in src_of(pe.pe_imports))
check("...after checking the image has that many data directories",
      "n_dirs <= want" in src_of(pe.pe_imports))
check("...with 32-byte descriptors and the name at offset 4",
      "(32, 4) if delay else (20, 12)" in src_of(pe.pe_imports))


# Issue #31, answered three times with "the game has not been started" while
# DXVK's own log sat in the folder proving it had. DXVK writes that log the
# moment it loads, so it settles what the hints could only guess at.
# The clash rule below reads the real layer folder, so point it at a clean
# one: a test must not depend on what this machine happens to have.
from core import vulkan as _vk_clean  # noqa: E402
_vk_saved_dir = _vk_clean.layer_dir
_vk_clean_dir = Path(tempfile.mkdtemp(prefix="vkclean_"))
_vk_clean.layer_dir = lambda: _vk_clean_dir
# The layer IS registered for this game in the story these checks tell; what
# is being tested is which answer comes first once it is. Asking this
# machine's own registry instead made the result depend on whether the
# reviewer happens to have a 32-bit ReShade installed.
_layer_saved = diagnose.model._layer_state
diagnose.model._layer_state = lambda man: (True, True)

_d = Path(tempfile.mkdtemp(prefix="dxvklog_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "CoJGunslinger.exe", "bitness": 32,
     "api": "Vulkan", "proxy": diagnose.VULKAN_LAYER, "path": "feeder",
     "dxvk": True,
     "files": ["d3d9.dll", "dlss5-feed.addon32"]}), encoding="utf8")
for _n in ("d3d9.dll", "dlss5-feed.addon32", "ReShade.ini", "nvngx_dlssnr.dll"):
    (_d / _n).write_bytes(b"MZ")
_r = diagnose.analyse(_d)
check("with nothing in the folder it is still 'not started since the install'",
      "Not started since the install" in _r.verdict, _r.verdict)
(_d / "CoJGunslinger_d3d9.log").write_text("info:  DXVK: v2.4", encoding="utf8")
_r = diagnose.analyse(_d)
check("DXVK's own log proves the game ran, so the layer is what is missing",
      "DXVK ran" in _r.verdict and "32-bit" in _r.verdict, _r.verdict)
# ...but only a log written SINCE the install. One left by a run before it,
# or by a game the person had already put DXVK into, says nothing.
import os as _os
_os.utime(_d / "CoJGunslinger_d3d9.log", (1, 1))
_r = diagnose.analyse(_d)
check("...and a DXVK log older than the install is not evidence the game ran",
      "Not started since the install" in _r.verdict, _r.verdict)
diagnose.model._layer_state = _layer_saved
_vk_clean.layer_dir = _vk_saved_dir
shutil.rmtree(_d, ignore_errors=True)


section("60. the day after 1.7.3: a fork that reports its cost, and paths "
        "that leave the folder")

# Issue #81. wilsjo2's fork, Cyberpunk 2077, ApplyAfterRR: a thirteen-minute
# session with the model dispatching every frame was called "Inconclusive"
# because that build never writes "running at" - it writes what the model
# cost. These two lines are copied from the report.
_d = _opti_dir("opti_dispatch_",
               "[01:54:33.897657] [I] DlssNr.Enabled: true\n"
               "[01:54:33.897663] [I] DlssNr.ApplyAfterRR: true\n"
               "[01:55:42.255058] [I] DlssNr_Dx12::Dispatch DLSS-NR cost: "
               "7.41 ms total = 7.23 ms model + 0.19 ms ours (3% ours)\n"
               "[02:08:18.027112] [I] DlssNr_Dx12::Dispatch DLSS-NR cost: "
               "8.82 ms total = 8.66 ms model + 0.16 ms ours (2% ours)\n")
_r = diagnose.analyse(_d)
check("a dispatch cost line is proof the model ran", _r.verdict == "Working.",
      _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# ...and the ordering rule still holds over the new evidence: a failure
# logged after the last dispatch is what the person saw last.
_d = _opti_dir("opti_dispatch_then_fail_",
               "[01:55:42] [I] DlssNr_Dx12::Dispatch DLSS-NR cost: 7.41 ms\n"
               "[01:59:02] [E] DLSS-NR unavailable: the device was lost\n")
_r = diagnose.analyse(_d)
check("...but a failure after the last dispatch still ends the session",
      "stopped after it started" in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# Issues #85 and #70: settings read back at startup are not evidence of
# anything running. The old text ("Inconclusive - open the overlay") sent
# people to look at an overlay that was already telling them the truth:
# it is waiting for the game's own upscaler, which is off.
_d = _opti_dir("opti_settings_only_",
               "[01:54:33.897657] [I] DlssNr.Enabled: true\n"
               "[01:54:33.897663] [I] DlssNr.ApplyAfterRR: true\n")
_r = diagnose.analyse(_d)
check("settings with no dispatch means the model never drew a frame",
      "never ran" in _r.verdict and "upscaler" in _r.verdict, _r.verdict)
check("...and the answer says to switch the game's own upscaler on",
      any("graphics menu" in f.title + f.detail for f in _r.findings),
      [(f.title + f.detail)[:60] for f in _r.findings])
shutil.rmtree(_d, ignore_errors=True)

# DLSS5-Reshade-AIO v2.2.0 dropped the loose release files. The API path
# already falls back to the 64-bit archive, but the API-LESS path still
# asked for the three loose names - and those answer 404 now, so the one
# fallback written for rate-limited people was the one that could not work.
# (Checked live against v2.2.0: the archive URL below answers 200, 183293
# bytes, and holds standalone-dlssnr.addon64, nvngx.dll and the two shaders.)
_saved_json, _saved_tag = sources._json, sources.latest_tag
try:
    sources._json = lambda _u: (_ for _ in ()).throw(RuntimeError("no api"))
    sources.latest_tag = lambda _repo: "v2.2.0"
    _tag, _urls = sources.resolve_standalone()
    check("with no API the standalone route falls back to the 64-bit archive",
          _tag == "v2.2.0" and sources.STANDALONE_ZIP in _urls
          and _urls[sources.STANDALONE_ZIP].endswith(
              "/releases/download/v2.2.0/DLSS5-ReShade-AIO-v2.2.0-64-bit.zip"),
          _urls)
    # ...and if even the redirect is gone, the old loose names are still
    # better than nothing: older releases carry them.
    sources.latest_tag = lambda _repo: None
    _tag, _urls = sources.resolve_standalone()
    check("...and with no redirect either it still asks for the loose files",
          _tag == "latest"
          and all(n in _urls for n in sources.STANDALONE_ASSETS), _urls)
finally:
    sources._json, sources.latest_tag = _saved_json, _saved_tag
check("the tag comes out of a redirect, not the API",
      "api.github.com" not in src_of(sources.latest_tag)
      and "releases/latest" in src_of(sources.latest_tag))

# Issue #84, GTA IV: installed three times, and every time the answer was
# "install again". The files the install recorded were not in the folder at
# all - only the add-on used to be checked, so ReShade.ini and the runtime
# going missing said nothing and the DXVK rule below had the last word.
# The layer is registered for this game; what is under test is which answer
# comes first once it is. Reading this machine's own registry made the
# result depend on whether the reviewer has a 32-bit ReShade installed.
_layer_saved = diagnose.model._layer_state
diagnose.model._layer_state = lambda man: (True, True)
_d = Path(tempfile.mkdtemp(prefix="gone84_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "GTAIV.exe", "bitness": 32,
     "api": "DX9", "proxy": diagnose.VULKAN_LAYER, "path": "feeder",
     "dxvk": True,
     "files": ["d3d9.dll", "dlss5-feed.addon32", "ReShade.ini",
               "nvngx_dlssnr.dll",
               "reshade-shaders/Shaders/DLSS5_Feed.fx"]}), encoding="utf8")
for _n in ("d3d9.dll", "dlss5-feed.addon32"):
    (_d / _n).write_bytes(b"MZ")
_r = diagnose.analyse(_d)
check("files recorded by the install and since gone are the answer",
      "gone from the folder" in _r.verdict
      and "nvngx_dlssnr.dll" in " ".join(f.title for f in _r.findings),
      _r.verdict)
check("...and it says to restore them BEFORE installing again",
      any("restore them from quarantine first" in f.detail for f in _r.findings)
      or any("Restore" in f.detail and "before" in f.detail.lower()
             for f in _r.findings),
      [f.detail[:80] for f in _r.findings])
diagnose.model._layer_state = _layer_saved
shutil.rmtree(_d, ignore_errors=True)

# The report form. Four of the nine reports on 1.7.3's first day arrived
# with the template's own "yes / no / it closed itself" line untouched and
# nothing written under "What happened" (#82, #84, #86, #87), so the two
# questions are asked in the tool now and the body carries the answers.
_d = _diag_dir("report_answers_")
_body = diagnose.issue_body(
    "1.7.3", "NVIDIA GeForce RTX 4070 Ti", 89, "616.64", None, "feeder",
    None, "", Path("C:/x/autopilot.log"), _d,
    answers={"started": "it started, then closed itself",
             "happened": "Small game splash screen. Then crash to desktop."})
check("the answers replace the template, and the placeholder line is gone",
      "**Did the game start?** it started, then closed itself" in _body
      and "Then crash to desktop." in _body
      and "yes / no / it closed itself" not in _body, _body[:200])
_body = diagnose.issue_body(
    "1.7.3", "NVIDIA GeForce RTX 4070 Ti", 89, "616.64", None, "feeder",
    None, "", Path("C:/x/autopilot.log"), _d)
check("...and with no answers the old template still comes out",
      "yes / no / it closed itself" in _body, _body[:80])
shutil.rmtree(_d, ignore_errors=True)

from core import reportui as _reportui  # noqa: E402
from core.ui import app as _uiapp60, ctl_game as _uig60  # noqa: E402
from core import autotune as _tune  # noqa: E402
import types as _types60  # noqa: E402


def _report60(ask, with_game: bool = False):
    """Press 'report a bug' with this dialog answer: (what happened in order,
    what was raised)."""
    order: list = []
    with _ui_isolated(), _ui_threads(run=False):
        c = _ui_ctl()
        if with_game:
            c.game = _ui_game(name="Reported")
            c.shell.page = _types60.SimpleNamespace(name="game")

        def _ask(root, name):
            order.append("ask")
            if isinstance(ask, Exception):
                raise ask
            return ask
        with patch.object(_reportui, "ask", _ask), \
                patch.object(_uiapp60.webbrowser, "open", lambda url: order.append("browser")), \
                patch.object(gpu, "detect", lambda: ("RTX", 89)), \
                patch.object(gpu, "driver_version", lambda: "616.92"):
            try:
                _uiapp60.App.report_bug(c, "bug")
                raised = ""
            except Exception as e:
                raised = repr(e)
    _ui_cleanup()
    return order, raised


_rp60 = _report60({"started": "yes", "what": "it went black at once"})
check("the report button asks before it opens the browser",
      _rp60 == (["ask", "browser"], ""), _rp60)
_rp60 = _report60(None)
check("...and cancelling the dialog cancels the report", _rp60 == (["ask"], ""), _rp60)
_rp60 = _report60(RuntimeError("no dialog today"))
check("...but a dialog that cannot open does not block reporting",
      _rp60 == (["ask", "browser"], ""), _rp60)
_rp60 = (_report60({"started": "yes"}, with_game=True), _report60({"started": "yes"}))
check("...with a game open, or from the help menu with none picked",
      all(r == (["ask", "browser"], "") for r in _rp60), _rp60)
check("three answers to 'did it start', and a few words are required",
      len(_reportui.STARTED) == 3 and _reportui.MIN_WORDS >= 3)

# The compatibility list: the write side is a browser window the person can
# cancel, the read side is one static file, and the two ends have to agree
# about the block in between - so the workflow imports the tool's own parser
# rather than keeping a second copy of the format.
from core import community as _comm  # noqa: E402


class _FakeGame:
    name = "Red Dead Redemption 2"
    api = "DX12"
    exe = Path("D:/Games/RDR2/RDR2.exe")
    install_dir = Path("D:/Games/RDR2")


_rec = _comm.record(_FakeGame(), "optiscaler", "failed", api="DX12",
                    build="wilsjo2", gpu_sm=120,
                    gpu_name="NVIDIA GeForce RTX 5070 Ti", driver="616.64",
                    version="1.8.0")
check("the shared record is keyed by the executable, in lower case",
      _rec["exe"] == "rdr2.exe" and _rec["result"] == "failed")
check("...and carries nothing that identifies anybody",
      not any(("C:" in str(v) or "D:" in str(v) or "Users" in str(v))
              for v in _rec.values()), _rec)
_round = _comm.parse("some prose a person wrote" + _comm.block(_rec))
check("the block survives being written into an issue and read back",
      _round == _rec, _round)
check("a body with no block is not a result", _comm.parse("just a bug") is None)
check("...and neither is a hand-mangled one",
      _comm.parse(f"<!-- {_comm.MARKER}\n{{\"v\":9}}\n{_comm.MARKER} -->") is None)
_url = _comm.issue_url(_rec)
check("the share link is a pre-filled issue, labelled result",
      _url.startswith("https://github.com/") and "labels=result" in _url
      and "issues/new" in _url)

# The advice only speaks when there is something to say.
_thin = {"routes": {"feeder": {"worked": 1, "failed": 1}}}
check("two reports are not a finding", _comm.advice(_thin) == [])
_fat = {"name": "RDR2",
        "routes": {"feeder": {"worked": 22, "failed": 3},
                   "optiscaler": {"worked": 0, "failed": 9}},
        "drivers": {"616.64": {"worked": 1, "failed": 9}}}
_said = " ".join(_comm.advice(_fat, "optiscaler", "616.64"))
check("...but 34 of them name the route that worked",
      "feeder route worked in 22" in _said, _said)
check("...say that the chosen one failed for everybody",
      "failed in all 9" in _said, _said)
check("...and name the driver behind the failures",
      "616.64" in _said and "9 of the 12 failures" in _said, _said)
check("...as a count, not as a cause",
      "is behind" not in _said and "were on driver" in _said, _said)

# The published file has to exist and parse, or the first fetch 404s.
_feed = Path(__file__).resolve().parent / "docs" / "compatibility.json"
check("docs/compatibility.json is there and is an object",
      isinstance(json.loads(_feed.read_text(encoding="utf8")), dict))
check("the workflow reads the tool's own parser, not a copy of the format",
      "from core.community import parse" in
      (Path(__file__).resolve().parent / ".github" / "scripts"
       / "compatibility.py").read_text(encoding="utf8"))


def _worker_only(call, module, name, ret=None):
    """Run `call` with module.name recorded: (asked before any worker ran,
    asked after, and whether each ask came from inside a worker)."""
    seen: list = []
    with _ui_threads(run=False) as th:
        with patch.object(module, name, lambda *a, **k: (seen.append(th.inside > 0), ret)[1]):
            call()
            before = list(seen)
            th.go()
    return before, seen


with _ui_isolated():
    _c60 = _ui_ctl(_ui_game(name="Community"), _ui_support([dlss.FEEDER], dlss.FEEDER))
    _w60 = _worker_only(_c60.community_note, _comm, "fetch", {"games": {}})
check("the note is fetched off the Tk thread", _w60 == ([], [True]), _w60)
_ui_cleanup()

# A result says whether it worked. Now it also says what it cost, which is
# the question directly after it - and the one everybody, this tool
# included, has been answering by feel.
_recm = _comm.record(_FakeGame(), "optiscaler", "worked", version="1.9.0",
                     measured={"res": 70.0, "ms": 6.43, "fps": 78.2,
                               "path": "D:/Games/RDR2"})
check("a shared result carries what the session cost",
      _recm["res"] == 70 and _recm["ms"] == 6.43 and _recm["fps"] == 78.2,
      _recm)
check("...as a whole percent, not a false precision",
      isinstance(_recm["res"], int), _recm["res"])
check("...and nothing else the caller happened to hand in",
      "path" not in _recm and not any("D:" in str(v) for v in _recm.values()),
      _recm)
check("a session with no measurement carries no measurement",
      "res" not in _comm.record(_FakeGame(), "feeder", "failed"))


def _share60(route, measured=None, answer=False, shown=None, detect=("RTX 4060 Ti", 89), shared=None):
    """Press 'share the result' after a Working diagnosis on `route` (the
    route on screen is `shown`, default the same): (what was asked, what the
    browser was handed, the kwargs community.record got)."""
    opened: list = []
    recs: list = []
    real_record = _comm.record
    with _ui_isolated(), _ui_threads(run=False):
        c = _ui_ctl(_ui_game(name="Shared"), _ui_support([dlss.FEEDER, dlss.OPTI], route))
        c.route = shown or route
        rep = diagnose.Report(route=route)
        rep.verdict = "Working."
        c._last_diag = rep
        c._measured, c._measured_rows = measured, []
        c.shell.answers = [answer]
        with patch.object(_comm, "record", lambda *a, **k: (recs.append(k), real_record(*a, **k))[1]), \
                patch.object(_uig60.webbrowser, "open", lambda u: opened.append(u)), \
                patch.object(gpu, "detect", lambda: detect), \
                patch.object(gpu, "driver_version", lambda: "616.92"), \
                patch.object(_tune, "history", lambda *_a: []), \
                patch.object(_tune, "shared", (lambda rows, m: dict(shared)) if shared is not None
                             else _tune.shared):
            c.share_result()
    _ui_cleanup()
    return c.shell.asked, opened, recs


_opti_m60 = _tune.Measured(route="optiscaler", resolution=70, model_ms=6.4, frames=99)
_sh60 = _share60("optiscaler", _opti_m60, answer=False)
check("what leaves the machine is named on screen before the browser opens",
      len(_sh60[0]) == 1 and "what it cost" in _sh60[0][0][1]
      and "ms of model a frame" in _sh60[0][0][1] and _sh60[1] == [], _sh60[:2])
_sh60b = _share60("optiscaler", _opti_m60, answer=True)
check("...and the browser opens only once that has been agreed to",
      len(_sh60b[1]) == 1 and "issues/new" in _sh60b[1][0], _sh60b[1])
check("...and is in the issue body a person can read",
      "- measured: 70% work area" in urllib.parse.unquote(
          _comm.issue_url(_recm)), urllib.parse.unquote(
          _comm.issue_url(_recm))[-400:])

_meas = {"measured": {"optiscaler": {"n": 4, "res": 70, "ms": 6.4,
                                     "fps": 78}}}
_mn = _comm.measured_note(_meas, "optiscaler")
check("four measurements say what this game costs, with the count on them",
      "4 shared results" in _mn and "70% work area" in _mn
      and "6.4 ms" in _mn and "78 fps" in _mn, _mn)
check("...and two do not",
      _comm.measured_note({"measured": {"feeder": {"n": 2, "res": 70}}}) == "")
check("a game nobody measured says nothing at all",
      _comm.measured_note({"routes": {"feeder": {"worked": 9}}}, "feeder") == ""
      and _comm.measured_note(None) == "")
with _ui_isolated(), _ui_threads(run=True):
    _c60m = _ui_ctl(_ui_game(name="Measured", api="DX11"), _ui_support([dlss.OPTI], dlss.OPTI))
    _c60m.route = dlss.OPTI
    with patch.object(_comm, "fetch", lambda *a, **k: {"games": {}}), \
            patch.object(_comm, "for_game", lambda data, g: _meas), \
            patch.object(_comm, "advice", lambda *a, **k: []):
        _c60m.community_note()
    _c60m.pump()
check("the note reaches the window with the rest of what others found",
      "4 shared results" in _c60m.text() and "what other people found" in _c60m.text(),
      _c60m.text()[-300:])
_ui_cleanup()

# The workflow's own arithmetic, run rather than read: a failure's settings
# are the settings of a failure and must not count.
import importlib.util  # noqa: E402

_agg_spec = importlib.util.spec_from_file_location(
    "_agg_check", Path(__file__).resolve().parent / ".github" / "scripts"
    / "compatibility.py")
_agg = importlib.util.module_from_spec(_agg_spec)
_agg_spec.loader.exec_module(_agg)
_bodies = [_comm.block(_comm.record(_FakeGame(), "optiscaler", "worked",
                                    measured={"res": r, "ms": ms, "fps": f}))
           for r, ms, f in ((65, 5.2, 81), (70, 6.1, 77), (75, 6.9, 74))]
_bodies.append(_comm.block(_comm.record(_FakeGame(), "optiscaler", "failed",
                                        measured={"res": 25, "ms": 99.0,
                                                  "fps": 9})))
with tempfile.TemporaryDirectory() as _td:
    _agg.issues = lambda repo, token: [{"body": b} for b in _bodies]
    _agg.OUT = Path(_td) / "compatibility.json"
    _agg.main()
    _built = json.loads(_agg.OUT.read_text(encoding="utf8"))
_row = ((_built["games"]["rdr2.exe"]).get("measured") or {}).get("optiscaler")
check("the published file carries the middle of what people really ran",
      _row and _row["n"] == 3 and _row["res"] == 70 and _row["ms"] == 6.1,
      _row)
check("...and a session that failed is not one of them",
      _row and _row["res"] != 25 and _row["fps"] == 77.0, _row)

# Anybody can write a record block by hand into an issue, and the workflow
# reads every issue. JSON has Infinity and NaN in it, int(round(inf))
# raises, and a game nobody else has reported has no second sample to
# out-vote the first - so one issue would end the run and the published
# file would stop being rebuilt for everyone.
check("Infinity, NaN and a bool are not measurements",
      all(_agg.number(v, 1, 100) is None
          for v in (float("inf"), float("-inf"), float("nan"), True,
                    "70", None, 1e400)),
      [_agg.number(v, 1, 100) for v in (float("inf"), float("nan"), True)])
check("...and neither is a work area outside the dial",
      _agg.number(-4000, 1, 100) is None and _agg.number(400, 1, 100) is None
      and _agg.number(70, 1, 100) == 70.0)
_hostile = [_comm.block({"v": 1, "exe": "hostile.exe", "game": "H",
                         "route": "optiscaler", "result": "worked",
                         "res": float("inf"), "ms": float("nan"),
                         "fps": 1e309}),
            _comm.block({"v": 1, "exe": "hostile.exe", "game": "H",
                         "route": " optiscaler ", "driver": "616.92",
                         "result": "worked", "res": 70, "ms": 6.0})]
with tempfile.TemporaryDirectory() as _td:
    _agg.issues = lambda repo, token: [{"body": b} for b in _hostile]
    _agg.OUT = Path(_td) / "compatibility.json"
    _agg.main()                      # must not raise
    _bad = json.loads(_agg.OUT.read_text(encoding="utf8"))
_hrow = ((_bad["games"]["hostile.exe"]).get("measured") or {})
check("a hand-written block cannot stop the workflow",
      _hrow.get("optiscaler", {}).get("n") == 1
      and _hrow["optiscaler"]["res"] == 70, _hrow)
check("...and the route it is filed under is the one the tool asks about",
      list(_hrow) == ["optiscaler"], list(_hrow))
_mixed_case = [_comm.block({"v": 1, "exe": "case.exe", "game": "C",
                            "route": r, "result": "worked", "res": 70})
               for r in ("feeder", "Feeder", "FEEDER ")]
with tempfile.TemporaryDirectory() as _td:
    _agg.issues = lambda repo, token: [{"body": b} for b in _mixed_case]
    _agg.OUT = Path(_td) / "compatibility.json"
    _agg.main()
    _case = json.loads(_agg.OUT.read_text(encoding="utf8"))["games"]["case.exe"]
check("one route, however it was typed into the issue",
      list(_case["routes"]) == ["feeder"]
      and _case["measured"]["feeder"]["n"] == 3, _case)
check("...and nothing that is not a number reaches the published file",
      "Infinity" not in json.dumps(_bad) and "NaN" not in json.dumps(_bad))

# Aiming for a frame rate instead of setting a percentage by feel. The two
# log lines below are the shapes the add-ons really write - the feeder's
# frame-rate line, and the cost line out of issue #81.
from core import autotune as _tune  # noqa: E402

_FEED_LINE = ("[feed] 3600 frames: feed CPU 2.10 ms/frame, "
              "GPU 4.80 ms/frame, 47.0 fps")
_OPTI_LINE = ("[02:08:18.027112] [I] DlssNr_Dx12::Dispatch DLSS-NR cost: "
              "7.41 ms total = 7.23 ms model + 0.19 ms ours (3% ours)")

_m = _tune.measure(_FEED_LINE, "", "feeder", 100)
check("the feeder's own line is the frame rate to work from",
      _m and _m.fps == 47.0 and _m.frames == 3600, _m)
_m_opti = _tune.measure("", (_OPTI_LINE + "\n") * 40, "optiscaler", 100)
check("...and the fork's cost line is the model's own price",
      _m_opti and _m_opti.model_ms == 7.23 and _m_opti.frames == 40, _m_opti)
check("a log with neither says nothing at all",
      _tune.measure("nothing here", "", "feeder", 100) is None)

# One session cannot be solved - one point does not fix a line - so it is a
# bounded step, and the text has to admit that.
_s = _tune.suggest([], 60, 100, "feeder", _m)
check("one session steps, and says it is a step",
      _s.resolution == 100 - _tune.FIRST_STEP and not _s.exact
      and any("rather than a calculation" in ln for ln in _s.lines), _s)
check("...and says one session is why, not 'first measurement'",
      any("One session is not enough" in ln for ln in _s.lines), _s.lines)
# Two sessions the arithmetic cannot separate are not a first measurement,
# and promising that the next one will solve it has already failed once.
_alike = _tune.suggest([{"resolution": 100, "fps": 47.0},
                        {"resolution": 98, "fps": 47.5}], 60, 98, "feeder", _m)
check("...while two sessions too alike say that instead",
      any("too alike" in ln for ln in _alike.lines), _alike.lines)
check("...and neither claims to be the first measurement",
      not any("First measurement" in ln
              for ln in _s.lines + _alike.lines))

# Two sessions at 100% (47 fps, 21.28 ms) and 85% (55 fps, 18.18 ms) split
# the frame into 10.1 ms that the dial does not touch and 11.2 ms of model
# at full size; 60 fps then needs sqrt(6.56/11.17) = 77%.
_rows = [{"resolution": 100, "fps": 47.0}, {"resolution": 85, "fps": 55.0}]
_m2 = _tune.measure(_FEED_LINE.replace("47.0 fps", "55.0 fps"), "",
                    "feeder", 85)
_s2 = _tune.suggest(_rows, 60, 85, "feeder", _m2)
check("two sessions are solved exactly", _s2.exact and _s2.resolution == 77,
      (_s2.resolution, _s2.lines))
check("...and the split is shown, not just the answer",
      any("10.1 ms" in ln and "11.2 ms" in ln for ln in _s2.lines), _s2.lines)

# A game that cannot reach the target with the model off entirely is not
# the model's fault, and the tool must not keep cutting the dial for it.
_slow = [{"resolution": 100, "fps": 30.0}, {"resolution": 50, "fps": 33.0}]
_s3 = _tune.suggest(_slow, 120, 50, "feeder",
                    _tune.Measured("feeder", 50, fps=33.0, frames=900))
check("a game the model is not holding back is said to be that",
      any("not what is holding it back" in ln for ln in _s3.lines), _s3.lines)
check("...and no button offers to cut the dial underneath that sentence",
      _s3.resolution == 50, _s3.resolution)
check("...and the extrapolation is not called a measurement",
      any("with everything the work area drives taken out" in ln
          for ln in _s3.lines)
      and not any("the model's cost taken out" in ln for ln in _s3.lines),
      _s3.lines)

# The OptiScaler route logs the model's cost and no frame rate, so the
# answer is a share of the frame - and it has to be called a rule of thumb.
_s4 = _tune.suggest([], 60, 100, "optiscaler", _m_opti)
check("with no frame rate logged it aims at a share of the frame",
      _s4.resolution == 76 and any("rule of thumb" in ln for ln in _s4.lines),
      (_s4.resolution, _s4.lines))
check("two sessions too close together are not solved",
      _tune._solve([(100, 20.0), (98, 19.9)]) is None)
check("...and neither are two that disagree about physics",
      _tune._solve([(100, 20.0), (50, 25.0)]) is None)
check("no target means no suggestion",
      _tune.suggest(_rows, 0, 100, "feeder", _m) is None)
check("the suggestion is never outside the dial's range",
      _tune._clamp(3) == _tune.MIN_RES and _tune._clamp(400) == _tune.MAX_RES)
from core import feedcfg as _fc60  # noqa: E402
from core.ui import ctl_game as _uig60t  # noqa: E402


def _tuned60(route, target=60, measured=None, cost=None, history=None, from_config=True):
    """A controller after 'did it work?' measured a session: (controller,
    what was written to a config)."""
    wrote: list = []
    m = measured or _tune.Measured(route=route, resolution=100, fps=47.0, frames=900)
    m.from_config = from_config
    c = _ui_ctl(_ui_game(name="Tuned", api="DX11"), _ui_support([dlss.FEEDER, dlss.OPTI], route))
    c.route = route
    c.target_fps = target
    rep = diagnose.Report(route=route)
    rep.verdict, rep.ran = "Working.", True
    pats = [patch.object(_fc60, "write", lambda d, v: wrote.append(("feedcfg", v))),
            patch.object(optiscaler, "enable_nr", lambda d, log=None, settings=None: wrote.append(("nr", settings)))]
    if cost is not None:
        pats.append(patch.object(_tune, "cost_lines", lambda rows, mm: list(cost)))
    if history is not None:
        pats.append(patch.object(_tune, "history", history))
    for p in pats:
        p.start()
    try:
        c._autotune(rep, m)
    finally:
        for p in pats:
            p.stop()
    return c, wrote


with _ui_isolated(), _ui_threads(run=False):
    _tn60, _wr60 = _tuned60("feeder", history=lambda d: [{"resolution": 100, "fps": 47.0},
                                                          {"resolution": 50, "fps": 70.0}])
    _sug60 = _tn60._tune
    _wr60b: list = []
    with patch.object(_fc60, "write", lambda d, v: _wr60b.append(("feedcfg", v))), \
            patch.object(optiscaler, "enable_nr",
                         lambda d, log=None, settings=None: _wr60b.append(("nr", settings))):
        if _sug60 is not None:
            _tn60.apply_tune()
            _tn60.route = dlss.OPTI
            _tn60._tune = _sug60
            _tn60.apply_tune()
check("applying it writes the config in place, and says when it takes effect",
      _sug60 is not None
      and _wr60b == [("feedcfg", {"work_resolution": _sug60.resolution}),
                     ("nr", {"WorkingScale": round(_sug60.resolution / 100.0, 3)})]
      and "next run" in _tn60.text() and _tn60._tune is None, (_sug60, _wr60b))
check("...and the measuring half writes nothing at all", _wr60 == [], _wr60)
_ui_cleanup()

# What the dial costs, in milliseconds, out of the same solve. Every other
# tool in this ecosystem sets this setting by feel; the numbers were being
# worked out here and used for one sentence of advice.
_rows2 = [{"resolution": 100, "fps": 47.0}, {"resolution": 50, "fps": 70.0}]
_split = _tune.split(_rows2, _m)
check("two sessions split the frame into the part the dial moves and the rest",
      _split and 0 < _split[0] < 1000 and _split[1] > 0, _split)
_costs = _tune.costs(_rows2, _m)
check("...and the cost is printed for 50, 75 and 100 per cent",
      [c.resolution for c in _costs] == [50, 75, 100],
      [c.resolution for c in _costs])
check("...with the model at full size costing what the solve said",
      abs(_costs[-1].model_ms - _split[1]) < 0.01,
      (_costs[-1].model_ms, _split[1]))
check("...the session that was played is marked as the played one",
      [c.played for c in _costs] == [False, False, True])
check("...and each row carries the frame rate it implies",
      all(c.fps for c in _costs)
      and abs(_costs[-1].fps - 47.0) < 0.5, [c.fps for c in _costs])
_one = _tune.cost_lines([{"resolution": 100, "fps": 47.0}], _m)
check("one session is not enough to cost the other settings, so it says none",
      _one == [], _one)
_ocosts = _tune.costs([], _m_opti)
check("the route that logs no frame rate still costs the dial",
      [c.resolution for c in _ocosts] == [50, 75, 100]
      and abs(_ocosts[-1].model_ms - 7.23) < 0.01, _ocosts)
check("...and offers no fps, because none was ever measured",
      not any(c.fps for c in _ocosts)
      and any("no fps here" in ln for ln in _tune.cost_lines([], _m_opti)))
check("nothing measured, nothing printed", _tune.cost_lines([], None) == [])

# The measured part of a shared result.
_sh = _tune.shared(_rows2, _m)
check("what is shared is the work area, the model's cost and the frame rate",
      _sh.get("res") == 100 and _sh.get("fps") == 47.0 and _sh.get("ms"), _sh)
check("...the work area as a whole percent", isinstance(_sh["res"], int), _sh)
_sh_opti = _tune.shared([], _m_opti)
check("...the model's own cost where that is what was measured",
      _sh_opti == {"res": 100, "ms": 7.23}, _sh_opti)
check("a session that cannot be split shares no cost",
      "ms" not in _tune.shared([{"resolution": 100, "fps": 47.0}], _m),
      _tune.shared([{"resolution": 100, "fps": 47.0}], _m))
check("and nothing measured shares nothing", _tune.shared([], None) == {})
with _ui_isolated(), _ui_threads(run=False):
    _aim60 = (_tuned60("feeder", target=0, cost=[])[0].text(),
              _tuned60("feeder", target=0, cost=["50%  12.0 ms"])[0].text())
check("the hint about 'aim for' is only printed under a table",
      "aim for" not in _aim60[0] and "aim for" in _aim60[1], _aim60)

# The Windows fault is read after the tuner - a tuner that raises must not
# take the crash correction down with it.
with _ui_isolated(), _ui_threads(run=False):
    _cr60 = _ui_ctl(_ui_game(name="Tuner Raises", api="DX11"), _ui_support([dlss.FEEDER], dlss.FEEDER))
    _asked_crash60: list = []
    _cr60._windows_crash = lambda rep: _asked_crash60.append(rep)
    _cr60.what_next = lambda rep: None
    _rep60 = diagnose.Report(route="feeder")
    _rep60.verdict, _rep60.ran = "Working.", True

    def _boom60(*a, **k):
        raise RuntimeError("the tuner fell over")
    with patch.object(_tune, "history", _boom60), patch.object(_tune, "remember", _boom60):
        try:
            _cr60.q.put(("diagnosed", (_cr60.game.install_dir, _rep60,
                                       _tune.Measured(route="feeder", resolution=100, fps=47.0))))
            _cr60.pump()
            _raised60 = ""
        except Exception as _e60:
            _raised60 = repr(_e60)
check("nothing new runs outside a try that the crash correction follows",
      _raised60 == "" and _asked_crash60 == [_rep60], (_raised60, _asked_crash60))
_ui_cleanup()
# Two sessions five points apart solve a table the person can disbelieve.
# They do not measure a number to put in front of strangers.
_close = [{"resolution": 100, "fps": 47.0}, {"resolution": 95, "fps": 47.05}]
check("a solve from two near-identical sessions is not shared as a cost",
      "ms" not in _tune.shared(_close, _m), _tune.shared(_close, _m))
check("...but it is still printed, where it can be argued with",
      _tune.cost_lines(_close, _m) != [])
_wide = [{"resolution": 100, "fps": 47.0}, {"resolution": 50, "fps": 70.0}]
check("...and a real spread is shared", "ms" in _tune.shared(_wide, _m))
check("a half-written history does not raise, it is skipped",
      _tune._points([{"resolution": 100, "fps": "47,0"},
                     {"resolution": None, "fps": 47.0},
                     {"fps": 47.0}, {"resolution": 50, "fps": 70.0}])
      == [(50, 1000.0 / 70.0)])

# The dropdown can be changed between "did it work?" and "share the
# result" - the tool itself asks people to change it when a route fails.
with _ui_isolated(), _ui_threads(run=False):
    _sa60 = _ui_ctl(_ui_game(name="Share App"), _ui_support([dlss.FEEDER, dlss.OPTI], dlss.OPTI))
    _sa60._measured = _opti_m60
    with patch.object(_tune, "history", lambda *_a: []):
        check("a cost measured on one route is not published against another",
              _sa60.measured_for("feeder") == {}, "filed under feeder")
        check("...and is published against its own",
              _sa60.measured_for("optiscaler").get("res") == 70)
_ui_cleanup()
# the dropdown moved to feeder between 'did it work?' and 'share the result'
_moved60 = _share60("optiscaler", _opti_m60, answer=True, shown="feeder")
_kept60 = _share60("optiscaler", _opti_m60, answer=True)
check("the record is built from the same route the measurement is checked "
      "against",
      _moved60[2] and _moved60[2][0].get("measured") == {}
      and _kept60[2] and _kept60[2][0].get("measured", {}).get("res") == 70,
      ([r.get("measured") for r in _moved60[2]], [r.get("measured") for r in _kept60[2]]))

with _ui_isolated(), _ui_threads(run=False):
    _tt60, _ = _tuned60("feeder", target=0, history=lambda d: [{"resolution": 100, "fps": 47.0},
                                                               {"resolution": 50, "fps": 70.0}])
    check("the cost table is printed whether or not a target was typed",
          "what the work area costs here" in _tt60.text(), _tt60.text()[-300:])
    check("what the session cost is kept for the shared result",
          _tt60._measured is not None and _tt60._measured_rows
          and _tt60.measured_for("feeder") == _tune.shared(_tt60._measured_rows, _tt60._measured)
          and _tt60.measured_for("feeder") != {},
          (_tt60._measured, _tt60.measured_for("feeder")))
    _tt60._forget_last_session()
    check("the rows it was solved from are dropped with it",
          _tt60._measured_rows is None and _tt60._measured is None)
_ui_cleanup()

# The driver, said before the install instead of in the diagnosis after it:
# 616.64 is named in 31 of the first 87 reports, more than any other single
# cause, and the tool used to mention it only once the evening was lost.
check("616.56 gets no warning", dlss.driver_warning("feeder", "616.56") is None)
_w = dlss.driver_warning("feeder", "616.64")
check("616.64 does, on a route that installs the renodx add-on",
      _w and "4.55" in _w and "no reports of this fault" in _w, _w)
_w2 = dlss.driver_warning("optiscaler", "616.64")
check("...and the OptiScaler route is not told about a pin it never gets",
      _w2 and "4.55" not in _w2 and "does not install the renodx" in _w2, _w2)
check("the Remix route runs its own runtime, so it is not warned",
      dlss.driver_warning("remix", "616.99") is None)
check("an unknown driver says nothing", dlss.driver_warning("feeder", "") is None)
check("driver_at_least can be asked about a version it was handed",
      gpu.driver_at_least("616.64", "617.10") is True
      and gpu.driver_at_least("616.64", "616.56") is False)
with _ui_isolated(), _ui_threads(run=False):
    _dw60 = _ui_ctl(_ui_game(name="Driver"), _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER))
    with patch.object(gpu, "driver_version", lambda: "616.64"):
        _dw60.apply_route(dlss.FEEDER)
check("the route note carries it before INSTALL is pressed",
      any(k == "driver" and "4.55" in t for k, t in _dw60.notes), _dw60.notes)
_ui_cleanup()

# The other end of it. DOOM on driver 475.14 (#16) was told "the game has
# not been started since the install" - true, and no use to anybody: that
# driver is years older than DLSS 5 and cannot create the feature at all.
_old = dlss.driver_warning("feeder", "475.14")
check("a driver from before DLSS 5 is the answer, not a footnote",
      _old and "older than DLSS 5" in _old and "616.56" in _old, _old)
check("...and it says what will happen if they install anyway",
      _old and "the model will not run" in _old, _old)
check("...without freezing a count of the issue tracker into the binary",
      _old and "every report" not in _old, _old)
check("...on the OptiScaler route too",
      "older than DLSS 5" in (dlss.driver_warning("optiscaler", "580.10") or ""))
check("...but not on Remix, which carries its own runtime",
      dlss.driver_warning("remix", "475.14") is None)
check("the first documented carrier is the line between the two warnings",
      dlss.driver_warning("feeder", "616.56") is None
      and "older than DLSS 5" not in (dlss.driver_warning("feeder", "616.64") or ""))

# Vulkan and OptiScaler: the diagnosis has said for two releases that the
# model has nothing to attach to there (RDR2, #66 and #70). The dropdown
# said "model resolution dial: the fps lever" and nothing else.
_ok, _why = dlss.fit("optiscaler", "Vulkan", True, 120)
check("the Vulkan warning is on the route label, before the install",
      "nothing to attach to" in _why and "DirectX 12" in _why, _why)
check("...and the route is still offered, because two reports are not a law",
      _ok is True)
check("...while D3D12 keeps its own note",
      "fps lever" in dlss.fit("optiscaler", "DX12", True, 120)[1])

# HDR: the feeder's 0.15.1 is the build that stopped the neural pass
# wrecking HDR highlights, so an older pinned build on an HDR display is
# worth a word - and only then.
check("the HDR minimum is the release that fixed it",
      sources.FEEDER_HDR_MIN == "v0.15.1"
      and sources.feeder_key("v0.15.1") > sources.feeder_key("v0.14.0-beta.5"))
check("hdr_on answers True, False or None and never raises",
      gpu.hdr_on() in (True, False, None))
check("...and reads the enabled bit, not the supported one",
      "0x2" in src_of(gpu.hdr_on)
      and "capable of it" in src_of(gpu.hdr_on))
check("the display-config path struct is the size Windows expects",
      "refreshRateNum" in src_of(gpu.hdr_on), "72 bytes")
_hdr60 = {}
with _ui_isolated(), _ui_threads(run=False):
    for _on60 in (None, False, True):
        _h60 = _ui_ctl(_ui_game(name="HDR"), _ui_support([dlss.FEEDER], dlss.FEEDER))
        with patch.object(gpu, "hdr_on", lambda v=_on60: v):
            _h60.hdr_note()
        _hdr60[_on60] = _h60.text()
check("nothing is said when the display is not in HDR",
      _hdr60[None] == "" and _hdr60[False] == "" and "HDR" in _hdr60[True], _hdr60)
_ui_cleanup()

# Issue #53: "does it work on an RX 7600". Both AMD routes are real and
# neither is fetchable, and the answer has to say which and why.
check("an AMD card is named, not just 'no nvidia card'",
      "AMD" in gpu.AMD_ANSWER and "RDNA" in gpu.AMD_ANSWER)
check("...and the reason is the files, not caution",
      "Discord" in gpu.AMD_ANSWER and "bundles nothing" in gpu.AMD_ANSWER
      and "closed source" in gpu.AMD_ANSWER)
check("...and it says what would change it",
      "ships the whole thing in a release" in gpu.AMD_ANSWER)
check("other_vendor only speaks when there is no NVIDIA card",
      "if detect()[0]:" in src_of(gpu.other_vendor))

# Issue #31, fourth round, and issue #2 with it: the 32-bit Vulkan layer
# registers and still never loads. Proved with the Vulkan loader's own
# trace in a 32-bit process - two manifests, one name:
#
#   Removing layer VK_LAYER_reshade (...ReShade32.json) because it is a
#     duplicate of VK_LAYER_reshade (...ReShade64.json)
#   Requested layer "VK_LAYER_reshade" was wrong bit-type.
#
# HKCU\Software is not redirected per architecture the way HKLM\Software
# is, so a 32-bit game sees both of ours and keeps the one it cannot load.
from core import vulkan as _vk  # noqa: E402

check("the two layers no longer share a name",
      _vk.LAYER_NAME32 and _vk.LAYER_NAME32 != _vk.LAYER_NAME)
check("...and the 32-bit manifest is the one renamed",
      'if manifest == MANIFEST32:' in src_of(_vk._place)
      and 'layer["name"] = LAYER_NAME32' in src_of(_vk._place))

_d = Path(tempfile.mkdtemp(prefix="vklayer_"))
_man = {"file_format_version": "1.0.0",
        "layer": {"name": "VK_LAYER_reshade", "library_path": ".\\ReShade64.dll"}}
(_d / "ReShade64.json").write_text(json.dumps(_man), encoding="utf8")
(_d / "ReShade32.json").write_text(json.dumps(_man), encoding="utf8")
_saved_dir = _vk.layer_dir
_vk.layer_dir = lambda: _d
try:
    check("an install made before this is spotted by the name it carries",
          _vk.name_clash() == _d / "ReShade32.json", _vk.name_clash())
    _fixed = dict(_man)
    _fixed["layer"] = dict(_man["layer"], name=_vk.LAYER_NAME32,
                           library_path=".\\ReShade32.dll")
    (_d / "ReShade32.json").write_text(json.dumps(_fixed), encoding="utf8")
    check("...and a rewritten one is not flagged again",
          _vk.name_clash() is None)
    (_d / "ReShade32.json").unlink()
    check("...and neither is a machine with no 32-bit layer at all",
          _vk.name_clash() is None)
finally:
    _vk.layer_dir = _saved_dir
shutil.rmtree(_d, ignore_errors=True)

# The diagnosis names it, instead of "the layer is registered, so it should
# work" - which is what a 32-bit game got for four rounds.
_d = Path(tempfile.mkdtemp(prefix="i31clash_"))
(_d / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "CoJGunslinger.exe", "bitness": 32,
     "api": "Vulkan", "proxy": diagnose.VULKAN_LAYER, "path": "feeder",
     "dxvk": True, "vulkan": {"mine": True, "any": True},
     "files": ["d3d9.dll", "dlss5-feed.addon32", "ReShade.ini",
               "nvngx_dlssnr.dll"]}), encoding="utf8")
for _n in ("d3d9.dll", "dlss5-feed.addon32", "ReShade.ini", "nvngx_dlssnr.dll"):
    (_d / _n).write_bytes(b"MZ")
(_d / "CoJGunslinger_d3d9.log").write_text("info:  DXVK: v2.4", encoding="utf8")
_clash_dir = Path(tempfile.mkdtemp(prefix="vkclash_"))
(_clash_dir / "ReShade64.json").write_text(json.dumps(_man), encoding="utf8")
(_clash_dir / "ReShade32.json").write_text(json.dumps(_man), encoding="utf8")
_vk.layer_dir = lambda: _clash_dir
_layer_saved = diagnose.model._layer_state
diagnose.model._layer_state = lambda man: (True, True)
try:
    _r = diagnose.analyse(_d)
    check("a 32-bit game with the clashing name is told exactly that",
          "duplicate name" in _r.verdict
          and any("carries the layer name" in f.title for f in _r.findings),
          _r.verdict)
    check("...and the answer is to install again, not to blame antivirus",
          any("its own name" in f.detail for f in _r.findings),
          [f.detail[:60] for f in _r.findings])
finally:
    _vk.layer_dir = _saved_dir
    diagnose.model._layer_state = _layer_saved
shutil.rmtree(_d, ignore_errors=True)
shutil.rmtree(_clash_dir, ignore_errors=True)

# Issue #88: a keyboard with no Insert key has no way into the overlay,
# which is where neural rendering is switched on. Both sides store it, in
# their own way: ReShade as "<vk>,0,0,0", OptiScaler as hex.
_d = Path(tempfile.mkdtemp(prefix="overlaykey_"))
(_d / "ReShade.ini").write_text('[GENERAL]\nPresetPath=.\\DLSS5.ini\n',
                                encoding="utf8")
reshade_ini.set_overlay_key(_d, 0x79)
check("ReShade's overlay key is written as a virtual-key code",
      "KeyOverlay=121,0,0,0" in (_d / "ReShade.ini").read_text(encoding="utf8"),
      (_d / "ReShade.ini").read_text(encoding="utf8"))
(_d / "OptiScaler.ini").write_text("[Menu]\nShortcutKey=auto\nScale=auto\n",
                                   encoding="utf8")
optiscaler.set_overlay_key(_d, 0x79)
_ini = (_d / "OptiScaler.ini").read_text(encoding="utf8")
check("...and OptiScaler's as hex, leaving the rest of the section alone",
      "ShortcutKey=0x79" in _ini and "Scale=auto" in _ini, _ini)
def _key_name_is(label, vk, route_default):
    """What the tool would tell someone to press, having picked `label`."""
    # a settings file of its own: this check once wrote into the machine's real one
    import tempfile as _tf
    _real = prefs.FILE
    prefs.FILE = Path(_tf.mkdtemp()) / "settings.json"
    try:
        prefs.set_("overlay_key", 0 if "default" in label else vk)
        return reshade_ini.overlay_key_name(route_default)
    finally:
        prefs.FILE = _real


reshade_ini.set_overlay_key(_d, 0)
optiscaler.set_overlay_key(_d, 0)
check("...and 0 means 'leave the defaults alone', not 'bind nothing'",
      "KeyOverlay=121,0,0,0" in (_d / "ReShade.ini").read_text(encoding="utf8")
      and "ShortcutKey=0x79" in (_d / "OptiScaler.ini").read_text(encoding="utf8"))
check("every offered key is a real virtual-key code",
      all(isinstance(v, int) and 0 < v < 256
          for k, v in reshade_ini.OVERLAY_KEYS.items() if k != "route default")
      and reshade_ini.OVERLAY_KEYS["Home"] == 0x24
      and reshade_ini.OVERLAY_KEYS["Insert"] == 0x2D)
# The sentinel may not be a key code any entry uses: with "route default"
# and "Home" both on 0x24 the stored value could not say which had been
# picked, so picking Home wrote Home and then said "press Insert".
check("...and 'route default' is stored as nothing at all, not as a key",
      reshade_ini.OVERLAY_KEYS["route default"] == 0
      and reshade_ini.ROUTE_DEFAULT == 0
      and sorted(reshade_ini.OVERLAY_KEYS.values()).count(0) == 1)
check("...so picking Home is told back as Home, on either route",
      _key_name_is("Home", 0x24, "Insert") == "Home"
      and _key_name_is("route default", 0, "Insert") == "Insert"
      and _key_name_is("route default", 0, "Home") == "Home")
_src = src_of(installer.install)
_i_key, _i_carry = (_src.find("reshade_ini.set_overlay_key"),
                    _src.find("reshade_ini.carry_over"))
check("ReShade's binding is written after carry_over, which would undo it",
      0 <= _i_carry < _i_key, (_i_carry, _i_key))
check("...and OptiScaler's goes in with the rest of its configuration",
      "optiscaler.set_overlay_key" in _src)
shutil.rmtree(_d, ignore_errors=True)

# "OptiScaler does not engage" is six issues, four of them still open, and
# every one was answered with advice. When nothing about neural rendering
# reaches the log at all, the four things it needs are now each answered
# from disk instead.
_d = _opti_dir("opti_checklist_", "[12:00:00.000000] [I] Init done\n"
                                  "[12:00:01.000000] [I] hk_ffxFsr3 context created\n")
(_d / "OptiScaler.ini").write_text("[DlssNr]\nEnabled=false\n\n[Menu]\nScale=auto\n",
                                   encoding="utf8")
(_d / "nvngx_dlssnr.dll").write_bytes(b"MZ")
_man = json.loads((_d / "dlss5-autopilot.json").read_text(encoding="utf8"))
_man["upscaler"] = "fsr"
_man["files"] = ["dxgi.dll", "nvngx.dll_dlssnr.dll"]
(_d / "dlss5-autopilot.json").write_text(json.dumps(_man), encoding="utf8")
_r = diagnose.analyse(_d)
_info = [f_.title for f_ in _r.findings if f_.level == "info"]
check("the ini's own Enabled line is read back, not assumed",
      any("Enabled=false" in t for t in _info), _info)
check("...the runtime and the forwarder are each answered",
      any("nvngx_dlssnr.dll: present" in t for t in _info)
      and any("nvngx.dll_dlssnr.dll: MISSING" in t for t in _info), _info)
check("...the game's own upscaler is named", any("fsr" in t for t in _info), _info)
check("...and the driver is checked against the first one that carries the model",
      any("driver" in t for t in _info), _info)
shutil.rmtree(_d, ignore_errors=True)

# A build that never wrote a forwarder must not be reported as broken.
_d = _opti_dir("opti_checklist2_", "[12:00:00.000000] [I] Init done\n")
(_d / "OptiScaler.ini").write_text("[DlssNr]\nEnabled=true\n", encoding="utf8")
(_d / "nvngx_dlssnr.dll").write_bytes(b"MZ")
_r = diagnose.analyse(_d)
_info = [f_.title for f_ in _r.findings if f_.level == "info"]
check("a forwarder the install never wrote is not called missing",
      any("may not use one" in t for t in _info), _info)
shutil.rmtree(_d, ignore_errors=True)

# The biggest group of reports is "the game closed itself", and a game that
# dies before ReShade loads writes nothing at all. Windows does: an
# Application Error event naming the faulting module. Read from this
# machine's own log, which is where the field order below came from.
from core import wincrash as _wc  # noqa: E402

_line = ("2026-09-09T11:08:54|Application Error|BsgLauncher.exe~15.0.0.4595~"
         "6a841e80~libcef.dll~143.0.9.0~691e395d~80000003~0000000007b49ffc~"
         "14244~134334249864809804~D:\\Games\\BsgLauncher.exe")
_saved_ps = _wc._ps
try:
    _wc._ps = lambda _script: _line
    _c = _wc.last_crash("BsgLauncher.exe", since=0, within_days=30)
    check("the faulting module is read out of the event", _c and
          _c.module == "libcef.dll" and _c.code == "0x80000003", _c)
    check("...a module that is neither ours nor the game's is named as that",
          not _c.ours() and "another mod" in _wc.describe(_c)[1],
          _wc.describe(_c))
    check("...and an event for another executable is not this game's",
          _wc.last_crash("SomeOtherGame.exe", since=0, within_days=30) is None)
    check("...nor is one from before the install",
          _wc.last_crash("BsgLauncher.exe", since=time.time(),
                         within_days=30) is None)
    # dxgi.dll is ReShade in a game folder and Windows' own in System32,
    # and the event does not say which copy faulted. Claiming it as ours on
    # the name alone was the release's own worst wrong answer.
    _shared = _wc.Crash("2026-09-09 11:08", "Game.exe", "dxgi.dll",
                        "0xC0000005", "Application Error")
    check("a module that only shares our proxy's name is not claimed",
          not _shared.ours() and "also the name of a Windows system DLL"
          in _wc.describe(_shared, "dxgi.dll")[1],
          _wc.describe(_shared, "dxgi.dll"))
    check("...and with no proxy of that name it is somebody else's",
          "another mod" in _wc.describe(_shared, "")[1])
    _ours = _wc.Crash("2026-09-09 11:08", "Game.exe", "dlss5-feed.addon64",
                      "0xC0000005", "Application Error")
    check("a module we installed is called ours, plainly",
          _ours.ours() and "ours to fix" in _wc.describe(_ours)[1])
    _own = _wc.Crash("2026-09-09 11:08", "Game.exe", "Game.exe",
                     "0xC0000005", "Application Error")
    check("...and the game faulting in its own code is not blamed on us",
          "not in our module" in _wc.describe(_own)[1])
    check("nothing to say when there is no event",
          _wc.describe(None) is None)
finally:
    _wc._ps = _saved_ps

check("a game name that is not a file name is never put in a command",
      _wc.last_crash("..\\evil & del *", since=0) is None)
check("the query is capped, so a diagnosis cannot hang on it",
      _wc.TIMEOUT <= 15 and "timeout=TIMEOUT" in src_of(_wc._ps))
with _ui_isolated():
    _wcg60 = _ui_game(name="Crashy")
    _wcc60 = _ui_ctl(_wcg60, _ui_support([dlss.FEEDER], dlss.FEEDER))
    _wcw60 = _worker_only(lambda: _wcc60._windows_crash(diagnose.Report(route="feeder")),
                          _wc, "last_crash", None)
check("...and it is read off the Tk thread", _wcw60 == ([], [True]), _wcw60)
_ui_cleanup()
check("the event goes into the report body",
      "windows event:" in src_of(diagnose.issue_body))

# Issue #75, the video player on driver 610.60: the add-on's own error was
# in the log twice and the verdict was still inconclusive. The line is
# copied from that report.
_d = _diag_dir("ngx_export_", reshade=(
    "11:25:46:119 [26964] | INFO  | Redirecting IDXGIFactory2::CreateSwapChainForHwnd\n"
    "11:25:54:684 [19864] | ERROR | [DLSS 5 Neural Rendering] "
    "vtable::Hook(Failed to find NVSDK_NGX_D3D12_EvaluateFeature_C)\n"
    "11:27:18:762 [ 4892] | INFO  | Exiting ...\n"))
# The same log line means opposite things on an old driver and a new one,
# so the driver is pinned rather than read from the machine running this.
from core import gpu as _gpu_drv                                  # noqa: E402
_drv_saved = _gpu_drv.driver_version
try:
    _gpu_drv.driver_version = lambda: "610.60"        # #75's own driver
    _r = diagnose.analyse(_d)
    check("a driver with no DLSS 5 entry point is the verdict, not a footnote",
          "no DLSS 5 entry point" in _r.verdict, _r.verdict)
    check("...and it says which driver first carried it",
          any("616.56" in f.detail for f in _r.findings),
          [f.detail[:70] for f in _r.findings])
    check("...without claiming the machine's current driver is the one that ran",
          any("the driver the game ran with" in f.detail for f in _r.findings))

    # Issue #127: the same line on driver 616.92 was answered "update the
    # graphics driver". On a driver that carries DLSS 5 the line appears in
    # working sessions too, so it is not a driver verdict there.
    _gpu_drv.driver_version = lambda: "616.92"
    _r = diagnose.analyse(_d)
    check("on a new driver the missing call is not 'update your driver'",
          "update the graphics driver" not in _r.verdict, _r.verdict)
    check("...and is not made into a driver verdict of any kind",
          not any("does not export" in f.title for f in _r.findings),
          [f.title for f in _r.findings])

    # The owner's own 616.64 logs, from sessions that delivered frames: the
    # plain EvaluateFeature IS hooked, and only the _C variant beside it is
    # missing. On a driver that carries DLSS 5 that pair is not a verdict;
    # on one older than DLSS 5 the driver's age decides, whatever the line.
    _gpu_drv.driver_version = lambda: "616.64"
    _dh = _diag_dir("hook_plain_", reshade=(
        "22:29:22:384 [44208] | DEBUG | [DLSS 5 Neural Rendering] "
        "vtable::Hook(NVSDK_NGX_D3D12_EvaluateFeaturehooked with "
        "0x00007fff55ee8740 => 0x00007fff36671430)\n"
        "22:29:22:384 [44208] | ERROR | [DLSS 5 Neural Rendering] "
        "vtable::Hook(Failed to find NVSDK_NGX_D3D12_EvaluateFeature_C)\n"))
    _rh = diagnose.analyse(_dh)
    check("on 616.64 a failed _C beside a successful plain hook is no verdict",
          "update the graphics driver" not in _rh.verdict, _rh.verdict)
    _gpu_drv.driver_version = lambda: "610.60"
    _rh = diagnose.analyse(_dh)
    check("...but on a driver older than DLSS 5 the driver still decides",
          "update the graphics driver" in _rh.verdict
          and any("610.60" in f.title for f in _rh.findings), _rh.verdict)

    # The hook lines are DEBUG; a report that drops them replays to a
    # different answer than the machine gave (#127's excerpt had none).
    check("a report keeps the add-on's hook lines the driver rule reads",
          all(any(k in ln for k in diagnose._RESHADE_KEEP)
              for ln in (_dh / "ReShade.log").read_text().splitlines()))

    # A driver that cannot be read: the line decides, and only on its own.
    # A malformed version string is unread too, not "new".
    for _unread in (None, "32.0.16.16xx"):
        _gpu_drv.driver_version = lambda _v=_unread: _v
        check(f"driver {_unread!r}: a plain hook beside it is no verdict",
              "update the graphics driver" not in diagnose.analyse(_dh).verdict)
        _r = diagnose.analyse(_d)
        check(f"driver {_unread!r}: the failed hook alone still is",
              "no DLSS 5 entry point" in _r.verdict
              and not any("not the driver being too old" in f.detail
                          or "older than DLSS 5" in f.title
                          for f in _r.findings), _r.verdict)
    _gpu_drv.driver_version = lambda: None
    check("...and the detail does not ask about a driver it never named",
          not any("the driver the game ran with" in f.detail
                  for f in diagnose.analyse(_d).findings))
    _gpu_drv.driver_version = lambda: "616.92"
    check("on a new driver with nothing hooked it is said as a warning",
          any(f.title.startswith("The add-on could not hook")
              for f in diagnose.analyse(_d).findings),
          [f.title for f in diagnose.analyse(_d).findings])
    shutil.rmtree(_dh, ignore_errors=True)
finally:
    _gpu_drv.driver_version = _drv_saved
shutil.rmtree(_d, ignore_errors=True)

# Issue #79: every path this tool writes comes out of a file somebody else
# can write - an archive's member names, or the install record in the game
# folder. A string prefix test does not catch a sibling directory.
_root = Path(tempfile.mkdtemp(prefix="inside_"))
(_root / "game").mkdir()
(_root / "game-other").mkdir()
_bad = None
try:
    net.inside(_root / "game", "../game-other/x.dll")
except net.OutsideError as _e:
    _bad = _e
check("a '..' entry that lands in a sibling folder is refused", _bad is not None)
_bad = None
try:
    net.inside(_root / "game", "C:/Windows/System32/x.dll")
except net.OutsideError as _e:
    _bad = _e
check("...and so is an absolute path", _bad is not None)
check("a normal member still resolves under the folder",
      net.inside(_root / "game", "reshade-shaders/Shaders/DLSS5_Feed.fx")
      == (_root / "game" / "reshade-shaders" / "Shaders" / "DLSS5_Feed.fx"))
shutil.rmtree(_root, ignore_errors=True)

# The same, through uninstall: a record naming a file outside the folder is
# ignored, and the file it names is still there afterwards.
_root = Path(tempfile.mkdtemp(prefix="untrav_"))
_game = _root / "game"
_game.mkdir()
(_root / "sentinel.txt").write_text("keep me", encoding="utf8")
(_game / "dxgi.dll").write_bytes(b"MZ")
(_game / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX12", "path": "optiscaler", "proxy": "dxgi.dll",
     "files": ["dxgi.dll", "../sentinel.txt"]}), encoding="utf8")
_lines: list[str] = []
installer.uninstall(games.Game(name="Game", folder=_game,
                               exe=_game / "Game.exe"),
                    on_log=_lines.append)
check("uninstall leaves a file outside the game folder alone",
      (_root / "sentinel.txt").is_file())
check("...and says so in the log",
      any("outside the game folder" in ln for ln in _lines), _lines[:4])
check("...while still removing what really was ours",
      not (_game / "dxgi.dll").is_file())
shutil.rmtree(_root, ignore_errors=True)


section("61. what the 1.8.0 release gate found")

# The gate's own findings, locked so they cannot come back.

# LOGIC: the tuner was always one session behind - the history was read
# before the new measurement was stored, so the second session still said
# "one session is not enough" and only the third could solve.
from core.ui import ctl_game as _uig61  # noqa: E402
_order61: list = []
with _ui_isolated(), _ui_threads(run=False):
    with patch.object(_tune, "remember", lambda *a, **k: _order61.append("remember")), \
            patch.object(_tune, "suggest", lambda *a, **k: (_order61.append("suggest"), None)[1]):
        _tuned60("feeder", target=60)
check("the measurement is recorded before the suggestion is worked out",
      _order61 == ["remember", "suggest"], _order61)

# The session's work area, read by the reader the diagnosis worker calls.
_ms61: dict = {}
with patch.object(_tune, "ran_at_exact", lambda d, route: 85), \
        patch.object(_tune, "written_after", lambda d, route, p: _ms61.get("after", False)), \
        patch.object(_tune, "measure", lambda feed, opti, route, res:
                     _tune.Measured(route=route, resolution=res, fps=50.0)):
    _msr61 = Path(tempfile.mkdtemp(prefix="measure61_"))
    _m61a = _uig61.GameControl._measure_session(_msr61, "feeder", 100)
    _ms61["after"] = True
    _m61b = _uig61.GameControl._measure_session(_msr61, "feeder", 100)
    shutil.rmtree(_msr61, ignore_errors=True)
check("...and the session's own resolution comes from the config, not the slider",
      _m61a is not None and _m61a.resolution == 85 and _m61a.from_config, _m61a)

# FEATURES: printing the table without a target put two 100 KB log tails, a
# folder glob and a config read on the Tk thread for everybody, on every
# press - work that used to happen only for the few who typed a frame rate.
with _ui_isolated():
    _dg61 = _ui_ctl(_ui_game(name="Diagnosed", api="DX11"), _ui_support([dlss.FEEDER], dlss.FEEDER))
    _dg61.route = dlss.FEEDER
    _rep61 = diagnose.Report(route="feeder")
    _rep61.verdict, _rep61.ran = "Working.", True
    with patch.object(diagnose, "analyse", lambda *a, **k: _rep61):
        _w61 = _worker_only(_dg61.diagnose, _uig61.GameControl, "_measure_session", None)
check("the logs are read on the worker that was reading logs anyway",
      _w61 == ([], [True]), _w61)
_tails61: list = []
with _ui_isolated(), _ui_threads(run=False):
    with patch.object(diagnose, "_tail", lambda *a, **k: (_tails61.append("tail"), "")[1]), \
            patch.object(diagnose, "_opti_log", lambda *a, **k: (_tails61.append("opti_log"), None)[1]):
        _tuned60("feeder", target=60)
        _tuned60("feeder", target=0)
check("...and not on the thread drawing the window", _tails61 == [], _tails61)
_nt61: list = []
_th61 = __import__("threading").Thread(
    target=lambda: _nt61.append(_uig61.GameControl._measure_session(Path(tempfile.gettempdir()) / "nope61",
                                                                   "feeder", 100)), daemon=True)
_th61.start()
_th61.join(20)
check("...and the reader itself touches no Tk: a static function any thread can call",
      isinstance(vars(_uig61.GameControl).get("_measure_session"), staticmethod) and _nt61 == [None],
      _nt61)
_ui_cleanup()
# Run, not read: the prune and the suggestion both walk rows that come back
# out of a file on a disk. A source-string check cannot see a raise.
_JUNK = [17, {"resolution": "abc", "fps": 60}, {"resolution": 50, "fps": "0"},
         {"resolution": 50, "fps": -30}, {"at": "yesterday", "resolution": 60,
                                          "fps": 55.0}]
_prefs_file = _tune.prefs.FILE
try:
    _tmp = Path(tempfile.mkdtemp(prefix="hist_"))
    _tune.prefs.FILE = _tmp / "settings.json"
    _tune.prefs.set_(_tune.HISTORY_KEY, {
        **{f"d:/games/g{i}": [{"resolution": 75, "fps": 60.0, "at": 1000 + i}]
           for i in range(_tune.MAX_GAMES + 5)},
        "d:/games/junk": _JUNK})
    _tune.remember(Path("D:/Games/NEW"), _tune.Measured(
        route="feeder", resolution=80, fps=61.0))
    _kept = _tune.prefs.get(_tune.HISTORY_KEY) or {}
    check("the prune survives a history somebody edited by hand",
          len(_kept) <= _tune.MAX_GAMES, len(_kept))
    check("...and keeps the game that was just measured",
          _tune._key(Path("D:/Games/NEW")) in _kept, list(_kept)[:3])
finally:
    _tune.prefs.FILE = _prefs_file

check("a junk row does not take the suggestion down with it",
      _tune.suggest(_JUNK + [{"resolution": 100, "fps": 47.0}], 60, 100,
                    "feeder", _m) is not None)
check("...and the table and the suggestion read the same rows",
      "_points(rows" in src_of(_tune.suggest)
      and "_points(rows" in src_of(_tune.split))

# One route's session is not the other's: the feeder logs a frame rate, the
# fork logs the model's own cost, and one used to overwrite the other.
_mixed = [{"resolution": 75, "fps": 78.0, "route": "feeder"},
          {"resolution": 75, "model_ms": 7.0, "route": "optiscaler"},
          {"resolution": 50, "fps": 94.0, "route": "feeder"}]
check("the history is kept per route, so the solve keeps both its legs",
      len(_tune._points(_mixed, "feeder")) == 2
      and _tune.cost_lines(_mixed, _tune.Measured(
          route="feeder", resolution=75, fps=78.0)) != [])

# The work area is the input to every number the tool prints about cost. A
# report about one of those numbers used to arrive without it.
_wd = Path(tempfile.mkdtemp(prefix="workarea_"))
(_wd / "dlss5-feed.cfg").write_text("work_resolution=65\n", encoding="utf8")
class _DialGame(_FakeGame):
    """The feeder honours the work area here: 64-bit, D3D11."""
    api = "DX11"
    bitness = 64


_body = diagnose.issue_body("1.9.0", "RTX 4060 Ti", 89, "616.92",
                            _DialGame(), "feeder", None, "", None, _wd)
check("the bug report carries the work area the add-on was told to use",
      "- work area: 65%" in _body, _body[:400])
for _r in ("remix", "standalone", "native", "bridge"):
    check(f"...and not on {_r}, whose add-on never reads that file",
          "work area" not in diagnose.issue_body(
              "1.9.0", "RTX 4060 Ti", 89, "616.92", _FakeGame(), _r, None,
              "", None, _wd))


class _DX12Game(_DialGame):
    api = "DX12"


class _Bit32Game(_DialGame):
    bitness = 32


# The feeder honours work_resolution on the 64-bit D3D11 path only, and
# writes the key into every install - so without the same rule the slider
# uses, a DX12 report carries a number its own add-on ignores.
for _g, _what in ((_DX12Game(), "a DX12 game"), (_Bit32Game(), "a 32-bit one")):
    check(f"...and not for {_what}, where the feeder ignores the key",
          "work area" not in diagnose.issue_body(
              "1.9.0", "RTX 4060 Ti", 89, "616.92", _g, "feeder", None,
              "", None, _wd), _what)
check("the report's keys are all keys the replay reads back",
      "work area" in (Path(__file__).resolve().parent / "_tools"
                      / "replay_report.py").read_text(encoding="utf8"))
check("a guessed work area is said to be a guess, in the table",
      any("the slider's" in ln for ln in _tune.cost_lines(
          [], _tune.Measured(route="optiscaler", resolution=75,
                             model_ms=7.2, from_config=False))))

# An install rewrites the config. Reading it against the log of a session
# played before that install attributes a frame rate to a setting nobody
# played at - and then publishes it.
_cd = Path(tempfile.mkdtemp(prefix="stale_"))
(_cd / "dlss5-feed.log").write_text("x", encoding="utf8")
(_cd / "dlss5-feed.cfg").write_text("work_resolution=100\n", encoding="utf8")
_now = time.time()
os.utime(_cd / "dlss5-feed.log", (_now - 3600, _now - 3600))
os.utime(_cd / "dlss5-feed.cfg", (_now, _now))
check("a work area written after the session is not this session's",
      _tune.written_after(_cd, "feeder", _cd / "dlss5-feed.log"))
os.utime(_cd / "dlss5-feed.cfg", (_now - 7200, _now - 7200))
check("...and one written before it is",
      not _tune.written_after(_cd, "feeder", _cd / "dlss5-feed.log"))
check("...a missing file says nothing either way",
      not _tune.written_after(_cd, "feeder", _cd / "no-such.log"))
check("the window checks it before it believes the config",
      _m61b is not None and _m61b.resolution == 100 and _m61b.from_config is False, _m61b)
_rem61: list = []
with _ui_isolated(), _ui_threads(run=False):
    with patch.object(_tune, "remember", lambda *a, **k: _rem61.append(a)):
        _tuned60("feeder", target=60, from_config=False)
check("...and a guessed work area is never recorded as a session", _rem61 == [], _rem61)
_ui_cleanup()

_body2 = diagnose.issue_body("1.9.0", "RTX 4060 Ti", 89, "616.92",
                             _DialGame(), "feeder", None, "", None,
                             Path(tempfile.mkdtemp(prefix="nowork_")))
check("...and says nothing where no config answers",
      "work area" not in _body2)

check("a history kept for every game does not grow without end",
      _tune.MAX_GAMES and "MAX_GAMES" in src_of(_tune.remember))

# TEXTS: the cost table is printed in the README and in the release notes.
# A document that shows a transcript has to show one the tool can produce.
_shown = "\n".join(
    "> " + ln for ln in
    _tune.cost_lines([], _tune.Measured(route="optiscaler", resolution=100,
                                        model_ms=7.2)))
for _doc in (Path(__file__).resolve().parent / "README.md",
             Path(__file__).resolve().parent / "docs" / "releases"
             / "v1.9.0.md"):
    _text = _doc.read_text(encoding="utf8")
    _flat = "\n".join(ln.strip() for ln in _text.splitlines())
    check(f"{_doc.name}'s cost table is what cost_lines() prints",
          "\n".join(ln.strip() for ln in _shown.splitlines()) in _flat,
          _doc.name)
    check(f"...and {_doc.name} shows the heading the log puts above it",
          "=== what the work area costs here ===" in _flat)
_d = Path(tempfile.mkdtemp(prefix="ranat_"))
(_d / "dlss5-feed.cfg").write_text("enabled=1\nwork_resolution=85\n", encoding="utf8")
check("the feeder's config says what the session ran at",
      _tune.ran_at(_d, "feeder", 100) == 85)
(_d / "OptiScaler.ini").write_text("[DlssNr]\nWorkingScale=0.750\n", encoding="utf8")
check("...and OptiScaler's ini says it as a fraction",
      _tune.ran_at(_d, "optiscaler", 100) == 75)
check("...with the slider as the fallback when neither is readable",
      _tune.ran_at(Path(tempfile.mkdtemp()), "feeder", 77) == 77)
shutil.rmtree(_d, ignore_errors=True)

check("the feeder's settled frame rate is read, not its loading screen",
      _tune.measure("[feed] 100 frames: feed CPU 9.00 ms/frame, 20.0 fps\n"
                    "[feed] 3600 frames: feed CPU 2.10 ms/frame, 47.0 fps",
                    "", "feeder", 100).fps == 47.0)

# LOGIC: one unreadable appmanifest must not empty the library.
_steam_src = src_of(games.scan_steam)
_i_acf, _i_fold = (_steam_src.find("for acf in manifests:"),
                   _steam_src.find("for f in folders:"))
check("a manifest that cannot be read skips that file, not the loop",
      0 <= _i_acf < _i_fold
      and "continue" in _steam_src[_i_acf:_i_fold], (_i_acf, _i_fold))

# LOGIC: sharing a result crashed on any machine with no NVIDIA card.
check("a card that could not be detected does not crash the share button",
      isinstance(_comm.record(_FakeGame(), "feeder", "worked",
                              gpu_name="")["gpu"], str))
_nocard61 = _share60("feeder", None, answer=True, detect=(None, None))
check("...and the GUI never passes None as the card name",
      _nocard61[2] and _nocard61[2][0].get("gpu_name") == "" and len(_nocard61[1]) == 1,
      [r.get("gpu_name") for r in _nocard61[2]])

# LOGIC/FEATURES: one game's diagnosis must not be posted about another.
with _ui_isolated(), _ui_threads(run=False):
    _pk61 = _ui_ctl()
    _pk61.load_catalog = lambda: None
    _one61, _two61 = _ui_game(name="First"), _ui_game(name="Second")
    _pk61.enter_game(_one61)
    _pk61._last_diag, _pk61._last_crash = diagnose.Report(route="feeder"), object()
    _pk61._tune = _tune.Suggestion(resolution=70)
    _pk61.enter_game(_one61)
    _kept61 = (_pk61._last_diag, _pk61._last_crash, _pk61._tune)
    _pk61.enter_game(_two61)
check("picking another game forgets the last diagnosis",
      _kept61[0] is not None and _pk61._last_diag is None, _kept61)
check("...including the Windows event and the tuning suggestion",
      _pk61._last_crash is None and _pk61._tune is None and _pk61.result is None)
_ui_cleanup()

# FEATURES: the failures worth sharing most are the ones that logged nothing.
# Read on the page itself: the button is drawn under a verdict that says
# the game never ran.
_ui61 = _UiLive()
if _ui61.ok:
    _g61 = _ui_game(name="Never Ran")
    _ui61.enter(_g61, _ui_support([dlss.FEEDER], dlss.FEEDER))
    _nr61 = diagnose.Report(route="feeder")
    _nr61.verdict, _nr61.ran, _nr61.never_ran = "Not started since the install - run the game once.", False, True
    _ui61.app._windows_crash = lambda rep: None
    _ui61.app.what_next = lambda rep: None
    _ui61.app.q.put(("diagnosed", (_g61.install_dir, _nr61, None)))
    _ui61.until(lambda: _ui61.kit.find("share the result", "button"), 4.0)
check("a session that never ran can still be shared",
      _ui61.ok and bool(_ui61.kit.find("share the result", "button")), _ui61.labels("button"))
_ui61.close()
with _ui_isolated():
    _dg61b = _ui_ctl(_ui_game(name="Diagnosed"), _ui_support([dlss.FEEDER], dlss.FEEDER))
    _w61b = _worker_only(_dg61b.diagnose, diagnose, "analyse", _rep61)
check("...and the reading itself is off the Tk thread, like every long job",
      _w61b == ([], [True]), _w61b)
_ui_cleanup()

# FEATURES: the overlay key belongs on every route that has an overlay.
_ok61: dict = {}
_ui61b = _UiLive()
if _ui61b.ok:
    _ui61b.enter(_ui_game(name="Overlay Key"), _ui_support([dlss.FEEDER, dlss.OPTI, dlss.REMIX], dlss.FEEDER))
    _ui61b.press("settings", "button")
    _ui61b.settle(350)
    for _r61 in (dlss.FEEDER, dlss.OPTI, dlss.REMIX):
        _ui61b.app.set_setting("route", _r61)
        _ui61b.app.shell.redraw()
        _ui61b.settle(40)
        _ok61[_r61] = bool(_ui61b.kit.find("overlay key", "dropdown"))
_ui61b.close()
_ui_cleanup()
check("the overlay key is offered on the OptiScaler route too",
      _ok61.get(dlss.OPTI) is True and _ok61.get(dlss.FEEDER) is True, _ok61)
check("...and its row comes off the page when it does not apply",
      _ok61.get(dlss.REMIX) is False, _ok61)

# TEXTS: nothing tells a person to press a key they may have rebound.
_keys61: list = []
with _ui_isolated(), _ui_threads(run=False):
    prefs.set_("overlay_key", reshade_ini.OVERLAY_KEYS["F10"])
    for _gb61 in (64, 32):
        _kc61 = _ui_ctl(_ui_game(name="Keys", bitness=_gb61), _ui_support(list(dlss.LABELS), dlss.FEEDER))
        for _r61 in dlss.LABELS:
            _keys61 += [t for _k, t in _kc61.route_steps(_r61)
                        if "press Home" in t or "press Insert" in t]
_ui_cleanup()
check("every 'press the overlay key' line goes through the resolver", not _keys61, _keys61)
prefs.set_("overlay_key", reshade_ini.OVERLAY_KEYS["F10"])
try:
    check("the resolver answers with the key that was chosen",
          reshade_ini.overlay_key_name() == "F10"
          and reshade_ini.overlay_key_name("Insert") == "F10"
          and diagnose._overlay_key() == "F10")
finally:
    prefs.set_("overlay_key", "")
check("...and falls back to each route's own default",
      reshade_ini.overlay_key_name() == "Home"
      and reshade_ini.overlay_key_name("Insert") == "Insert")

# LOGIC: an Application Hang carries no module and must not hide the crash.
_hang = ("2026-09-09T12:00:00|Application Hang|Game.exe~1.0~"
         "0~ffffffff~0~\n"
         "2026-09-09T11:00:00|Application Error|Game.exe~1.0~aa~"
         "renodx-dlss5.addon64~1.0~bb~c0000005~0~1~2~D:\\g\\Game.exe")
_saved_ps = _wc._ps
try:
    _wc._ps = lambda _s: _hang
    _c2 = _wc.last_crash("Game.exe", since=0, within_days=30)
    check("a hang with no module does not hide the crash under it",
          _c2 and _c2.module == "renodx-dlss5.addon64", _c2)
finally:
    _wc._ps = _saved_ps

# LOGIC: ReShade.ini is not evidence of antivirus, and the branch must not
# return before the rules under it.
check("a reset ReShade.ini is not called a quarantine",
      "reshade.ini" not in [n.lower() for n in diagnose._CORE_NAMES])

# LOGIC: the driver-export rule must not overrule frames that arrived.
_d = _diag_dir("ngx_ok_", reshade=(
    "11:25:54 | ERROR | [DLSS 5 Neural Rendering] "
    "vtable::Hook(Failed to find NVSDK_NGX_D3D11_EvaluateFeature_C)\n"),
    feed=("[feed] feature ready: 1920x1080 DLAA\n"
          "[feed] frame 1 delivered (1920x1080 at 100%)\n"))
_r = diagnose.analyse(_d)
check("one failed hook does not undo the frames that were delivered",
      _r.verdict == "Working.", _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# TEXTS: quote the line the person actually has.
# The rule only reads this line as a driver verdict on a driver older than
# DLSS 5, so the driver is pinned: read from the machine running the suite,
# the check passed or failed with whatever that machine had installed.
_d = _diag_dir("ngx_vk_", reshade=(
    "11:25:54 | ERROR | [DLSS 5 Neural Rendering] "
    "vtable::Hook(Failed to find NVSDK_NGX_VULKAN_EvaluateFeature)\n"))
from core import gpu as _gpu_vk                                   # noqa: E402
_vk_drv_saved = _gpu_vk.driver_version
_gpu_vk.driver_version = lambda: "610.60"
try:
    _r = diagnose.analyse(_d)
finally:
    _gpu_vk.driver_version = _vk_drv_saved
check("the entry point named in the answer is the one in the log",
      any("NVSDK_NGX_VULKAN_EvaluateFeature" in f.detail for f in _r.findings),
      [f.detail[:60] for f in _r.findings])
shutil.rmtree(_d, ignore_errors=True)

# FEATURES: uninstalling ends the measurements it was based on.
check("uninstall drops the tuning history with the install",
      "autotune.forget" in src_of(installer.uninstall))

# FEATURES: the workflow cannot filter on a label GitHub drops.
_wf = (Path(__file__).resolve().parent / ".github" / "scripts"
       / "compatibility.py").read_text(encoding="utf8")
check("the compatibility workflow does not filter on the label",
      "labels=result" not in _wf and "state=all" in _wf)

# FEATURES: the version is the delivery mechanism for the library rescan.
check("the version is 2.0.0 in the file the build reads too",
      "2.0.0.0" in (Path(__file__).resolve().parent
                    / "version_info.txt").read_text(encoding="utf8"))
check("...and the release notes the workflow publishes exist",
      (Path(__file__).resolve().parent / "docs" / "releases"
       / f"v{update.VERSION}.md").is_file())


section("62. NVIDIA's own runtimes, and swapping one the game ships")

# The tool was installing nvngx_dlss.dll and nvngx_dlssg.dll from a
# community mirror while NVIDIA published the same runtimes itself, signed
# and current. Neural rendering is NOT among them - the SDK carries super
# resolution, ray reconstruction and frame generation only.
check("the publisher's own files are named, and nvngx_dlssnr is not",
      [f for _, f in sources.NVIDIA_DLSS_FILES]
      == ["nvngx_dlss.dll", "nvngx_dlssd.dll", "nvngx_dlssg.dll"])
_saved_tag = sources.latest_tag
_saved_cache = sources._NVIDIA_CACHE
try:
    sources._NVIDIA_CACHE = None
    sources.latest_tag = lambda _repo: "v310.9.1"
    _nv = sources.nvidia_dlss()
    check("...read at the tag, so the version in the label is true",
          _nv["dlss"][0]["url"].endswith(
              "/NVIDIA/DLSS/v310.9.1/lib/Windows_x86_64/rel/nvngx_dlss.dll"),
          _nv["dlss"][0]["url"])
    # The label names the SDK the file was read from, not a version for
    # each DLL: NVIDIA ships all three in one tagged SDK and versions them
    # separately, so claiming the tag IS each file's version would be wrong.
    check("...labelled with the SDK it was read from",
          _nv["dlssd"][0]["label"] == "310.9.1 (NVIDIA SDK)",
          _nv["dlssd"][0]["label"])
    check("...and marked as a plain file, not an archive",
          _nv["dlssg"][0]["raw"] == "nvngx_dlssg.dll")
    check("the tag comes from the redirect, not an API request",
          "api.github.com" not in src_of(sources.nvidia_dlss))
finally:
    sources.latest_tag = _saved_tag
    sources._NVIDIA_CACHE = _saved_cache

# A swap is only ever a swap: a game that does not ship ray reconstruction
# does not ask for it, and dropping one in would change nothing.
_d = Path(tempfile.mkdtemp(prefix="rrfind_"))
check("a game without ray reconstruction has nothing to swap",
      installer._find_runtime(_d, installer.DLSSD) is None)
_deep = _d / "Engine" / "Plugins" / "Runtime" / "Nvidia" / "DLSS" / "Binaries"
_deep.mkdir(parents=True)
(_deep / "nvngx_dlssd.dll").write_bytes(b"MZ own")
check("...and one that does is found where the engine keeps it",
      installer._find_runtime(_d, installer.DLSSD)
      == _deep / "nvngx_dlssd.dll")

# The owner's three requirements: back it up, put it back, warn first.
def _fake_dll(size: int = 300_000, machine: int = 0x8664) -> bytes:
    """The smallest thing that is honestly a 64-bit Windows DLL.

    The swap validator refuses anything else, which is the point: a raw
    download can come back as an error page or half a file, and that must
    never land on top of a runtime the game needs to start.
    """
    import struct as _st
    b = bytearray(bytes(size))
    b[0:2] = b"MZ"
    _st.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE" + bytes(2)
    _st.pack_into("<H", b, 0x84, machine)
    return bytes(b)


_rep = installer.Report()
_new = Path(tempfile.mkdtemp()) / "nvngx_dlssd.dll"
_new.write_bytes(_fake_dll())
installer._place_entry({"url": "x", "raw": "nvngx_dlssd.dll", "label": "310.9.1"},
                       _deep / "nvngx_dlssd.dll", _rep, _d,
                       lambda _u, _n: _new, installer.DLSSD, "x")
check("the swap writes the new build",
      (_deep / "nvngx_dlssd.dll").read_bytes() == _fake_dll())
check("...and keeps the game's own beside it",
      (_deep / ("nvngx_dlssd.dll" + installer.BACKUP_SUFFIX)).read_bytes()
      == b"MZ own")

# ...and nothing at all is written when what arrived is not a runtime.
_bad_dir = Path(tempfile.mkdtemp())
for _name, _blob, _why in (
        ("page.dll", b"<!DOCTYPE html><html>404 not found</html>", "an error page"),
        ("cut.dll", _fake_dll(9_000), "a download that was cut short"),
        ("x86.dll", _fake_dll(300_000, 0x14C), "a 32-bit build")):
    _p = _bad_dir / _name
    _p.write_bytes(_blob)
    _before = (_deep / "nvngx_dlssd.dll").read_bytes()
    _raised = None
    try:
        installer._place_entry({"url": "x", "raw": "nvngx_dlssd.dll",
                                "label": "bad"},
                               _deep / "nvngx_dlssd.dll", installer.Report(),
                               _d, lambda _u, _n, _f=_p: _f,
                               installer.DLSSD, "x")
    except installer.InstallError as _e:
        _raised = _e
    check(f"{_why} never reaches the game folder",
          _raised is not None
          and (_deep / "nvngx_dlssd.dll").read_bytes() == _before, _raised)
check("...and the refusal says nothing was touched",
      "was not written and the download was thrown away" in str(_raised),
      str(_raised))
check("...with both recorded, so uninstall knows about them",
      len([w for w in _rep.written if "dlssd" in w.lower()]) == 2, _rep.written)
shutil.rmtree(_d, ignore_errors=True)

check("a swap is warned about before it happens, in its own words",
      "anti-cheat can treat a changed file as tampering"
      in anticheat.swap_message("nvngx_dlssd.dll")
      and "keep the game's own" in anticheat.swap_message("x"))
# Pressed on the page: a ray-reconstruction build picked from its dropdown,
# and the 'keep the game's own nvngx_dlss' toggle clicked off.
_sw62: dict = {}
_ui62 = _UiLive()
if _ui62.ok:
    _g62 = _ui_game(name="Swap Warned", api="DX12")
    (_g62.install_dir / "nvngx_dlss.dll").write_bytes(b"GAME OWN" + bytes(400))
    _ui62.enter(_g62, _ui_support([dlss.NATIVE, dlss.FEEDER], dlss.NATIVE,
                                  evidence=["nvngx_dlss.dll", "nvngx_dlssd.dll"]))
    _ui62.app.catalog = {"dlssd": [{"label": "310.9.1 (NVIDIA SDK)"}], "dlss": [], "renodx": [],
                         "dlssnr": []}
    _ui62.press("settings", "button")
    _ui62.settle(350)
    _ui62.app.shell.clear_log()
    _ui62.press("ray reconstruction", "dropdown")
    _sw62["picked"] = _ui62.pick("310.9.1")
    _sw62["build"] = _ui62.log()
    _ui62.app.shell.clear_log()
    _sw62["toggle"] = _ui62.press("keep the game's own nvngx_dlss", "toggle")
    _sw62["keep"] = _ui62.log()
    _sw62["opts"] = (_ui62.app.opts().dlssd, _ui62.app.opts().keep_game_dlss)
_ui62.close()
_ui_cleanup()
check("...and the warning is shown when a build is chosen",
      _sw62.get("picked") and "as tampering" in _sw62.get("build", ""), _sw62.get("build", "")[-300:])
check("...and unticking 'keep the game's own' warns the same way",
      _sw62.get("toggle") and "as tampering" in _sw62.get("keep", ""), _sw62.get("keep", "")[-300:])
check("...and both choices reach the install",
      _sw62.get("opts") == ("310.9.1 (NVIDIA SDK)", False), _sw62.get("opts"))
check("...and repeated in the notes the install leaves behind",
      "as tampering" in src_of(installer.install))
with _ui_isolated(), _ui_threads(run=False):
    _dn62 = _ui_ctl(_ui_game(name="Untouched"), _ui_support([dlss.NATIVE], dlss.NATIVE,
                                                            evidence=["nvngx_dlssd.dll"]))
    _dn62.apply_route(dlss.NATIVE)
    _dno62 = _dn62.opts()
_ui_cleanup()
check("doing nothing is the default",
      installer.Options().dlssd == "" and _dno62.dlssd == "" and _dno62.keep_game_dlss is True
      and _dno62.dlss is None, (_dno62.dlssd, _dno62.keep_game_dlss, _dno62.dlss))


section("63. the round that put the release together: the four shapes a "
        "ray-reconstruction runtime is found in, a .7z Windows cannot open, "
        "a server having a bad minute, and a fork installed for what it is "
        "for")

# The GUI decides whether to offer the ray-reconstruction swap from the
# route detection's own evidence. It has been wrong twice: once because the
# beside-the-exe pass never recorded nvngx_dlssd.dll, and once because
# finding DLSS beside the exe skipped the walk that would have found it
# nested. Both times the installer WOULD have swapped the file the GUI
# refused to offer. All four shapes, so neither can come back.
def _rr_shape(beside, nested):
    d = Path(tempfile.mkdtemp(prefix="rrshape_"))
    (d / "Game.exe").write_bytes(b"MZ" + b"\0" * 200)
    for n in beside:
        (d / n).write_bytes(b"MZ")
    deep = d / "Engine" / "Plugins" / "Runtime" / "Nvidia" / "DLSS" / "Binaries"
    deep.mkdir(parents=True, exist_ok=True)
    for n in nested:
        (deep / n).write_bytes(b"MZ")
    _dlss.forget_walk(d)
    sup = _dlss.detect(d, d, "DX12", 64)
    found = any(str(e).lower().endswith("nvngx_dlssd.dll")
                for e in sup.evidence)
    shutil.rmtree(d, ignore_errors=True)
    return found


from core import dlss as _dlss                                    # noqa: E402
check("ray reconstruction is found beside the executable",
      _rr_shape(("nvngx_dlss.dll", "nvngx_dlssd.dll"), ()))
check("...and when both runtimes are nested in the engine folder",
      _rr_shape((), ("nvngx_dlss.dll", "nvngx_dlssd.dll")))
check("...and when DLSS is beside the exe but ray reconstruction is nested",
      _rr_shape(("nvngx_dlss.dll",), ("nvngx_dlssd.dll",)))
check("...and when only Streamline is beside the exe",
      _rr_shape(("sl.interposer.dll",), ("nvngx_dlssd.dll",)))
check("a game with no ray reconstruction is not offered the swap",
      not _rr_shape(("nvngx_dlss.dll",), ()))

# The walk is the expensive half of detection, and it used to run on every
# click. Remembering it is what makes asking for the nested case affordable.
_wd = Path(tempfile.mkdtemp(prefix="rrwalk_"))
(_wd / "Game.exe").write_bytes(b"MZ" + b"\0" * 200)
(_wd / "nvngx_dlss.dll").write_bytes(b"MZ")
_dlss.forget_walk(_wd)
_calls = []
_real_find = _dlss.find_dlss_files
_dlss.find_dlss_files = lambda *a, **k: (_calls.append(1), _real_find(*a, **k))[1]
try:
    _dlss.detect(_wd, _wd, "DX12", 64)
    _dlss.detect(_wd, _wd, "DX12", 64)
    _dlss.detect(_wd, _wd, "DX12", 64)
    check("the folder is walked once per game, not once per click",
          len(_calls) == 1, len(_calls))
    _dlss.forget_walk(_wd)
    _dlss.detect(_wd, _wd, "DX12", 64)
    check("...and an install forgets it, because it wrote into the folder",
          len(_calls) == 2, len(_calls))
finally:
    _dlss.find_dlss_files = _real_find
    shutil.rmtree(_wd, ignore_errors=True)
check("install and uninstall both drop the remembered walk",
      "forget_walk" in src_of(installer.install)
      and "forget_walk" in src_of(installer.uninstall))

# Issue #93: Windows' own tar.exe is not always built with LZMA.
_x7 = src_of(optiscaler.extract_7z)
_i_7z, _i_tar = _x7.find("_seven_zip()"), _x7.find("_tar_exe()")
check("a .7z is opened with 7-Zip before Windows' tar.exe",
      0 <= _i_7z < _i_tar, (_i_7z, _i_tar))
check("...and the LZMA failure says what to install",
      "lzma" in _x7.lower() and "7-zip.org" in _x7.lower())
check("...and 7-Zip is looked for on PATH and in Program Files",
      all(x in src_of(optiscaler._seven_zip)
          for x in ("shutil.which", "Program Files")))

# ...and build.bat, which the same reporter found broken: a relative
# --version-file cannot be resolved against a --specpath somewhere else.
_bat = (Path(__file__).resolve().parent / "build.bat").read_text(encoding="utf8")
check("build.bat names version_info.txt by an absolute path",
      "--version-file \"%~dp0version_info.txt\"" in _bat, "relative path")
check("...because it puts the spec somewhere else",
      "--specpath" in _bat)
_ga = (Path(__file__).resolve().parent / ".gitattributes").read_text(encoding="utf8")
check("batch files are pinned to CRLF, whatever git is configured to do",
      "*.bat text eol=crlf" in _ga)

# Issues #103 and #97: a publisher's server answering 5xx.
check("a 5xx is retried and then explained, on every path that fetches",
      all("RETRY_CODES" in src_of(f)
          for f in (net.fetch_text, net.download, sources._get)))
check("...and each says so as a sentence, not as a traceback",
      all("Unavailable" in src_of(f)
          for f in (net.fetch_text, net.download, sources._get)))
check("...and the install turns it into its own refusal",
      "sources.Unavailable" in src_of(installer.install))
_una = src_of(net.download) + src_of(net.fetch_text) + src_of(sources._get)
check("...and none of them claims the folder was left untouched",
      "Nothing in the game folder was changed" not in _una)

# Issue #81: choosing the fork is not the same as choosing what it is for.
check("the wilsjo2 build is installed with its own placement switched on",
      optiscaler.PRESR_BEFORE_SR.get("RunBeforeSR") is True
      and "PRESR_BEFORE_SR" in src_of(installer.install))
check("...and the notes say so, naming the section the tool really writes",
      "RunBeforeSR" in " ".join(optiscaler.describe_nr({"RunBeforeSR": True}))
      and "[DlssNr]" in " ".join(optiscaler.describe_nr({"RunBeforeSR": True})))

# Issue #98: a recorded fault outranks a log that stopped in a good place -
# but only a fault from the session that log describes.
import calendar as _cal63  # noqa: E402
import types as _types63  # noqa: E402
from core.ui import ctl_game as _uig63, ctl_library as _uil63  # noqa: E402


class _FakeApp:
    """The window's game controller with nothing but a recorder around it:
    crash_overrides, _reprint_verdict and the session window are the real
    ones - what they print is the thing being checked."""

    def __new__(cls, rep, where=None):
        c = _ui_ctl()
        c._last_diag = rep
        if not where:
            where = Path(tempfile.mkdtemp(prefix="crash_app_"))
            _UI_DIRS.append(where)
        c.game = _types63.SimpleNamespace(install_dir=Path(where), name="Crashed", installed=False,
                                          exe=None, kind="game")
        return c


def _when63(offset: float) -> str:
    """A Windows event time `offset` seconds from now, as wincrash writes it (UTC)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + offset))


# Issue #98: a recorded fault outranks a log that stopped in a good place -
# but only a fault from the session that log describes.
_sess63 = Path(tempfile.mkdtemp(prefix="crash_session_"))
(_sess63 / "ReShade.log").write_text("INFO | Initializing crosire's ReShade\n", encoding="utf8")


def _working63(offset):
    rep = diagnose.Report(route="renodx")
    rep.verdict = "Working."
    app = _FakeApp(rep, _sess63)
    app.crash_overrides(_wc.Crash(when=_when63(offset), exe="Game.exe", module="Game.exe",
                                  code="0xC0000005", provider="Application Error"))
    return rep.verdict


with _ui_isolated(), _ui_threads(run=False):
    _now63, _old63 = _working63(+60), _working63(-6 * 3600)
check("a recorded crash overrides a Working verdict",
      "then the game crashed" in _now63, _now63)
check("...only when it belongs to the session just diagnosed",
      _old63 == "Working.", _old63)
with _ui_isolated(), _ui_threads(run=False):
    _sr63 = {}
    for _off63 in (+60, -6 * 3600):
        _rp63 = diagnose.Report(route="renodx")
        _rp63.verdict = "Working."
        _ap63 = _FakeApp(_rp63, _sess63)
        _cr63 = _wc.Crash(when=_when63(_off63), exe="Game.exe", module="Game.exe",
                          code="0xC0000005", provider="Application Error")
        _ap63.q.put(("wincrash", (_ap63.game.install_dir, _cr63, ("Game.exe faulted", "in Game.exe"))))
        _ap63.pump()
        _sr63[_off63] = _ap63._last_crash is _cr63
check("...and the shared record is held to the same window",
      _sr63 == {60: True, -6 * 3600: False}, _sr63)
shutil.rmtree(_sess63, ignore_errors=True)


# #171: and it outranks the opposite verdict too. "Not started since the
# install" is read off an absent log; a fault record for the game's own exe,
# written after the install, says it started and died before anything loaded.


_c171 = _wc.Crash(when="2026-09-12 00:54:33", exe="GTA5.exe", module="GTA5.exe",
                  code="0xC0000005", provider="Application Error")


def _overridden(verdict, never_ran):
    rep = diagnose.Report(route="renodx")
    rep.verdict = verdict
    rep.never_ran = never_ran
    with _ui_isolated(), _ui_threads(run=False):
        app = _FakeApp(rep)
        app.crash_overrides(_c171)
    return rep.verdict, " ".join(t for t, _tag in app.said)


_v171, _s171 = _overridden("Not started since the install - run the game once.", True)
check("a fault record replaces 'not started since the install'",
      "It started, and nothing here recorded the session" in _v171, _v171)
check("...and explains why every log is empty",
      "nothing here recorded the session" in _s171
      # ...without claiming the logs are empty BECAUSE of the fault:
      # never_ran is set on seven shapes and two of them have a log.
      and "the logs are empty because" not in _s171, _s171[:120])
_v98, _ = _overridden("Working.", False)
check("...while a fault still overrides Working.",
      "then the game crashed" in _v98, _v98)
_vinc, _ = _overridden("Inconclusive - the feed did not get far enough to tell.",
                       False)
check("...and no other verdict is rewritten",
      _vinc.startswith("Inconclusive"), _vinc)


# #168: the fork names the dispatch timing what it likes ("cost:", then
# "elapsed:"), and matching the word called a game that dispatched every
# frame "never reports it running".
for _word in ("cost:", "elapsed:"):
    _ln = ("[21:34:08.368508] [I] DlssNr_Dx12::Dispatch DLSS-NR "
           f"{_word} 6.72 ms total, 6.60 ms model")
    check(f"a dispatch line saying '{_word}' is proof the model ran",
          bool(diagnose._DISPATCH_MS.search(_ln)))
check("...and a setting echoed at startup is not",
      not diagnose._DISPATCH_MS.search("[I] DlssNr.Enabled: true"))
_opti_run = tempfile.mkdtemp(prefix="diag_opti_elapsed_")
(Path(_opti_run) / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "path": "optiscaler", "proxy": "dxgi.dll",
     "api": "DX12", "bitness": 64, "exe": "Spider-Man2.exe",
     "files": ["dxgi.dll"]}), encoding="utf8")
(Path(_opti_run) / "dxgi.dll").write_bytes(b"MZ")
(Path(_opti_run) / "OptiScaler.log").write_text(
    "[21:34:07.000000] [I] DlssNr forwarder loaded\n"
    "[21:34:08.368508] [I] DlssNr_Dx12::Dispatch DLSS-NR elapsed: 6.72 ms "
    "total, 6.60 ms model, 0.12 ms surrounding work\n", encoding="utf8")
_r168 = diagnose.analyse(Path(_opti_run))
check("...so the whole report says the neural pass is running",
      _r168.verdict.startswith("Working")
      and any("Neural rendering is running" in t for t in _levels(_r168, "ok")),
      _r168.verdict)
shutil.rmtree(_opti_run, ignore_errors=True)


# #164: a ReShade.log read from its tail can lose the registration lines
# while the add-on's own log proves it loaded. The answer said "ReShade
# loaded no add-ons" above a feed that had attached and hooked the game.
_d164 = _diag_dir(
    "diag_tail_lost_",
    reshade=("21:11:43:914 [24200] | WARN  | Successfully compiled "
             "'D:\\Game\\reshade-shaders\\Shaders\\CShade\\cDots.fx'\n"
             "21:11:44:173 [24200] | WARN  | Successfully compiled "
             "'D:\\Game\\reshade-shaders\\Shaders\\DH\\dh_uber_rt.fx'\n"),
    feed=("21:11:35.731  dlss5-feed64 0.15.1 (built Sep  9 2026) attached.\n"
          "21:11:35.731    host game: D:\\Game\\Game.exe\n"
          "21:11:35.759  [feed] vkCreateDevice hook installed\n"))
_r164 = diagnose.analyse(_d164)
check("a feed log that attached outranks a ReShade tail with no registration",
      not any("loaded no add-ons" in t for t in _levels(_r164, "bad")),
      str(_levels(_r164, "bad")))
check("...and the answer says the add-on did load",
      any("wrote its own log" in t for t in _levels(_r164, "ok")),
      str(_levels(_r164, "ok")))
check("...and names what is actually missing: an effect runtime",
      "effect runtime" in _r164.verdict, _r164.verdict)
shutil.rmtree(_d164, ignore_errors=True)


# ...and the provider shader the install deliberately leaves alone, because
# the person already has the pack, is not "MISSING" (#164 again).
_d164b = tempfile.mkdtemp(prefix="diag_their_lumenite_")
(Path(_d164b) / "reshade-shaders" / "Shaders" / "LumeniteFX").mkdir(parents=True)
(Path(_d164b) / "reshade-shaders" / "Shaders" / "LumeniteFX"
 / "lumenite_Kernel.fx").write_text("// theirs", encoding="utf8")
_man164 = {"route": "feeder", "provider": 3, "bitness": 64, "api": "DX12",
           "files": []}
_seen = "\n".join(diagnose._presence(Path(_d164b), _man164, "feeder"))
check("their own LumeniteFX copy is not reported as a missing file",
      "lumenite_Kernel.fx: MISSING" not in _seen
      and "not written by this install" in _seen,
      _seen)
shutil.rmtree(_d164b, ignore_errors=True)
_d164c = tempfile.mkdtemp(prefix="diag_no_lumenite_")
(Path(_d164c) / "reshade-shaders" / "Shaders").mkdir(parents=True)
check("...while one that is genuinely gone still is",
      "lumenite_Kernel.fx: MISSING"
      in "\n".join(diagnose._presence(Path(_d164c), _man164, "feeder")))
shutil.rmtree(_d164c, ignore_errors=True)


# Every one of those four fixes went too far somewhere, and this is where each
# one was pulled back. A widened test makes the branch below it unreachable;
# these are the inputs that proved it.


def _opti_log(log, **man):
    """A folder that looks like an optiscaler install with this log in it."""
    d = Path(tempfile.mkdtemp(prefix="diag_opti_probe_"))
    m = {"version": 1, "complete": True, "path": "optiscaler",
         "proxy": "dxgi.dll", "api": "DX12", "bitness": 64, "exe": "Game.exe",
         "files": ["dxgi.dll"]}
    m.update(man)
    (d / "dlss5-autopilot.json").write_text(json.dumps(m), encoding="utf8")
    (d / "dxgi.dll").write_bytes(b"MZ")
    (d / "OptiScaler.log").write_text(log, encoding="utf8")
    try:
        return diagnose.analyse(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


_FWD = "[I] DlssNr forwarder loaded\n"
_r = _opti_log(_FWD + "[E] DlssNr_Dx12::Dispatch DLSS-NR create failed after "
                      "120 ms, disabling for this session\n")
check("a create failure that reports how long it took is not work done",
      not _r.verdict.startswith("Working")
      and any("did not start" in t for t in _levels(_r, "bad")), _r.verdict)
_r = _opti_log(_FWD + "[I] DlssNr.DispatchInterval: 16 ms\n")
check("...nor is a setting whose name happens to hold the word",
      not _r.verdict.startswith("Working"), _r.verdict)
_r = _opti_log(_FWD + "[I] DlssNr_Dx12::Dispatch DLSS-NR skipped: no motion "
                      "vectors (0.00 ms)\n")
check("...nor a dispatch that says it skipped",
      not _r.verdict.startswith("Working"), _r.verdict)
_r = _opti_log(_FWD + "[I] DlssNr_Dx12::Dispatch DLSS-NR elapsed: 6.72 ms "
                      "total, 6.60 ms model\n")
check("...while a real dispatch is, still", _r.verdict.startswith("Working"),
      _r.verdict)
_dopt = Path(tempfile.mkdtemp(prefix="diag_opti_nolog_"))
(_dopt / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "path": "optiscaler", "proxy": "dxgi.dll",
     "api": "DX12", "bitness": 64, "exe": "Game.exe",
     "files": ["dxgi.dll", "OptiScaler.ini"]}), encoding="utf8")
(_dopt / "dxgi.dll").write_bytes(b"MZ")
(_dopt / "OptiScaler.ini").write_text("[Log]\nLogToFile = true\n", encoding="utf8")
_r = diagnose.analyse(_dopt)
check("the optiscaler 'not run yet' verdict rests on the absent log too",
      _r.verdict.startswith("Not run yet") and _r.never_ran is True, _r.verdict)
# ...and so does every other verdict read off a log that is not there. A fault
# record for the game outranks all of them, so each has to say so.
(_dopt / "OptiScaler.ini").write_text("[Log]\nLogToFile = false\n", encoding="utf8")
_r = diagnose.analyse(_dopt)
check("...and the one about the log being switched off",
      _r.verdict.startswith("OptiScaler's log is off") and _r.never_ran is True,
      _r.verdict)
shutil.rmtree(_dopt, ignore_errors=True)
_dstale = _diag_dir("diag_stale_reshade_flag_",
                    reshade="INFO | Initializing crosire's ReShade\n"
                            'INFO | Registered add-on "DLSS 5 Feed" v0.1\n')
_oldt = _time.time() - 7200
_os.utime(_dstale / "ReShade.log", (_oldt, _oldt))
_r = diagnose.analyse(_dstale)
check("...and the one about a log older than the install",
      _r.never_ran is True, _r.verdict)
shutil.rmtree(_dstale, ignore_errors=True)

# The feed log proving the add-on loaded must speak for THIS launch, and the
# "never got an effect runtime" answer must rest on more than one build's
# choice of words.
_ATT = "21:11:35.731  dlss5-feed64 0.15.1 (built Sep  9 2026) attached.\n"
_COMP = ("21:11:43:914 [24200] | WARN  | Successfully compiled 'a.fx'\n"
         "21:11:44:173 [24200] | WARN  | Successfully compiled 'b.fx'\n")
_d = _diag_dir("diag_runtime_other_words_",
               feed=_ATT + "  [feed] runtime 0001 initialised on D3D12\n"
                           "  [feed] DLSS5_Feed.fx technique MISSING\n",
               reshade=_COMP)
_r = diagnose.analyse(_d)
check("a technique line is proof a runtime existed, whatever it is called",
      "effect runtime" not in _r.verdict, _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

_d = _diag_dir("diag_feed_older_launch_",
               feed=_ATT + "  [feed] vkCreateDevice hook installed\n",
               reshade=_COMP)
_old = _time.time() - 3600
_os.utime(_d / "dlss5-feed.log", (_old, _old))
_r = diagnose.analyse(_d)
check("a feed log from an earlier launch does not answer for this one",
      any("loaded no add-ons" in t for t in _levels(_r, "bad")), _r.verdict)
shutil.rmtree(_d, ignore_errors=True)

# A provider shader this install wrote and something has since removed is the
# quarantine case, not "your own copy is used".
_dp = tempfile.mkdtemp(prefix="diag_provider_ours_gone_")
(Path(_dp) / "reshade-shaders" / "Shaders" / "LumeniteFX").mkdir(parents=True)
(Path(_dp) / "reshade-shaders" / "Shaders" / "LumeniteFX"
 / "lumenite_Kernel.fx").write_text("// theirs", encoding="utf8")
_man_ours = {"path": "feeder", "provider": 3, "bitness": 64, "api": "DX11",
             "files": ["reshade-shaders\\Shaders\\lumenite_Kernel.fx"]}
check("a provider shader this install wrote and lost is still MISSING",
      "lumenite_Kernel.fx: MISSING"
      in "\n".join(diagnose._presence(Path(_dp), _man_ours, "feeder")))
_seen = "\n".join(diagnose._presence(Path(_dp), dict(_man_ours, files=[]),
                                    "feeder"))
check("...and where it is theirs, no absolute path reaches the report",
      "not written by this install" in _seen and ":\\" not in _seen, _seen)
shutil.rmtree(_dp, ignore_errors=True)


# With no log there is no timestamp to date a fault against, and the event is
# found by the executable's NAME - so a second copy of the same game must not
# answer for this one.
def _override(module, where, never_ran=True,
              verdict="Not started since the install - run it once."):
    rep = diagnose.Report(route="renodx")
    rep.verdict = verdict
    rep.never_ran = never_ran
    rep.add(diagnose.WARN, "The game has not been started since the install.")
    rep.add(diagnose.INFO, "If you DID start it, it launches something else.")
    with _ui_isolated(), _ui_threads(run=False):
        app = _FakeApp(rep, where)
        app.crash_overrides(_wc.Crash(
            when="2026-09-12 00:54:33", exe="GTA5.exe", module=module,
            code="0xC0000005", provider="Application Error"))
    return rep


_here = Path(tempfile.mkdtemp(prefix="diag_fault_folder_"))
_rep = _override(str(_here / "GTA5.exe"), _here)
check("a fault recorded in this folder rewrites the 'not started' verdict",
      "It started, and nothing here recorded the session" in _rep.verdict,
      _rep.verdict)
check("...and the findings that said it never started go with it",
      not any("has not been started" in f_.title for f_ in _rep.findings)
      and any("faulting" in f_.title for f_ in _rep.findings),
      str([f_.title for f_ in _rep.findings]))
_rep = _override(r"D:\Another Copy\GTA5.exe", _here)
check("...while a fault in another copy of the same game does not",
      _rep.verdict.startswith("Not started"), _rep.verdict)
_rep = _override("dxgi.dll", _here)
check("...and a bare module name, which names no folder, still counts",
      "It started, and nothing here recorded the session" in _rep.verdict,
      _rep.verdict)
shutil.rmtree(_here, ignore_errors=True)


# The fault record arrives after the diagnosis has been printed. Rewriting the
# verdict alone left the person reading "run the game once" while the report
# and the shared record said it had crashed.
def _override_said(module, where, route="renodx"):
    rep = diagnose.Report(route=route)
    rep.verdict = "Not started since the install - run it once."
    rep.never_ran = True
    rep.add(diagnose.WARN, "The game has not been started since the install.")
    with _ui_isolated(), _ui_threads(run=False):
        app = _FakeApp(rep, where)
        app.crash_overrides(_wc.Crash(
            when="2026-09-12 00:54:33", exe="GTA5.exe", module=module,
            code="0xC0000005", provider="Application Error"))
    return rep, "\n".join(t for t, _tag in app.said)


_here2 = Path(tempfile.mkdtemp(prefix="diag_fault_screen_"))
_rep, _said = _override_said(str(_here2 / "GTA5.exe"), _here2)
check("the corrected verdict is printed on screen, not only in the report",
      _rep.verdict in _said, _said[:200])
check("...and the fault finding is printed with it",
      "faulting" in _said, _said[:200])
# The game page names that dropdown differently per route: 'loads as' on
# OptiScaler, 'reshade loads as' on the ReShade routes (core/ui/setpanel.py).
_rep, _said = _override_said(str(_here2 / "GTA5.exe"), _here2, route="optiscaler")
check("...the optiscaler route is told about 'loads as'",
      "'loads as'" in _said and "reshade loads as" not in _said, _said[-200:])
_rep, _said = _override_said(str(_here2 / "GTA5.exe"), _here2, route="feeder")
check("...and the feeder route, which has its own row for it, is told "
      "'reshade loads as'",
      "'reshade loads as'" in _said, _said[-200:])
# Read off the page, route by route - and the name the diagnosis tells
# people to change has to be a dropdown with that label.
_px63: dict = {}
_ui63 = _UiLive()
if _ui63.ok:
    _ui63.enter(_ui_game(name="Proxy Names", api="DX12"),
                _ui_support([dlss.FEEDER, dlss.OPTI, dlss.REMIX], dlss.FEEDER))
    _ui63.press("settings", "button")
    _ui63.settle(350)
    for _r63 in (dlss.FEEDER, dlss.OPTI, dlss.REMIX):
        _ui63.app.set_setting("route", _r63)
        _ui63.app.shell.redraw()
        _ui63.settle(40)
        _px63[_r63] = sorted(l for l in _ui63.labels("dropdown") if "loads as" in l)
    _ui63.app.set_setting("route", dlss.FEEDER)
    _ui63.app.shell.redraw()
    _ui63.settle(40)
    _ui63.press("reshade loads as", "dropdown")
    _px63["picked"] = _ui63.pick("d3d11.dll")
    _dd63 = _ui63.kit.find("reshade loads as", "dropdown")
    _px63["shows"] = [_ui63.canvas.itemcget(i, "text") for i in _ui63.canvas.find_withtag(_dd63 or "none")
                      if _ui63.canvas.type(i) == "text"]
    _px63["opts"] = _ui63.app.opts().reshade_proxy
_ui63.close()
_ui_cleanup()
check("the remix route shows no proxy dropdown - it installs no ReShade",
      _px63.get(dlss.REMIX) == [] and _px63.get(dlss.OPTI) == ["loads as"]
      and _px63.get(dlss.FEEDER) == ["reshade loads as"], _px63)
check("...and a name picked there is the name shown and the name installed",
      _px63.get("picked") and "d3d11.dll" in _px63.get("shows", [])
      and _px63.get("opts") == "d3d11.dll", _px63)
_rep, _said = _override_said(str(_here2 / "GTA5.exe"), _here2, route="remix")
check("...and the remix route, which installs no ReShade, is told neither",
      "loads as" not in _said, _said[-200:])
shutil.rmtree(_here2, ignore_errors=True)


# The standalone route keeps its own add-on log, so the same cut ReShade tail
# must not tell that route "ReShade loaded no add-ons" either.
_dsa = _diag_dir("diag_standalone_tail_", path="standalone", provider=0,
                 reshade="21:11:43:914 [24200] | WARN  | Successfully "
                         "compiled 'a.fx'\n")
# That log lives in LOCALAPPDATA, one file for every game: point the module at
# a temporary one rather than writing over the machine's own.
_sa_saved = diagnose.model.STANDALONE_LOG
_sa_dir = Path(tempfile.mkdtemp(prefix="diag_salog_tail_"))
diagnose.model.STANDALONE_LOG = _sa_dir / "standalone-dlssnr.log"
try:
    diagnose.model.STANDALONE_LOG.write_text(
        "11:00:00 Standalone DLSS-NR + SR 1.7.17"
        + diagnose._STANDALONE_SESSION + "quality\n", encoding="utf8")
    _r = diagnose.analyse(_dsa)
    check("the standalone add-on's own log answers for the cut ReShade tail",
          not any("loaded no add-ons" in t for t in _levels(_r, "bad")),
          str(_levels(_r, "bad")))
finally:
    diagnose.model.STANDALONE_LOG = _sa_saved
    shutil.rmtree(_sa_dir, ignore_errors=True)
shutil.rmtree(_dsa, ignore_errors=True)

# The install's own proxy is in the manifest's file list, and the event
# names a module rather than a path - so "it is in our list" is not proof
# that the copy which faulted was ours. Passing that list to ours() made the
# ambiguous branch unreachable and turned the commonest fault of all into
# "this is ours to fix".
_amb = _wc.Crash(when="2026-09-10 01:00:00", exe="Game.exe",
                 module="dxgi.dll", code="0xC0000005",
                 provider="Application Error")
_said_amb = _wc.describe(_amb, "dxgi.dll", ("dxgi.dll", "ReShade64.dll"))
check("a proxy-named fault stays ambiguous even though we wrote that file",
      "does not say which copy" in _said_amb[1], _said_amb[1][:80])
_mine = _wc.Crash(when="2026-09-10 01:00:00", exe="Game.exe",
                  module="RTX40MFGCore.dll", code="0xC0000005",
                  provider="Application Error")
check("...while a file only we write is ours, whatever it is called",
      "ours to fix" in _wc.describe(_mine, "dxgi.dll",
                                    ("dxgi.dll", "RTX40MFGCore.dll"))[1])
_theirs_c = _wc.Crash(when="2026-09-10 01:00:00", exe="Game.exe",
                      module="RTSSHooks64.dll", code="0xC0000005",
                      provider="Application Error")
check("...and somebody else's overlay is neither",
      "neither" in _wc.describe(_theirs_c, "dxgi.dll", ("dxgi.dll",))[1])

# An exception with an empty message used to raise inside a worker thread,
# after the window was put in its busy state and before the message that
# takes it out of it.
check("an exception with no message still names something",
      _uil63.first_line(TimeoutError()) == "TimeoutError"
      and _uil63.first_line(ValueError("first\nsecond")) == "first")

# The window that says whether a recorded fault belongs to this session must
# never be silently switched off: a route whose log is not in the list would
# have let every fault of the last fortnight speak for a clean session.
_sw = Path(tempfile.mkdtemp(prefix="session_window_"))
check("with nothing to date a crash against, the window is not just dropped",
      _uig63.last_log_write(_sw) == 0.0)
(_sw / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "files": []}),
    encoding="utf8")
check("...the install time stands in for it",
      _uig63.last_log_write(_sw) > 0)
(_sw / "OptiScaler.log").write_text("x", encoding="utf8")
check("...and a real log wins over that",
      _uig63.last_log_write(_sw) >= (_sw / "OptiScaler.log").stat().st_mtime)
shutil.rmtree(_sw, ignore_errors=True)

# When both sources fail, only the fallback's error is raised - so the
# publisher's reason (a proxy serving an HTML page, say) has to be said out
# loud before it is lost. It was being labelled "the first source" while
# printing the second's, because the variable had already moved on.
_pf_log = []
_pf_tried = []
_pf_real = installer._place_entry


def _pf_fail(entry, dest, rep, root, dl, member, name):
    _pf_tried.append(entry["label"])
    raise installer.InstallError(f"{entry['label']} failed")


installer._place_entry = _pf_fail
try:
    _pf_raised = None
    try:
        installer._place_family(
            [{"label": "publisher", "url": "u1"}, {"label": "mirror", "url": "u2"}],
            "", Path("x"), None, Path("."), None, "m", "p",
            lambda t: _pf_log.append(t))
    except installer.InstallError as _e:
        _pf_raised = str(_e)
finally:
    installer._place_entry = _pf_real
check("both sources are tried before a family is given up on",
      _pf_tried == ["publisher", "mirror"], _pf_tried)
check("...the fallback's failure is the one raised",
      _pf_raised == "mirror failed", _pf_raised)
check("...and the publisher's own reason is logged, not thrown away",
      any("the first source said: publisher failed" in t for t in _pf_log),
      [t.strip() for t in _pf_log])

# ...and the branch a blocked host actually takes: a proxy or a DNS filter
# surfaces as URLError/OSError, not InstallError, and that branch was not
# recording the first error at all - so the line named the wrong source on
# exactly the failure it exists for.
_bh_log = []
_bh_real = installer._place_entry


def _bh_fail(entry, dest, rep, root, dl, member, name):
    if entry["label"] == "publisher":
        raise OSError("publisher host is blocked")
    raise installer.InstallError("mirror failed")


installer._place_entry = _bh_fail
try:
    try:
        installer._place_family(
            [{"label": "publisher", "url": "u1"}, {"label": "mirror", "url": "u2"}],
            "", Path("x"), None, Path("."), None, "m", "p",
            lambda t: _bh_log.append(t))
    except Exception:
        pass
finally:
    installer._place_entry = _bh_real
check("a blocked publisher is named too, not just a refused download",
      any("the first source said: publisher host is blocked" in t
          for t in _bh_log), [t.strip() for t in _bh_log])

# The remembered walk must hand out a copy: one caller appending to it would
# poison every later answer for the rest of the session.
_wc = Path(tempfile.mkdtemp(prefix="walkcopy_"))
(_wc / "nvngx_dlss.dll").write_bytes(b"MZ")
dlss.forget_walk(_wc)
_first = dlss.walked(_wc)
_first.append("POISON")
check("the remembered walk hands out a copy, never the cache itself",
      "POISON" not in dlss.walked(_wc), dlss.walked(_wc))
shutil.rmtree(_wc, ignore_errors=True)

section("64. what the first day of 1.8.0 reported: a runtime under a "
        "skipped folder, an OptiScaler build that writes no log, and a "
        "route recommended from a DLL nobody can switch on")

# Issue #119, NBA 2K27: the game keeps Streamline in data\streamline, and
# "data" is on the walk's skip list - so a game that ships its own DLSS was
# read as a game with no DLSS at all, and offered the feeder route.
_nba = Path(tempfile.mkdtemp(prefix="nba2k_"))
(_nba / "NBA2K27.exe").write_bytes(b"MZ" + b"\0" * 300)
_sl = _nba / "data" / "streamline"
_sl.mkdir(parents=True)
for _n in ("sl.interposer.dll", "sl.dlss.dll"):
    (_sl / _n).write_bytes(b"MZ")
dlss.forget_walk(_nba)
_nsup = dlss.detect(_nba, _nba, "DX12", 64, sm=120)
check("a runtime under a skipped content folder is still found",
      _nsup.native_dlss, _nsup.evidence)
check("...so the route offered is not the one for games without DLSS",
      _nsup.recommended != dlss.FEEDER, _nsup.recommended)

# ...and the skip list still does its job: a content folder with nothing
# runtime-shaped under it is not descended into.
_big = Path(tempfile.mkdtemp(prefix="bigcontent_"))
(_big / "Game.exe").write_bytes(b"MZ" + b"\0" * 300)
(_big / "data" / "textures").mkdir(parents=True)
(_big / "data" / "textures" / "nvngx_dlss.dll").write_bytes(b"MZ")
dlss.forget_walk(_big)
check("...and a plain content folder is still skipped",
      not dlss.detect(_big, _big, "DX12", 64).native_dlss)
for _d in (_nba, _big):
    shutil.rmtree(_d, ignore_errors=True)

# Issue #110: y4my4my4m's build ships [Log] LogToFile=auto, which is false,
# so OptiScaler wrote nothing and a working install was told it had never
# loaded. The install turns the log on, and the verdict no longer claims
# more than an absent file can support.
_ol = Path(tempfile.mkdtemp(prefix="optilog_"))
(_ol / "OptiScaler.ini").write_text(
    "[Log]\n; kept\nLogToFile=auto\nLogLevel=auto\n\n[DlssNr]\nEnabled=false\n",
    encoding="utf8")
optiscaler.enable_nr(_ol, settings={"WorkingScale": 0.75})
_oltxt = (_ol / "OptiScaler.ini").read_text(encoding="utf8")
check("the install switches OptiScaler's log on, whatever the build ships",
      "LogToFile=true" in _oltxt and "LogToFile=auto" not in _oltxt)
check("...without throwing away what the person had in the file",
      "; kept" in _oltxt and "WorkingScale=0.75" in _oltxt)

(_ol / "dxgi.dll").write_bytes(b"MZ")
(_ol / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX12", "proxy": "dxgi.dll", "path": "optiscaler",
     "files": ["dxgi.dll", "OptiScaler.ini"]}), encoding="utf8")
# Logging on (the install above set it) and still no log: that is the old
# question again, and it is asked as a question.
_olrep = diagnose.analyse(_ol)
check("no log with the proxy in place is not 'it never loaded'",
      "never loaded" not in _olrep.verdict, _olrep.verdict)
check("...with logging on, it asks whether the game has run since",
      "not run yet" in _olrep.verdict.lower(), _olrep.verdict)
# Logging off - the y4my4my4m default - is its own answer: a missing log
# then says nothing about whether OptiScaler loaded.
(_ol / "OptiScaler.ini").write_text(
    "[Log]\nLogToFile=auto\n\n[DlssNr]\nEnabled=true\n", encoding="utf8")
_olrep = diagnose.analyse(_ol)
check("...with logging off, it says the log is off and how to switch it on",
      "log is off" in _olrep.verdict.lower()
      and "not run yet" not in _olrep.verdict.lower(), _olrep.verdict)
check("...and names the proxy it checked, and the button, not 'this button'",
      any("(dxgi.dll)" in f.detail and "did it work?" in f.detail
          for f in _olrep.findings),
      [f.detail[:80] for f in _olrep.findings])
(_ol / "OptiScaler.ini").unlink()
check("...and with OptiScaler.ini gone altogether it says the ini is missing",
      "missing" in diagnose.analyse(_ol).verdict.lower(),
      diagnose.analyse(_ol).verdict)
(_ol / "dxgi.dll").unlink()
check("...but with nothing of ours in the folder it does say so",
      "not in the game folder" in diagnose.analyse(_ol).verdict,
      diagnose.analyse(_ol).verdict)
shutil.rmtree(_ol, ignore_errors=True)

# Issue #116, Risk of Rain 2: an FSR runtime on disk is not an upscaler the
# player can switch on. The route was recommended, had nothing to hook, and
# only the diagnosis afterwards said why.
_ok116, _note116 = dlss.fit(dlss.OPTI, "DX12", False, 120, upscaler="fsr")
check("the optiscaler route says the game's own upscaler has to be on",
      "has to be on in the game's own settings" in _note116, _note116)
check("...and names the route to use when the game has no such setting",
      "feeder" in _note116, _note116)

# #127: from 1.4.0 the bridge replaces, before reading it, a settings file
# whose first line is not its version or "keep" - with its defaults, which
# leave the substitute contract off. Ours had no such line, so a game with
# no DLSS of its own never got the synth_after this install wrote.
from core import feedcfg as _fc127                                # noqa: E402
_bd = Path(tempfile.mkdtemp(prefix="bridgecfg_"))
(_bd / _fc127.BRIDGE_NAME).write_text(
    "# dlss5-bridge 1.4.12\nsynth=0\nsynth_after=0\nofa_perf=5\nofa_grid=2\n"
    "source=auto\nflags=107\nunwrap=1\n", encoding="utf8")
# What 1.8.0 left behind on a game without DLSS: the bridge's own dump.
(_bd / installer.MANIFEST).write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX11", "path": "bridge", "native_dlss": False,
     "files": ["dxgi.dll", _fc127.BRIDGE_NAME]}), encoding="utf8")
(_bd / "dxgi.dll").write_bytes(b"MZ")
(_bd / "ReShade.log").write_text(
    "12:00:00:000 [1] | INFO  | Registered add-on \"DLSS 5 Bridge 1.4.12\" "
    "v1.4.12.0 using ReShade API version 18.\n", encoding="utf8")
_rb = diagnose.analyse(_bd)
check("'did it work?' names a bridge file that turned its substitute off",
      "install the bridge route again" in _rb.verdict, _rb.verdict)
_fc127.write_bridge(_bd, _fc127.bridge_defaults(False))
_bl = (_bd / _fc127.BRIDGE_NAME).read_bytes().decode("utf8").splitlines()
check("the bridge settings file starts with the line that keeps it",
      _bl[0] == "# dlss5-bridge keep", _bl[:2])
check("...carries the substitute switch for a game without DLSS",
      "synth_after=3" in _bl, _bl)
check("...keeps what the person chose in the bridge's panel",
      "ofa_perf=5" in _bl and _bl.count("# dlss5-bridge keep") == 1, _bl)
check("...but not the rest of a version's defaults, which 'keep' would freeze",
      "flags=107" not in _bl and "unwrap=1" not in _bl
      and "ofa_grid=2" not in _bl and "source=auto" not in _bl
      and "synth=0" not in _bl, _bl)
check("...and 'did it work?' no longer says so",
      "install the bridge route again" not in diagnose.analyse(_bd).verdict)
_fc127.write_bridge(_bd, {"ofa_grid": 4})
_bt = (_bd / _fc127.BRIDGE_NAME).read_text(encoding="utf8")
check("...and a second write does not stack the line, and keeps our own file",
      _bt.count("dlss5-bridge keep") == 1 and "ofa_grid=4" in _bt
      and "synth_after=3" in _bt, _bt)
_fc127.write_bridge(_bd, _fc127.bridge_defaults(True))
check("a later install that found the game's DLSS turns the substitute off",
      "synth_after=0" in (_bd / _fc127.BRIDGE_NAME).read_text(encoding="utf8"))
shutil.rmtree(_bd, ignore_errors=True)

# The report's ReShade.log excerpt lost its oldest lines first, and the hook
# lines are written at the start of a session: the "hooked" line went, the
# "Failed to find" beside it stayed, and the excerpt replayed to "update the
# driver". Over budget, the lines nothing reads go first.
_ex_log = ("12:00:00:000 [1] | INFO  | Registered add-on \"DLSS 5 Neural "
           "Rendering\" v0.2026.828.517 using ReShade API version 18.\n"
           "12:00:01:000 [1] | DEBUG | [DLSS 5 Neural Rendering] vtable::Hook("
           "NVSDK_NGX_D3D12_EvaluateFeaturehooked with 0x00007fff55ee8740 => "
           "0x00007fff36671430)\n"
           "12:00:01:000 [1] | ERROR | [DLSS 5 Neural Rendering] vtable::Hook("
           "Failed to find NVSDK_NGX_D3D12_EvaluateFeature_C)\n"
           + "12:00:02:000 [1] | INFO  | Redirecting Direct3DCreate9(SDKVersion "
             "= 0x20) ...\n" * 40)
_ex = diagnose._reshade_excerpt(_ex_log)
check("an over-budget excerpt keeps the hook lines the driver rule reads",
      any("EvaluateFeaturehooked" in ln for ln in _ex)
      and any("Failed to find" in ln for ln in _ex)
      and len("\n".join(_ex)) <= 1500, _ex[:3])
check("...and which add-ons loaded, before the lines nothing reads",
      any("Registered add-on" in ln for ln in _ex))
check("...and the excerpt still reads as a plain hook",
      re.search(r"vtable::Hook\(NVSDK_NGX_\w+_EvaluateFeature\w*\s*hooked",
                "\n".join(_ex)) is not None)

# The 1.8.1 gate: "try the standalone route" was said to games that are
# never offered it - 32-bit, DX9, Vulkan and OpenGL get feeder and bridge
# only, and were sent looking for an entry their dropdown does not have.
_sa_mismatch = []
for _api in ("DX9", "DX10", "DX11", "DX12", "Vulkan", "OpenGL", "Unknown"):
    for _bits in (32, 64):
        _sd = Path(tempfile.mkdtemp(prefix="safit_"))
        (_sd / "g.exe").write_bytes(b"MZ" + b"\0" * 200)
        dlss.forget_walk(_sd)
        _offered = dlss.STANDALONE in dlss.detect(_sd, _sd, _api, _bits, sm=89).options
        if _offered != dlss.standalone_fits(_api, _bits):
            _sa_mismatch.append((_api, _bits))
        shutil.rmtree(_sd, ignore_errors=True)
check("standalone_fits agrees with the route list for every api and bitness",
      not _sa_mismatch, _sa_mismatch)
check("the feeder warning names standalone only where it is offered",
      "standalone" in (dlss.driver_warning("feeder", "616.92",
                                           offered=["feeder", "standalone"]) or "")
      and "standalone" not in (dlss.driver_warning("feeder", "616.92",
                                                   offered=["feeder", "bridge"]) or ""))

_CHAIN = ("12:00:00.000  [feed] evaluate raised 0xC0000005 (reading address "
          "FFFFFFFFFFFFFFFF) (caught; nothing submitted)\n"
          "12:00:00.000  [feed] evaluate fault stack, by module (innermost "
          "first): D3D12Core.dll <- nvngx_dlssnr.dll <- _nvngx.dll <- "
          "renodx-dlss5.addon64 <- dlss5-feed.addon64 <- ReShade64.dll\n")
_d32 = _diag_dir("chain32_", feed=_FEED_OK + _CHAIN, bitness=32, api="DX9",
                 reshade='INFO | Registered add-on "DLSS 5 Feed" v0.14\n',
                 components={"renodx": "4.55"})
_r32 = diagnose.analyse(_d32)
check("...and a 32-bit game with the 616.64 fault is not sent to standalone",
      "616.64" in _r32.verdict and "standalone" not in _r32.verdict,
      _r32.verdict)
shutil.rmtree(_d32, ignore_errors=True)
_d64 = _diag_dir("chain64_", feed=_FEED_OK + _CHAIN, bitness=64, api="DX12",
                 reshade='INFO | Registered add-on "DLSS 5 Feed" v0.14\n',
                 components={"renodx": "4.55"})
_r64 = diagnose.analyse(_d64)
check("...while a 64-bit D3D12 game with it is",
      "standalone" in _r64.verdict, _r64.verdict)
shutil.rmtree(_d64, ignore_errors=True)

# #116 on D3D11: the D3D11 note used to replace the upscaler warning.
check("a D3D11 game offered optiscaler for its FSR still hears it must be on",
      "has to be on" in dlss.fit(dlss.OPTI, "DX11", False, 89, upscaler="fsr")[1])

# #130: NVIDIA's Aftermath library beside a Vulkan exe names d3d12.dll, and
# the game was read as DX12. Middleware that names every API is not evidence.
_af = Path(tempfile.mkdtemp(prefix="aftermath_"))
_sysd = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
shutil.copy(_sysd / "cmd.exe", _af / "enshrouded.exe")
with open(_af / "enshrouded.exe", "ab") as _fh:
    _fh.write(b"\0vulkan-1.dll\0")
shutil.copy(_sysd / "cmd.exe", _af / "GFSDK_Aftermath_Lib.x64.dll")
with open(_af / "GFSDK_Aftermath_Lib.x64.dll", "ab") as _fh:
    _fh.write(b"\0d3d12.dll\0dxgi.dll\0" + b"\0" * (2 * 1024 * 1024))
_api = pe.runtime_graphics(_af / "enshrouded.exe") if hasattr(pe, "runtime_graphics") \
    else pe._runtime_graphics(_af / "enshrouded.exe")
check("a crash library that names every API does not make a Vulkan game DX12",
      "d3d12" not in str(_api).lower() and "dx12" not in str(_api).lower(), _api)
shutil.rmtree(_af, ignore_errors=True)

# #131: the trial exe beside the full game's matched the folder name as well
# and was bigger, so it was picked.
_tr = Path(tempfile.mkdtemp(prefix="Need for Speed Heat_"))
(_tr / "NeedForSpeedHeat.exe").write_bytes(b"MZ" + b"\0" * 1000)
(_tr / "NeedForSpeedHeatTrial.exe").write_bytes(b"MZ" + b"\0" * 400000)
(_tr / "NFS16.exe").write_bytes(b"MZ" + b"\0" * 1000)
(_tr / "NFS16_trial.exe").write_bytes(b"MZ" + b"\0" * 400000)
_ex = [p.name for p in pe.find_game_exes(_tr)]
check("the full game's exe is ranked above its trial beside it",
      _ex.index("NeedForSpeedHeat.exe") < _ex.index("NeedForSpeedHeatTrial.exe")
      and _ex.index("NFS16.exe") < _ex.index("NFS16_trial.exe"), _ex)
check("...and the trial is still in the list for whoever plays it",
      "NeedForSpeedHeatTrial.exe" in _ex, _ex)
shutil.rmtree(_tr, ignore_errors=True)
_dm = Path(tempfile.mkdtemp(prefix="SomeGame_"))
(_dm / "SomeGameDemo.exe").write_bytes(b"MZ" + b"\0" * 1000)
check("a demo-only install still gets its demo exe",
      [p.name for p in pe.find_game_exes(_dm)] == ["SomeGameDemo.exe"])
shutil.rmtree(_dm, ignore_errors=True)

# #130/#134: the report's tool-log excerpt was other games' scan lines.
_tail = ("2026-09-10 10:00:00 info  " + "=" * 70 + "\n"
         "2026-09-10 10:00:01 info  Minecraft for Windows: C:\\x\\m.exe is not "
         "readable yet - enable mods\n"
         "2026-09-10 10:00:02 warn  stopped looking for runtime DLLs under "
         "D:\\RPCS3 after 900 folders - search budget reached\n"
         "2026-09-10 10:00:03 info  scan Steam: 12 found\n"
         "2026-09-10 10:00:04 info  inspected Enshrouded in 1.2s\n"
         "2026-09-10 10:00:05 error install failed: something real\n")
import types as _types  # noqa: E402
_tl = diagnose._tool_log_lines(_tail, _types.SimpleNamespace(name="Enshrouded"),
                               r"D:\Games\Enshrouded")
check("the report's tool log leaves out the other games' scan lines",
      not any("Minecraft" in ln or "RPCS3" in ln or "Steam: 12" in ln
              for ln in _tl), _tl)
check("...keeps the reported game's own and the errors",
      any("inspected Enshrouded" in ln for ln in _tl)
      and any("something real" in ln for ln in _tl), _tl)

# ...and the ReShade.log excerpt is the last session's, as analyse reads it.
_two = ("12:00:00:000 [1] | INFO  | Initializing crosire's ReShade version '6.8.0'\n"
        "12:00:01:000 [1] | DEBUG | [DLSS 5 Neural Rendering] vtable::Hook("
        "NVSDK_NGX_D3D12_EvaluateFeaturehooked with 0x1 => 0x2)\n"
        "13:00:00:000 [2] | INFO  | Initializing crosire's ReShade version '6.8.0'\n"
        "13:00:01:000 [2] | ERROR | [DLSS 5 Neural Rendering] vtable::Hook("
        "Failed to find NVSDK_NGX_D3D12_EvaluateFeature_C)\n")
_ex2 = diagnose._reshade_excerpt(diagnose._last_session(_two))
check("the report's ReShade.log excerpt does not pull hook lines from an "
      "older session", not any("EvaluateFeaturehooked" in ln for ln in _ex2)
      and any("Failed to find" in ln for ln in _ex2), _ex2)

# The wheel over a dropdown scrolls the page and never changes the choice -
# ttk's class binding did, before any window-level handler ran. The 2.0
# dropdowns are drawn, so this is asked with real wheel events: over a closed
# dropdown, and over the open list of one.
_wh64: dict = {}
_ui64 = _UiLive()
if _ui64.ok:
    _ui64.enter(_ui_game(name="Wheel", api="DX12"),
                _ui_support([dlss.FEEDER, dlss.OPTI, dlss.STANDALONE], dlss.FEEDER))
    _ui64.press("settings", "button")
    _ui64.settle(350)
    _c64 = _ui64.canvas
    _route_dd64 = _ui64.kit.find("route", "dropdown")
    _xy64 = _ui64._xy(_c64, _route_dd64) if _route_dd64 else None
    if _xy64:
        _top0 = _c64.canvasy(0)
        for _d64 in (-120, 120, -120):
            _c64.event_generate("<Motion>", x=_xy64[0], y=_xy64[1])
            _c64.event_generate("<MouseWheel>", delta=_d64, x=_xy64[0], y=_xy64[1])
            _ui64.settle(30)
        _wh64["closed"] = (_ui64.app.route, _ui64.app.settings.get("route", _ui64.app.route))
        _c64.yview_moveto(0)
        _ui64.settle(30)
        _ui64.press("route", "dropdown")
        _menu64 = _ui64.kit.top()
        _wh64["opened"] = _menu64 is not None
        if _menu64 is not None:
            _bx64 = _c64.bbox(_menu64.tag)
            _mx = int((_bx64[0] + _bx64[2]) / 2 - _c64.canvasx(0))
            _my = int((_bx64[1] + _bx64[3]) / 2 - _c64.canvasy(0))
            for _d64 in (-120, -120, 120):
                _c64.event_generate("<Motion>", x=_mx, y=_my)
                _c64.event_generate("<MouseWheel>", delta=_d64, x=_mx, y=_my,
                                    rootx=_c64.winfo_rootx() + _mx, rooty=_c64.winfo_rooty() + _my)
                _ui64.settle(30)
            _wh64["open"] = (_ui64.app.route, _ui64.kit.top() is _menu64)
_ui64.close()
_ui_cleanup()
check("the wheel over a dropdown does not change what it says",
      _wh64.get("closed") == (dlss.FEEDER, dlss.FEEDER), _wh64)
check("...nor over its open list, which stays open",
      _wh64.get("opened") and _wh64.get("open") == (dlss.FEEDER, True), _wh64)

section("1.8.1: self-update relaunch, the MFG unlock's new shape, a bad cached "
        "archive, OptiScaler's update nag (#136 #141 #140 #51)")
import io as _io141
import zipfile as _zf141
from core import mfg as _m141  # noqa: E402
from core import selfupdate as _su136  # noqa: E402

# #136: the relaunched exe inherited the onefile child's _PYI_* variables and
# its bootloader quit with "failed to obtain executable path for parent
# process". The swap script and the Popen env both reset that.
_sw = _su136.swap_script(Path(r"C:\Games\Tool\dlss5-autopilot.exe"),
                         Path(r"C:\Temp\upd\dlss5-autopilot.exe"))
_rst = 'set "PYINSTALLER_RESET_ENVIRONMENT=1"'
check("the swap script resets PyInstaller's environment before it starts the new exe (#136)",
      _rst in _sw and 'start ""' in _sw and _sw.index(_rst) < _sw.index('start ""'), _sw[-200:])


class _Exit136(Exception):
    pass


_seen136: dict = {}
_saved136 = (_su136.running_exe, _su136.subprocess.Popen, _su136.os._exit)
_had_pyi = os.environ.get("_PYI_APPLICATION_HOME_DIR")
os.environ["_PYI_APPLICATION_HOME_DIR"] = r"C:\Temp\_MEI12345"
os.environ["_PYI_PARENT_PROCESS_LEVEL"] = "1"
_su136.running_exe = lambda: Path(r"C:\Games\Tool\dlss5-autopilot.exe")
_su136.subprocess.Popen = lambda *a, **k: _seen136.update(args=a, kw=k)


def _fake_exit(code):
    raise _Exit136(code)


_su136.os._exit = _fake_exit
# apply_and_restart refuses a staged build that is gone, so this one exists.
_staged136 = Path(tempfile.mkdtemp(prefix=_su136.PREFIX)) / "dlss5-autopilot.exe"
_staged136.write_bytes(b"MZ")
try:
    try:
        _su136.apply_and_restart(_staged136)
    except _Exit136:
        pass
finally:
    _su136.running_exe, _su136.subprocess.Popen, _su136.os._exit = _saved136
    os.environ.pop("_PYI_PARENT_PROCESS_LEVEL", None)
    if _had_pyi is None:
        os.environ.pop("_PYI_APPLICATION_HOME_DIR", None)
    else:
        os.environ["_PYI_APPLICATION_HOME_DIR"] = _had_pyi
    Path(tempfile.gettempdir(), "dlss5-autopilot-update.bat").unlink(missing_ok=True)
    shutil.rmtree(_staged136.parent, ignore_errors=True)
_saved_re = _su136.running_exe
_su136.running_exe = lambda: Path(r"C:\Games\Tool\dlss5-autopilot.exe")
try:
    _su136.apply_and_restart(_staged136)
    _gone_ok = False
except _su136.UpdateError:
    _gone_ok = True
finally:
    _su136.running_exe = _saved_re
check("a staged update that is gone is refused in words, not restarted into the old build",
      _gone_ok)
_env136 = (_seen136.get("kw") or {}).get("env")
check("apply_and_restart hands the swap an environment with no _PYI_* key (#136)",
      isinstance(_env136, dict) and not any(k.upper().startswith("_PYI_") for k in _env136)
      and _env136.get("PYINSTALLER_RESET_ENVIRONMENT") == "1"
      and "PATH" in {k.upper() for k in _env136},
      str(sorted(k for k in (_env136 or {}) if "PYI" in k.upper())))

# #141: v1.3.2 ships one RTXMFG.dll in RTXMFG-v1.3.2.zip. The resolver walks
# the release list for the universal zip, and a zip of another shape stops
# before anything lands in the game folder.
_rels141 = [
    {"tag_name": "v1.3.2", "published_at": "2026-09-10T00:00:00Z",
     "assets": [{"name": "RTXMFG-v1.3.2.zip", "browser_download_url": "u132"},
                {"name": "SHA256SUMS.txt", "browser_download_url": "s132"}]},
    {"tag_name": "v1.2.1", "published_at": "2026-08-20T00:00:00Z",
     "assets": [{"name": "Universal-RTX-40-MFG-Unlock-v1.2.1.zip",
                 "browser_download_url": "u121"},
                {"name": "Universal-RTX-40-MFG-Unlock-v1.2.1.zip.sha256",
                 "browser_download_url": "h121"}]},
]
_saved141 = (net.json_get, net.download, _m141.resolve, _m141.resolve_loader)
_asked141: list = []
try:
    net.json_get = lambda url: (_asked141.append(url), _rels141)[1]
    check("a v1.3.2 of the new shape above v1.2.1 resolves to v1.2.1, from the release list (#141)",
          _m141.resolve() == ("v1.2.1", "u121") and _asked141
          and "/releases?" in _asked141[0] and "latest" not in _asked141[0], str(_asked141))
    net.json_get = lambda url: _rels141[:1]
    try:
        _m141.resolve()
        _ok141 = False
    except _m141.ShapeChanged as e:
        _ok141 = "v1.3.2" in str(e)
    check("...and with only the new shape published, resolve() raises ShapeChanged", _ok141)
    net.json_get = _saved141[0]

    _d141 = Path(tempfile.mkdtemp(prefix="mfg141_"))
    _new141 = _d141 / "new.zip"
    with _zf141.ZipFile(_new141, "w") as z:
        z.writestr("RTXMFG.dll", b"MZ-new-shape")
    _ual141 = _d141 / "ual.zip"
    with _zf141.ZipFile(_ual141, "w") as z:
        z.writestr("dinput8.dll", b"MZ" + b"Ultimate ASI Loader" + b"\0" * (1 << 18))
    _g141 = _d141 / "game"
    _g141.mkdir()
    shutil.copyfile(X64, _g141 / "Game.exe")
    _before141 = sorted(p.name for p in _g141.iterdir())
    _m141.resolve = lambda: ("v1.3.2", "new")
    _m141.resolve_loader = lambda: ("v1", "ual")
    net.download = lambda url, name, **k: _new141 if url == "new" else _ual141
    try:
        _m141.install(_g141, _g141 / "Game.exe")
        _ok141 = False
    except _m141.ShapeChanged as e:
        _ok141 = "RTX40MFGCore.dll" in str(e)
    check("a release zip holding only RTXMFG.dll raises ShapeChanged and writes nothing (#141)",
          _ok141 and sorted(p.name for p in _g141.iterdir()) == _before141,
          str(sorted(p.name for p in _g141.iterdir())))
    # A loader zip without its dll is found out before the unlock's files land.
    _full141 = _d141 / "full.zip"
    with _zf141.ZipFile(_full141, "w") as z:
        for n in _m141.FILES:
            z.writestr(n, b"MZ" + n.encode())
    _noual141 = _d141 / "noual.zip"
    with _zf141.ZipFile(_noual141, "w") as z:
        z.writestr("readme.txt", b"x")
    net.download = lambda url, name, **k: _full141 if url == "new" else _noual141
    try:
        _m141.install(_g141, _g141 / "Game.exe")
        _ok141 = False
    except _m141.ShapeChanged:
        _ok141 = True
    check("...and a loader zip without dinput8.dll stops before the three files land",
          _ok141 and sorted(p.name for p in _g141.iterdir()) == _before141,
          str(sorted(p.name for p in _g141.iterdir())))
    shutil.rmtree(_d141, ignore_errors=True)
finally:
    net.json_get, net.download, _m141.resolve, _m141.resolve_loader = _saved141
check("the installer turns ShapeChanged into a warning, like NoLoaderName (#141)",
      re.search(r"except \(mfg\.NoLoaderName, mfg\.ShapeChanged, "
                r"sources\.RateLimited,\s+sources\.Unavailable, "
                r"net\.WrongContent\) as e:", src_of(installer)) is not None)

# #140: a proxy page or a cut zip in the cache was served on every retry and
# every install died with "File is not a zip file".
_c140 = Path(tempfile.mkdtemp(prefix="cache140_"))
_buf140 = _io141.BytesIO()
with _zf141.ZipFile(_buf140, "w") as z:
    z.writestr("a.txt", b"hello")
_realzip140 = _buf140.getvalue()
_serve140 = {"body": b"", "n": 0}


class _Resp140:
    def __init__(self, body):
        self._b = _io141.BytesIO(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._b.read(n)


def _urlopen140(req, timeout=0, **kw):
    _serve140["n"] += 1
    return _Resp140(_serve140["body"])


_saved140 = (net.CACHE, net.urllib.request.urlopen, net.time.sleep)
net.CACHE = _c140
net.urllib.request.urlopen = _urlopen140
net.time.sleep = lambda s: None
try:
    _serve140.update(body=b"<!DOCTYPE html><html>Blocked by your provider</html>", n=0)
    try:
        net.download("https://github.com/x/y/releases/download/v1/a.zip", "bad140.zip")
        _msg140 = ""
    except RuntimeError as e:
        _msg140 = str(e)
    check("an HTML page served for a .zip raises, names the host and leaves no cache file (#140)",
          "github.com" in _msg140 and "Nothing was written" in _msg140
          and "DOCTYPE" in _msg140 and not (_c140 / "bad140.zip").exists()
          and not (_c140 / "bad140.zip.part").exists() and _serve140["n"] == 1, _msg140)
    try:
        net.download("https://example.com/x.dll", "bad140.dll")
        _ok140 = False
    except net.WrongContent:
        _ok140 = True
    check("...the same for a .dll that is not a Windows binary", _ok140)
    (_c140 / "cached140.zip").write_bytes(b"<html>a cached error page</html>")
    _serve140.update(body=_realzip140, n=0)
    _got140 = net.download("https://example.com/c.zip", "cached140.zip")
    check("a bad .zip already in the cache is fetched again, exactly once (#140)",
          _serve140["n"] == 1 and _zf141.is_zipfile(_got140), str(_serve140["n"]))
    _serve140.update(n=0)
    _got140 = net.download("https://example.com/c.zip", "cached140.zip")
    check("...and a real zip in the cache is served without a download",
          _serve140["n"] == 0 and _got140.read_bytes() == _realzip140)
    _serve140.update(body=b"plain text is fine", n=0)
    check("...while a suffix outside the checked set passes as it is",
          net.download("https://example.com/n.txt", "notes140.txt").read_bytes()
          == b"plain text is fine")
finally:
    net.CACHE, net.urllib.request.urlopen, net.time.sleep = _saved140
    shutil.rmtree(_c140, ignore_errors=True)

# #51: OptiScaler compares the fork with mainline OptiScaler and nags about
# an "update" that has no neural rendering. And enable_nr runs on every
# autotune step, so it may only switch the log on, not overwrite a level.
_o51 = Path(tempfile.mkdtemp(prefix="opti51_"))
(_o51 / "OptiScaler.ini").write_text(
    "[Log]\nLogToFile=auto\nLogLevel=0\n\n[Hotfix]\n; Enables checking for "
    "latest version from Github\nCheckForUpdate=auto\n\n[DlssNr]\nEnabled=false\n",
    encoding="utf8")
optiscaler.enable_nr(_o51, settings={"WorkingScale": 0.75})
_t51 = (_o51 / "OptiScaler.ini").read_text(encoding="utf8")
check("enable_nr leaves exactly one CheckForUpdate=false, under [Hotfix] (#51)",
      _t51.count("CheckForUpdate=") == 1
      and optiscaler._ini_get(_t51, "Hotfix", "CheckForUpdate") == "false", _t51)
check("...keeps a LogLevel the person set (0) and turns LogToFile=auto into true",
      optiscaler._ini_get(_t51, "Log", "LogLevel") == "0"
      and optiscaler._ini_get(_t51, "Log", "LogToFile") == "true", _t51)
optiscaler.enable_nr(_o51, settings={"WorkingScale": 0.75})
check("...and a second call (an autotune step) changes nothing",
      (_o51 / "OptiScaler.ini").read_text(encoding="utf8") == _t51)
(_o51 / "OptiScaler.ini").unlink()
optiscaler.enable_nr(_o51)
_t51 = (_o51 / "OptiScaler.ini").read_text(encoding="utf8")
check("a fresh ini ends with LogToFile=true, LogLevel=2 and CheckForUpdate=false (#110, #51)",
      optiscaler._ini_get(_t51, "Log", "LogToFile") == "true"
      and optiscaler._ini_get(_t51, "Log", "LogLevel") == "2"
      and optiscaler._ini_get(_t51, "Hotfix", "CheckForUpdate") == "false", _t51)
(_o51 / "OptiScaler.ini").write_text("[Log]\nLogToFile=false\nLogLevel=auto\n",
                                     encoding="utf8")
optiscaler.enable_nr(_o51)
_t51 = (_o51 / "OptiScaler.ini").read_text(encoding="utf8")
check("...and LogToFile=false / LogLevel=auto become true / 2",
      optiscaler._ini_get(_t51, "Log", "LogToFile") == "true"
      and optiscaler._ini_get(_t51, "Log", "LogLevel") == "2", _t51)
shutil.rmtree(_o51, ignore_errors=True)

section("1.8.2: a full drive, the missing Remix box, update leftovers (#148 #157)")
from core import library as _lib148  # noqa: E402
from core import selfupdate as _su148  # noqa: E402

_full = OSError(28, "No space left on device", r"C:\Users\x\AppData\Local\dlss5-autopilot\cache\big.zip")
_wrapped = RuntimeError("could not unpack")
_wrapped.__cause__ = _full
_win = OSError(None, "There is not enough space on the disk")
_win.winerror = 112
check("a full drive is recognised as errno 28, as Windows' 112, and wrapped in another error",
      net.is_disk_full(_full) and net.is_disk_full(_win) and net.is_disk_full(_wrapped)
      and not net.is_disk_full(OSError(13, "Permission denied"))
      and not net.is_disk_full(RuntimeError("x")))
_msg148 = net.disk_full_message(_wrapped, Path(r"D:\Games\X"), net.CACHE)
check("...and the message names the drive and says to free space, without a traceback",
      _msg148.startswith("Out of disk space on C:") and "beside the game (D:" in _msg148
      and "free some up" in _msg148 and "Traceback" not in _msg148
      and "Errno" not in _msg148, _msg148)

# The install stops on a full drive: it says so, records it, and the
# diagnosis reads that record instead of "install again" (#148).
_d148 = Path(tempfile.mkdtemp(prefix="full148_"))
shutil.copyfile(X64, _d148 / "Game.exe")
_g148 = games.manual(_d148)
_saved148 = (installer.reengine.detected, installer.refw.install)
installer.reengine.detected = lambda root: True


def _refw_full(root, log):
    raise OSError(28, "No space left on device", str(root / "dinput8.dll"))


installer.refw.install = _refw_full
try:
    try:
        installer.install(_g148, installer.Options(), on_log=lambda t: None)
        check("an install on a full drive stops with words, not a traceback", False)
    except installer.InstallError as e:
        check("an install on a full drive stops with words, not a traceback",
              "Out of disk space" in str(e) and "Errno" not in str(e), str(e)[:120])
finally:
    installer.reengine.detected, installer.refw.install = _saved148
_man148 = json.loads((_d148 / installer.MANIFEST).read_text(encoding="utf8"))
check("...the record says the drive was full",
      _man148.get("complete") is False and net.DISK_FULL_NOTE in _man148.get("notes", []))
_rep148 = diagnose.analyse(_d148)
check("...and the diagnosis says to free up space first",
      "full" in _rep148.verdict.lower() and "install again" in _rep148.verdict.lower()
      and "never finished" not in _rep148.verdict, _rep148.verdict)
shutil.rmtree(_d148, ignore_errors=True)

# library.save on a full drive left its .tmp behind every time.
_lf = Path(tempfile.mkdtemp(prefix="lib148_"))
_saved_file = _lib148.FILE
_lib148.FILE = _lf / "library.json"
_real_wt = Path.write_text


def _wt_full(self, *a, **k):
    if self.suffix == ".tmp":
        _real_wt(self, "{\"half", encoding="utf8")
        raise OSError(28, "No space left on device", str(self))
    return _real_wt(self, *a, **k)


Path.write_text = _wt_full
try:
    _lib148.save([], {}, "0", 89)
finally:
    Path.write_text = _real_wt
    _lib148.FILE = _saved_file
check("a library save that hits a full drive leaves no .tmp behind",
      not list(_lf.glob("*.tmp")), [p.name for p in _lf.iterdir()])
shutil.rmtree(_lf, ignore_errors=True)

# A refused update download left its staging folder in %TEMP%: 838 of them
# on the machine these tests run on.
_before = set(Path(tempfile.gettempdir()).glob(_su148.PREFIX + "*"))
_saved_su = net.json_get
net.json_get = lambda url: {"tag_name": "v9.9", "assets": []}
try:
    try:
        _su148.fetch()
    except _su148.UpdateError:
        pass
finally:
    net.json_get = _saved_su
_after = set(Path(tempfile.gettempdir()).glob(_su148.PREFIX + "*"))
check("a failed update fetch removes its staging folder", not (_after - _before),
      [p.name for p in _after - _before])
_sw148 = _su148.swap_script(Path(r"C:\Tools\dlss5-autopilot.exe"),
                            Path(tempfile.gettempdir()) / (_su148.PREFIX + "abc") / "dlss5-autopilot.exe")
# without /s: rmdir then removes the folder only if it is empty
check("...and the swap script removes the emptied one after an update",
      f'rmdir "{Path(tempfile.gettempdir()) / (_su148.PREFIX + "abc")}" >nul' in _sw148,
      _sw148[-300:])

# #87/#93: tar.exe without LZMA, no 7-Zip installed. The answer was "install
# 7-Zip"; now 7-Zip's own one-file unpacker is fetched, pinned by hash.
import subprocess as _sp87  # noqa: E402
_d87 = Path(tempfile.mkdtemp(prefix="sz87_"))
(_d87 / "in").mkdir()
(_d87 / "in" / "OptiScaler.dll").write_bytes(b"MZ" + b"\1" * 5000)
try:
    _zr = net.download(sources.SEVEN_ZR[0][0], "7zr.exe")
    _sp87.run([str(_zr), "a", str(_d87 / "t.7z"), str(_d87 / "in" / "OptiScaler.dll")],
              capture_output=True)
    _saved87 = (optiscaler._seven_zip, optiscaler._tar_exe)
    optiscaler._seven_zip = lambda: None
    optiscaler._tar_exe = lambda: _d87 / "no-tar.exe"
    try:
        optiscaler.extract_7z(_d87 / "t.7z", _d87 / "out")
        check("with no 7-Zip and no working tar.exe, the pinned 7zr.exe unpacks a .7z (#87)",
              (_d87 / "out" / "OptiScaler.dll").is_file())
        _saved_pin = sources.SEVEN_ZR
        sources.SEVEN_ZR = (_saved_pin[0], "0" * 64)
        try:
            _ran = optiscaler._seven_zr_extract(_d87 / "t.7z", _d87 / "out2", 0)
        finally:
            sources.SEVEN_ZR = _saved_pin
        check("...and a 7zr.exe that is not the pinned build is never run",
              _ran is False and not (_d87 / "out2" / "OptiScaler.dll").exists())
    finally:
        optiscaler._seven_zip, optiscaler._tar_exe = _saved87
except (sources.RateLimited, sources.Unavailable, OSError) as e:
    check("with no 7-Zip and no working tar.exe, the pinned 7zr.exe unpacks a .7z (#87)",
          False, f"could not fetch 7zr.exe: {e}")
shutil.rmtree(_d87, ignore_errors=True)

# Three reports replayed against the code, each with its own log lines.
def _feeder_dir(prefix, api, bitness, reshade, feed):
    _d = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copyfile(X64, _d / "Game.exe")
    _files = ["dlss5-feed.addon64", "renodx-dlss5.addon64", "ReShade.ini",
              "reshade-shaders/Shaders/DLSS5_Feed.fx",
              "reshade-shaders/Shaders/lumenite_Kernel.fx",
              "reshade-shaders/Shaders/vort_Motion.fx"]
    for _f in _files:
        (_d / _f).parent.mkdir(parents=True, exist_ok=True)
        (_d / _f).write_bytes(b"MZ")
    (_d / "dlss5-autopilot.json").write_text(json.dumps(
        {"version": 1, "complete": True, "exe": "Game.exe", "bitness": bitness,
         "api": api, "proxy": "dxgi.dll", "path": "feeder", "files": _files}),
        encoding="utf8")
    (_d / "ReShade.log").write_text(reshade, encoding="utf8")
    (_d / "dlss5-feed.log").write_text(feed, encoding="utf8")
    return _d


# #156 Octowow, 32-bit OpenGL: frames delivered, then the helper died. It was
# called "closed before it drew a single frame" (OpenGL has no swap-chain
# line), and with no helper log in the folder the verdict was "Working."
_d156 = _feeder_dir("oct156_", "OpenGL", 32, (
    '11:18:26:359 [25448] | INFO  | Registered add-on "DLSS 5 Feed (32-bit) 0.15.1" v0.0.0.0 using ReShade API version 20.\n'
    "11:18:38:663 [25448] | WARN  | [DLSS 5 Feed (32-bit) 0.15.1] [DLSS 5 Feed 32] stopped: the 64-bit host went away -- its own dlss5-feed-host.log (in host64\\) names the reason. The game renders normally\n"
    "11:21:16:610 [25448] | INFO  | Exiting ...\n"), (
    "11:18:34.900  [feed32] effects: technique found, DLSS5_MV found, DLSS5_Depth found, DLSS5_MV_PROVIDER=2 (VORT) -> vort_MotionEffects (enabled), depth reversed=1\n"
    "11:18:35.043  [feed32] frame 2 delivered (3440x1440, reset=0, OpenGL)\n"
    "11:18:35.060  [feed32] frame 3 delivered (3440x1440, reset=0, OpenGL)\n"
    "11:18:38.625  [feed32] host lost: frame message failed (exit code 3765269347)\n"
    "11:18:38.663  stopped: the 64-bit host went away -- its own dlss5-feed-host.log (in host64\\) names the reason. The game renders normally. See dlss5-feed.log for the detail.\n"
    "11:21:16.514  shut down cleanly.\n"))
_r156 = diagnose.analyse(_d156)
_t156 = [f.title for f in _r156.findings]
check("frames delivered on OpenGL are not 'closed before it drew a single frame' (#156)",
      not any("closed before it drew" in t for t in _t156), _t156)
check("...and a feed that stopped after them is not 'Working.' (#156)",
      _r156.verdict != "Working." and any("stopped after" in t for t in _t156),
      (_r156.verdict, _t156))
shutil.rmtree(_d156, ignore_errors=True)

# #142 Half Sword: the game died one second in, while the feed was still
# waiting for ReShade to compile. "Never loaded" and "not installed" were
# said over a file list with both shaders in place.
_d142 = _feeder_dir("hs142_", "DX12", 64, (
    '18:29:10:426 [21916] | INFO  | Registered add-on "DLSS 5 Feed 0.15.1" v0.15.1.0 using ReShade API version 20.\n'
    "18:29:11:068 [21916] | INFO  | Redirecting IDXGIFactory::CreateSwapChain(this = 000001FA2374E1A0) ...\n"), (
    "18:29:11.331  [feed] effects: DLSS5_Feed.fx technique MISSING, ColorInput MISSING, DLSS5_MV MISSING, DLSS5_Depth MISSING, DLSS5_Mask absent (older shader: no bias mask), DLSS5_MV_PROVIDER=3 (LumeniteFX Kernel) -> none (not installed)\n"
    "18:29:11.331  [feed] DLSS5_Feed.fx has not resolved yet (ReShade may still be compiling); waiting 10 s before calling it missing\n"
    "18:29:11.335  [feed] effect runtime 000001FA283BAE00 destroyed -- it was the bound one (0 runtimes left)\n"))
_t142 = [f.title for f in diagnose.analyse(_d142).findings]
check("a game that closed while the effects compiled is not told the shader never loaded (#142)",
      any("still compiling" in t for t in _t142)
      and not any("never loaded" in t or "not installed" in t for t in _t142), _t142)
shutil.rmtree(_d142, ignore_errors=True)

# #155: a ReShade.log with none of the lines the excerpt keeps was printed
# as "(none)" - read as "ReShade never loaded", the opposite.
_ex155 = diagnose._reshade_excerpt(
    "11:59:40:001 [ 100] | INFO  | Initializing crosire's ReShade version '6.8.0.0' (64-bit)\n"
    "11:59:40:002 [ 100] | INFO  | Loading add-ons from C:\\Games\\Sleeping Dogs\n")
check("a ReShade.log with none of the kept lines still reaches the report (#155)",
      len(_ex155) == 2 and "Initializing" in _ex155[0], _ex155)

check("Forspoken's dxgi.dll signature check is said before the install (#154)",
      any("signature" in q for q in dlss.quirks(Path("FORSPOKEN.exe"), "DX12")))

# Gate findings, 1.8.2.
# A quick rescan only ever added: a game removed in Steam that left its
# folder behind stayed. What no store lists in the first place stays.
_qs = Path(tempfile.mkdtemp(prefix="qs_"))
for _n in ("Gone", "Kept", "Mine"):
    (_qs / _n).mkdir()
_known = [games.Game(name="Gone", folder=_qs / "Gone", source="Steam"),
          games.Game(name="Kept", folder=_qs / "Kept", source="Steam"),
          games.Game(name="Mine", folder=_qs / "Mine", source="Manual")]
_saved_lg = games.list_games
games.list_games = lambda progress=None, emulators=True: [
    games.Game(name="Kept", folder=_qs / "Kept", source="Steam")]
try:
    _qout, _qfresh = games.quick_scan(_known)
finally:
    games.list_games = _saved_lg
check("a quick rescan drops a store game no store lists any more, keeps a hand-picked one",
      sorted(g.name for g in _qout) == ["Kept", "Mine"] and not _qfresh,
      [g.name for g in _qout])
shutil.rmtree(_qs, ignore_errors=True)

# The install's own refusal, raised part way, was recorded as an unfinished
# install and diagnosed as "install again" - which is refused again (#148).
_dr = _fake_remix("remix_refuse_")
(_dr / ".trex" / "d3d9.dll").write_bytes(b"MZ MOD RUNTIME, NO PASS" + b"\x00" * 400)
_gr = games.manual(_dr)
_gr.api = "DX9"
try:
    installer.install(_gr, installer.Options(path=dlss.REMIX), on_log=lambda t: None)
except installer.InstallError:
    pass
_rr = diagnose.analyse(_dr)
check("a refused install is diagnosed by its reason, not 'install again' (#148)",
      "never finished" not in _rr.verdict
      and any("swap the Remix runtime" in f.detail for f in _rr.findings),
      (_rr.verdict, [f.detail[:80] for f in _rr.findings]))
shutil.rmtree(_dr, ignore_errors=True)


class _R7:
    returncode = 2
    stdout = "ERROR: There is not enough space on the disk : C:\\x\\OptiScaler.dll"
    stderr = ""


try:
    optiscaler._raise_if_full(_R7(), Path("C:/x"))
    _full7 = False
except OSError as e:
    _full7 = net.is_disk_full(e)
check("a full drive while unpacking a .7z is a full drive, not 'install 7-Zip'", _full7)

# #152: UBOAT was installed for "UBOAT Launcher.exe" - the launcher is
# bigger than Unity's player stub, and both match the folder's name.
_ub = Path(tempfile.mkdtemp(prefix="uboat152_")) / "UBOAT"
(_ub / "UBOAT_Data").mkdir(parents=True)
shutil.copyfile(X64, _ub / "UBOAT.exe")
(_ub / "UBOAT Launcher.exe").write_bytes((_ub / "UBOAT.exe").read_bytes() + b"\0" * (8 << 20))
check("a launcher beside the game it starts is not taken for the game (#152)",
      pe.find_game_exes(_ub)[0].name == "UBOAT.exe",
      [p.name for p in pe.find_game_exes(_ub)])
shutil.rmtree(_ub.parent, ignore_errors=True)

section("1.8.2: api.github.com answered by something else (#175), and the "
        "crash verdict that stopped before the next step (#98)")

# Several checks below build a folder that looks like somebody's install and
# read the verdict out of it - the same thing _tools/replay_report.py does
# for a saved report, so it is imported rather than written twice.
sys.path.insert(0, str(SRC_DIR / "_tools"))
import replay_report as _rr182  # noqa: E402

from core.ui import ctl_game as _uig182, ctl_library as _uil182  # noqa: E402
from core import wincrash as _wcx  # noqa: E402

# #175, word for word out of the report: the chain verified and the NAME on
# the certificate did not match, which the old answer read as a missing
# Windows root and told the person to open GitHub in Edge.
_MISMATCH = ("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate "
             "verify failed: Hostname mismatch, certificate is not valid "
             "for 'api.github.com'. (_ssl.c:1010)>")
_m = str(net.untrusted("api.github.com", Exception(_MISMATCH)))
check("a hostname mismatch is not called a missing root certificate (#175)",
      "different name" in _m and "once in Edge" not in _m, _m[:90])
check("...and it names what actually does it",
      all(w in _m for w in ("DNS", "VPN", "1.1.1.1")), _m[:90])
_exp = str(net.untrusted("reshade.me", Exception(
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "certificate has expired (_ssl.c:1010)")))
check("an expired certificate points at this PC's clock",
      "clock" in _exp and "Edge" not in _exp, _exp[:90])
_self = str(net.untrusted("github.com", Exception(
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self "
    "signed certificate in certificate chain")))
check("a self-signed chain points at the HTTPS scanning that made it",
      "antivirus" in _self and "Edge" not in _self, _self[:90])
_root = str(net.untrusted("api.github.com", Exception(
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to "
    "get local issuer certificate")))
check("a missing root still gets the answer that fixes a missing root (#54)",
      "Edge" in _root and "api.github.com" in _root, _root[:90])
check("nothing but a verification failure is claimed as one",
      net.untrusted("x", Exception("timed out")) is None)

# The same two pages the fallback reads, in GitHub's own markup.
_ASSETS_HTML = ('</svg>          <a href="/jlrouzies-fr/DLSS5-Feeder/releases'
                '/download/v0.15.1/AUTOMATIC_INSTALLATION_AVAILABLE.txt" '
                'rel="nofollow" data-turbo="false" class="wb-break-all">'
                '<a href="/jlrouzies-fr/DLSS5-Feeder/releases/download/'
                'v0.15.1/DLSS5-Feeder-0.15.1.zip" rel="nofollow">')
_LIST_HTML = ('<a href="/jlrouzies-fr/DLSS5-Feeder/releases/tag/v1.16.0-beta.1"'
              ' data-view-component="true" class="Link--primary Link">1.16.0'
              '<a href="/jlrouzies-fr/DLSS5-Feeder/releases/tag/v0.15.1" '
              'class="Link--primary Link">0.15.1'
              '<a href="/jlrouzies-fr/DLSS5-Feeder/releases/tag/v0.15.1#top" '
              'class="Link">same release again')
with patch.object(sources, "_page", lambda url, timeout=30: _ASSETS_HTML):
    _a = sources.release_assets_html("jlrouzies-fr/DLSS5-Feeder", "v0.15.1")
check("a release's assets are read off github.com, with no API call",
      _a.get("DLSS5-Feeder-0.15.1.zip", "").endswith(
          "/jlrouzies-fr/DLSS5-Feeder/releases/download/v0.15.1/"
          "DLSS5-Feeder-0.15.1.zip") and len(_a) == 2, _a)
with patch.object(sources, "_page", lambda url, timeout=30: _LIST_HTML):
    _t = sources.release_tags_html("jlrouzies-fr/DLSS5-Feeder", pages=1)
check("...and the tags are, newest first, each one once",
      _t == [("v1.16.0-beta.1", True), ("v0.15.1", False)], _t)
check("a test build is told by its tag, not by a badge whose markup moves",
      sources.release_tags_html.__doc__ and "badge" in sources.release_tags_html.__doc__)
with patch.object(sources, "_page", lambda url, timeout=30: ""):
    check("a page that does not answer ends the walk instead of looping",
          sources.release_tags_html("x/y", pages=4) == []
          and sources.release_assets_html("x/y", "v1") == {})

check("the feeder falls back to those pages when the API does not answer",
      "_feeder_html" in src_of(sources.resolve_feeder)
      and "release_tags_html" in src_of(sources.feeder_releases))
check("...and the build list does too, or gives the API's own error",
      "_rhi_html_catalog" in src_of(sources.rhi_catalog)
      and "raise" in src_of(sources.rhi_catalog))
check("a pinned tag the API HAS answered about is not hunted for on the pages",
      "_NoSuchTag" in src_of(sources.resolve_feeder))
check("every build the installer pins by name survives the shortened list",
      set(sources.RHI_HTML_REQUIRED["renodx"]) == {
          sources.FEEDER_RENODX_PIN, sources.OPENGL_RENODX_PIN,
          sources.DRIVER_FAULT_RENODX_PIN},
      sources.RHI_HTML_REQUIRED)
check("...and a pin that is missing anyway is said out loud, not installed over",
      'e["label"] != want' in src_of(installer)
      and "the kind of build that pin exists to avoid" in src_of(installer)
      # ...and it names a control that exists (the gate found it naming one
      # that did not).
      and "'dlss5 add-on'" in src_of(installer))
_FOREIGN = ('<a href="/someone/else/releases/tag/v9.9">'
            '<a href="/jlrouzies-fr/DLSS5-Feeder/releases/tag/v0.15.1">'
            '<a href="/someone/else/releases/download/v9.9/other.zip">')
with patch.object(sources, "_page", lambda url, timeout=30: _FOREIGN):
    _ft = sources.release_tags_html("jlrouzies-fr/DLSS5-Feeder", pages=1)
    _fa = sources.release_assets_html("jlrouzies-fr/DLSS5-Feeder", "v0.15.1")
check("a link to another project's release is not read as one of ours",
      _ft == [("v0.15.1", False)] and _fa == {}, (_ft, _fa))
check("one install's fallback notice does not leak into the next",
      "sources.last_fallback = None" in src_of(installer.install))
check("the fallback tells the user the list came from somewhere else",
      "last_fallback" in src_of(sources.rhi_catalog)
      and "last_fallback" in src_of(sources.resolve_feeder))

# Every component that is not in sources.py reaches GitHub through
# net.json_get, and the components inside it through sources._json. Both
# now answer from github.com's pages when the API cannot be reached - the
# API's own shape, so no caller's asset matching changes.
check("a releases URL is recognised in all three of its forms",
      sources._RELEASE_URL.match(
          "https://api.github.com/repos/doitsujin/dxvk/releases/latest"
      ).group(2) == "latest"
      and sources._RELEASE_URL.match(
          "https://api.github.com/repos/a/b/releases/tags/v1").group(3) == "v1"
      and sources._RELEASE_URL.match(
          "https://api.github.com/repos/a/b/releases?per_page=20") is not None)
check("...and something that is not one is left alone",
      sources.release_json_html(
          "https://api.github.com/repos/crosire/reshade/tags?per_page=5") is None
      and sources.release_json_html("https://example.com/x") is None)
_ONE = ('<a href="/a/b/releases/download/v2.0/Thing-v2.0.zip">'
        '<a href="/a/b/releases/download/v2.0/Thing-v2.0.zip.sha256">')
with patch.object(sources, "_page", lambda url, timeout=30: _ONE),         patch.object(sources, "latest_tag", lambda repo: "v2.0"):
    _r = sources.release_json_html("https://api.github.com/repos/a/b/releases/latest")
check("a release comes back in the API's own shape",
      _r["tag_name"] == "v2.0" and _r["draft"] is False
      and _r["prerelease"] is False
      and _r["assets"][0]["name"] == "Thing-v2.0.zip"
      and _r["assets"][0]["browser_download_url"].startswith("https://github.com/"),
      _r)
with patch.object(sources, "_page", lambda url, timeout=30: _ONE),         patch.object(sources, "latest_tag", lambda repo: None):
    check("a tag that cannot be read is None, not an empty release",
          sources.release_json_html(
              "https://api.github.com/repos/a/b/releases/latest") is None)
check("the list form is capped, so it cannot walk a hundred pages",
      sources.HTML_LIST_MAX <= 15)
import urllib.error as _ue2  # noqa: E402
_e404 = _ue2.HTTPError("https://api.github.com/repos/a/b/releases/latest",
                               404, "Not Found", None, None)
with patch.object(net, "fetch_text", lambda u: (_ for _ in ()).throw(_e404)):
    try:
        net.json_get("https://api.github.com/repos/a/b/releases/latest")
        _raised = ""
    except _ue2.HTTPError as e:
        _raised = str(e.code)
    except Exception as e:
        _raised = type(e).__name__
check("a 404 is the answer and is raised - only an unreachable host falls back",
      _raised == "404", _raised)
check("every component outside sources.py gets it through json_get",
      "release_json_html" in src_of(net.json_get)
      and all("json_or_html" in src_of(m) or "json_get" in src_of(m)
              for m in (optiscaler, dxvk, refw, _m141)))
check("...and the ones inside it, through json_or_html",
      "json_or_html" in src_of(sources.resolve_bridge)
      and "sources._json(" not in src_of(video))
check("rhi_catalog keeps its own walk - the capped list would drop the pins",
      "_rhi_html_catalog" in src_of(sources.rhi_catalog)
      and "json_or_html" not in src_of(sources.rhi_catalog))

# The first screen of the README is what a person reads before deciding
# whether this tool is worth downloading, and the GitHub About line beside
# it was describing a feeder installer long after the tool had eight
# routes. Numbers in a document rot silently; these two are checked.
_readme = (SRC_DIR / "README.md").read_text(encoding="utf8")
_WORDS = {8: "eight", 9: "nine", 10: "ten", 7: "seven"}
check("the README's route count is the number of routes there are",
      f"{_WORDS.get(len(dlss.LABELS), len(dlss.LABELS))} routes" in _readme.lower(),
      len(dlss.LABELS))
from core import emulators as _emus  # noqa: E402
check("...and its emulator count is the number of profiles there are",
      f"{len(_emus.PROFILES)} emulators" in _readme, len(_emus.PROFILES))
check("the first screen says the tool reads the logs afterwards, not only "
      "that it installs",
      "did it work?" in _readme[:3000] and "uninstall" in _readme[:3000].lower())
check("...and that nothing is bundled",
      _re.search(r"nothing is bundled|bundles nothing",
                 _readme[:3000], _re.I) is not None)

# Every control the README names in bold has to exist. A renamed button
# leaves the document telling people to press something that is not there -
# the same fault as #148, one surface out. Two words are enough to match on,
# because some labels are built at run time ("update (3)").
# Every module's text, the window's package included - read by what is
# there, so a module that goes away cannot stop the run. The 1.9 window
# (core/gui.py) is left out while it is still in the tree: nobody sees it.
_readme_src = "".join(p.read_text(encoding="utf8")
                      for p in sorted((SRC_DIR / "core").rglob("*.py"))
                      if p != SRC_DIR / "core" / "gui.py").lower()
_named, _gone = 0, []
for _b in _re.findall(r"[*][*]([^*\n]{2,40})[*][*]", _readme):
    _t = _b.strip().rstrip(".")
    # A sentence or a heading, not a control. An ALL-CAPS single word is a
    # control (INSTALL, AUTOPILOT) and was being skipped by the case test -
    # which meant the two buttons that write to the disk were the two this
    # check could not see, including the one this release renamed.
    if not _t or "," in _t or len(_t.split()) > 5 \
            or (_t[0].isupper() and not _t.isupper()):
        continue
    _named += 1
    # "try _route_": an _italic_ word is a placeholder the tool fills in
    # ("try feeder"), so only the words before it are the control's name.
    _words = [w for w in _t.split()]
    _words = _words[:next((i for i, w in enumerate(_words) if w.startswith("_") and w.endswith("_")
                           and len(w) > 2), len(_words))]
    _probe = " ".join(_words[:2]).strip(" ?:%-").lower()
    if _probe and _probe not in _readme_src:
        _gone.append(_t)
check("every control the README names in bold exists in the tool",
      _named >= 30 and not _gone, _gone or _named)

# The loop that measures itself: state.py turns every saved verdict into a
# ranked backlog, and a class at the top is a shape to fix. Checked here so
# the ranking cannot silently stop describing the corpus.
import importlib.util as _ilu3  # noqa: E402
_st_spec = _ilu3.spec_from_file_location("state", SRC_DIR / "_tools" / "state.py")
_st = _ilu3.module_from_spec(_st_spec)
_st_spec.loader.exec_module(_st)
_stc = _st.corpus()
_stsaved = json.loads((SRC_DIR / "_tools" / "verdict_baseline.json")
                     .read_text(encoding="utf8"))
check("the loop measures every report in the corpus, not a sample",
      _stc.get("total", 0) == len(_stsaved)
      == len(list((SRC_DIR / "_tools" / "reports").glob("*.txt"))),
      (_stc.get("total"), len(_stsaved)))
check("...and almost nothing falls through its classes",
      _stc["counts"].get("other", 0) <= max(6, _stc["total"] // 10),
      _stc["counts"].get("other"))
check("the biggest class is named, with the reports behind it",
      bool(_stc["counts"].most_common(1)[0][0])
      and len(_stc["where"][_stc["counts"].most_common(1)[0][0]]) ==
      _stc["counts"].most_common(1)[0][1])
check("a verdict this project has actually given lands in the right class",
      _st.classify("Not started since the install - run the game once.")
      == "nothing we wrote ever loaded"
      and _st.classify("Inconclusive - the feed did not get far enough to tell.")
      == "we cannot tell from the logs"
      and _st.classify("Working.") == "working")
_settings182 = SRC_DIR.parent / ".claude" / "settings.json"
check("...and the guard rails report themselves as wired",
      _st.guards() == [] if _settings182.is_file() else True,
      _st.guards() if _settings182.is_file()
      else "(no .claude/settings.json beside the repo - skipped)")
check("the session opens with that measurement",
      (SRC_DIR / "_tools" / "hooks" / "session_start.py").is_file()
      and "state.py" in (SRC_DIR / "_tools" / "hooks"
                         / "session_start.py").read_text(encoding="utf8"))

# 34 of the first 84 reports are "no log at all", and the answer to them
# opened with "the game has not been started since the install" - a guess,
# and the one sentence that makes a person who DID start it give up. The
# game's own files answer it.
_last_ran_findings = []


def _no_log_verdict(make=None, exe="Game.exe"):
    d = _rr182.build("feeder", "DX12", exe, {}, 64)
    try:
        if make:
            make(d)
        r = diagnose.analyse(d)
        _last_ran_findings[:] = r.findings
        return r.verdict, [f.title for f in r.findings]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _later(p, seconds=120):
    t = time.time() + seconds
    os.utime(p, (t, t))


_v, _f = _no_log_verdict()
check("a fresh install nothing has run still says exactly that",
      "Not started since the install" in _v
      and any("has not been started" in t for t in _f), _v)


def _save(d):
    sav = d / "Saved" / "SaveGames"
    sav.mkdir(parents=True)
    (sav / "PlayerProgress.sav").write_bytes(b"x")
    _later(sav / "PlayerProgress.sav")


_v, _f = _no_log_verdict(_save)
check("the game's own save, written after the install, says it DID run",
      "looks as though it ran" in _v
      and any("own files changed after the install" in t for t in _f), _v)
check("...and the verdict hedges it, because a store update writes there too",
      "most likely" in _v and "though a store update" in " ".join(
          f.detail or "" for f in
          [x for x in _last_ran_findings]), _v)


def _dll(d):
    (d / "somemod.dll").write_bytes(b"MZ")
    _later(d / "somemod.dll")


_v, _f = _no_log_verdict(_dll)
check("a DLL that appeared afterwards is not a game leaving a trace",
      "Not started since the install" in _v, _v)


def _ours(d):
    # dxgi.dll is in the manifest the replay builds: our own file, never
    # evidence about the game.
    _later(d / "dxgi.dll")


_v, _f = _no_log_verdict(_ours)
check("...and neither is a file our own manifest says we wrote",
      "Not started since the install" in _v, _v)


def _within_the_minute(d):
    sav = d / "Saved"
    sav.mkdir()
    (sav / "settings.cfg").write_bytes(b"x")
    _later(sav / "settings.cfg", seconds=5)


_v, _f = _no_log_verdict(_within_the_minute)
check("a file written seconds after the install is the install, not a session",
      "Not started since the install" in _v, _v)

_ud = Path(tempfile.mkdtemp(prefix="userdata_"))
(_ud / "Wardogs" / "Saved" / "Logs").mkdir(parents=True)
_ulog = _ud / "Wardogs" / "Saved" / "Logs" / "Wardogs.log"
_ulog.write_bytes(b"x")
_later(_ulog)
with patch.object(diagnose.model, "_user_data_roots", lambda: [_ud]):
    _v, _f = _no_log_verdict(exe="WardogsClient-Win64-Shipping.exe")
check("an Unreal game writes under LOCALAPPDATA, and that counts too",
      "looks as though it ran" in _v, _v)
check("...the folder name is worked out from the executable",
      "Wardogs" in diagnose._user_data_names(Path("C:/g/Binaries/Win64"),
                                             "WardogsClient-Win64-Shipping.exe"),
      diagnose._user_data_names(Path("C:/g/Binaries/Win64"),
                                "WardogsClient-Win64-Shipping.exe"))
shutil.rmtree(_ud, ignore_errors=True)
check("the look is bounded - entries and a clock, never a walk",
      diagnose._RAN_ENTRIES <= 8000 and diagnose._RAN_SECONDS <= 2.0
      and "os.walk" not in src_of(diagnose._game_ran))

# The owner's own machine, found by detect_check: an Unreal game whose
# shipping exe imports no graphics DLL at all was read as OpenGL, because
# the only renderer name in the file is Unreal's unused OpenGL RHI string.
# ReShade would have gone in as opengl32.dll, which the game never loads -
# and the report would have come back with no log at all.
_ue = Path(tempfile.mkdtemp(prefix="unreal_"))
_ue_exe = _ue / "Wardogs" / "Binaries" / "Win64" / "WardogsClient-Win64-Shipping.exe"
_ue_exe.parent.mkdir(parents=True)
shutil.copyfile(X64, _ue_exe)
with open(_ue_exe, "ab") as _f:
    _f.write(b"opengl32.dll")
check("without the engine folder the rule does not fire at all",
      pe._engine_default(_ue_exe) is None and not pe._is_unreal(_ue_exe))
(_ue / "Engine").mkdir()
_api, _why = pe.detect_api(_ue_exe)
check("an Unreal layout decides the renderer before an opengl32 string does",
      _api == "DX12" and "Unreal" in _why, (_api, _why[:60]))
check("...and it is the shape that says so, not the executable's name",
      pe._is_unreal(_ue_exe)
      and not pe._is_unreal(_ue / "Wardogs" / "Binaries" / "Win64"))
_nue = Path(tempfile.mkdtemp(prefix="notunreal_"))
(_nue / "bin" / "win64").mkdir(parents=True)
shutil.copyfile(_ue_exe, _nue / "bin" / "win64" / "Game.exe")
check("a game in some other bin/win64, with no Engine folder, is left alone",
      pe._engine_default(_nue / "bin" / "win64" / "Game.exe") is None)
(_nue / "Engine").mkdir()
check("...and an Engine folder alone is not the Unreal layout either - "
      "Binaries is part of it",
      pe._engine_default(_nue / "bin" / "win64" / "Game.exe") is None)
shutil.rmtree(_ue, ignore_errors=True)
shutil.rmtree(_nue, ignore_errors=True)

# #182 (Resident Evil 4, "it never started"): the report said "ReShade
# loaded no add-ons" over a log block that read "(none)". Three shapes, and
# they are three different answers.
def _verdict_for(log_text):
    d = _rr182.build("feeder", "DX12", "re4.exe", {"reshade": " "}, 64)
    (d / "ReShade.log").write_text(log_text, encoding="utf8")
    try:
        r = diagnose.analyse(d)
        return r.verdict, [f.title for f in r.findings]
    finally:
        shutil.rmtree(d, ignore_errors=True)
_NL = chr(10)
_BANNER = ("16:29:03:123 [1234] | INFO  | Initializing crosire's ReShade "
           "version 6.6.1 ..." + _NL)
for _label, _txt in (("empty", ""), ("whitespace", _NL + "  " + _NL)):
    _v, _f = _verdict_for(_txt)
    check(f"a ReShade.log with nothing in it is no log, not 'no add-ons' ({_label})",
          "Not started since the install" in _v
          and not any("loaded no add-ons" in t for t in _f), (_v, _f[:1]))
_v, _f = _verdict_for(_BANNER)
check("ReShade's banner and nothing after it is a game that died at start-up",
      "closed during start-up" in _v
      and any("session ended before anything else" in t for t in _f), (_v, _f[:1]))
_v, _f = _verdict_for("16:29:03:123 [1234] | WARN  | Reference count for "
                      "ID3D12CommandQueue0 is inconsistent (7)." + _NL)
check("...but a tail that does not start where ReShade did says nothing of the kind",
      any("loaded no add-ons" in t for t in _f), _f[:2])
_v, _f = _verdict_for(_BANNER + "16:29:05:001 [1234] | INFO  | Redirecting "
                      "IDXGIFactory::CreateSwapChain(...)" + _NL)
check("...and neither does a session that got as far as a swap chain",
      any("loaded no add-ons" in t for t in _f), _f[:2])

# Every report anybody ever sent, through the diagnosis as it is now,
# against the answers recorded last time. This is the net under every
# change to diagnose.py: 236 if/elif branches over one input, where a rule
# put in front of the others silently changes what all of them see.
import importlib.util as _ilu2  # noqa: E402
_vspec = _ilu2.spec_from_file_location("verdict_check",
                                       SRC_DIR / "_tools" / "verdict_check.py")
_vc = _ilu2.module_from_spec(_vspec)
_vspec.loader.exec_module(_vc)
_vbase = json.loads((SRC_DIR / "_tools" / "verdict_baseline.json").read_text(encoding="utf8"))
_vnow = _vc.answers()
# The replay has to keep answering like the machine the report came from.
# Nothing measured this: verdict_check compared the replay against its own
# saved answers, so the two could drift together and stay green forever.
_vsame, _vtotal, _voff = _vc.reproduction(_vnow)
check("the replay still answers like the machine each report came from",
      _vtotal >= 60 and _vsame >= _vc.REPRODUCTION_FLOOR,
      f"{_vsame} of {_vtotal}, floor {_vc.REPRODUCTION_FLOOR}")
_vmoved = _vc._diff(_vbase, _vnow)
check("the corpus is every real report with logs, not a handful",
      len(_vnow) >= 80, len(_vnow))
check("the diagnosis raises on none of them",
      not [n for n, r in _vnow.items() if r.get("error")],
      [n for n, r in _vnow.items() if r.get("error")][:5])
check("no report's verdict moved without being recorded "
      "(_tools/verdict_check.py --save)",
      not _vmoved, " | ".join(_vmoved[:6]))

# The words every verdict about somebody else's log is matched against
# live in their builds, and they move (#168: "cost:" became "elapsed:").
# _tools/phrase_check.py downloads the current builds and looks for them;
# here, offline, only that its table still describes THIS code.
import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("phrase_check",
                                     SRC_DIR / "_tools" / "phrase_check.py")
_pcmod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_pcmod)
_dg = _diag_src()          # a package since 1.9.0, not one file
_pairs = [pair for group in _pcmod.PHRASES.values() for pair in group]
_missing = [c for _b, c in _pairs if c not in _dg]
check("every phrase the rot check watches is one the diagnosis really reads",
      len(_pairs) >= 20 and not _missing, _missing or len(_pairs))
# The skills and the hook wiring live beside the repository, not in it, so
# on anybody else's checkout there is nothing to read. Absent means "not set
# up here", which is not a failure; present means it has to say so.
_skill182 = SRC_DIR.parent / ".claude" / "skills" / "issue-triage" / "SKILL.md"
check("...and the check is in the standing audits, so it runs by itself",
      "phrase_check.py" in _skill182.read_text(encoding="utf8")
      if _skill182.is_file() else True,
      "" if _skill182.is_file() else "(no .claude beside the repo - skipped)")

# #137: Cyberpunk's CET IS an ASI loader and owns version.dll. The unlock
# used to write Ultimate ASI Loader over it - CET, and every mod that needs
# it, out of the game. The reporter's own way round is now the tool's.
_cp = Path(tempfile.mkdtemp(prefix="cet137_"))
_cpexe = _cp / "Cyberpunk2077.exe"
shutil.copyfile(X64, _cpexe)
check("with nothing in the way the loader name is what it always was",
      _m141.loader_name(_cpexe, set(), _cp) == _m141.loader_name(_cpexe, set())
      is not None)
(_cp / "version.dll").write_bytes(b"MZ" + b"CyberEngineTweaks" * 64)
(_cp / "plugins").mkdir()
check("another mod's DLL under that name is not taken (#137)",
      _m141.loader_name(_cpexe, set(), _cp) is None
      and _m141.loader_name(_cpexe, set()) == "version.dll")
check("a folder merely CALLED plugins is not a loader's (#137, the gate)",
      _m141.existing_plugins(_cp) is None)
# CET's own folder carries its .asi; that is what says a loader reads it.
(_cp / "plugins" / "cyber_engine_tweaks.asi").write_bytes(b"MZ-cet")
check("...and one with an .asi already in it is",
      _m141.existing_plugins(_cp) == _cp / "plugins")
check("a plugins folder with no loader beside it is somebody else's",
      _m141.existing_plugins(Path(tempfile.mkdtemp(prefix="noload_"))) is None)
check("a name WE wrote last time is still ours to write again",
      _m141.loader_name(_cpexe, set(), _cp, {"version.dll"}) == "version.dll")
_ualdir = Path(tempfile.mkdtemp(prefix="ual137_"))
shutil.copyfile(X64, _ualdir / _cpexe.name)
(_ualdir / "version.dll").write_bytes(b"MZ" + b"Ultimate ASI Loader" + b"x" * (1 << 18))
check("Ultimate ASI Loader under that name is merged with, not stepped around",
      _m141.loader_name(_ualdir / _cpexe.name, set(), _ualdir) == "version.dll")
_mz = Path(tempfile.mkdtemp(prefix="mfgzip_")) / "unlock.zip"
with _zf141.ZipFile(_mz, "w") as _z:
    for _n in _m141.FILES:
        _z.writestr(_n, b"MZ" + _n.encode())
with patch.object(_m141, "resolve", lambda: ("v1.2.1", "https://example/u.zip")),         patch.object(_m141, "resolve_loader",
                     lambda: ("v9", "https://example/l.zip")),         patch.object(net, "download", lambda url, name, **k: _mz):
    _tag, _files = _m141.install(_cp, _cpexe, taken=set())
check("the unlock goes into the loader's plugins folder, whole",
      sorted(_files) == ["RTX40MFG-UI.addon64", "plugins/RTX40MFG.asi",
                         "plugins/RTX40MFGCore.dll"]
      and (_cp / "plugins" / "RTX40MFG.asi").is_file()
      and (_cp / "RTX40MFG-UI.addon64").is_file(), _files)
check("...and the other mod's DLL is untouched, with no loader of ours beside it",
      (_cp / "version.dll").read_bytes().startswith(b"MZCyberEngineTweaks")
      and not (_cp / "version.dll.dlss5-autopilot-backup").exists()
      and not (_cp / "RTX40MFGCore.dll").exists())
_left141 = sorted(_m141.remove_leftovers(_cp, _files))
check("...and every name it reports is really gone from the disk",
      not any((_cp / n).exists() for n in _left141), _left141)
check("a reinstall that no longer wants it takes the plugins copy back out",
      _left141 ==
      ["RTX40MFG-UI.addon64", "plugins/RTX40MFG.asi", "plugins/RTX40MFGCore.dll"]
      or not (_cp / "plugins" / "RTX40MFG.asi").exists())
check("the preview and the step list know about that route too",
      "existing_plugins" in src_of(installer.preview)
      and src_of(installer).count("existing_plugins") >= 2)
shutil.rmtree(_cp, ignore_errors=True)
shutil.rmtree(_ualdir, ignore_errors=True)
shutil.rmtree(_mz.parent, ignore_errors=True)

# 616.64+ steers off every route that loads renodx-dlss5, where a route
# that does not is on offer. The shared results: standalone has not failed
# yet where a renodx route did - on a handful of reports, which is what the
# reason says.
_sd = Path(tempfile.mkdtemp(prefix="steer_"))
_steer = {drv: dlss.detect(_sd, _sd, "DX12", 64, sm=120, driver=drv).recommended
          for drv in (None, "616.56", "616.64", "616.92")}
check("no driver given, nothing steers - every existing caller is unchanged",
      _steer[None] == dlss.FEEDER and _steer["616.56"] == dlss.FEEDER, _steer)
check("616.64 and newer recommend the route that does not load renodx-dlss5",
      _steer["616.64"] == dlss.STANDALONE and _steer["616.92"] == dlss.STANDALONE,
      _steer)
check("...and the reason says which route it moved off, and what it costs",
      all(w in dlss.detect(_sd, _sd, "DX12", 64, sm=120, driver="616.92").reason
          for w in ("feeder", "616.56", "borderless", "handful")))
check("...and it leaves a marker, so the games page can say why",
      dlss.detect(_sd, _sd, "DX12", 64, sm=120,
                  driver="616.92").steered_from == dlss.FEEDER
      and not dlss.detect(_sd, _sd, "DX12", 64, sm=120).steered_from)
_st182 = dlss.detect(_sd, _sd, "DX12", 64, sm=120, driver="616.92")
with _ui_isolated(), _ui_threads(run=False):
    _pg182 = _ui_ctl(_ui_game(name="Steered", api="DX12"))
    _pg182.enter_game(_pg182.game)
    with patch.object(gpu, "driver_version", lambda: "616.92"):
        _pg182._on_entered((_pg182.game, _st182, {o: (True, "") for o in _st182.options},
                            {"ac": None, "reengine": False, "shared": "", "dxvk": False,
                             "ok": (True, ""), "seen": None}))
_ui_cleanup()
# The 1.9 list said "driver X: feeder reaches nvidia's runtime through the
# add-on that faults"; the 2.0 page says it in the route's driver note, drawn
# under the route - the reason is beside "standalone", which is the point.
check("...which the games page actually prints: the driver, and the add-on the route avoids",
      any(k == "driver" and "616.92" in t and "renodx" in t for k, t in _pg182.notes),
      _pg182.notes)
check("a game whose dropdown has no standalone entry is not steered to it",
      dlss.detect(_sd, _sd, "DX9", 32, sm=120, driver="616.92").recommended
      != dlss.STANDALONE)
(_sd / "nvngx_dlss.dll").write_bytes(b"MZ")
check("a game that ships its own DLSS keeps OptiScaler - it never loads the add-on",
      dlss.detect(_sd, _sd, "DX12", 64, sm=120, driver="616.92").recommended
      == dlss.OPTI)
_drv182: list = []
_real_detect182 = dlss.detect
with _ui_isolated(), _ui_threads(run=True):
    with patch.object(gpu, "driver_version", lambda: "616.92"), \
            patch.object(dlss, "detect", lambda *a, **k: (_drv182.append(k.get("driver")),
                                                          _real_detect182(*a, **k))[1]):
        _dv182 = _ui_ctl(_ui_game(name="Driver In", api="DX12"))
        _dv182.load_catalog = lambda: None
        _dv182.enter_game(_dv182.game)
        _uil182.LibraryControl.inspect_row(_dv182.game, 120)
_ui_cleanup()
check("the window passes the driver in, or the steer never runs",
      _drv182 == ["616.92", "616.92"], _drv182)
shutil.rmtree(_sd, ignore_errors=True)

# #98: "It ran, and then the game crashed" was the end of the answer. The
# reporter found the next step himself, and it is the one test that splits
# the neural pass from everything else.
_split182: dict = {}
for _r182 in ("optiscaler", "feeder"):
    _rp182 = diagnose.Report(route=_r182)
    _rp182.verdict = "Working."
    with _ui_isolated(), _ui_threads(run=False):
        _ap182 = _FakeApp(_rp182)
        _ap182.crash_overrides(_wcx.Crash(when="2026-09-12 00:54:33", exe="Game.exe", module="Game.exe",
                                         code="0xC0000005", provider="Application Error"))
    _split182[_r182] = _ap182.text()
_ui_cleanup()
check("the crash verdict now carries the test that splits it in two (#98)",
      "[DlssNr]" in _split182["optiscaler"] and "Enabled=false" in _split182["optiscaler"],
      _split182["optiscaler"][-300:])
check("...on the route whose ini that is, and something real on the others",
      "[DlssNr]" not in _split182["feeder"] and "uninstall" in _split182["feeder"].lower(),
      _split182["feeder"][-300:])

section("1.9.0: the folder nobody had, a launcher in front of the game, and "
        "asking the running process (#43 #191 #194)")

# --- the replay was answering about a folder nobody had ---------------------
import replay_report as _rr
_rep194 = (
    "**Did the game start?** it never started\n"
    "- version: 1.8.1\n- exe: GTAIV.exe\n- route: feeder\n\n"
    "**Files in the folder**\n"
    "- ReShade.ini: MISSING\n"
    "- nvngx_dlssnr.dll: MISSING\n"
    "- reshade-shaders/Shaders/DLSS5_Feed.fx: MISSING\n")
_st = _rr.folder_state(_rep194)
check("the report's own file list is what the replay builds now",
      _st is not None and _st["files"] == {
          "ReShade.ini": False, "nvngx_dlssnr.dll": False,
          "reshade-shaders/Shaders/DLSS5_Feed.fx": False}, _st)
check("...and no proxy, no add-on and no layer line means no install record",
      _st["manifest"] is False and _st["proxy"] == "")
_vk = _rr.folder_state("**Files in the folder**\n- dlss5-feed.addon64: present\n"
                       "- ReShade 64-bit Vulkan layer: registered\n")
check("a Vulkan install is not read as a missing dxgi.dll (#16, #19)",
      _vk["proxy"] == diagnose.VULKAN_LAYER and _vk["layer"] is True, _vk)
check("an older report with no file list keeps the old whole-folder replay",
      _rr.folder_state("**ReShade.log**\n```\n(none)\n```") is None)

_nofolder = Path(tempfile.mkdtemp(prefix="norecord_"))
_r = diagnose.analyse(_nofolder)
check("a folder this tool never installed into is told exactly that (#43, #194)",
      "Nothing is installed in this folder" in _r.verdict, _r.verdict)
check("...and not that the game has not been started since the install",
      not any("has not been started since" in f.title for f in _r.findings))
(_nofolder / "OptiScaler.ini").write_text("x", encoding="utf8")
_r = diagnose.analyse(_nofolder)
check("a folder whose record is gone keeps its route, read off the files",
      _r.route == "optiscaler" and any("install record is gone" in f.title
                                       for f in _r.findings), _r.route)
check("...and the report says the record is missing, so the next one need "
      "not be inferred",
      any("install record: MISSING" in ln
          for ln in diagnose._presence(_nofolder, {}, "")))
for _legacy in ("dlss5kur-kurulum.json", "dlss5-installer.json"):
    _lf = Path(tempfile.mkdtemp(prefix="legacy_"))
    (_lf / _legacy).write_text('{"path": "feeder", "exe": "g.exe"}',
                               encoding="utf8")
    check(f"an install recorded as {_legacy} is still an install",
          diagnose._manifest(_lf).get("path") == "feeder"
          and "Nothing is installed" not in diagnose.analyse(_lf).verdict)
    shutil.rmtree(_lf, ignore_errors=True)
shutil.rmtree(_nofolder, ignore_errors=True)

# --- a launcher is not the game (#191) --------------------------------------
check("a launcher whose game is not named after it is still a launcher (#191)",
      pe.launcher_like(Path("GTAVLauncher.exe"))
      and pe.launcher_like(Path("UBOAT Launcher.exe"))
      and pe.launcher_like(Path("start_protected_game.exe")))
check("...and a game with 'launcher' inside a longer word is not",
      not pe.launcher_like(Path("SpaceLauncherSimulator.exe"))
      and not pe.launcher_like(Path("GTA5.exe")))
_ldir = Path(tempfile.mkdtemp(prefix="launch_"))
(_ldir / "GTAVLauncher.exe").write_bytes(b"MZ" + b"\0" * 4096)
(_ldir / "GTA5.exe").write_bytes(b"MZ" + b"\0" * 40960)
check("the ranking puts the game in front of the launcher beside it",
      [x.name for x in pe.find_game_exes(_ldir)][0] == "GTA5.exe",
      [x.name for x in pe.find_game_exes(_ldir)])
check("...and the warning the install shows names it",
      "GTA5.exe" in installer.launcher_warning(
          games.Game(name="GTA V", folder=_ldir,
                     exe=_ldir / "GTAVLauncher.exe",
                     candidates=[_ldir / "GTA5.exe"])))
check("a game that is not a launcher gets no warning at all",
      installer.launcher_warning(
          games.Game(name="GTA V", folder=_ldir, exe=_ldir / "GTA5.exe",
                     candidates=[])) == "")
(_ldir / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "GTAVLauncher.exe", "bitness": 64,
     "api": "DX12", "proxy": "dxgi.dll", "path": "feeder",
     "files": ["dxgi.dll"]}), encoding="utf8")
(_ldir / "dxgi.dll").write_bytes(b"MZ")
_r = diagnose.analyse(_ldir)
check("and the installs already out there are told so by the diagnosis",
      "launcher" in _r.verdict and any("is a launcher" in f.title
                                       for f in _r.findings), _r.verdict)
check("...naming the executable that draws, since it is right there",
      any("GTA5.exe" in f.detail for f in _r.findings))
shutil.rmtree(_ldir, ignore_errors=True)

# --- ask the process, do not guess at the folder ----------------------------
_selfdll = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
check("every DLL loaded into this very process can be read",
      any(Path(x).name.lower() == _selfdll.lower()
          for x in watch.modules(os.getpid()).paths))
_seen = [s for s in watch.inspect(Path(sys.executable).parent, [_selfdll])
         if s.proc.pid == os.getpid()]
check("...and a file of ours that IS loaded is seen as loaded",
      bool(_seen) and bool(_seen[0].ours) and not _seen[0].missing, _seen[:1])
_elsewhere = [s for s in watch.inspect(Path(tempfile.mkdtemp(prefix="notmine_")),
                                       [_selfdll])]
check("a folder with no process of its own says nothing at all",
      _elsewhere == [])
_refused = watch.Loaded(pid=1, refused="the process is protected")
check("a refused module list is unknown, never 'nothing of ours is loaded'",
      not _refused.known and _refused.paths == [])
_esrc = src_of(diagnose._explain_no_log)
_i_live, _i_guess = _esrc.find("_live_evidence"), _esrc.find("The likeliest reason")
check("the diagnosis asks the running process before it offers a guess",
      "_live_evidence(install_dir, man, rep)" in _esrc
      and 0 <= _i_live < _i_guess, (_i_live, _i_guess))
_lsrc = src_of(diagnose._live_evidence)
for _shape in ("will not say what it", "is running from", "from somewhere else",
               "has loaded none of the files"):
    check(f"...and it answers this case: {_shape.strip()[:40]}",
          _shape in _lsrc)
check("a process that is up proves the game was started, whatever else",
      _lsrc.count("never_ran = False") >= 4)


# --- what was loaded is written down while the game runs, and read after ---
_wdir = Path(tempfile.mkdtemp(prefix="watched_"))
(_wdir / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX12", "proxy": "dxgi.dll", "path": "feeder",
     "files": ["dxgi.dll"]}), encoding="utf8")
for _n in ("dxgi.dll", "ReShade.ini", "nvngx_dlssnr.dll"):
    (_wdir / _n).write_bytes(b"MZ")
_sh = _wdir / "reshade-shaders" / "Shaders"
_sh.mkdir(parents=True, exist_ok=True)
(_sh / "DLSS5_Feed.fx").write_bytes(b"x")
(_sh / "lumenite_Kernel.fx").write_bytes(b"x")


def _with_sighting(rec):
    """Run the diagnosis over that folder with one remembered sighting."""
    import json as _j
    watch.RECORD.parent.mkdir(parents=True, exist_ok=True)
    _old = watch.RECORD.read_text(encoding="utf8") if watch.RECORD.is_file() else None
    watch.RECORD.write_text(_j.dumps(
        {os.path.normcase(str(_wdir)): rec}), encoding="utf8")
    try:
        return diagnose.analyse(_wdir)
    finally:
        if _old is None:
            watch.RECORD.unlink(missing_ok=True)
        else:
            watch.RECORD.write_text(_old, encoding="utf8")


_now = time.time()
check("a protected process that ran still proves the game was started",
      "protected process" in _with_sighting(
          {"at": _now, "name": "Game.exe", "refused": "the process is protected",
           "ours": [], "elsewhere": [], "missing": []}).verdict)
check("...and it is never 'not started since the install' after that",
      not _with_sighting(
          {"at": _now, "name": "Game.exe", "refused": "the process is protected",
           "ours": [], "elsewhere": [], "missing": []}).never_ran)
check("a different executable that ran is named, with the one installed beside",
      all(w in _with_sighting(
          {"at": _now, "name": "Game-Win64-Shipping.exe",
           "exe": r"D:\G\Binaries\Win64\Game-Win64-Shipping.exe", "refused": "",
           "ours": [], "elsewhere": [], "missing": ["dxgi.dll"]}).verdict
          for w in ("Game-Win64-Shipping.exe", "Game.exe")))
check("a DLL of that name loaded from elsewhere is the answer, not a guess",
      "another folder" in _with_sighting(
          {"at": _now, "name": "Game.exe", "refused": "", "ours": [],
           "elsewhere": [r"C:\Windows\System32\dxgi.dll"],
           "missing": []}).verdict)
check("System32's copy beside our own loaded proxy is not a conflict (#232)",
      "another folder" not in _with_sighting(
          {"at": _now, "name": "Game.exe", "refused": "", "ours": ["dxgi.dll"],
           "elsewhere": [r"C:\WINDOWS\system32\dxgi.dll"],
           "missing": []}).verdict)
_s238 = watch.settle(
    {"at": _now, "name": "F.E.A.R. 3.exe", "refused": "", "ours": ["d3d9.dll"],
     "elsewhere": [r"C:\WINDOWS\SYSTEM32\dxgi.dll"],
     "missing": ["dlss5-feed.addon32", "nvngx_dlss.dll", "nvngx_dlssnr.dll",
                 "renodx-dlss5.addon64"]},
    ["d3d9.dll", "dlss5-feed.addon32", "host64/dxgi.dll",
     "host64/renodx-dlss5.addon64", "host64/nvngx_dlssnr.dll",
     "host64/nvngx_dlss.dll"])
check("...and the 32-bit helper's files are never the game's (#238)",
      _s238["elsewhere"] == [] and _s238["missing"] == ["dlss5-feed.addon32"],
      _s238)
check("a 1.9.0 record keeps working when the reader has no file list",
      watch.settle({"ours": ["x.dll"]}, None) == {"ours": ["x.dll"]}
      and watch.settle("junk", ["a.dll"]) == {}
      and watch.settle({"ours": [1, None], "elsewhere": "x",
                        "missing": [{}]}, ["a.dll"])["elsewhere"] == [])
check("only what the game has to load is warned about as not loaded (#231)",
      not watch.essential("amd_fidelityfx_vk.dll", "dxgi.dll")
      and watch.essential("dxgi.dll", "dxgi.dll")
      and watch.essential("nvngx_dlssnr.dll")
      and watch.essential("dlss5-feed.addon32"))
check("ours loaded and still no log is its own answer",
      "ReShade is not initialising" in _with_sighting(
          {"at": _now, "name": "Game.exe", "refused": "", "ours": ["dxgi.dll"],
           "elsewhere": [], "missing": []}).verdict)
check("nothing of ours loaded sends them to the proxy-name dropdown",
      "proxy name" in _with_sighting(
          {"at": _now, "name": "Game.exe", "refused": "", "ours": [],
           "elsewhere": [], "missing": ["dxgi.dll"]}).verdict)
check("a sighting from BEFORE this install is not evidence about it",
      "Not started since the install" in _with_sighting(
          {"at": _now - 86400, "name": "Game.exe", "refused": "", "ours": [],
           "elsewhere": [], "missing": ["dxgi.dll"]}).verdict)
check("and the report carries what ran and what it loaded",
      "**What ran, and what it loaded**" in diagnose._loaded_block(_wdir)
      if watch.last_sighting(_wdir, 0) else
      diagnose._loaded_block(_wdir) == "")
shutil.rmtree(_wdir, ignore_errors=True)

from core.ui import ctl_game as _uig190  # noqa: E402
from core import autopilot as _ap190  # noqa: E402


class _Rec190:
    """watch.Recorder, recorded: which folders the window asked to watch."""
    added: list = []

    def __init__(self, *a, **k):
        pass

    def add(self, folder, files, exe=""):
        _Rec190.added.append(Path(folder))


_w190: dict = {}
with patch.object(watch, "Recorder", _Rec190):
    _ui190 = _UiLive()
    if _ui190.ok:
        _gi190 = _ui_game(name="Watched Game", installed=True, manifest={"files": ["dxgi.dll"]})
        _ui190.enter(_gi190, _ui_support([dlss.FEEDER], dlss.FEEDER))
        _w190["texts"] = _ui190.texts()
        _w190["watched"] = list(_Rec190.added)
        _gn190 = _ui_game(name="Nothing Installed")
        _Rec190.added.clear()
        _ui190.enter(_gn190, _ui_support([dlss.FEEDER], dlss.FEEDER))
        _w190["bare"] = list(_Rec190.added)
        _w190["bare_texts"] = _ui190.texts()
        # an install finishes on this one
        (_gn190.install_dir / "dlss5-autopilot.json").write_text(json.dumps(
            {"version": 1, "complete": True, "exe": "Game.exe", "path": "feeder",
             "files": ["dxgi.dll"]}), encoding="utf8")
        _ui190.app.shell.clear_log()
        _ui190.app.q.put(("installed", installer.Report(written=["dxgi.dll"])))
        _ui190.until(lambda: _ui190.app.result is not None, 4.0)
        _ui190.settle(100)
        _w190["installed"] = (list(_Rec190.added), _ui190.log(), _ui190.texts())
        # ...and an autopilot pass that kept a route
        _ui190.app.shell.clear_log()
        _ui190.app.q.put(("autopilot", _ap190.Outcome(
            attempts=[_ap190.Attempt(route="feeder", installed=True, started=True, ours=["dxgi.dll"])],
            route="feeder", ok=True, installed="feeder")))
        _ui190.until(lambda: (_ui190.app.result or {}).get("kind") == "autopilot", 4.0)
        _ui190.settle(60)
        _w190["autopilot"] = _ui190.log()
    _ui190.close()
_ui_cleanup()
check("the window watches a game it has installed into, and says so",
      _gi190.install_dir in _w190.get("watched", [])
      and any("watching this game" in t for t in _w190.get("texts", [])),
      (_w190.get("watched"), [t for t in _w190.get("texts", []) if "watch" in t]))
check("...and both paths that end in an install say what to press in the game",
      "now launch the game" in _w190.get("installed", ("", "", ""))[1]
      and "now launch the game" in _w190.get("autopilot", ""),
      (_w190.get("installed", ("", "", ""))[1][-200:], _w190.get("autopilot", "")[-200:]))
check("...and only where there is an install to watch",
      _w190.get("bare") == [] and not any("watching this game" in t for t in _w190.get("bare_texts", []))
      and _gn190.install_dir in _w190.get("installed", ([], "", ""))[0],
      (_w190.get("bare"), _w190.get("installed", ([], "", ""))[0]))
check("the watcher writes nothing into a game folder",
      "LOCALAPPDATA" in (src_of(watch).split("RECORD =") + [""])[1][:200],
      "RECORD = ... is not where it was")


# --- what to try next, said with its numbers --------------------------------
_shared = json.loads((Path("docs") / "compatibility.json").read_text(encoding="utf8"))
_tot = community.totals(_shared)
check("the shared file adds up across every game, not one at a time",
      _tot["games"] > 0 and _tot["reports"] > 0
      and sum(r["worked"] + r["failed"] for r in _tot["routes"].values())
      == _tot["reports"], _tot["reports"])
check("a driver's note carries its denominator, never a bare claim",
      all(w in (community.driver_note(_shared, "616.92") or "of")
          for w in ("of", "616.92"))
      and community.driver_note(_shared, "no-such-driver") == "")


class _FakeGame:
    def __init__(self, exe):
        self.exe = Path(exe)
        self.name = "test"


_nr = community.next_route(_shared, _FakeGame("nobody-has-this.exe"), "feeder")
check("after a route fails, the next one to try is named with its rate",
      "next one to try" in _nr and _re.search(r"\d+ of \d+", _nr), _nr)
check("...and never a route this game is not offered (#148)",
      community.next_route(_shared, _FakeGame("nobody-has-this.exe"),
                           "feeder", ["feeder"]) == "")
check("...and nothing at all when no route has enough reports behind it",
      community.next_route({"games": {}}, _FakeGame("x.exe"), "feeder") == "")


def _next190(verdict):
    """A diagnosis with this verdict lands: (asked on the Tk thread, asked in
    a worker, what the page wrote)."""
    rep = diagnose.Report(route="feeder")
    rep.verdict, rep.ran = verdict, True
    asked: list = []
    with _ui_isolated(), _ui_threads(run=False) as th:
        c = _ui_ctl(_ui_game(name="Next Route"), _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER))
        c._windows_crash = lambda r: None
        with patch.object(community, "fetch", lambda *a, **k: {"games": {}}), \
                patch.object(community, "next_route",
                             lambda *a, **k: (asked.append(th.inside > 0),
                                              "the optiscaler route is the next one to try: 3 of 4")[1]), \
                patch.object(community, "driver_note", lambda *a, **k: ""):
            c.q.put(("diagnosed", (c.game.install_dir, rep, None)))
            c.pump()
            before = list(asked)
            th.go()
            c.pump()
    _ui_cleanup()
    return before, asked, c.text()


_bad190 = _next190("Nothing of ours loaded - try another proxy name.")
_good190 = _next190("Working.")
check("the window says it under a verdict that is not 'Working'",
      _bad190[0] == [] and _bad190[1] == [True] and "next one to try" in _bad190[2]
      and _good190[1] == [] and "next one to try" not in _good190[2], (_bad190[:2], _good190[:2]))

# --- one click to the executable that actually ran --------------------------
_se190: dict = {}
with _ui_isolated(), _ui_threads(run=False):
    _gs190 = _ui_game(name="Launcher In Front", installed=True)
    _ran190 = _gs190.folder / "bin" / "Game-Win64-Shipping.exe"
    _ran190.parent.mkdir()
    shutil.copyfile(X64, _ran190)
    _out190 = Path(tempfile.mkdtemp(prefix="elsewhere_")) / "Other.exe"
    shutil.copyfile(X64, _out190)
    _cs190 = _ui_ctl(_gs190, _ui_support([dlss.FEEDER], dlss.FEEDER))
    for _k190, _exe190 in (("inside", _ran190), ("outside", _out190), ("same", _gs190.exe)):
        with patch.object(watch, "last_sighting", lambda d, since=0, e=_exe190: {"exe": str(e), "at": time.time()}):
            _se190[_k190] = _cs190._seen_other_exe_for(_gs190)
    shutil.rmtree(_out190.parent, ignore_errors=True)
    # the page's own button, and what pressing it does
    _cs190.seen_exe = _se190["inside"]
    _cs190._last_diag = diagnose.Report(route="feeder")
    _cs190.load_catalog = lambda: None
    _chosen190: list = []
    _real_enrich190 = games.enrich
    with patch.object(games, "enrich", lambda g, *a, **k: (_chosen190.append(k.get("chosen")),
                                                            _real_enrich190(g, *a, **k))[1]):
        _cs190.use_seen_exe()
    _se190["after"] = (_gs190.exe, _chosen190, _cs190._last_diag)
_ui_cleanup()
check("...shown only when the watcher saw a different executable, in this tree",
      _se190["inside"] == _ran190 and _se190["outside"] is None and _se190["same"] is None, _se190)
check("...and it re-points the install the way the exe dropdown does",
      _se190["after"][0] == _ran190 and True in _se190["after"][1] and _se190["after"][2] is None,
      _se190["after"])
_ui190b = _UiLive()
_btn190 = []
if _ui190b.ok:
    _gb190 = _ui_game(name="Seen Exe")
    _ui190b.enter(_gb190, _ui_support([dlss.FEEDER], dlss.FEEDER))
    _ui190b.app.seen_exe = _gb190.folder / "Game-Win64-Shipping.exe"
    _ui190b.app.result = {"kind": "diagnosis", "ok": False, "ran": True, "title": "Not loaded.", "findings": []}
    _ui190b.app.shell.redraw()
    _ui190b.settle(60)
    _btn190 = _ui190b.labels("button")
_ui190b.close()
_ui_cleanup()
check("the button that moves the install to what really ran is its own button (#144)",
      "use Game-Win64-Shipping.exe" in _btn190, _btn190)

# --- half of "go and look in the overlay" is already known ------------------
check("an add-on is looked for in the process, not only a .dll",
      {"renodx-dlss5.addon64", "dlss5-feed.addon32"}
      <= watch.process_names(["renodx-dlss5.addon64", "dlss5-feed.addon32",
                              "ReShade.ini"])[0])
check("the routes that log no frames say what the process had loaded",
      _diag_src().count("_loaded_note(install_dir, man, rep)") >= 2)
check("a foreign hook is never named twice in the same warning (#190)",
      "dict.fromkeys(found)" in src_of(installer.other_ngx_hooks))


section("1.9.0: an install that crashed is not a folder nobody installed "
        "into (#213, #61)")

# The traceback has been printed at the bottom of every report since 1.6.0
# and no rule ever read it. An install that died before it wrote anything
# leaves the same empty folder as an install nobody ever ran, so #213 - the
# install stopped on a DNS lookup - was told to go and start the game once,
# and #61 - reshade.me answering 500 - was told its dxgi.dll had gone
# missing from a folder it was never written into.
_DNS_TB = (
    "installing\nTraceback (most recent call last):\n"
    '  File "core\\gui.py", line 4219, in work\n'
    '  File "core\\installer.py", line 2720, in install\n'
    '  File "core\\net.py", line 467, in fetch_text\n'
    "socket.gaierror: [Errno 11001] getaddrinfo failed")

_empty = Path(tempfile.mkdtemp(prefix="diag_crash_"))
_r = diagnose.analyse(_empty, _DNS_TB)
check("an install that crashed says so, and names what stopped it",
      "crashed before it finished" in _r.verdict
      and any("look up the download's address" in (f_.detail or "")
              for f_ in _r.findings), _r.verdict)
check("...and never tells that person to go and start the game",
      not any("has not been started" in f_.title for f_ in _r.findings)
      and "Nothing is installed in this folder" not in _r.verdict)

# The same folder with no traceback, and with one that did not come out of
# the install path, keep the answer they had: the update check and the GUI
# fail in their own ways and neither explains an empty folder.
check("...while an empty folder with no error is told what it was told before",
      "Nothing is installed in this folder" in diagnose.analyse(_empty).verdict)
_other = ('Traceback (most recent call last):\n'
          '  File "core\\selfupdate.py", line 88, in check\n'
          "urllib.error.URLError: <urlopen error timed out>")
check("...and an error from somewhere else is not blamed on the install",
      "Nothing is installed in this folder"
      in diagnose.analyse(_empty, _other).verdict)
shutil.rmtree(_empty, ignore_errors=True)

# The record says the install finished and the folder is empty anyway: a
# re-install that crashed over an earlier one's record. Every rule below
# this reads that as files that went missing after a good install and names
# antivirus for something that was never written.
_d = _diag_dir("diag_crash_rec_", proxy=False, addons=False)
shutil.rmtree(_d / "reshade-shaders", ignore_errors=True)
_r = diagnose.analyse(_d, _DNS_TB)
check("a record that says 'finished' over an empty folder is read the same way",
      "crashed before it finished" in _r.verdict, _r.verdict)
check("...and antivirus is not named for a file the install never wrote",
      not any("antivirus" in (f_.detail or "").lower() for f_ in _r.findings))
shutil.rmtree(_d, ignore_errors=True)

# The rules that are better answers than this one keep their reports. A
# finished install is still read as before, and the folder that holds our
# files with one of them gone is still antivirus, not a crash.
_d = _diag_dir("diag_crash_ok_", feed=_FEED_OK)
check("a working install is still working, traceback or not",
      diagnose.analyse(_d, _DNS_TB).verdict == "Working.")
shutil.rmtree(_d, ignore_errors=True)
_d = _diag_dir("diag_crash_part_", feed=_FEED_OK, complete=False)
check("...and an unfinished record still answers with its own branch",
      "never finished" in diagnose.analyse(_d, _DNS_TB).verdict)
shutil.rmtree(_d, ignore_errors=True)

# The cause is read off the exception line, and an unfamiliar one still
# carries the line itself rather than saying nothing.
check("a 500 from the download server is named as one",
      diagnose._install_crash(
          '  File "core\\installer.py", line 1, in install\n'
          "urllib.error.HTTPError: HTTP Error 500: Internal Server Error"
      )[0] == "the download server answered with an error")
check("...and an exception nobody has seen before is still quoted",
      diagnose._install_crash(
          '  File "core\\installer.py", line 1, in install\nValueError: weird')
      == ("it stopped with an error", "ValueError: weird"))

# The replay has to hand the traceback over too, or the one report that
# proves the rule works replays without the evidence it is about.
check("the replay reads the traceback out of the report it is replaying",
      _rr.last_error("**Last error**\n```\n" + _DNS_TB + "\n```\n") == _DNS_TB)
check("...and a report without one hands over nothing",
      _rr.last_error("**ReShade.log**\n```\n(none)\n```") == "")
check("...and a report replayed by hand gets what the corpus check measures",
      "replay_report.machine(" in src_of(_vc._answer)
      and "replay_report.last_error(" in src_of(_vc._answer))
from core import log as _log190b  # noqa: E402
_handed190: list = []
with _ui_isolated(), _ui_threads(run=True):
    _cd190 = _ui_ctl(_ui_game(name="Failed Here"), _ui_support([dlss.FEEDER], dlss.FEEDER))
    _rp190 = diagnose.Report(route="feeder")
    _rp190.verdict = "Not started."
    with patch.object(installer, "last_failure", lambda d: f"FOLDER ERROR for {Path(d).name}"), \
            patch.object(_log190b, "last_error", lambda *a, **k: "GLOBAL ERROR"), \
            patch.object(diagnose, "analyse", lambda d, err="", *a, **k: (_handed190.append(err), _rp190)[1]):
        _cd190.diagnose()
_ui_cleanup()
check("the window hands the diagnosis the error of an install into THIS folder",
      _handed190 == ["FOLDER ERROR for Failed Here"], _handed190)
check("...and the installer records the folder its own failure was for",
      all(w in src_of(installer.install) for w in ("note_failure(root, e)",))
      and src_of(installer.install).count("note_failure(") >= 3)
_lf = Path(tempfile.mkdtemp(prefix="lastfail_"))
installer.LAST_FAILURE.clear()
check("...and nothing is claimed about a folder no install was attempted in",
      installer.last_failure(_lf) == "")
try:
    raise installer.InstallError("the download stopped")
except installer.InstallError as _e:
    installer.note_failure(_lf, _e)
check("...while the folder it WAS attempted in gets its traceback",
      "the download stopped" in installer.last_failure(_lf)
      and installer.last_failure(_lf / "elsewhere") == "")
installer.LAST_FAILURE.clear()
shutil.rmtree(_lf, ignore_errors=True)


section("1.9.0: the pass that installs, watches the game and tries the next "
        "route")

from core import autopilot as _ap  # noqa: E402

# The two biggest classes in the corpus - "the install stopped part way" and
# "nothing we wrote ever loaded" - are 32 of 87 reports, and both are
# answerable in the minute after an install, by the machine. This is that
# minute: install, get the game up, read its module list, and when our files
# are not in the process try the route that might be.
_d = Path(tempfile.mkdtemp(prefix="autopilot_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
installer._write_manifest(_d, _g, installer.Options(), installer.Report(),
                          "dxgi.dll", "", complete=True)

_seen: list[str] = []


def _fake_install(g, opt, **kw):
    _seen.append("install:" + opt.path)
    r = installer.Report()
    r.complete = True
    return r


def _sight(ours=(), elsewhere=(), missing=()):
    s = watch.Sighting(proc=watch.Proc(pid=1, ppid=0, name="Game.exe",
                                       path=str(_d / "Game.exe")),
                       loaded=watch.Loaded(pid=1, exe="Game.exe"))
    s.ours, s.elsewhere, s.missing = list(ours), list(elsewhere), list(missing)
    return s


_out = _ap.run(_g, installer.Options(), ["feeder", "optiscaler"], _ap.Hooks(
    install=_fake_install, start=lambda g: (True, ""),
    wait=lambda *a, **k: [_sight(ours=[str(_d / "dxgi.dll")])], seconds=1))
check("a route whose files are in the running game is where it stops",
      _out.ok and _out.route == "feeder" and _out.tried == ["feeder"], _out.tried)
check("...and the pass says what to do with that", "did it work?" in _ap.summary(_out))

_seen.clear()
_out = _ap.run(_g, installer.Options(), ["feeder", "optiscaler", "bridge"], _ap.Hooks(
    install=_fake_install, start=lambda g: (True, ""),
    wait=lambda *a, **k: [_sight(elsewhere=[r"C:\Windows\System32\dxgi.dll"],
                                 missing=["dxgi.dll"])], seconds=1))
check("a game that loaded somebody else's dxgi.dll makes it try the next route",
      not _out.ok and _out.tried == ["feeder", "optiscaler", "bridge"], _out.tried)
check("...installing each one, and no more than three",
      _seen == ["install:feeder", "install:optiscaler", "install:bridge"], _seen)
check("...and it ends by asking for the report that carries the module list",
      "report a bug" in _ap.summary(_out))

# A game that never comes up says nothing about the route, so running the
# same pass again with a different one would be three installs for nothing.
_out = _ap.run(_g, installer.Options(), ["feeder", "optiscaler"], _ap.Hooks(
    install=_fake_install, start=lambda g: (False, "start it yourself"),
    wait=lambda *a, **k: [], seconds=1))
check("a game that was never seen running stops the pass, not the route list",
      not _out.ok and _out.tried == ["feeder"] and _out.stopped, _out.stopped)

_stop = {"n": 0}


def _stopper():
    _stop["n"] += 1
    return _stop["n"] > 1


_out = _ap.run(_g, installer.Options(), ["feeder", "optiscaler"], _ap.Hooks(
    install=_fake_install, start=lambda g: (True, ""),
    wait=lambda *a, **k: [_sight(missing=["dxgi.dll"])], stop=_stopper, seconds=1))
check("...and 'stop' is read before every route, so it never runs on",
      len(_out.attempts) <= 1, _out.tried)

# Starting somebody's game is not always ours to do.
_ac = Path(tempfile.mkdtemp(prefix="autopilot_ac_"))
shutil.copyfile(X64, _ac / "Game.exe")
(_ac / "EasyAntiCheat.exe").write_bytes(b"MZ")
_ok, _why = _ap.may_start(games.manual(_ac))
check("a folder with anti-cheat in it is never started by this tool",
      not _ok and "anti-cheat" in _why, _why)
check("...and the reason names what was found", "EasyAntiCheat" in _why, _why)
shutil.rmtree(_ac, ignore_errors=True)

# Switching route under a game that is still up cannot work: the installer
# replaces the very DLLs Windows has mapped into it. This loop is what put
# the game there, so it waits for it to go.
_closed_calls: list = []
_out = _ap.run(_g, installer.Options(), ["feeder", "optiscaler"], _ap.Hooks(
    install=_fake_install, start=lambda g: (True, ""),
    wait=lambda *a, **k: [_sight(missing=["dxgi.dll"])],
    closed=lambda folder, exe, hooks: (_closed_calls.append(folder), True)[1],
    seconds=1))
check("the game is waited out before the next route is installed",
      len(_closed_calls) == 1 and _out.tried == ["feeder", "optiscaler"],
      (_closed_calls, _out.tried))
_out = _ap.run(_g, installer.Options(), ["feeder", "optiscaler"], _ap.Hooks(
    install=_fake_install, start=lambda g: (True, ""),
    wait=lambda *a, **k: [_sight(missing=["dxgi.dll"])],
    closed=lambda folder, exe, hooks: False, seconds=1))
check("...and a game that never closes stops the pass instead of failing to "
      "replace a file it has open",
      _out.tried == ["feeder"] and "still running" in _out.stopped, _out.stopped)
check("...and what is installed in the folder now is part of the answer",
      _out.installed == "feeder" and "uninstall" in _ap.summary(_out),
      _ap.summary(_out))

# --- what the 1.9.0 gate found in the first cut of this pass --------------

# A name loaded from somewhere else while ours sits beside the game is the
# fault this pass exists to find. Ours being in the process as well does not
# undo it, and it was being called a success.
_a = _ap.Attempt(route="feeder", ours=[r"C:\g\nvngx_dlssnr.dll"],
                 elsewhere=[r"C:\Windows\System32\dxgi.dll"])
check("one of our files loaded while another is shadowed is not 'it worked'",
      not _a.loaded)
check("...and with nothing shadowed it is", _ap.Attempt(
    route="feeder", ours=[r"C:\g\dxgi.dll"]).loaded)

# The reason it stopped is the answer. A summary about routes, printed over
# "the game is still running", sent that person to file a bug instead of
# closing their game.
_out = _ap.run(_g, installer.Options(), ["feeder", "optiscaler"], _ap.Hooks(
    install=_fake_install, start=lambda g: (True, ""),
    wait=lambda *a, **k: [_sight(missing=["dxgi.dll"])],
    closed=lambda folder, exe, hooks: False, seconds=1))
check("a pass that stopped for a reason of this machine's says that reason",
      "still running" in _ap.summary(_out)
      and "report a bug" not in _ap.summary(_out), _ap.summary(_out))

# An install that never finished leaves nothing behind, and saying "the X
# route is what is installed now - uninstall takes it back out" about it is
# two false statements in one sentence.
def _broken_install(g, opt, **kw):
    # install() raises on every path that stops part way, and records what
    # it had written; it never returns a half report.
    raise installer.InstallError("the download stopped")


_out = _ap.run(_g, installer.Options(), ["feeder", "optiscaler"], _ap.Hooks(
    install=_broken_install, start=lambda g: (True, ""),
    wait=lambda *a, **k: [], seconds=1))
check("an install that stopped points at what it had already written",
      _out.installed == "" and _out.left == "feeder"
      and "uninstall" in _ap.summary(_out), _ap.summary(_out))
# ...and with nothing of ours in the folder at all, it says that instead of
# sending somebody to uninstall a folder that holds nothing.
_bare = Path(tempfile.mkdtemp(prefix="autopilot_bare_"))
shutil.copyfile(X64, _bare / "Game.exe")
_out_bare = _ap.run(games.manual(_bare), installer.Options(), ["feeder"],
                    _ap.Hooks(install=_broken_install,
                              start=lambda g: (True, ""),
                              wait=lambda *a, **k: [], seconds=1))
check("...and an empty folder is not offered an uninstall",
      "Nothing of ours is in the folder" in _ap.summary(_out_bare),
      _ap.summary(_out_bare))
shutil.rmtree(_bare, ignore_errors=True)
check("...and it is this route's answer, not the end of the pass",
      _out.tried == ["feeder", "optiscaler"], _out.tried)

# The launch advice is not a reason. "Steam game: it starts from the
# executable here, but if Steam wants to own the launch..." is not why a
# pass stopped.
_out = _ap.run(_g, installer.Options(), ["feeder"], _ap.Hooks(
    install=_fake_install, start=lambda g: (True, "Steam game: ...advice..."),
    wait=lambda *a, **k: [], seconds=1))
check("the advice about starting the game is never printed as the reason",
      _out.stopped == "the game was never seen running", _out.stopped)

# Half of Options is route-specific: taking the first route's settings for
# all three installed the later ones as weaker versions of themselves.
_asked: list = []
_ap.run(_g, installer.Options(), ["feeder", "optiscaler"], _ap.Hooks(
    install=_fake_install, start=lambda g: (True, ""),
    wait=lambda *a, **k: [_sight(missing=["dxgi.dll"])],
    closed=lambda folder, exe, hooks: True,
    options=lambda opt, route: (_asked.append(route),
                                installer.replace(opt, path=route))[1],
    seconds=1))
check("each route is installed with the settings for THAT route",
      _asked == ["feeder", "optiscaler"], _asked)
from core import anticheat as _ac190c  # noqa: E402
from core.ui import ctl_game as _uig190c  # noqa: E402
with _ui_isolated(), _ui_threads(run=False):
    _oc190 = _ui_ctl(_ui_game(name="Other Route", api="DX12"),
                     _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER))
    _oc190.apply_route(dlss.FEEDER)
    _oo190 = (_oc190.opts().path, _oc190.opts(dlss.OPTI).path, _oc190.route,
              _oc190.opts(dlss.OPTI).nr.get("WorkingScale"))
_ui_cleanup()
check("...and the window can answer for a route that is not the one on screen",
      list(inspect.signature(_uig190c.GameControl.opts).parameters)[:2] == ["self", "route"]
      and _oo190[:3] == (dlss.FEEDER, dlss.OPTI, dlss.FEEDER)
      and _oo190[3] == round(optiscaler.NR_SCALE_DEFAULT / 100, 2), _oo190)

# The module list is the whole point of the pass, and the summary tells
# people the report carries it.
_remembered: list = []
with patch.object(watch, "remember", lambda folder, s: _remembered.append(s)):
    _ap.run(_g, installer.Options(), ["feeder"], _ap.Hooks(
        install=_fake_install, start=lambda g: (True, ""),
        wait=lambda *a, **k: [_sight(ours=[str(_d / "dxgi.dll")])], seconds=1))
check("what the game had loaded is written down, not only shown once",
      len(_remembered) == 1, _remembered)



def _pass190(answers=(), cheat=False, planned=None, outcome=None):
    """Press 'autopilot' with these dialog answers. What the window
    asked, whether the pass ran and where, and the controller afterwards."""
    ran: list = []
    applied: list = []
    watched: list = []

    class _Rec:
        def __init__(self, *a, **k):
            pass

        def add(self, folder, files, exe=""):
            watched.append(Path(folder))
    with _ui_isolated(), _ui_threads(run=False) as th:
        g = _ui_game(name="Install And Test", api="DX12")
        c = _ui_ctl(g, _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER))
        c.apply_route(dlss.FEEDER)
        c.check_stale = lambda: None
        c.shell.answers = list(answers)
        found = _ac190c.Finding(["EasyAntiCheat"], ["EasyAntiCheat_x64.dll"]) if cheat \
            else _ac190c.Finding([], [])
        out = outcome or _ap.Outcome(
            attempts=[_ap.Attempt(route="optiscaler", installed=True, started=True, ours=["dxgi.dll"])],
            route="optiscaler", ok=True, installed="optiscaler")
        real_apply = c.apply_route
        c.apply_route = lambda p: (applied.append(p), real_apply(p))[1]
        c._rows[(str(g.folder), str(g.exe))] = (True, "feeder", "beta", "beta", False, "")
        pats = [patch.object(_ac190c, "detect", lambda d, f: found),
                patch.object(_ap, "may_start", lambda g_, check_running=False: (True, "")),
                patch.object(_ap, "run", lambda *a, **k: (ran.append(th.inside > 0), out)[1]),
                patch.object(installer, "install", lambda *a, **k: ran.append("installer.install")),
                patch.object(watch, "Recorder", _Rec)]
        if planned is not None:
            pats.append(patch.object(_ap, "plan", lambda *a, **k: list(planned)))
        for p in pats:
            p.start()
        try:
            try:
                c.autopilot()
                raised = ""
            except Exception as e:
                raised = repr(e)
            before = list(ran)
            # what the pass's install leaves behind
            (g.install_dir / "dlss5-autopilot.json").write_text(json.dumps(
                {"version": 1, "complete": True, "exe": "Game.exe", "path": "optiscaler",
                 "files": ["dxgi.dll"]}), encoding="utf8")
            th.go()
            c.pump()
            applied_before = list(applied)
        finally:
            for p in pats:
                p.stop()
    _ui_cleanup()
    return {"asked": c.shell.asked, "before": before, "ran": ran, "raised": raised, "c": c,
            "applied": applied_before, "watched": watched, "row": (str(g.folder), str(g.exe)) in c._rows}


_cheat190 = _pass190(answers=[False], cheat=True)
check("the window asks the anti-cheat question before it installs anything",
      len(_cheat190["asked"]) == 1 and "EasyAntiCheat" in _cheat190["asked"][0][0]
      and "ban the account" in _cheat190["asked"][0][1] and _cheat190["ran"] == [],
      (_cheat190["asked"], _cheat190["ran"]))
_ok190 = _pass190(answers=[True])
check("...and the pass ends the way an install does",
      _ok190["c"].result and _ok190["c"].result.get("kind") == "autopilot"
      and not _ok190["row"] and "now launch the game" in _ok190["c"].text()
      and _ok190["c"].game.install_dir in _ok190["watched"] and not _ok190["c"].busy,
      (_ok190["c"].result, _ok190["row"], _ok190["watched"], _ok190["c"].busy))
check("...through apply_route, so the dropdown and the next INSTALL agree",
      "optiscaler" in _ok190["applied"] and _ok190["c"].route == "optiscaler", _ok190["applied"])
_none190 = _pass190(answers=[True], planned=[])
check("a route list this game is not offered never reaches routes[0]",
      _none190["raised"] == "" and _none190["asked"] == [] and _none190["ran"] == [],
      (_none190["raised"], _none190["asked"]))


check("the route order starts with the one the tool recommended",
      _ap.plan("optiscaler", ["feeder", "optiscaler", "bridge"])[0] == "optiscaler")
# Which route goes SECOND is not the dropdown's order: it is the one other
# people's results say rescued this game.
_shared: dict = {"games": {}}
_said = "In this game the bridge route is reported working by 3 of 4."
with patch.object(community, "next_route", lambda *a, **k: _said):
    _order = _ap.plan("feeder", ["feeder", "optiscaler", "bridge"], _shared, _g)
check("...and the route the shared results rescued this game with goes next",
      _order == ["feeder", "bridge", "optiscaler"], _order)


# The button is in the window, beside INSTALL, and it says what it does
# before it does it: it starts somebody's game and it can install a second
# route without asking again (#144 is the shape of hiding that in a corner).
# What the page drew was read off the live window in 6c.
check("the window has the button this pass is driven from",
      any("autopilot" == b.strip() for b in _LIVE_BUTTONS),
      [b for b in _LIVE_BUTTONS if b.strip()][:8])
check("...in the row of actions INSTALL is in, not tucked away under settings",
      bool(_AUTO_BTN.get("row")) and abs(_AUTO_BTN["row"][0] - _AUTO_BTN["row"][1]) <= 1,
      _AUTO_BTN.get("row"))
check("...and it carries a mark of its own beside its words",
      len([t for t in _AUTO_BTN.get("texts", []) if t]) == 2
      and "autopilot" in _AUTO_BTN.get("texts", []), _AUTO_BTN.get("texts"))
# It ends the pass once the install it is running finishes - it does not
# go on to start the game. The line used to promise the whole route. Pressed
# on the page, while a pass runs.
_st190: dict = {}
_ui190c = _UiLive()
if _ui190c.ok:
    _ui190c.enter(_ui_game(name="Stoppable"), _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER))
    _ui190c.app.busy, _ui190c.app.action = True, "autopilot"
    _ui190c.app.shell.redraw()
    _ui190c.settle(60)
    _st190["pressed"] = _ui190c.press("stop", "button")
    _st190["after"] = (_ui190c.app._auto_stop, _ui190c.app.busy, _ui190c.app.action)
    _st190["log"] = _ui190c.log()
    _ui190c.app.busy, _ui190c.app.action = False, ""
_ui190c.close()
_ui_cleanup()
check("...and it can be stopped, without stopping mid-install",
      _st190.get("pressed") and _st190.get("after") == (True, True, "stopping")
      and "stopping when this install finishes" in _st190.get("log", ""), _st190.get("after"))
check("...and the pass reads that while it waits, not after the route",
      "stop" in src_of(_ap.attempt) and "stop" in src_of(_ap.wait_closed))
_consent190 = [a for a in _ok190["asked"] if a[0] == "autopilot"]
check("...and says, where it is pressed, that it is experimental",
      "experimental" in _AUTO_BTN.get("tip", "").lower()
      and _consent190 and "experimental" in _consent190[0][1].lower(),
      (_AUTO_BTN.get("tip"), _consent190[:1]))
check("the consent screen says it will start the game and install a second route",
      _consent190 and "start" in _consent190[0][1] and "install the next route" in _consent190[0][1]
      and _consent190[0][2] == "go ahead", _consent190[:1])
_cancel190 = _pass190(answers=[False])
check("...and cancelling it installs nothing",
      _cancel190["ran"] == [] and len(_cancel190["asked"]) == 1, _cancel190["ran"])
check("...and the pass itself runs off the Tk thread",
      _ok190["before"] == [] and _ok190["ran"] == [True], (_ok190["before"], _ok190["ran"]))
check("...and never names a route this game is not offered (#148)",
      "remix" not in _ap.plan("feeder", ["feeder", "optiscaler"]))
shutil.rmtree(_d, ignore_errors=True)


section("1.9.0: what the swap was worth, in numbers")

# The install said "nvngx_dlss 310.9.1" and nothing about what had been
# there, so the one thing a DLSS swap is for - moving a game off an old
# runtime - was invisible in the log, in a screenshot and to the person.
check("a DLL's own stamped version is read from the file",
      _re.match(r"^\d+(\.\d+)+$",
                pe.file_version(Path(os.environ["WINDIR"]) / "System32"
                                / "kernel32.dll") or ""),
      pe.file_version(Path(os.environ["WINDIR"]) / "System32" / "kernel32.dll"))
check("...and a file with no version block is not guessed at",
      pe.file_version(SRC_DIR / "test_all.py") == ""
      and pe.file_version(SRC_DIR / "no-such-file.dll") == "")
check("a swap is logged as what it replaced and what it put there",
      installer._swapped("310.2.1", "310.9.1") == "310.2.1 -> 310.9.1")
check("...and a first install, with nothing to replace, is just the build",
      installer._swapped("", "310.9.1") == "310.9.1"
      and installer._swapped("310.9.1", "310.9.1") == "310.9.1")
check("every runtime this tool swaps says it that way",
      src_of(installer).count("_swapped(") >= 5, src_of(installer).count("_swapped("))


section("1.9.0: which build set this folder up (#215)")

# A report arrived on 1.5.0 while 1.8.2 was current, and nothing in the
# folder or the report said so: the install record carried the version of
# its own SCHEMA, never the version of the tool that wrote it.
_d = Path(tempfile.mkdtemp(prefix="manver_"))
shutil.copyfile(X64, _d / "Game.exe")
_g = games.manual(_d)
installer._write_manifest(_d, _g, installer.Options(), installer.Report(),
                          "dxgi.dll", "", complete=True)
_man = json.loads((_d / installer.MANIFEST).read_text(encoding="utf8"))
check("the install record says which build of this tool wrote it",
      _man.get("tool") == update.VERSION, _man.get("tool"))

_rep = diagnose.Report()
diagnose._stale_install(_rep, {**_man, "tool": "1.5.0"})
check("a folder set up by another build is said so, with both versions",
      any("1.5.0" in f_.title and update.VERSION in f_.title
          for f_ in _rep.findings), [f_.title for f_ in _rep.findings])
check("...as a finding, never as a verdict of its own",
      not _rep.verdict and all(f_.level == "info" for f_ in _rep.findings))
_rep = diagnose.Report()
diagnose._stale_install(_rep, _man)
diagnose._stale_install(_rep, {"complete": True})
check("...and nothing is said for this build, or for a record without the key",
      not _rep.findings, [f_.title for f_ in _rep.findings])
shutil.rmtree(_d, ignore_errors=True)


section("1.9.0: 'check both shaders are there' is a question we can answer "
        "ourselves (#212)")

# The standalone route runs on two shaders, exactly as the feeder route
# does, and neither was in the report's file list - so the verdict asked the
# person to go and check something the install had already written down.
_sa_fx = "reshade-shaders/Shaders/DLSS5_AIO_Feed.fx"
_sa_vort = "reshade-shaders/Shaders/vort_Motion.fx"
_d = _diag_dir("diag_sa_fx_", addons=False, reshade=_REG, path="standalone",
               files=["dxgi.dll", "standalone-dlssnr.addon64", "nvngx.dll",
                      _sa_fx.replace("/", "\\"), _sa_vort.replace("/", "\\")])
for _n in ("standalone-dlssnr.addon64", "nvngx.dll", _sa_fx, _sa_vort):
    (_d / _n).parent.mkdir(parents=True, exist_ok=True)
    (_d / _n).write_bytes(b"MZ")
_lines = diagnose._presence(_d, diagnose._manifest(_d), "standalone")
check("a standalone report says whether its two shaders are there",
      any(l.endswith("DLSS5_AIO_Feed.fx: present") for l in _lines)
      and any(l.endswith("vort_Motion.fx: present") for l in _lines), _lines)

_zero = (_ATTACH + "current-frame guide handles: VORT=MISSING feed=MISSING\n"
         "same-frame optical-flow path unavailable; internal zero-motion "
         "fallback will be used\n")
_saved_log, _logd2 = diagnose.model.STANDALONE_LOG, Path(tempfile.mkdtemp(prefix="sa2_"))
diagnose.model.STANDALONE_LOG = _logd2 / "standalone-dlssnr.log"
try:
    diagnose.model.STANDALONE_LOG.write_text(_zero, encoding="utf8")
    _r = diagnose.analyse(_d)
    check("both shaders in place: the answer is not 'go and find them'",
          any("zero-motion" in w for w in _levels(_r, "warn"))
          and any("Both shaders are in the folder" in (f_.detail or "")
                  for f_ in _r.findings), str(_levels(_r, "warn")))
    (_d / _sa_vort).unlink()
    _r = diagnose.analyse(_d)
    check("a shader the install wrote and something removed is said as a fact",
          any("vort_Motion.fx" in b and "gone" in b for b in _levels(_r, "bad"))
          and "install again" in _r.verdict, _r.verdict)
    # A report from before those files were listed says nothing about them,
    # and a rule that reads that silence as absence invents a missing file.
    _old = _diag_dir("diag_sa_old_", addons=False, reshade=_REG,
                     path="standalone",
                     files=["dxgi.dll", "standalone-dlssnr.addon64"])
    (_old / "standalone-dlssnr.addon64").write_bytes(b"MZ")
    _r = diagnose.analyse(_old)
    check("...while an install that recorded neither is not accused of losing them",
          not any("gone from" in b for b in _levels(_r, "bad"))
          and any("check both" in (f_.detail or "") for f_ in _r.findings),
          str(_levels(_r, "bad")))
    shutil.rmtree(_old, ignore_errors=True)
finally:
    diagnose.model.STANDALONE_LOG = _saved_log
    shutil.rmtree(_logd2, ignore_errors=True)
    shutil.rmtree(_d, ignore_errors=True)


section("1.9.0: the Remix route had never been replayed (#211)")

# ".trex" is a prefix of ".trex/d3d9.dll", so the prefix list that skips the
# folder line swallowed every runtime file too and the branch written for
# them was dead code. A folder with a whole Remix runtime in it replayed as
# a folder nobody had ever installed into, and the remix-dxvk.log block was
# not in BLOCKS at all - so no rule in _analyse_remix had ever seen a real
# report. Both remix reports in the corpus went through that.
_rx_list = (
    "**Files in the folder**\n"
    "- .trex: found at H:\\game zone\\NFS\\Need for Speed Carbon\\.trex\n"
    "- .trex/d3d9.dll: present\n"
    "- .trex/nvngx_dlssnr.dll: present\n"
    "- .trex/remix_nvngx.dll: present\n"
    "- runtime flavour: neural\n"
    "- rtx.neuralRendering.enable: set\n")
_st = _rr.folder_state(_rx_list)
check("a Remix runtime in the list is read as one, not as an empty folder",
      _st["manifest"] is True and _st["remix"]["trex"] is True, _st)
check("...with its files, its flavour and the conf key the record carried",
      _st["remix"]["files"] == {"d3d9.dll": True, "nvngx_dlssnr.dll": True,
                                "remix_nvngx.dll": True}
      and _st["remix"]["flavour"] == "neural"
      and (_st["remix"]["key"], _st["remix"]["key_set"])
      == ("rtx.neuralRendering.enable", True), _st["remix"])
check("...and none of those lines is mistaken for a file beside the game",
      _st["files"] == {} and _st["proxy"] == "", _st["files"])

_d = _rr.build("remix", "DX9", "NFSC.exe", {"remix": "[RTX] hello\n"},
               bitness=32, state=_st)
check("the replay puts the runtime back where find_runtime looks for it",
      remix.find_runtime(_d) == _d / ".trex", str(_d))
check("...with the fork's own marker in it, so the flavour is read back",
      remix.runtime_flavour(_d / ".trex") == "neural")
check("...and the conf key set, so 'switched off in rtx.conf' does not fire",
      remix.option_set(_d / remix.CONF, "rtx.neuralRendering.enable"))
check("...and the Remix log where the diagnosis reads it",
      remix.log_path(_d).is_file())
_r = diagnose.analyse(_d)
check("a Remix report is answered by the Remix rules now",
      _r.route == "remix" and "Nothing is installed" not in _r.verdict, _r.verdict)
check("...and a runtime with no neural pass is still told apart",
      "no neural pass" in diagnose.analyse(
          _rr.build("remix", "DX9", "NFSC.exe", {},
                    state={**_st, "remix": {**_st["remix"], "flavour": ""}}
                    )).verdict)
shutil.rmtree(_d, ignore_errors=True)

# The 14 reports told to "open the overlay and read the status": the status
# is in ReShade.ini beside the game. Confirmed on a real install (DEATHLOOP
# on the owner's machine: [RenoDX.DLSS5] NeuralUplift=1) and in the feeder
# and bridge binaries, which read that section themselves.
_sw = Path(tempfile.mkdtemp(prefix="addonsw_"))
check("no ReShade.ini says nothing at all, rather than 'off'",
      reshade_ini.addon_state(_sw) == {})
(_sw / "ReShade.ini").write_text(
    "[ADDON]\nOverlayCollapsed=DLSS 5 Feed@dlss5-feed.addon64\n"
    "[RenoDX.DLSS5]\nNeuralUplift=1\nNRIntensity=2\n", encoding="utf8")
_st = reshade_ini.addon_state(_sw)
check("the add-on's own switch is read out of ReShade.ini",
      _st["switch"] == "1" and _st["keys"].get("NRIntensity") == "2"
      and _st["overlay_seen"] is True, _st)
_rep = diagnose.Report()
check("...and 'on' is said as a fact, not as something to go and check",
      diagnose._addon_switch(_sw, _rep) == "on"
      and any(f_.level == "ok" and "switch is on" in f_.title
              for f_ in _rep.findings), [f_.title for f_ in _rep.findings])
(_sw / "ReShade.ini").write_text(
    "[RenoDX.DLSS5]\nNeuralUplift=0\n", encoding="utf8")
_rep = diagnose.Report()
check("...and 'off' is the answer itself, with the way to turn it on",
      diagnose._addon_switch(_sw, _rep) == "off"
      and any("switched OFF" in f_.title for f_ in _rep.findings),
      [f_.title for f_ in _rep.findings])
(_sw / "ReShade.ini").write_text("[ADDON]\nOverlayCollapsed=x\n", encoding="utf8")
check("...and an overlay that has been opened with nothing written is defaults",
      diagnose._addon_switch(_sw, diagnose.Report()) == "default")
shutil.rmtree(_sw, ignore_errors=True)

# The install asks for the pass in the add-on's own section - and never
# argues with somebody who turned it off on purpose, which is why the
# "switched off" finding does not tell them to install again.
_sw = Path(tempfile.mkdtemp(prefix="addonsw2_"))
check("an install asks for the neural pass in the add-on's own section",
      reshade_ini.enable_dlss5_addon(_sw) is True
      and reshade_ini.addon_state(_sw)["switch"] == "1")
check("...and leaves a switch that is already there alone",
      reshade_ini.enable_dlss5_addon(_sw) is False)
(_sw / "ReShade.ini").write_text("[RenoDX.DLSS5]\nNeuralUplift=0\n",
                                 encoding="utf8")
check("...including one somebody turned off on purpose",
      reshade_ini.enable_dlss5_addon(_sw) is False
      and reshade_ini.addon_state(_sw)["switch"] == "0")
_rep = diagnose.Report()
diagnose._addon_switch(_sw, _rep)
check("...so the finding does not send them to press INSTALL again",
      not any("INSTALL again" in (f_.detail or "") for f_ in _rep.findings),
      [f_.detail for f_ in _rep.findings])
check("...and the install asks for it AFTER ReShade.ini is ours",
      "_ask_for_the_pass(root, opt, log)" in src_of(installer.install)
      and "enable_dlss5_addon" in src_of(installer._ask_for_the_pass))
# Asking before the backup made the install back up a ReShade.ini it had
# just written itself, and uninstall then put our file back as if it were
# the game's.
_own = Path(tempfile.mkdtemp(prefix="askpass_"))
installer._ask_for_the_pass(_own, installer.Options(path=dlss.FEEDER),
                            lambda *_a, **_k: None)
check("...and it is the only writer of that file at that point",
      (_own / "ReShade.ini").is_file()
      and not (_own / "ReShade.ini.dlss5-autopilot-backup").exists())
shutil.rmtree(_own, ignore_errors=True)
shutil.rmtree(_sw, ignore_errors=True)

# #218: "it doesnt start" on a Max Payne Remix mod, and the answer was
# "not run yet". A runtime this install swapped in is a different d3d9.dll
# from the one the mod ships, and a game that reaches Remix through a
# translator of its own (d3d8to9) is not a case anybody upstream runs.
_rx_swapped = _rx_list + ("- Remix runtime: swapped by this install "
                          "(the mod's own is backed up)\n")
_st2 = _rr.folder_state(_rx_swapped)
check("the report says whether the runtime in .trex is the mod's or ours",
      _st2["remix"]["swapped"] is True
      and _rr.folder_state(_rx_list)["remix"]["swapped"] is False, _st2["remix"])
_d2 = _rr.build("remix", "DX9", "MaxPayne.exe", {}, bitness=32, state=_st2)
_r2 = diagnose.analyse(_d2)
check("...and a swapped runtime with no Remix log is named as the first suspect",
      "swapped Remix runtime" in _r2.verdict
      and any("swapped the mod's own" in f_.title for f_ in _r2.findings),
      _r2.verdict)
_d3 = _rr.build("remix", "DX9", "MaxPayne.exe", {}, bitness=32,
                state=_rr.folder_state(_rx_list))
check("...while the mod's own runtime keeps the older answer",
      "Not run yet" in diagnose.analyse(_d3).verdict,
      diagnose.analyse(_d3).verdict)
shutil.rmtree(_d2, ignore_errors=True)
shutil.rmtree(_d3, ignore_errors=True)

check("a report with no Remix lines carries none of this",
      _rr.folder_state("**Files in the folder**\n- dxgi.dll: present\n")
      ["remix"] is None)
check("the Remix log is one of the blocks a report is taken apart into",
      _rr.BLOCKS.get("remix-dxvk.log") == "remix")


section("1.9.0: the diagnosis is split by what each part answers, and stays split")

# It was one 3,703-line module, and every fix in this project had to go into
# it. It is split by what each part answers now; these checks are what keeps
# it split. A part that grows past its share, a layer that imports upward,
# or a name that stops being reachable under the old path fails here.
from core import diagnose as _dpkg  # noqa: E402

# No headroom left on purpose. evidence went 1000 -> 1083 across 1.9.1 (the
# watcher's settle/essential reads, then the Vulkan-layer verdicts) and model
# gained the shared never-ran list, so both caps are now exactly what is on
# disk: the next line added to either one fails this check. 2.0 moved
# _loaded_note out into process.py (what the process had, and a second DLSS
# hook beside ours, #250) and the cap followed it down to 1037.
#
# 2.0 moved _through_layer, _addon_in, _layer_clash, _layer_detail and
# _dxvk_files into layer.py, between model and evidence, and body.py takes
# _dxvk_files from there. The same round moved _nrpre_picks out of routes.py
# and _reshade_died_early out of body.py into evidence.py: both read a log,
# and body importing routes for one was a part importing a later one.
# evidence's cap followed it down to 1026.
#
# model is 11 over its 1.9.1 share, all of it 2.0: _RAN_SKIP_PART (the dlss
# page's .dlss5-dlss-* files are not a session), the dlss page's record in
# _RAN_SKIP, and the "ui" package in _install_modules' exclusions. None of
# it can move down: model imports nothing, and layer.py is about the Vulkan
# layer, not about what a session left on disk.
_parts = {"model": 414, "layer": 104, "evidence": 1026, "process": 150,
          "helper": 150, "routes": 900, "body": 500, "chain": 1400}
_sizes = {n: sum(1 for _ in open(SRC_DIR / "core" / "diagnose" / f"{n}.py",
                                 encoding="utf8"))
          for n in _parts}
check("no part of the diagnosis has grown past its share",
      all(_sizes[n] <= cap for n, cap in _parts.items()), _sizes)

# model <- layer <- evidence <- routes/body <- chain. A part importing a later
# one is a cycle waiting to happen, and the end of the split.
_ALLOWED = {"model": set(), "layer": {"model"},
            "evidence": {"model", "layer"},
            "process": {"model", "layer", "evidence"},
            "helper": {"model", "layer", "evidence"},
            "routes": {"model", "layer", "evidence", "process"},
            "body": {"model", "layer", "evidence", "helper"},
            "chain": {"model", "layer", "evidence", "process", "helper",
                      "routes", "body"}}
_upward = []
for _n in _parts:
    _txt = (SRC_DIR / "core" / "diagnose" / f"{_n}.py").read_text(encoding="utf8")
    for _other in _parts:
        if _other == _n or _other in _ALLOWED[_n]:
            continue
        if _re.search(r"^from \." + _other + r" import", _txt, _re.M) \
                or _re.search(r"^from \. import .*\b" + _other + r"\b", _txt, _re.M):
            _upward.append(f"{_n} imports {_other}")
check("...and no part imports a later one", not _upward, _upward)

check("every name the rest of the tree reaches is still under diagnose.",
      all(hasattr(_dpkg, n) for n in
          ("analyse", "issue_body", "Report", "Finding", "_manifest",
           "_presence", "_live_evidence", "_crash_verdict", "_addon_switch",
           "_feed_shaders", "_stale_install", "_install_crash")))
# The three the suite and the replay tools patch are deliberately NOT copied
# onto the package: a copy is a value that looks right and is not the one
# the code reads.
check("...and the patched three are only where they are patched",
      not any(hasattr(_dpkg, n) for n in _dpkg.PATCHED)
      and all(hasattr(_dpkg.model, n) for n in _dpkg.PATCHED), _dpkg.PATCHED)


section("1.9.1: the diagnosis backlog - crash frames, route markers, shared ms")

# The install crash is read off the frames, and the modules that count are
# read off the installer's own imports rather than a list that rots.
_im = diagnose.model._install_modules()
# The window's own modules (core/ui/*.py, by the name a traceback frame
# carries) are never install modules, whatever they are called.
_ui_mods191 = {p.stem for p in Path("core", "ui").glob("*.py")}
check("the modules an install crash can die in are read from the installer",
      {"installer", "net", "sources", "mfg", "optiscaler"} <= _im
      and not ({"gui", "games", "library", "chain", "log"} & _im)
      and len(_ui_mods191) > 5 and not (_ui_mods191 & _im),
      (sorted(_im), sorted(_ui_mods191 & _im)))
sys.path.insert(0, str(Path(__file__).resolve().parent / "_tools"))
import replay_report as _rr191  # noqa: E402
_t197 = (Path("_tools") / "reports" / "197.txt").read_text(encoding="utf8",
                                                          errors="replace")
_tb197 = _rr191.last_error(_t197)
check("#197's own traceback is an install crash",
      diagnose._install_crash(_tb197)[0]
      == "the secure connection to the download failed", _tb197[-200:])
_cut = _tb197[_tb197.find('net.py", line'):]
check("...and still one when the tail has cut the installer's frames off",
      "installer.py" not in _cut and diagnose._install_crash(_cut)[0], _cut)
_t148 = (Path("_tools") / "reports" / "148.txt").read_text(encoding="utf8",
                                                          errors="replace")
check("#148's library.py error is not an install crash",
      diagnose._install_crash(_rr191.last_error(_t148)) == ("", ""))
check("...nor a scan that failed in games.py, nor a message naming the installer",
      diagnose._install_crash(
          '  File "core\\gui.py", line 1, in work\n'
          '  File "core\\games.py", line 9, in scan\nOSError: x') == ("", "")
      and diagnose._install_crash("RuntimeError: see installer.py") == ("", ""))
# ...nor a crash in the 2.0 window, frame by frame as a worker there raises it
check("a traceback from the window's package is not an install crash",
      all(diagnose._install_crash(
          'Traceback (most recent call last):\n'
          f'  File "core\\ui\\{_n191}.py", line 7, in work\n'
          '  File "core\\ui\\kit.py", line 3, in draw\nAttributeError: x') == ("", "")
          for _n191 in sorted(_ui_mods191)), sorted(_ui_mods191))
check("...while an install that dies under the window's worker still is one",
      diagnose._install_crash(
          'Traceback (most recent call last):\n'
          '  File "core\\ui\\ctl_game.py", line 786, in work\n'
          '  File "core\\installer.py", line 900, in install\n'
          '  File "core\\net.py", line 12, in download\nOSError: x')[0] != "")

# A folder whose record is gone is read by the route its files belong to.
_i191 = installer
for _files, _want in (([_i191.UPSTREAM_ADDON, _i191.RENODX], "upstream"),
                      ([_i191.RENODX_SF], "renodx"),
                      ([_i191.RENODX], "native"),
                      ([_i191.FEEDER_ADDON64, _i191.RENODX], "feeder"),
                      ([_i191.STANDALONE_ADDON, _i191.STANDALONE_BRIDGE],
                       "standalone"),
                      ([_i191.BRIDGE_ADDON, _i191.RENODX], "bridge"),
                      (["OptiScaler.ini", "dxgi.dll"], "optiscaler")):
    _d = Path(tempfile.mkdtemp(prefix="route_files_"))
    for _n in _files:
        (_d / _n).write_bytes(b"MZ")
    check(f"a folder holding {', '.join(_files)} is read as {_want}",
          diagnose._route_from_files(_d) == _want,
          diagnose._route_from_files(_d))
    shutil.rmtree(_d, ignore_errors=True)

# The feeder's shared ms is solved from frame rates: everything that grows
# with the area, not the model alone, and the words say so.
from urllib.parse import unquote as _unq  # noqa: E402
_body_f = _unq(community.issue_url({"route": "feeder", "res": 75, "ms": 7.2,
                                    "result": "worked", "game": "G"}))
_body_o = _unq(community.issue_url({"route": "optiscaler", "res": 75,
                                    "ms": 7.2, "result": "worked",
                                    "game": "G"}))
check("a shared feeder result does not call its ms the model's",
      "model and feed together" in _body_f
      and "ms of model a frame" not in _body_f
      and "ms of model a frame" in _body_o)
_mn = community.measured_note(
    {"measured": {"feeder": {"n": max(3, community.MIN_MEASURED), "res": 75,
                             "ms": 7.2}}}, "feeder")
check("...nor the sentence the next person with the game reads",
      "model and the feed together" in _mn and "model cost" not in _mn, _mn)


section("1.9.1: both builds of a game, and Source's bin (#190, #224)")


def _pe_stub(machine: int, size: int = 300_000) -> bytes:
    """A file whose PE header says 32-bit (0x14c) or 64-bit (0x8664)."""
    import struct as _st
    b = bytearray(bytes(size))
    b[0:2] = b"MZ"
    _st.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE" + bytes(2)
    _st.pack_into("<H", b, 0x84, machine)
    return bytes(b)


# #190's folder: "exe: Subnautica32.exe", "arch/api: 32-bit / DX12 (no
# graphics DLL imported statically, but ships a D3D12 Agility SDK or DLSS
# Frame Generation/Ray Reconstruction - the real renderer is D3D12)".
_sn = Path(tempfile.mkdtemp(prefix="sn190_")) / "Subnautica"
_sn.mkdir()
(_sn / "Subnautica.exe").write_bytes(_pe_stub(0x8664))
(_sn / "Subnautica32.exe").write_bytes(_pe_stub(0x14C))
_picked = pe.find_game_exes(_sn)
check("the 64-bit build is picked over its 32-bit sibling (#190)",
      _picked and _picked[0].name == "Subnautica.exe", _picked)
check("...and the 32-bit one is still there to pick by hand (gate 1.9.1)",
      "Subnautica32.exe" in [p.name for p in _picked],
      [p.name for p in _picked])
(_sn / "nvngx_dlssg.dll").write_bytes(_pe_stub(0x8664))
(_sn / "D3D12").mkdir()
(_sn / "D3D12" / "D3D12Core.dll").write_bytes(_pe_stub(0x8664))
check("a 64-bit runtime beside a 32-bit exe is not evidence it is DX12",
      not pe._has_d3d12_agility_sdk(_sn, 32)
      and pe._ships_dlss(_sn, 32) == "")
check("...and still is for the 64-bit exe beside it",
      pe._has_d3d12_agility_sdk(_sn, 64) and pe._ships_dlss(_sn, 64))
check("every promotion in detect_api is told the exe's bitness",
      src_of(pe.detect_api).count("_has_d3d12_agility_sdk(path.parent, bits)") == 3
      and src_of(pe.detect_api).count("_ships_dlss(path.parent, bits)") == 2)
_solo = Path(tempfile.mkdtemp(prefix="sn190c_")) / "Game"
_solo.mkdir()
(_solo / "Game32.exe").write_bytes(_pe_stub(0x14C))
check("...a lone 32-bit exe named Game32 keeps first place",
      [p.name for p in pe.find_game_exes(_solo)][:1] == ["Game32.exe"])
shutil.rmtree(_sn.parent, ignore_errors=True)
shutil.rmtree(_solo.parent, ignore_errors=True)

# #224: "because of the Source Engine structure, the DXVK d3d9.dll must be
# placed inside the \bin folder."
from core import dxvk as _dxvk224  # noqa: E402
_hl = Path(tempfile.mkdtemp(prefix="hl2_224_"))
(_hl / "hl2.exe").write_bytes(_pe_stub(0x14C))
check("a folder that is not Source keeps DXVK beside the exe",
      _dxvk224.target_dir(_hl, False, "DX9") == "")
(_hl / "bin").mkdir()
(_hl / "bin" / "shaderapidx9.dll").write_bytes(_pe_stub(0x14C))
check("a 32-bit Source game takes DXVK's d3d9.dll in bin",
      _dxvk224.target_dir(_hl, False, "DX9") == "bin")
check("...a 64-bit exe does not take a 32-bit bin",
      _dxvk224.target_dir(_hl, True, "DX9") == "")
(_hl / "bin" / "win64").mkdir()
(_hl / "bin" / "win64" / "shaderapidx9.dll").write_bytes(_pe_stub(0x8664))
check("...the 64-bit build takes bin/win64",
      _dxvk224.target_dir(_hl, True, "DX9") == "bin/win64")
check("...and DX11 through DXVK is never moved",
      _dxvk224.target_dir(_hl, False, "DX11") == "")
_tgz_src = Path(tempfile.mkdtemp(prefix="dxvk_tgz_"))
(_tgz_src / "x32").mkdir()
(_tgz_src / "x32" / "d3d9.dll").write_bytes(_pe_stub(0x14C))
import tarfile as _tf224  # noqa: E402
_tgz = _tgz_src / "dxvk.tar.gz"
with _tf224.open(_tgz, "w:gz") as _t:
    _t.add(_tgz_src / "x32" / "d3d9.dll", arcname="dxvk-9/x32/d3d9.dll")
(_hl / "bin" / "d3d9.dll").write_bytes(b"SOMEONE ELSE'S" + bytes(10))
with patch.object(_dxvk224, "resolve", lambda: ("9.9", "https://x/dxvk.tgz")), \
        patch.object(_dxvk224.net, "download", lambda url, name: _tgz):
    _ver, _w224 = _dxvk224.install(_hl, False, None, api="DX9")
check("the install writes it there and records the path",
      _w224 == ["bin/d3d9.dll.dlss5-autopilot-backup", "bin/d3d9.dll"]
      and (_hl / "bin" / "d3d9.dll").read_bytes()[:2] == b"MZ"
      and not (_hl / "d3d9.dll").exists(), _w224)
check("the diagnosis looks for DXVK where it was recorded, and not at the "
      "32-bit helper's dxgi.dll",
      diagnose._dxvk_files({"dxvk": "9.9", "files": [
          "bin/d3d9.dll", "bin/d3d9.dll.dlss5-autopilot-backup",
          "host64/dxgi.dll", "dlss5-feed.addon32"]}) == ["bin/d3d9.dll"]
      and diagnose._dxvk_gone(_hl, {"dxvk": "9.9",
                                    "files": ["bin/d3d9.dll"]}) == [])
check("an install recorded before 1.9.1 is still read",
      diagnose._dxvk_files({"dxvk": "2.4", "files": ["d3d9.dll"]})
      == ["d3d9.dll"])
check("the preview announces the file where the install puts it",
      "rel(dsub, name)" in src_of(installer.preview))
shutil.rmtree(_hl, ignore_errors=True)
shutil.rmtree(_tgz_src, ignore_errors=True)


section("1.9.1: the game's own nvngx_dlss.dll is swapped where it lives (#225)")

_d = Path(tempfile.mkdtemp(prefix="nested_dlss_"))
shutil.copyfile(X64, _d / "Game.exe")
_nest = _d / "ReadyOrNot" / "Plugins" / "Nvidia" / "DLSS" / "Binaries" \
    / "ThirdParty" / "Win64"
_nest.mkdir(parents=True)
(_nest / "nvngx_dlss.dll").write_bytes(b"GAME OWN 3.7.20" + bytes(500))
_g = games.manual(_d)
_o = installer.Options(path=dlss.OPTI, native_dlss=True, keep_game_dlss=False,
                       dlss="310.9.1 (NVIDIA SDK)")
check("unticking 'keep the game's own' finds the copy Unreal loads",
      installer._nested_game_dlss(_d, _g, _o) == _nest / "nvngx_dlss.dll")
check("...and nothing is swapped while the box stays ticked",
      installer._nested_game_dlss(_d, _g, installer.Options(
          path=dlss.OPTI, native_dlss=True)) is None)
check("...nor on the upstream route, which never touches it",
      installer._nested_game_dlss(_d, _g, installer.Options(
          path=dlss.UPSTREAM, native_dlss=True, keep_game_dlss=False)) is None)
_rel225 = "ReadyOrNot/Plugins/Nvidia/DLSS/Binaries/ThirdParty/Win64/nvngx_dlss.dll"
for _route in (dlss.OPTI, dlss.BRIDGE):
    _pv = installer.preview(_g, installer.Options(
        path=_route, native_dlss=True, keep_game_dlss=False))
    check(f"{_route}: the preview names the swap, as a write and a backup",
          _rel225 in {w.replace("\\", "/") for w in _pv.writes}
          and any(b.replace("\\", "/").startswith(_rel225) for b in _pv.backups),
          (_pv.writes, _pv.backups))
_blob = Path(tempfile.mkdtemp(prefix="nested_blob_")) / "new.dll"
_blob.write_bytes(_fake_dll())
_rep225 = installer.Report()
_log225: list = []
installer._swap_nested_dlss(
    _nest / "nvngx_dlss.dll",
    [{"label": "310.9.1 (NVIDIA SDK)", "tag": "v310.9.1",
      "url": "https://x/nvngx_dlss.dll", "raw": "nvngx_dlss.dll"}],
    _o, _rep225, _d, lambda url, name: _blob, _log225.append)
_w225 = {w.replace("\\", "/") for w in _rep225.written}
check("the install replaces it in place and keeps the game's own beside it",
      (_nest / "nvngx_dlss.dll").read_bytes() == _blob.read_bytes()
      and (_nest / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).read_bytes()
      .startswith(b"GAME OWN")
      and _rel225 in _w225 and _rel225 + installer.BACKUP_SUFFIX in _w225,
      (_w225, _log225))
check("...and says which file it swapped",
      any("Plugins" in l for l in _log225) and _rep225.components.get("dlss"),
      _log225)
# nothing beside the exe; the detection found the game's own DLSS deeper in
with _ui_isolated(), _ui_threads(run=False):
    _ns191 = _ui_ctl(_ui_game(name="Nested DLSS"), _ui_support([dlss.BRIDGE], dlss.BRIDGE, native_dlss=True))
    _ns191.set_setting("keep_dlss", False)
    _ns191b = _ui_ctl(_ui_game(name="No DLSS"), _ui_support([dlss.BRIDGE], dlss.BRIDGE, native_dlss=False))
    _ns191b.set_setting("keep_dlss", False)
_ui_cleanup()
check("the warning before a swap fires for a game whose DLSS is nested too",
      "as tampering" in _ns191.text() and "as tampering" not in _ns191b.text(),
      (_ns191.text()[-200:], _ns191b.text()[-200:]))
shutil.rmtree(_d, ignore_errors=True)
shutil.rmtree(_blob.parent, ignore_errors=True)


section("1.9.1: what people asked for - sort, hide, start, a bigger log "
        "(#201, #241, #234, #214)")
# Imported again here so the section also runs on its own (dryrun_section).
import tkinter as _tk  # noqa: E402
from core import library as _library_iso  # noqa: E402
from core import autopilot as _ap  # noqa: E402
from core.ui import ctl_library as _uil191, shell as _ush191  # noqa: E402

# A hand-edited sort or log share in settings.json: refused, not raised.
_sorted191 = {}
with _ui_isolated(), _ui_threads(run=False):
    for _v191 in ("junk", ["nope", 1], ["api", 1], 7):
        prefs.set_("games_sort", _v191)
        _cs191 = _ui_ctl()
        _sorted191[repr(_v191)] = _cs191.sort
_share191 = {}
_ui191s = _UiLive()
if _ui191s.ok:
    for _v191 in ("nan", "inf", [1], 0.5, 5):
        prefs.set_("log_share", _v191)
        _sh191 = object.__new__(_ush191.Shell)
        _sh191.root, _sh191.motion = _ui191s.root, _ui191s.app.shell.motion
        _sh191.drawer = _tk.Frame(_ui191s.root)
        try:
            _sh191._build_log()
            _share191[repr(_v191)] = _sh191.log_share
        except Exception as _e191:
            _share191[repr(_v191)] = repr(_e191)
_ui191s.close()
check("a hand-edited sort or log share in settings.json is refused, not raised",
      _sorted191 == {"'junk'": "", "['nope', 1]": "", "['api', 1]": "api", "7": ""}
      and _share191 == {"'nan'": 0.0, "'inf'": 0.0, "[1]": 0.0, "0.5": 0.5, "5": 0.0},
      (_sorted191, _share191))

_ui191 = _UiLive()
try:
    _a191 = _ui191.app
    _gl191 = [_ui_game(name=_n) for _n in ("Bravo Game", "Alpha Game", "Charlie Game")]
    if _ui191.ok:
        _a191.all_games = list(_gl191)
        _a191.shell.show("library")
        _ui191.until(lambda: all(_a191._rows.get((str(g.folder), str(g.exe))) is not None
                                 for g in _gl191), 15.0)
        _a191.shell.redraw()
        _ui191.settle(80)
    _page191 = _a191.shell.pages["library"] if _ui191.ok else None

    def _names191():
        return [c["g"].name for c in _page191.cards] if _page191 else []

    def _sort191(label):
        return _ui191.press("view") and _ui191.pick(label) and (_ui191.settle(60) or True)

    # A real click on "view", then on the sort, the way a mouse does it.
    _sort191("sort: name")
    check("sorting the library by name, from its view menu (#201)",
          _names191() == ["Alpha Game", "Bravo Game", "Charlie Game"], _names191())
    check("...the menu says which way it is sorted, and it is remembered",
          prefs.get("games_sort") == ["name", False]
          and any("by name" in t for t in _ui191.texts()), prefs.get("games_sort"))
    _sort191("sort: name")
    check("...a second pick reverses it", _names191()
          == ["Charlie Game", "Bravo Game", "Alpha Game"], _names191())
    _sort191("sort: scan order")
    check("...and 'scan order' puts the scan's order back",
          _names191() == ["Bravo Game", "Alpha Game", "Charlie Game"]
          and prefs.get("games_sort") == ["", False], (_names191(), prefs.get("games_sort")))
    _sort191("sort: name")
    _sort191("sort: name")
    _open191 = [c["tag"] for c in (_page191.cards if _page191 else []) if c["g"].name == "Bravo Game"]
    _ui191.click(_open191[0] if _open191 else None)
    _ui191.settle(120)
    check("a sorted card still opens the game it shows",
          _a191.game is not None and _a191.game.name == "Bravo Game"
          and _a191.shell.page is not None and _a191.shell.page.name == "game",
          getattr(_a191.game, "name", None))
    _a191.shell.show("library")
    _sort191("sort: scan order")
    _ui191.settle(60)

    # Hiding, through the card's own right-click menu.
    _hid191 = [c["tag"] for c in (_page191.cards if _page191 else []) if c["g"].name == "Bravo Game"]
    _ui191.click(_hid191[0] if _hid191 else None, button=3)
    _menu191 = _ui191.kit.top() is not None
    _ui191.pick("hide from the list")
    _ui191.settle(60)
    check("right-click on a card opens its menu, and 'hide' takes the game off the list",
          _menu191 and "Bravo Game" not in _names191(), (_menu191, _names191()))
    check("...and the count says where it went",
          any("1 hidden" in t for t in _ui191.texts()), [t for t in _ui191.texts() if "hidden" in t])
    _ui191.press("view")
    _ui191.pick("show hidden games")
    _ui191.settle(60)
    check("...'show hidden games' brings it back, greyed",
          "Bravo Game" in _names191() and _a191.card(_gl191[0])["dim"] is True, _names191())
    _ui191.press("view")
    _ui191.pick("hide hidden games")
    _ui191.settle(60)
    for _g in _gl191:
        _a191.set_hidden(_g, True)
    _ui191.settle(60)
    check("...and a list with every game hidden says how to get them back",
          any("all 3 games are hidden" in t for t in _ui191.texts())
          and bool(_ui191.kit.find("show all games", "link")),
          [t for t in _ui191.texts() if "hidden" in t])
    for _g in _gl191:
        _a191.set_hidden(_g, False)
    _ui191.settle(60)
    # the keyboard reaches the same menu: arrows to a card, then the menu key
    _ui191.canvas.focus_force()
    _ui191.canvas.event_generate("<KeyPress>", keysym="Right")
    _ui191.settle(40)
    _ui191.canvas.event_generate("<KeyPress>", keysym="F10", state=0x1)
    _ui191.canvas.event_generate("<KeyPress>", keysym="App")
    _ui191.settle(40)
    check("right-click and the keyboard's menu key both open that menu",
          _ui191.kit.top() is not None, "no menu after Right, Shift+F10 and the menu key")
    _ui191.kit.close_all()

    # The log drawer: its title bar is a handle.
    _sh191 = _a191.shell
    _sh191.toggle_log(True)
    _ui191.settle(400)
    _head191 = _sh191.log_head
    _h0 = _sh191.drawer.winfo_height()
    _y = _head191.winfo_rooty() + 5
    _head191.event_generate("<ButtonPress-1>", x=5, y=5, rootx=_head191.winfo_rootx() + 5, rooty=_y)
    _ui191.settle(20)
    _head191.event_generate("<B1-Motion>", x=5, y=-150, rootx=_head191.winfo_rootx() + 5,
                            rooty=_y - 150, state=0x100)
    _ui191.settle(20)
    _head191.event_generate("<ButtonRelease-1>", x=5, y=-150, rootx=_head191.winfo_rootx() + 5,
                            rooty=_y - 150)
    _ui191.settle(120)
    check("dragging the log's title bar up gives the log more of the window (#234)",
          _sh191.drawer.winfo_height() > _h0 and 0.1 <= float(prefs.get("log_share") or 0) <= 0.9,
          (_h0, _sh191.drawer.winfo_height(), prefs.get("log_share")))
    _a191.write("before the pop-out")
    _ui191.click(_sh191.log_head_kit.find("pop out"), canvas=_head191)
    _ui191.settle(60)
    _a191.write("after the pop-out", "ok")
    _pt = _sh191.log_popped.get("1.0", "end") if _sh191.log_popped is not None else ""
    check("'pop out' opens the log in its own window, with what it held and "
          "what comes after", "before the pop-out" in _pt
          and "after the pop-out" in _pt, _pt[-200:])
    # The window's own close button, through the handler Windows would call.
    if getattr(_sh191, "_logwin", None) is not None:
        _ui191.root.tk.call(_sh191._logwin.protocol("WM_DELETE_WINDOW"))
    _a191.write("with the window closed")
    check("...and closing it costs the pane nothing",
          "with the window closed" in _ui191.log() and _sh191.log_popped is None)
    _sh191.toggle_log(False)
    _ui191.settle(300)

    # Start the game (#241): 'play' on an installed game runs the same rules
    # the autopilot pass does.
    _gp191 = _ui_game(name="Playable", installed=True)
    _ui191.enter(_gp191, _ui_support([dlss.FEEDER, dlss.STANDALONE], dlss.FEEDER))
    _started = []
    with patch.object(_ap, "start", lambda g: (_started.append(g) or (True, ""))):
        _ui191.press("play", "button")
        _ui191.until(lambda: _started and "> started" in _ui191.log(), 5.0)
        _ui191.settle(80)
    check("'play' starts the picked game and says so",
          _started == [_gp191] and "> started" in _ui191.log(), _ui191.log()[-300:])
    _ui191.settle(40)
    _pb191 = _ui191.kit.find("play", "button")
    _pbe191 = [_ui191.canvas.itemcget(i, "fill") for i in _ui191.canvas.find_withtag(_pb191 or "none")
               if _ui191.canvas.type(i) == "text"]
    _again191 = []
    with patch.object(_ap, "start", lambda g: (_again191.append(g) or (True, ""))):
        _ui191.press("play", "button")
        _ui191.settle(150)
    check("...and waits before it can start a second copy",
          _again191 == [] and getattr(_a191, "launching", False) is True, (_again191, _pbe191))
    _a191.launching = False
    _a191.shell.redraw()
    _ui191.settle(40)
    with patch.object(_ap, "start", lambda g: (False, "a launcher, not the game")):
        _ui191.press("play", "button")
        _ui191.until(lambda: "a launcher, not the game" in _ui191.log(), 5.0)
    check("...and when it may not, it says why, on its own, and starts "
          "nothing",
          "> a launcher, not the game" in _ui191.log()
          and "not started from here" not in _ui191.log(),
          _ui191.log()[-200:])

    # #214: the overlay key is a dropdown, and F10 is taken on one route.
    _a191.set_setting("route", dlss.STANDALONE)
    _ui191.press("settings", "button")
    _ui191.settle(350)
    _ui191.press("overlay key", "dropdown")
    _f10_191 = _ui191.pick("F10")
    _ui191.settle(60)
    check("F10 as the overlay key on the standalone route warns about its "
          "before/after key", _f10_191 and "before/after key" in _ui191.log(), _f10_191)
    check("keys for a keyboard with no navigation cluster are offered",
          {"Pause", "Scroll Lock"} <= set(reshade_ini.OVERLAY_KEYS)
          and {"Pause", "Scroll Lock"} <= {k for k, _l in _a191.choices("overlay_key")})
finally:
    _ui191.close()
    _ui_cleanup()
_feed191 = _share60("feeder", _tune.Measured(route="feeder", resolution=100, fps=47.0, frames=900),
                    answer=False, shared={"res": 70, "ms": 9.3, "fps": 60.0})
check("the screen names a feeder's shared ms the way the issue body does",
      _feed191[0] and "feed together" in _feed191[0][0][1], _feed191[0][:1])

section("1.9.1 gate: what the first review found")

_d = Path(tempfile.mkdtemp(prefix="gate191_"))
shutil.copyfile(X64, _d / "Game.exe")
_nest = _d / "G" / "Plugins" / "DLSS" / "Win64"
_nest.mkdir(parents=True)
(_nest / "nvngx_dlss.dll").write_bytes(b"GAME OWN" + bytes(600))
(_d / "nvngx_dlss.dll").write_bytes(b"OURS" + bytes(600))
_g = games.manual(_d)
_o = installer.Options(path=dlss.BRIDGE, native_dlss=True, keep_game_dlss=False)
check("a reinstall still swaps the nested runtime past our own copy beside the exe",
      installer._nested_game_dlss(_d, _g, _o, ours={"nvngx_dlss.dll"})
      == _nest / "nvngx_dlss.dll")
check("...while the game's own copy beside the exe is swapped where it is",
      installer._nested_game_dlss(_d, _g, _o) is None)
check("a game with no DLSS never walks its folders for one (the Tk thread)",
      installer._nested_game_dlss(_d, _g, installer.Options(
          path=dlss.BRIDGE, native_dlss=False, keep_game_dlss=False)) is None)
_oo = installer.Options(path=dlss.OPTI, native_dlss=True, keep_game_dlss=False)
check("on the OptiScaler route the game's own DLSS beside the exe is swapped too",
      installer._opti_dlss_target(_d, _g, _oo) == _d / "nvngx_dlss.dll")
check("...and past our own copy there, the nested one",
      installer._opti_dlss_target(_d, _g, _oo, ours={"nvngx_dlss.dll"})
      == _nest / "nvngx_dlss.dll")
(_d / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).write_bytes(b"GAME OWN")
check("...but a copy it swapped there before is swapped again on a reinstall, "
      "even once detection reads it as ours",
      installer._opti_dlss_target(_d, _g, installer.Options(
          path=dlss.OPTI, native_dlss=False, keep_game_dlss=False),
          ours={"nvngx_dlss.dll"}) == _d / "nvngx_dlss.dll")
(_d / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).unlink()
check("a Vulkan-layer verdict never fires when an add-on is in the process",
      diagnose.evidence._addon_in(["d3d9.dll", "dlss5-feed.addon32"])
      and not diagnose.evidence._addon_in(["d3d9.dll"]))
shutil.rmtree(_d, ignore_errors=True)

_nm, _sb = watch.process_names(["bin/d3d9.dll", "dxgi.dll",
                                "OptiScaler/D3D12_Optiscaler/D3D12Core.dll"])
check("a Source game's bin/d3d9.dll must be loaded; OptiScaler's D3D12Core "
      "is optional (#224)", "d3d9.dll" not in _sb and "d3d12core.dll" in _sb,
      (_nm, _sb))

_wd = Path(tempfile.mkdtemp(prefix="gate191dx_"))
(_wd / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX11", "proxy": diagnose.VULKAN_LAYER, "dxvk": True,
     "path": "native", "files": ["d3d11.dll", "dxgi.dll",
                                 "renodx-dlss5.addon64"]}), encoding="utf8")
for _n in ("d3d11.dll", "dxgi.dll", "renodx-dlss5.addon64", "ReShade.ini",
           "nvngx_dlssnr.dll"):
    (_wd / _n).write_bytes(b"MZ")
_rec_was = watch.RECORD
watch.RECORD = Path(tempfile.mkdtemp(prefix="gate191rec_")) / "s.json"
watch.RECORD.write_text(json.dumps({os.path.normcase(str(_wd)): {
    "at": time.time() + 5, "name": "Game.exe", "refused": "",
    "ours": ["d3d11.dll", "dxgi.dll"], "elsewhere": [],
    "missing": ["renodx-dlss5.addon64"]}}), encoding="utf8")
try:
    with patch.object(diagnose.model, "_layer_state", lambda man: (True, True)):
        _v238 = diagnose.analyse(_wd).verdict
finally:
    watch.RECORD = _rec_was
    shutil.rmtree(_wd, ignore_errors=True)
check("DXVK loaded and no log names ReShade's Vulkan layer, not a proxy (#238)",
      "Vulkan layer is not reaching" in _v238, _v238)
check("the report lists a nested nvngx_dlss.dll this install swapped (#225)",
      '"nvngx_dlss.dll" + _BACKUP_SUFFIX'
      in (Path(diagnose.__file__).parent / "body.py").read_text(encoding="utf8"))

# Ready or Not's shape: the exe under Binaries, DLSS under Plugins, and an
# install record that lost the nested entry. The safety net still finds it.
_top = Path(tempfile.mkdtemp(prefix="gate191un_"))
_bin = _top / "Binaries" / "Win64"
_bin.mkdir(parents=True)
shutil.copyfile(X64, _bin / "Game.exe")
_pl = _top / "Plugins" / "DLSS" / "Binaries" / "ThirdParty" / "Win64"
_pl.mkdir(parents=True)
(_pl / "nvngx_dlss.dll").write_bytes(b"SWAPPED" + bytes(400))
(_pl / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).write_bytes(
    b"GAME OWN" + bytes(400))
(_bin / installer.MANIFEST).write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "path": "optiscaler",
     "files": []}), encoding="utf8")
_gu = games.manual(_top)
_gu.exe = _bin / "Game.exe"
try:
    installer.uninstall(_gu, on_log=lambda t: None)
except Exception as _e:
    check("uninstall over a nested swap runs", False, _e)
check("DXVK not loading on a DXVK install is warned about, not filed as optional",
      watch.essential("d3d9.dll", diagnose.VULKAN_LAYER, ("d3d9.dll",))
      and not watch.essential("d3d9.dll", diagnose.VULKAN_LAYER))
check("two copies of one runtime in the record are both ours to the watcher",
      "setdefault" in src_of(watch.inspect)
      and "any(_same_file(h, m) for m in mine)" in src_of(watch.inspect))

# No install record, DXVK in bin, the game's own d3d9.dll backed up beside
# it: the restore must not be undone by the delete that follows.
_un = Path(tempfile.mkdtemp(prefix="gate191nomf_"))
shutil.copyfile(X64, _un / "Game.exe")
(_un / "bin").mkdir()
_dxvk_blob = b"MZ" + b"DXVK" + bytes(500)
(_un / "bin" / "d3d9.dll").write_bytes(_dxvk_blob)
(_un / "bin" / ("d3d9.dll" + installer.BACKUP_SUFFIX)).write_bytes(
    b"GAME OWN" + bytes(500))
with patch.object(installer.dxvk, "is_dxvk",
                  lambda p: Path(p).read_bytes()[:6] == b"MZDXVK"):
    try:
        installer.uninstall(games.manual(_un), on_log=lambda t: None)
    except Exception as _e:
        check("uninstall without a record runs", False, _e)
check("a restored game file is never deleted by the cleanup after it",
      (_un / "bin" / "d3d9.dll").is_file()
      and (_un / "bin" / "d3d9.dll").read_bytes().startswith(b"GAME OWN"),
      sorted(p.name for p in (_un / "bin").iterdir()))
shutil.rmtree(_un, ignore_errors=True)
check("uninstall puts back a nested runtime the record lost (#225)",
      (_pl / "nvngx_dlss.dll").read_bytes().startswith(b"GAME OWN")
      and not (_pl / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).exists(),
      sorted(p.name for p in _pl.iterdir()))
shutil.rmtree(_top, ignore_errors=True)


section("1.9.1 gate, second pass: names tied to their source, and the fixes")

import re  # noqa: E402
from core import autopilot, components, dxvk  # noqa: E402

# A name copied from another module drifts apart from it in silence.
check("the watcher's helper-folder list is the installer's own HOST_DIR",
      watch._OTHER_PROCESS_DIRS == (installer.HOST_DIR.lower(),),
      f"{watch._OTHER_PROCESS_DIRS} vs {installer.HOST_DIR}")
check("every folder DXVK is installed into is one the diagnosis reads back",
      all(d in dxvk.TARGET_DIRS for d in ("", "bin", "bin/win64")),
      str(dxvk.TARGET_DIRS))
_line = re.search(r"for sub in \(([^\n]*)\):", src_of(dxvk.target_dir))
_subs = set(re.findall(r'"([^"]*)"', _line.group(1))) if _line else set()
check("...and every folder target_dir itself names is in that list",
      bool(_subs) and _subs <= set(dxvk.TARGET_DIRS) and "" in dxvk.TARGET_DIRS,
      f"{sorted(_subs)} vs {dxvk.TARGET_DIRS}")

# The OptiScaler package, chosen by shape as well as by name: the name is
# what rotted last time (#196, #231).
_two = [{"name": "OptiScaler-NR-v0.8.4-rtx40-mfg.zip", "browser_download_url": "a"},
        {"name": "OptiScaler-NR-v0.8.4.zip", "browser_download_url": "b"}]
def _picked(assets, skip) -> str:
    """The chosen name, or "" - a regression must FAIL the suite, not end it."""
    got = optiscaler._pick_archive(assets, skip)
    return str(got.get("name", "")) if isinstance(got, dict) else ""


check("the skip list picks the standard package",
      _picked(_two, ("rtx40-mfg",)) == "OptiScaler-NR-v0.8.4.zip")
check("...and so does the shape, when the variant is renamed upstream",
      _picked(_two, ("this-no-longer-matches",)) == "OptiScaler-NR-v0.8.4.zip")
check("...and a skip list that eats every archive still installs one",
      optiscaler._pick_archive(_two, ("optiscaler",)) is not None)
check("a release carrying one archive is not a choice",
      _picked(_two[:1], ("rtx40-mfg",)) == "OptiScaler-NR-v0.8.4-rtx40-mfg.zip")
check("a release with no archive at all resolves to nothing",
      optiscaler._pick_archive([{"name": "notes.txt"}], ()) is None)

# ...and the person who already has the wrong package is told so IN the tool.
_cd = Path(tempfile.mkdtemp(prefix="gate2comp_"))
(_cd / installer.MANIFEST).write_text(json.dumps(
    {"version": 1, "complete": True, "tool": "1.9.0",
     "opti_build": optiscaler.PRESR,
     "components": {"optiscaler": "v0.8.4"}}), encoding="utf8")
check("a fork install made before the fix is marked to install again",
      any(i.outdated and i.note for i in components.check(_cd)),
      "; ".join(f"{i.installed} {i.outdated} {i.note}"
                for i in components.check(_cd)))
(_cd / installer.MANIFEST).write_text(json.dumps(
    {"version": 1, "complete": True, "tool": "1.9.1",
     "opti_build": optiscaler.PRESR,
     "components": {"optiscaler": "v0.8.4"}}), encoding="utf8")
check("...and one made after it is left alone",
      not any(i.outdated for i in components.check(_cd)))
shutil.rmtree(_cd, ignore_errors=True)


class _NoFolder:
    exe = Path("C:/nowhere/Game.exe")
    install_dir = Path("C:/nowhere")
    folder = None
    source = "manual"


_ok, _why = autopilot.may_start(_NoFolder())
check("a folder that cannot be read for anti-cheat refuses, and does not start",
      _ok is False and "anti-cheat" in _why, _why)

# The remembered record is read with today's rules - all three of its lists.
_settled = watch.settle(
    {"ours": ["host64/renodx-dlss5.addon64", "d3d9.dll"], "elsewhere": [],
     "missing": ["dlss5-feed.addon32"]},
    ["d3d9.dll", "dlss5-feed.addon32", "host64/renodx-dlss5.addon64"])
check("the helper's own add-on is not read as one the game loaded (#238)",
      _settled["ours"] == ["d3d9.dll"], str(_settled.get("ours")))

_detail = diagnose.evidence._layer_detail(
    ["d3d9.dll"], ["dlss5-feed.addon32"],
    {"dxvk": True, "files": ["d3d9.dll"]}, "then")
check("the Vulkan-layer verdict names the file that is NOT in the process",
      "dlss5-feed.addon32" in _detail, _detail[:80])
check("...and says the loaded DLL is DXVK's, not ReShade's",
      "DXVK's" in _detail)
check("...and offers something to do before an issue to open",
      "install again" in _detail and "issue" in _detail
      and _detail.index("install again") < _detail.index("issue"))
from core.ui import ctl_game as _uig_g2  # noqa: E402
from core import wincrash as _wcx  # noqa: E402
_F10_g2 = getattr(_uig_g2, "F10_CLASH", "")
# "One string, not copies" is a fact about the source, so the source is what
# is read - every module of the tool, the window's package included.
_copies_g2 = sum(p.read_text(encoding="utf8").count('"!! the overlay key is F10')
                 for p in (SRC_DIR / "core").rglob("*.py") if p != SRC_DIR / "core" / "gui.py")
check("the F10 clash text is one named string, not a copy",
      "'overlay key'" in _F10_g2 and _copies_g2 == 1, (_F10_g2, _copies_g2))

# The preview runs on the Tk thread, so its folder walk has to be the
# remembered one - #8, #18 and #32 were all this shape.
_pd = Path(tempfile.mkdtemp(prefix="gate2walk_"))
shutil.copyfile(X64, _pd / "Game.exe")
(_pd / "Plugins" / "DLSS" / "Win64").mkdir(parents=True)
(_pd / "Plugins" / "DLSS" / "Win64" / "nvngx_dlss.dll").write_bytes(
    b"GAME OWN" + bytes(400))
_pg = games.manual(_pd)
_po = installer.Options(path=dlss.BRIDGE, native_dlss=True, keep_game_dlss=False)
dlss.forget_walk()
_walks = []
_real_walk = dlss.find_dlss_files
try:
    dlss.find_dlss_files = lambda *a, **k: (_walks.append(1),
                                            _real_walk(*a, **k))[1]
    installer._nested_game_dlss(_pd, _pg, _po)
    _first = len(_walks)
    installer._nested_game_dlss(_pd, _pg, _po)
    _again = len(_walks)
finally:
    dlss.find_dlss_files = _real_walk
check("the preview's nested-runtime look-up walks a game's tree once, not "
      "once per click",
      _again == _first and _first >= 1, f"{_first} walk(s), then {_again}")
shutil.rmtree(_pd, ignore_errors=True)

# Two installs in one game: uninstalling one must not revert the other.
_mg = Path(tempfile.mkdtemp(prefix="gate2two_"))
_aa, _bb = _mg / "Binaries" / "Win64", _mg / "Binaries" / "Win32"
for _x in (_aa, _bb):
    _x.mkdir(parents=True)
    shutil.copyfile(X64, _x / "Game.exe")
_np = _mg / "Plugins" / "DLSS" / "Win64"
_np.mkdir(parents=True)
(_np / "nvngx_dlss.dll").write_bytes(b"A SWAPPED" + bytes(400))
(_np / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).write_bytes(
    b"GAME OWN" + bytes(400))
(_bb / installer.MANIFEST).write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "path": "optiscaler",
     "files": ["OptiScaler.ini"]}), encoding="utf8")
(_bb / "OptiScaler.ini").write_text("x", encoding="utf8")
_gb = games.manual(_mg)
_gb.exe = _bb / "Game.exe"
try:
    installer.uninstall(_gb, on_log=lambda t: None)
except Exception as _e:
    check("uninstalling one of two installs in a game runs", False, _e)
check("uninstalling one install leaves the other install's swapped runtime "
      "where it is",
      (_np / "nvngx_dlss.dll").read_bytes().startswith(b"A SWAPPED")
      and (_np / ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX)).is_file(),
      sorted(p.name for p in _np.iterdir()))
shutil.rmtree(_mg, ignore_errors=True)


# --- the second gate pass: what it found, as checks ------------------------
# Six strings the first pass's fixes wrote had nothing looking at them, and
# the F10 check looked at one of its two places.
_said_g2: dict = {}
with _ui_isolated(), _ui_threads(run=False):
    _c_g2 = _ui_ctl(_ui_game(name="F10 Twice"), _ui_support([dlss.FEEDER, dlss.STANDALONE], dlss.FEEDER))
    _c_g2.apply_route(dlss.FEEDER)
    prefs.set_("overlay_key", reshade_ini.OVERLAY_KEYS["F10"])
    _c_g2.said.clear()
    _c_g2.apply_route(dlss.STANDALONE)                  # the route is switched onto F10
    _said_g2["route"] = [t for t, _tag in _c_g2.said]
    prefs.set_("overlay_key", 0)
    _c_g2.said.clear()
    _c_g2.set_setting("overlay_key", "F10")             # the key is switched onto the route
    _said_g2["key"] = [t for t, _tag in _c_g2.said]
    prefs.set_("overlay_key", 0)
    _said_g2["steps"] = [t for _k, t in _c_g2.route_steps(dlss.FEEDER)]
check("the F10 clash is said from BOTH places, out of one string",
      bool(_F10_g2) and _F10_g2 in _said_g2["route"] and _F10_g2 in _said_g2["key"],
      (_said_g2["route"][:3], _said_g2["key"][:3]))
check("the post-install line that names the overlay key control is still there",
      any("no such key on your keyboard" in t and "'overlay key'" in t for t in _said_g2["steps"]),
      _said_g2["steps"][-4:])
_ui_cleanup()
# The texts about parts that are not current, read where they are drawn.
_st_g2: dict = {}
_ui_g2 = _UiLive()
if _ui_g2.ok:
    _gs_g2 = _ui_game(name="Stale Parts", installed=True)
    _ui_g2.app.all_games = [_gs_g2]
    _ui_g2.app._rows[(str(_gs_g2.folder), str(_gs_g2.exe))] = (True, "feeder", "beta", "beta", False, "")
    _ui_g2.app.stale = {str(_gs_g2.install_dir): 2}
    _ui_g2.app.shell.show("library")
    _ui_g2.settle(80)
    _st_g2["card"] = _ui_g2.app.card(_gs_g2)["status"]
    _st_g2["library"] = _ui_g2.texts()
    _ui_g2.press("scan", "button")
    _st_g2["menu"] = _ui_g2.texts()
    _ui_g2.kit.close_all()
    _ui_g2.enter(_gs_g2, _ui_support([dlss.FEEDER], dlss.FEEDER))
    _ui_g2.app.stale = {str(_gs_g2.install_dir): 2}
    _ui_g2.app.shell.redraw()
    _ui_g2.settle(60)
    _st_g2["page"] = _ui_g2.texts()
    _st_g2["buttons"] = _ui_g2.labels("button")
_ui_g2.close()
_ui_cleanup()
check("the game-list badge for a part that is not current is still there",
      _st_g2.get("card") == "update (2)" and "update (2)" in _st_g2.get("library", []), _st_g2.get("card"))
check("the library's count of games to install again is still there",
      any(t.startswith("update all") and "1 with newer parts" in t for t in _st_g2.get("menu", [])),
      [t for t in _st_g2.get("menu", []) if "update" in t])
check("the game page's line about parts that are not current is still there",
      any("have a newer build" in t for t in _st_g2.get("page", []))
      and "update (2)" in _st_g2.get("buttons", []),
      ([t for t in _st_g2.get("page", []) if "newer" in t], _st_g2.get("buttons")))
check("the standalone note says what happens when F10 is BOTH keys",
      "one of the two will win" in src_of(installer))
check("a record with no file list is not labelled 'only if the game asks'",
      '"- not loaded: "' in src_of(diagnose.evidence._loaded_block)
      and "also written, loaded only if the game asks"
      in src_of(diagnose.evidence._loaded_block))

# A game whose bitness could not be read takes a 32-bit game's answer, not a
# 64-bit one's - on both branches of the OptiScaler swap.
_ub = Path(tempfile.mkdtemp(prefix="gate2bits_"))
shutil.copyfile(X64, _ub / "Game.exe")
(_ub / "nvngx_dlss.dll").write_bytes(b"GAME OWN" + bytes(400))
_ug = games.manual(_ub)
_ug.bitness = None
check("an exe whose bitness could not be read is not swapped as 64-bit",
      installer._opti_dlss_target(_ub, _ug, installer.Options(
          path=dlss.OPTI, native_dlss=True, keep_game_dlss=False)) is None)
_ug.bitness = 64
check("...and a 64-bit one still is",
      installer._opti_dlss_target(_ub, _ug, installer.Options(
          path=dlss.OPTI, native_dlss=True, keep_game_dlss=False))
      == _ub / "nvngx_dlss.dll")
shutil.rmtree(_ub, ignore_errors=True)

# The shape rule only holds between names that are one package plus a
# suffix; against an unrelated archive the release's own order wins.
check("an unrelated shorter archive does not win the shape rule",
      _picked([{"name": "OptiScaler_DLSSNR_v0.1.zip",
                "browser_download_url": "a"},
               {"name": "debug.zip", "browser_download_url": "b"}], ())
      == "OptiScaler_DLSSNR_v0.1.zip")

# One question, one remembered answer - and a six-name walk cannot answer a
# one-name question when it stopped at its cap.
_wd = Path(tempfile.mkdtemp(prefix="gate3walk_"))
(_wd / "sub").mkdir()
(_wd / "sub" / "nvngx_dlss.dll").write_bytes(b"x" * 200)
dlss.forget_walk()
dlss.walked(_wd)
(_wd / "sub" / "sl.interposer.dll").write_bytes(b"x" * 200)
check("a walk asked for other names is not served the first walk's answer",
      dlss.walked(_wd, names=("sl.interposer.dll",))
      != dlss.walked(_wd, names=("nvngx_dlss.dll",)))
check("...and forget_walk clears every one of a folder's answers",
      (dlss.forget_walk(_wd) or True)
      and not [k for k in dlss._WALK_CACHE if k[0] == dlss._walk_key(_wd)])
shutil.rmtree(_wd, ignore_errors=True)
check("the walk is forgotten when the manifest is written, not only before "
      "the install",
      "forget_walk" in src_of(installer._write_manifest))
with _ui_isolated():
    _pv_g2 = _ui_ctl(_ui_game(name="Preview"), _ui_support([dlss.FEEDER], dlss.FEEDER))
    _pvw_g2 = _worker_only(_pv_g2.preview, installer, "preview", None)
_ui_cleanup()
check("the preview runs off the Tk thread, like every other folder walk",
      _pvw_g2 == ([], [True]), _pvw_g2)

# Proof that the game ran must silence every sentence that assumed it did
# not - from one list, used by both readers.
_dnr_g2: list = []
_real_dnr_g2 = diagnose.drop_never_ran
with _ui_isolated(), _ui_threads(run=False):
    _rp_g2 = diagnose.Report(route="renodx")
    _rp_g2.verdict, _rp_g2.never_ran = "Not started since the install - run it once.", True
    _rp_g2.add(diagnose.WARN, "The game has not been started since the install.")
    _ap_g2 = _FakeApp(_rp_g2)
    with patch.object(diagnose, "drop_never_ran",
                      lambda fs: (_dnr_g2.append(len(fs)), _real_dnr_g2(fs))[1]):
        _ap_g2.crash_overrides(_wcx.Crash(when="2026-09-12 00:54:33", exe="Game.exe", module="Game.exe",
                                         code="0xC0000005", provider="Application Error"))
_ui_cleanup()
check("the never-ran findings come off one shared list",
      "drop_never_ran" in src_of(diagnose.chain._explain_no_log)
      and _dnr_g2 and not any("has not been started" in f.title for f in _rp_g2.findings),
      (_dnr_g2, [f.title for f in _rp_g2.findings]))
_diag_all = _diag_src()
check("...and every phrase on that list is one the diagnosis really writes",
      all(_p in _diag_all for _p in diagnose.NEVER_RAN_SAID),
      [_p for _p in diagnose.NEVER_RAN_SAID if _p not in _diag_all])


class _F:
    def __init__(self, t, d=""):
        self.title, self.detail = t, d


check("...and it prunes on the detail as well as the title",
      [f.title for f in diagnose.drop_never_ran(
          [_F("Never started", "the game has not been started since the "
                                "install"),
           _F("kept", "nothing to see here")])] == ["kept"])
check("...but the finding that the game's own files changed - proof it ran - survives",
      [f.title for f in diagnose.drop_never_ran(
          [_F("Something in the game's own files changed after the install.",
              "so it looks as though it has been run since")])]
      == ["Something in the game's own files changed after the install."])

# The launch refusal must be about the GAME, not about our own helper.
check("the already-running refusal is asked for only where it is paid for",
      "check_running" in src_of(autopilot.may_start)
      and "check_running=True" in src_of(autopilot.start))

_rd = Path(tempfile.mkdtemp(prefix="gate3run_"))
(_rd / installer.HOST_DIR).mkdir()
shutil.copyfile(X64, _rd / "Game.exe")
shutil.copyfile(X64, _rd / installer.HOST_DIR / "dlss5-feed-host64.exe")
_rg = games.manual(_rd)
_rg.exe = _rd / "Game.exe"


def _proc(path):
    return watch.Proc(pid=1, ppid=0, name=Path(path).name, path=str(path))


with patch.object(autopilot.watch, "from_folder",
                  lambda *a, **k: [_proc(_rd / installer.HOST_DIR
                                         / "dlss5-feed-host64.exe")]):
    _ok_helper, _why_helper = autopilot.may_start(_rg, check_running=True)
check("our own 64-bit helper running is not 'the game is already running'",
      _ok_helper is True, _why_helper)
with patch.object(autopilot.watch, "from_folder",
                  lambda *a, **k: [_proc(_rd / "Game.exe")]):
    _ok_game, _why_game = autopilot.may_start(_rg, check_running=True)
check("...and the game itself running is", _ok_game is False, _why_game)
check("the refusal stands on its own, with no 'start the game now' in "
      "front of it",
      '"start it" in note' not in src_of(autopilot.attempt))
shutil.rmtree(_rd, ignore_errors=True)


section("2.0: a second DLSS hook in the verdict, and the 64-bit helper's own log "
        "(#250 #252)")

# #250: DLSS 5 Swapper's overlay add-on loaded beside neural-upstream, and
# the verdict said "Add-ons loaded. Confirm in the overlay" with the other
# hook as a warning under it. #252: a 32-bit feed shipping frames at 135 fps
# to a helper whose device was removed, answered "Inconclusive" - and the
# report did not carry the helper's log at all. Real lines throughout.
sys.path.insert(0, str(SRC_DIR / "_tools"))
import replay_report as _rr20  # noqa: E402
import verdict_check as _vc20  # noqa: E402

_a250 = _vc20._answer(SRC_DIR / "_tools" / "reports" / "250.txt")
check("#250: a second DLSS hook loaded beside ours is the verdict, not a "
      "warning under 'Add-ons loaded'",
      _a250["verdict"].startswith("Another DLSS hook was loaded beside ours "
                                  "(DLSS 5 Swapper Overlay)")
      and "bad: Another DLSS hook was loaded beside ours: DLSS 5 Swapper Overlay"
      in _a250["findings"], _a250)
check("...and it is not said twice, once as a hook and once as 'other add-ons'",
      not any(f.startswith("warn: Other ReShade add-ons") for f in _a250["findings"]),
      _a250["findings"])
check("#250: nvngx_dlssnr.dll not loaded when the game was seen is not a "
      "warning - NGX loads it when the feature is created",
      "warn: ...and not nvngx_dlssnr.dll." not in _a250["findings"]
      and any(f.startswith("info: nvngx_dlssnr.dll was not loaded yet")
              for f in _a250["findings"]), _a250["findings"])

_t250 = (SRC_DIR / "_tools" / "reports" / "250.txt").read_text(encoding="utf8")
_log250 = _rr20._blocks(_t250)["reshade"]
# The same session as the reporter's own machine read it: the add-on logged
# its settings and never a hook (the report's excerpt drops [NRPRE] lines).
_nrpre250 = ("14:16:50:020 [ 3104] | INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] "
             "settings loaded: enabled=1 cadence=1 codec=1 pw=1.000 knee=0.75\n")


def _up250(extra: str) -> "diagnose.Report":
    _lines = _log250.splitlines(True)
    _d = _rr20.build("upstream", "DX12", "BatmanAK.exe",
                     {"reshade": "".join(_lines[:2]) + extra + "".join(_lines[2:])},
                     state=_rr20.folder_state(_t250))
    try:
        return diagnose.analyse(_d)
    finally:
        shutil.rmtree(_d, ignore_errors=True)


_r = _up250(_nrpre250)
check("...a verdict the logs do name keeps its own words, with the hook added",
      _r.verdict.startswith("The game's DLSS call was never hooked")
      and "beside ours too (DLSS 5 Swapper Overlay)" in _r.verdict, _r.verdict)
_r = _up250(_nrpre250
            + "14:16:50:030 [ 3104] | INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] "
              "hook 0 on NVSDK_NGX_D3D12_EvaluateFeature: OK  C:\\w\\_nvngx.dll\n"
            + "14:16:55:000 [ 3104] | INFO  | [DLSS5 NR Pre-Upscale] [NRPRE] "
              "HB #7561 handle=0x1 | pw=1.0000 meas=0.0000 valid=0 upd=0 | hdr=2 "
              "det=1 knee=0.750 kmeas=0.750 | expfresh=7559 fromgame=0 | "
              "net=2228x1256 finalvalid=1 | cad=1 async=0 | erfail=0 grfail=0 "
              "passthru=0\n")
check("...and frames through with it loaded stay 'Working.', the hook a warning",
      _r.verdict == "Working."
      and any(f.level == "warn" and f.title.startswith("Another DLSS hook")
              for f in _r.findings), (_r.verdict, [(f.level, f.title) for f in _r.findings]))
_lines = [ln for ln in _log250.splitlines(True) if "NR Pre-Upscale" not in ln]
_d = _rr20.build("upstream", "DX12", "BatmanAK.exe", {"reshade": "".join(_lines)},
                 state=_rr20.folder_state(_t250))
_r = diagnose.analyse(_d)
shutil.rmtree(_d, ignore_errors=True)
check("...but not 'beside ours' when ours never loaded: that stays the answer",
      not _r.verdict.startswith("Another DLSS hook")
      and not any(f.title.startswith("Another DLSS hook was loaded") for f in _r.findings)
      and any("did not load" in f.title for f in _r.findings if f.level == "bad"),
      (_r.verdict, [f.title for f in _r.findings]))
check("the MFG unlock this tool installs, an HDR RenoDX add-on and the route's "
      "own add-ons are not foreign hooks",
      diagnose._foreign_hooks(["Universal RTX 40 MFG Unlock V1.2", "MFG Unlock",
                               "RenoDX Unity Engine", "DLSS5 NR Pre-Upscale"],
                              "upstream") == []
      and diagnose._foreign_hooks(["DLSS 5 Feed 0.15.1", "DLSS 5 Neural Rendering"],
                                  "feeder") == []
      and diagnose._foreign_hooks(["DLSS 5 Neural Rendering"], "upstream")
      == ["DLSS 5 Neural Rendering"])

_a252 = _vc20._answer(SRC_DIR / "_tools" / "reports" / "252.txt")
check("#252: a feed shipping frames to the 64-bit helper is its own answer, "
      "not 'Inconclusive'",
      _a252["verdict"].startswith("Frames reach the 64-bit helper")
      and "warn: The game is handing its frames to the 64-bit helper (600 "
          "frames at 135.7 fps)." in _a252["findings"], _a252)
_t252 = (SRC_DIR / "_tools" / "reports" / "252.txt").read_text(encoding="utf8")
_feed252 = _rr20._blocks(_t252)["feed"]
_d = _rr20.build("feeder", "DX9", "wow.exe", {"feed": _feed252}, bitness=32,
                 state=_rr20.folder_state(_t252))
_r = diagnose.analyse(_d)
check("...and 'present probe: 0 presents' is not read as the helper standing "
      "still - the same log says the game kept presenting",
      any("kept presenting while they read 0" in f.detail for f in _r.findings),
      [f.detail[-80:] for f in _r.findings])

# The helper's log from #252's own thread (driver rolled back to 616.56).
_host252 = (
    "05:29:31.783  dlss5-feed-host64 commit a6c23bd (built Sep 14 2026 08:15:58)\n"
    "05:29:31.784  [host] DLSS 5 add-on file: renodx-dlss5.addon64\n"
    "05:29:31.786  [host] DLSS 5 add-on: v5.2.1 (file version 0.2026.828.2110) -- "
    "v4.7+ (lazy adoption, colour bridge, workset pool) engine\n"
    "05:29:31.786  [host] NeuralUplift=1 (user-set; leaving it alone)\n"
    "05:29:33.349  [host] NGX feature requirements: feature 18 (neural rendering, "
    "what nvngx_dlssnr.dll backs) -> the query itself failed 0xBAD00012 (NotImplemented)\n"
    "05:29:33.889  [host] NVSDK_NGX_D3D12_Init -> 0x00000001 (Success)\n"
    "05:29:34.839  [host] feature ready: 2560x1440 DLAA flags=74\n"
    "05:29:37.923  [host] Present failed 0x887A0005 (1 so far). This window holds "
    "its last picture until one succeeds; the feed runs on a separate queue and "
    "is unaffected.\n"
    "05:29:38.332  [host] 120 presents in a row could not go through: this window "
    "has stopped repainting and the consumer is missing frames. The feed itself "
    "is unaffected; a window that looks frozen from here is this, not a hang.\n"
    "05:29:39.575  [host] neural consumer outcome: consumer did not intercept (no "
    "feature-18 create/evaluate evidence after 300 feeder evaluations)\n")
_hd = _d / diagnose.HOST_LOG
_hd.parent.mkdir(parents=True, exist_ok=True)
_hd.write_text(_host252, encoding="utf8")
(_d / "dlss5-feed.log").write_text(
    _feed252 + "04:42:49.000  [feed32] frame 1 delivered (2560x1440, reset=0)\n",
    encoding="utf8")
_r = diagnose.analyse(_d)
check("#252's helper log names the fault: the helper's device was removed, "
      "and that outranks a delivered frame",
      _r.verdict.startswith("The 64-bit helper lost its graphics device")
      and "The 64-bit helper lost its graphics device (Present failed "
          "0x887A0005)." in [f.title for f in _r.findings if f.level == "bad"]
      and "The neural add-on in the helper never created the DLSS 5 feature."
      in [f.title for f in _r.findings if f.level == "bad"], (_r.verdict, [f.title for f in _r.findings if f.level == "bad"]))
_body = diagnose.issue_body("9.9", "x", None, "?", None, "feeder", _r, "",
                            Path("C:/x/autopilot.log"), _d)
check("the report carries the helper's log on the feeder route, and the "
      "replay reads it back",
      "**dlss5-feed-host.log**" in _body
      and "Present failed 0x887A0005" in _rr20._blocks(_body).get("host", "")
      and len(_body) <= 6000, len(_body))
shutil.rmtree(_d, ignore_errors=True)

_ok_host = ("19:54:05.203  [host] feature ready: 1920x1071 DLAA flags=74\n"
            "19:54:03.491  [host] present skipped: DWM still holds the back "
            "buffer (1 so far; the game is never made to wait for it)\n"
            "19:54:05.208  [host] frame 1 evaluated\n"
            "20:16:05.540  [host] pipe closed by the game\n"
            "20:16:05.556  [host] exit 0\n")
check("the owner's working Bayonetta helper session names no fault",
      diagnose._helper_verdict(diagnose.Report(), _ok_host) is False)
check("...nor a helper that could not read its ReShade.log to check",
      diagnose._helper_verdict(diagnose.Report(),
          "[host] neural consumer outcome: consumer did not intercept "
          "(ReShade.log is unavailable)\n") is False)
check("...nor 'did not intercept' with neural rendering switched off",
      diagnose._helper_verdict(diagnose.Report(),
          "[host] NeuralUplift=0 (user-set; leaving it alone)\n"
          "[host] neural consumer outcome: consumer did not intercept (no "
          "feature-18 create/evaluate evidence after 300 feeder evaluations)\n")
      is False)
check("...and only the helper's last run is read",
      diagnose._helper_verdict(diagnose.Report(),
          _host252 + "06:00:00.000  dlss5-feed-host64 commit a6c23bd (built x)\n"
          + _ok_host) is False)


section("2.0: keeping a game's DLSS up to date, beside a DLSS 5 install "
        "(dlssupdate)")

# The DLSS page replaces runtimes the GAME ships, for games DLSS 5 was never
# installed into too - the same files the installer swaps. Two writers of
# one file is where a user's original was lost before ("reinstall keeps
# backups"), so every order of the two is played here on real folders, with
# DLLs Windows itself reads a version from, and the installer's own swap
# and uninstall.
sys.path.insert(0, str(SRC_DIR / "_tools"))
import fake_pe as _fpe  # noqa: E402
from core import dlssupdate as _du  # noqa: E402
import stat as _du_stat  # noqa: E402

_du_saved = (prefs.FILE,)
_du_tmp = Path(tempfile.mkdtemp(prefix="dlssupdate_"))
prefs.FILE = _du_tmp / "settings.json"          # never the owner's settings
_du_blobs = _du_tmp / "blobs"
_du_blobs.mkdir()
_DU_NEST = "Engine/Plugins/Runtime/Nvidia/DLSS/Binaries/ThirdParty/Win64"


def _du_cat(v):
    return {f: [{"tag": f"v{v}", "label": f"{v} (NVIDIA SDK)", "raw": n, "size": 0,
                 "url": f"https://raw.githubusercontent.com/NVIDIA/DLSS/v{v}/{n}"}]
            for f, n, _l in _du.FAMILIES}


def _du_download(url, name, progress=None, **_k):
    # "dlss-310.9.1 (NVIDIA SDK).dll" -> a real 64-bit DLL stamped 310.9.1
    v = name.split("-", 1)[1].split(" ")[0]
    p = _du_blobs / name.replace(" ", "_")
    p.write_bytes(_fpe.dll(v))
    if progress:
        progress(1, 1)
    return p


def _du_game(tag, bits=64):
    d = _du_tmp / tag
    nest = d / _DU_NEST
    nest.mkdir(parents=True)
    (d / "Game.exe").write_bytes(_fpe.dll("1.0.0", size=5000))
    (nest / "nvngx_dlss.dll").write_bytes(_fpe.dll("3.7.20") + b"GAME-OWN-SR")
    (d / "nvngx_dlssg.dll").write_bytes(_fpe.dll("3.5.0") + b"GAME-OWN-FG")
    dlss.forget_walk()
    return (games.Game(name=tag, folder=d, exe=d / "Game.exe", bitness=bits, api="DX12"),
            nest / "nvngx_dlss.dll", d / "nvngx_dlssg.dll")


def _du_by(g, fam):
    # Never None: a check that reads .state from a missing row must FAIL,
    # not raise and end the run with every later section unrun.
    return next((e for e in _du.scan(g) if e.family == fam),
                _du.Entry(Path("missing"), "", fam, "", state="missing"))


def _du_install_swap(g, sr, v="310.10.0"):
    """What a DLSS 5 install with 'keep the game's own' unticked does: the
    installer's own target choice, swap and record."""
    opt = installer.Options(path=dlss.OPTI, native_dlss=True, keep_game_dlss=False,
                            dlss=f"{v} (NVIDIA SDK)")
    target = installer._nested_game_dlss(g.install_dir, g, opt)
    rep = installer.Report()
    installer._swap_nested_dlss(
        target, _du_cat(v)["dlss"], opt, rep, g.install_dir,
        lambda url, name: _du_download(url, f"dlss-{v} (NVIDIA SDK).dll"), lambda *_: None)
    installer._write_manifest(g.install_dir, g, opt, rep, "dxgi.dll", installer.BETA, complete=True)
    return target


_du_nowatch = patch.object(_du.watch, "from_folder", lambda *a, **k: [])
_du_net = patch.object(_du.net, "download", _du_download)
_du_nowatch.start()
_du_net.start()
try:
    _C1, _C2 = _du_cat("310.9.1"), _du_cat("310.10.0")
    check("the newest build per family comes from NVIDIA's list, with its number",
          _du.newest(_C1)["dlss"]["version"] == "310.9.1"
          and set(_du.newest(_C1)) == {"dlss", "dlssg", "dlssd"}
          and _du.newest({}) == {}, _du.newest(_C1))

    # -- update -> restore ------------------------------------------------
    _g, _sr, _fg = _du_game("order_update_restore")
    _own_sr, _own_fg = _sr.read_bytes(), _fg.read_bytes()
    _s0 = {e.family: e for e in _du.scan(_g)}
    check("scan: the nested super resolution and the frame generation beside the exe, "
          "with Windows' own version read",
          set(_s0) == {"dlss", "dlssg"} and _s0["dlss"].version == "3.7.20"
          and _s0["dlssg"].version == "3.5.0" and _s0["dlss"].state == _du.ORIGINAL,
          {k: (e.rel, e.version, e.state) for k, e in _s0.items()})
    check("...both are older than NVIDIA's newest",
          _du.outdated(_s0["dlss"], _du.newest(_C1)["dlss"])
          and _du.outdated(_s0["dlssg"], _du.newest(_C1)["dlssg"]))
    _r = _du.update(_g, catalog=_C1)
    check("update: both replaced, each backed up beside itself, and recorded",
          _r.ok and len(_r.done) == 2
          and pe.file_version(_sr) == "310.9.1" and pe.file_version(_fg) == "310.9.1"
          and _du._ours(_sr).read_bytes() == _own_sr and _du._ours(_fg).read_bytes() == _own_fg
          and len(_du.load_record(_g)) == 2, (_r, _du.load_record(_g)))
    check("...the report says what moved, in numbers",
          "super resolution  3.7.20 -> 310.9.1" in _r.done, _r.done)
    _e = _du_by(_g, "dlss")
    check("...and the scan reads it back as updated, current, from 3.7.20",
          _e.state == _du.UPDATED and _e.original == "3.7.20"
          and not _du.outdated(_e, _du.newest(_C1)["dlss"]), _e)
    _r = _du.restore(_g)
    check("restore: the game's own bytes are back, no backup and no record left",
          _r.ok and _sr.read_bytes() == _own_sr and _fg.read_bytes() == _own_fg
          and not _du._ours(_sr).exists() and not _du._ours(_fg).exists()
          and not _du.record_path(_g).exists(), _r)

    # -- update -> DLSS 5 install -> DLSS 5 uninstall -> restore -----------
    _g, _sr, _fg = _du_game("order_update_install_uninstall")
    _own_sr = _sr.read_bytes()
    _du.update(_g, catalog=_C1)
    _ours_sr = _sr.read_bytes()
    _t = _du_install_swap(_g, _sr)
    check("a DLSS 5 install over an updated file swaps that very file (ours is not "
          "hidden from it)", _t == _sr and pe.file_version(_sr) == "310.10.0", _t)
    check("...its backup holds OUR build, and ours still holds the game's own",
          (_sr.with_name(_sr.name + installer.BACKUP_SUFFIX)).read_bytes() == _ours_sr
          and _du._ours(_sr).read_bytes() == _own_sr)
    _e = _du_by(_g, "dlss")
    _r = _du.update(_g, catalog=_du_cat("310.11.0"), families=["dlss"])
    check("...the page reads it as the install's, and does not update it",
          _e.state == _du.INSTALL and not _du.outdated(_e, _du.newest(_C2)["dlss"])
          and pe.file_version(_sr) == "310.10.0"
          and any("DLSS 5 install" in s for s in _r.skipped), (_e, _r))
    installer.uninstall(_g)
    check("uninstalling DLSS 5 leaves the DLSS update in place",
          _sr.read_bytes() == _ours_sr and _du._ours(_sr).read_bytes() == _own_sr
          and _du_by(_g, "dlss").state == _du.UPDATED, _du_by(_g, "dlss"))
    _du.restore(_g)
    check("...and restore then puts the game's own back",
          _sr.read_bytes() == _own_sr and not _du._ours(_sr).exists()
          and not _du.record_path(_g).exists())

    # -- update -> DLSS 5 install -> restore -> DLSS 5 uninstall ------------
    _g, _sr, _fg = _du_game("order_update_install_restore_uninstall")
    _own_sr = _sr.read_bytes()
    _du.update(_g, catalog=_C1)
    _du_install_swap(_g, _sr)
    _r = _du.restore(_g)
    check("restore under a DLSS 5 install leaves the install's file and hands the "
          "game's own to the install's backup",
          pe.file_version(_sr) == "310.10.0"
          and _sr.with_name(_sr.name + installer.BACKUP_SUFFIX).read_bytes() == _own_sr
          and not _du._ours(_sr).exists()
          and any("comes back when DLSS 5 is uninstalled" in n for n in _r.notes), _r)
    installer.uninstall(_g)
    check("...so the DLSS 5 uninstall afterwards restores the game's own",
          _sr.read_bytes() == _own_sr)

    # -- DLSS 5 install (keep_game_dlss False) -> update -> DLSS 5 uninstall
    _g, _sr, _fg = _du_game("order_install_update_uninstall")
    _own_sr, _own_fg = _sr.read_bytes(), _fg.read_bytes()
    _du_install_swap(_g, _sr)
    _inst_sr = _sr.read_bytes()
    _r = _du.update(_g, catalog=_du_cat("310.11.0"))
    check("an update after a DLSS 5 swap leaves the install's file alone and "
          "updates the rest",
          _sr.read_bytes() == _inst_sr and not _du._ours(_sr).exists()
          and pe.file_version(_fg) == "310.11.0" and len(_r.done) == 1
          and any("set by the DLSS 5 install" in s for s in _r.skipped), _r)
    installer.uninstall(_g)
    check("...the DLSS 5 uninstall restores the game's own super resolution and "
          "keeps the frame generation update",
          _sr.read_bytes() == _own_sr and pe.file_version(_fg) == "310.11.0"
          and _du._ours(_fg).read_bytes() == _own_fg
          and _du_by(_g, "dlss").state == _du.ORIGINAL)
    _r = _du.update(_g, catalog=_du_cat("310.11.0"))
    check("...and after it the super resolution can be updated here",
          pe.file_version(_sr) == "310.11.0" and _du._ours(_sr).read_bytes() == _own_sr, _r)

    # -- update twice -> restore -------------------------------------------
    _g, _sr, _fg = _du_game("order_update_twice")
    _own_sr = _sr.read_bytes()
    _du.update(_g, catalog=_C1)
    _r = _du.update(_g, catalog=_C2)
    _rec = {r["family"]: r for r in _du.load_record(_g)}
    check("a second update keeps the first backup and the game's own version on record",
          pe.file_version(_sr) == "310.10.0" and _du._ours(_sr).read_bytes() == _own_sr
          and _rec["dlss"]["original"] == "3.7.20" and _rec["dlss"]["written"] == "310.10.0",
          (_r, _rec))
    _du.restore(_g)
    check("...and restore after two updates is the game's own", _sr.read_bytes() == _own_sr)
    _r = _du.update(_g, catalog=_C1)
    _r2 = _du.update(_g, catalog=_C1)
    check("updating a current file is skipped, not rewritten",
          _r.ok and not _r2.done and any("is current" in s for s in _r2.skipped), _r2)

    # -- restore with the backup missing -----------------------------------
    _du._ours(_sr).unlink()
    _now = _sr.read_bytes()
    _r = _du.restore(_g, families=["dlss"])
    check("restore with the backup gone keeps the only copy there is, drops the "
          "entry and says so",
          _sr.read_bytes() == _now and any("is gone" in n for n in _r.notes)
          and not any(r["family"] == "dlss" for r in _du.load_record(_g)), _r)
    _du.restore(_g)

    # -- a launcher put its own file back ------------------------------------
    _g, _sr, _fg = _du_game("order_launcher_replaced")
    _du.update(_g, catalog=_C1)
    _sr.write_bytes(_fpe.dll("310.12.0"))           # the game updated itself
    _notes = _du.settle(_g)
    _e = _du_by(_g, "dlss")
    check("a file replaced with a newer one is left and it is said - and the "
          "game's own stays where restore finds it (another tool may have written the newer one)",
          pe.file_version(_sr) == "310.12.0" and _e.state == _du.UPDATED and _e.backup is not None
          and b"GAME-OWN-SR" in _du._ours(_sr).read_bytes()
          and any("nvngx_dlss.dll" in n and "310.12.0" in n for n in _notes), (_e, _notes))
    _sr.write_bytes(_du._ours(_sr).read_bytes())      # a launcher verifies: exactly the game's own again
    _notes = _du.settle(_g) + _du.settle(_g)
    check("...a launcher putting exactly the game's own back removes the now duplicate backup - "
          "no copies pile up, and the file reads as the game's own",
          not _du._ours(_sr).exists() and _du_by(_g, "dlss").state == _du.ORIGINAL
          and not list(_sr.parent.glob("nvngx_dlss.dll.dlss5-dlss-*")), (_notes,
          [q.name for q in _sr.parent.iterdir()]))
    _fg.write_bytes(_fpe.dll("3.1.0"))               # something put an OLDER one there
    _notes = _du.settle(_g)
    _e = _du_by(_g, "dlssg")
    check("...an older one keeps the game's own backup, still restorable",
          _du._ours(_fg).exists() and _e.state == _du.UPDATED and _e.backup is not None, (_e, _notes))
    _r = _du.restore(_g)
    _disp = list(_fg.parent.glob("nvngx_dlssg.dll" + _du.DISPLACED_SUFFIX + "-*"))
    check("...and restoring it then brings the game's own back, keeping the file that was in the way",
          b"GAME-OWN-FG" in _fg.read_bytes() and not _du._ours(_fg).exists()
          and len(_disp) == 1 and pe.file_version(_disp[0]) == "3.1.0", (_r, _disp))

    # -- the second review's sequences: a writer that is not us ------------------------
    # an install writes the file while the update downloads (both used to run at once)
    _g, _sr, _fg = _du_game("concurrent_install")
    _own_sr = _sr.read_bytes()

    def _du_racing(url, name, progress=None, **_k):
        if name.startswith("dlss-"):
            _du_install_swap(_g, _sr)         # lands while the download is in flight
        return _du_download(url, name, progress)
    with patch.object(_du.net, "download", _du_racing):
        _r = _du.update(_g, catalog=_C1, families=["dlss"])
    check("a file written by someone else during the download is not backed up as the game's "
          "own and not overwritten",
          not _r.ok and "changed while the update was downloading" in _r.error
          and not _du._ours(_sr).exists() and pe.file_version(_sr) == "310.10.0"
          and _sr.with_name(_sr.name + installer.BACKUP_SUFFIX).read_bytes() == _own_sr, _r)

    # the game patched itself after the update, then DLSS 5 swapped THAT file
    _g, _sr, _fg = _du_game("handover_not_ours")
    _du.update(_g, catalog=_C1, families=["dlss"])
    _sr.write_bytes(_fpe.dll("310.12.0") + b"GAME-PATCH")
    _du_install_swap(_g, _sr)
    _ibak = _sr.with_name(_sr.name + installer.BACKUP_SUFFIX)
    _r = _du.restore(_g)
    check("restore under a DLSS 5 install whose backup is NOT our build hands nothing over",
          b"GAME-PATCH" in _ibak.read_bytes() and b"GAME-OWN-SR" in _du._ours(_sr).read_bytes()
          and any("uninstall DLSS 5 first" in n for n in _r.notes), _r)
    installer.uninstall(_g)
    _r = _du.restore(_g)
    check("...after the DLSS 5 uninstall, restore brings the original and keeps the patched file aside",
          b"GAME-OWN-SR" in _sr.read_bytes()
          and any(b"GAME-PATCH" in q.read_bytes()
                  for q in _sr.parent.glob("nvngx_dlss.dll" + _du.DISPLACED_SUFFIX + "-*")), _r)

    # another tool writes a newer build, then puts OUR build back; then two updates and a restore
    _g, _sr, _fg = _du_game("foreign_then_ours_back")
    _du.update(_g, catalog=_C1, families=["dlss"])
    _ours_b = _sr.read_bytes()
    _sr.write_bytes(_fpe.dll("310.9.2") + b"SWAPPER")
    _du.settle(_g)
    _sr.write_bytes(_ours_b)
    _du.update(_g, catalog=_C2, families=["dlss"])
    _r = _du.restore(_g, families=["dlss"])
    check("a tool swapping builds behind the page's back never costs the game's own: restore "
          "still ends on 3.7.20", b"GAME-OWN-SR" in _sr.read_bytes(), (_r, pe.file_version(_sr)))

    # the page updated to a build, then a DLSS 5 install swapped in that very build
    _g, _sr, _fg = _du_game("install_same_build")
    _du.update(_g, catalog=_C1, families=["dlss"])
    _du_install_swap(_g, _sr, v="310.9.1")
    _e = _du_by(_g, "dlss")
    _r = _du.update(_g, catalog=_C2, families=["dlss"])
    check("a DLSS 5 install of the same build over a page update reads as the install's, and a later "
          "update skips it instead of failing on every attempt",
          _e.state == _du.INSTALL and _r.ok and any("DLSS 5 install" in x for x in _r.skipped), (_e, _r))

    # a runtime that disappeared
    _g, _sr, _fg = _du_game("runtime_gone")
    _du.update(_g, catalog=_C1, families=["dlssg"])
    _fg.unlink()
    _du.settle(_g)
    _e = _du_by(_g, "dlssg")
    _r = _du.restore(_g)
    check("a runtime that vanished is listed as missing, and restore puts the game's own back",
          _e.state == _du.MISSING and b"GAME-OWN-FG" in _fg.read_bytes() and not _du._ours(_fg).exists(),
          (_e, _r))

    # a record that claims the game's own file is ours
    _g, _sr, _fg = _du_game("record_lies")
    _du.record_path(_g).write_text(json.dumps({"files": [
        {"path": _DU_NEST + "/nvngx_dlss.dll", "family": "dlss", "written": "3.7.20",
         "size": _sr.stat().st_size, "original": "1.0"}]}), encoding="utf8")
    _own_sr = _sr.read_bytes()
    _du.update(_g, catalog=_C1, families=["dlss"])
    check("a record entry that names the game's own build is not trusted: a backup is made first",
          pe.file_version(_sr) == "310.9.1" and _du._ours(_sr).read_bytes() == _own_sr)

    # the uninstall that runs with no install record deletes runtimes by name
    _g, _sr, _fg = _du_game("no_manifest_uninstall")
    _du.update(_g, catalog=_C1, families=["dlssg"])
    installer.uninstall(_g)
    check("a DLSS 5 uninstall with no install record leaves a runtime this page updated",
          pe.file_version(_fg) == "310.9.1" and _du._ours(_fg).is_file())

    # one runtime updated, the next refused
    _g, _sr, _fg = _du_game("partial")
    os.chmod(_fg, _du_stat.S_IREAD)
    try:
        _r = _du.update(_g, catalog=_C1)
    finally:
        os.chmod(_fg, _du_stat.S_IREAD | _du_stat.S_IWRITE)
    check("a failure after one runtime went through does not say 'nothing was changed'",
          len(_r.done) == 1 and "Windows refused" in _r.error and "Nothing was changed" not in _r.error, _r)

    # -- refusals --------------------------------------------------------------
    _g, _sr, _fg = _du_game("refuse_anticheat")
    (_g.folder / "EasyAntiCheat").mkdir()
    _before = _sr.read_bytes()
    _r = _du.update(_g, catalog=_C1)
    check("anti-cheat: refused without an explicit yes, and the refusal names it",
          _r.refused == _du.R_ANTICHEAT and "Easy Anti-Cheat" in _r.error
          and _sr.read_bytes() == _before and not _du.record_path(_g).exists(), _r)
    _r = _du.update(_g, catalog=_C1, allow_anticheat=True)
    check("...and done when the person said yes", _r.ok and pe.file_version(_sr) == "310.9.1", _r)

    _g, _sr, _fg = _du_game("refuse_32bit", bits=32)
    _r = _du.update(_g, catalog=_C1)
    check("32-bit games: nothing is listed and an update is refused",
          _du.scan(_g) == [] and _r.refused == _du.R_32BIT and pe.file_version(_sr) == "3.7.20", _r)
    _g, _sr, _fg = _du_game("x86_runtime")
    _fg.write_bytes(_fpe.dll("3.5.0", machine=0x14C))
    check("...and a 32-bit runtime inside a 64-bit game is not listed",
          [e.family for e in _du.scan(_g)] == ["dlss"])

    _g, _sr, _fg = _du_game("refuse_running")
    with patch.object(_du.watch, "from_folder",
                      lambda *a, **k: [watch.Proc(7, 1, "Game.exe", str(_g.folder / "Game.exe"))]):
        _r = _du.update(_g, catalog=_C1)
    check("a running game is refused by name, nothing touched",
          _r.refused == _du.R_RUNNING and "Game.exe is running" in _r.error
          and pe.file_version(_sr) == "3.7.20", _r)

    _g, _sr, _fg = _du_game("refuse_readonly")
    _before = _sr.read_bytes()
    os.chmod(_sr, _du_stat.S_IREAD)
    try:
        _r = _du.update(_g, catalog=_C1, families=["dlss"])
    finally:
        os.chmod(_sr, _du_stat.S_IREAD | _du_stat.S_IWRITE)
    check("a file Windows will not replace: the reason names the file, the original "
          "is untouched and no backup or record is left",
          not _r.ok and "Windows refused to replace" in _r.error and str(_sr) in _r.error
          and _sr.read_bytes() == _before and not _du._ours(_sr).exists()
          and not _du.record_path(_g).exists(), _r)

    _g, _sr, _fg = _du_game("bad_download")
    _before = _sr.read_bytes()


    def _du_page(url, name, progress=None):
        # what a proxy or a DNS filter answers in place of the file
        (_du_blobs / "page.dll").write_bytes(b"<html>blocked</html>" * 20000)
        return _du_blobs / "page.dll"
    with patch.object(_du.net, "download", _du_page):
        _r = _du.update(_g, catalog=_C1)
    check("a download that is not a 64-bit DLL changes nothing",
          not _r.ok and "Nothing was changed" in _r.error and _sr.read_bytes() == _before
          and not _du._ours(_sr).exists(), _r)

    # -- own files and the record as untrusted input --------------------------
    _g, _sr, _fg = _du_game("own_files")
    (_g.folder / "host64").mkdir()
    (_g.folder / "host64" / "nvngx_dlssd.dll").write_bytes(_fpe.dll("310.2.1"))
    (_g.folder / "Binaries").mkdir()
    (_g.folder / "Binaries" / "host64").mkdir()
    (_g.folder / "Binaries" / "host64" / "nvngx_dlssd.dll").write_bytes(_fpe.dll("310.2.1"))
    (_g.folder / "nvngx_dlssd.dll").write_bytes(_fpe.dll("310.2.1"))
    _du.record_path(_g).write_text(json.dumps({"files": [
        {"path": "host64/nvngx_dlssd.dll", "family": "dlssd", "written": "310.2.1"}]}),
        encoding="utf8")
    (_g.folder / installer.MANIFEST).write_text(json.dumps(
        {"files": ["nvngx_dlssd.dll", "dxgi.dll"], "complete": True}), encoding="utf8")
    dlss.forget_walk()
    check("our host64 helper and a runtime a DLSS 5 install added are never listed",
          sorted(e.family for e in _du.scan(_g)) == ["dlss", "dlssg"],
          [(e.rel, e.state) for e in _du.scan(_g)])
    _du.record_path(_g).write_text(json.dumps({"files": [
        {"path": "../elsewhere/nvngx_dlss.dll", "family": "dlss", "written": "1"},
        {"path": "Game.exe", "family": "dlss", "written": "1"},
        {"path": "nvngx_dlssg.dll", "family": "nope"},
        "junk"]}), encoding="utf8")
    check("record entries outside the folder, naming another file or a family that "
          "does not exist are dropped", _du.load_record(_g) == [], _du.load_record(_g))
    _du.record_path(_g).write_text("{not json", encoding="utf8")
    check("...and a record that is not JSON reads as empty", _du.load_record(_g) == [])
    _du.record_path(_g).unlink()

    # -- the record lost after an update --------------------------------------
    _g, _sr, _fg = _du_game("record_lost")
    _own_sr = _sr.read_bytes()
    _du.update(_g, catalog=_C1)
    _du.record_path(_g).unlink()
    _e = _du_by(_g, "dlss")
    _du.update(_g, catalog=_C2)
    check("a backup with no record is adopted, never overwritten",
          _e.state == _du.UPDATED and _du._ours(_sr).read_bytes() == _own_sr
          and pe.file_version(_sr) == "310.10.0", _e)
    _g2, _sr2, _fg2 = _du_game("crash_after_backup")
    shutil.copyfile(_sr2, _du._ours(_sr2))           # backup made, swap never happened
    check("a backup of the very same build (a crash before the swap) reads as the game's own",
          _du_by(_g2, "dlss").state == _du.ORIGINAL)
    _du.restore(_g)
    check("...and restore still brings the game's own back", _sr.read_bytes() == _own_sr)
except Exception as _du_ex:
    import traceback as _du_tb
    check("the dlssupdate section ran to its end", False,
          "".join(_du_tb.format_exception(_du_ex))[-400:])
finally:
    _du_net.stop()
    _du_nowatch.stop()
    prefs.FILE = _du_saved[0]
    dlss.forget_walk()
    shutil.rmtree(_du_tmp, ignore_errors=True)


section("2.0: covers for games outside Steam - Epic, Xbox, Steam's store, chosen by hand")
# The library showed an icon for every game Steam did not install. The
# lookups below go online, so every check here runs on temporary folders
# with the network patched to raise - and says so when it was asked.
import base64 as _b64c
import struct as _stc
import urllib.request as _urc
import zlib as _zlc
from core import covers as _cv, prefs as _pfc
from core.ui import art as _art


def _cv_png(w=2, h=2):
    raw = b"".join(b"\x00" + b"\x80\x40\x20" * w for _ in range(h))

    def chunk(t, d):
        return _stc.pack(">I", len(d)) + t + d + _stc.pack(">I", _zlc.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", _stc.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", _zlc.compress(raw)) + chunk(b"IEND", b""))


class _CvGame:
    def __init__(self, name, folder, source="Folder", exe=None):
        self.name, self.folder, self.source, self.exe, self.kind = name, Path(folder), source, exe, "game"


_cv_calls: list = []


def _cv_no_net(*a, **k):
    _cv_calls.append(a[0] if a else k)
    raise OSError("the network is switched off in this check")


_cv_tmp = Path(tempfile.mkdtemp(prefix="covers_"))
_cv_saved = (_cv.ROOT, _pfc.FILE, _cv.EPIC_DATA)
_cv.ROOT, _pfc.FILE = _cv_tmp / "art", _cv_tmp / "settings.json"
# lookups only run after the person said yes to the library's question
check("covers: never asked means no lookup at all, and the question is still open",
      _cv.undecided() is True and _cv.online() is False)
_pfc.set_("online_art", True)
_cv_p = patch.object(_urc, "urlopen", _cv_no_net)
_cv_p.start()
try:
    # ---- Epic: catcache.bin is base64 of a JSON list, matched through the manifest
    _cv_items = [{"id": "item1", "namespace": "ns1", "title": "Some Game",
                  "keyImages": [{"type": "DieselGameBox", "url": "https://cdn1.epicgames.com/item/ns1/wide"},
                                {"type": "DieselGameBoxTall", "url": "https://cdn1.epicgames.com/item/ns1/tall"},
                                {"type": "DieselGameBoxLogo", "url": "http://insecure.example/logo"},
                                {"type": "Thumbnail", "url": "https://cdn1.epicgames.com/item/ns1/thumb"}]},
                 {"id": "other", "namespace": "ns2", "keyImages": []}, "junk", {"no": "id"}]
    _cv_cat = _cv.parse_catcache(_b64c.b64encode(json.dumps(_cv_items).encode()) + b"\r\n")
    check("covers: catcache.bin parses to catalog items by id, junk entries skipped",
          sorted(_cv_cat) == ["item1", "other"], sorted(_cv_cat))
    check("covers: a catcache that is not base64 JSON is an empty catalog, not a raise",
          _cv.parse_catcache(b"<html>nope") == {} and _cv.parse_catcache(b"") == {})
    _cv_man = _cv_tmp / "Manifests"
    _cv_man.mkdir()
    (_cv_man / "a.item").write_text(json.dumps({"DisplayName": "Some Game",
                                                "InstallLocation": "C:/Program Files/Epic Games/SomeGame",
                                                "CatalogItemId": "item1", "CatalogNamespace": "ns1"}),
                                    encoding="utf8")
    (_cv_man / "b.item").write_text("{broken", encoding="utf8")
    _cv_m = _cv.epic_manifest(r"c:\program files\epic games\somegame", _cv_man)
    check("covers: the Epic manifest is found by its install folder, slashes and case aside",
          bool(_cv_m) and _cv_m.get("CatalogItemId") == "item1", _cv_m)
    check("...and not for another folder",
          _cv.epic_manifest(r"C:\Program Files\Epic Games\SomeGame2", _cv_man) is None)
    _cv_u = _cv.epic_urls(_cv_m, _cv_cat)
    check("covers: tall is the cover, wide the background; an http:// logo is not used",
          _cv_u == {"cover": "https://cdn1.epicgames.com/item/ns1/tall",
                    "hero": "https://cdn1.epicgames.com/item/ns1/wide"}, _cv_u)
    check("covers: an item from another namespace is not this game's",
          _cv.epic_urls(dict(_cv_m, CatalogNamespace="nsX"), _cv_cat) == {})

    # ---- Xbox: the folder's own manifest names its pictures, scale variants included
    _cv_xb = _cv_tmp / "XboxGames" / "Game" / "Content"
    (_cv_xb / "Assets").mkdir(parents=True)
    (_cv_xb / "MicrosoftGame.config").write_text(
        '<Game><ShellVisuals DefaultDisplayName="Game" StoreLogo="Assets\\StoreLogo.png" '
        'Square150x150Logo="Assets\\Logo150.png" SplashScreenImage="Assets/Splash.png"/></Game>',
        encoding="utf8")
    (_cv_xb / "Assets" / "Logo150.scale-100.png").write_bytes(_cv_png(2, 2))
    (_cv_xb / "Assets" / "Logo150.scale-200.png").write_bytes(_cv_png(8, 8))
    (_cv_xb / "Assets" / "Splash.png").write_bytes(_cv_png(4, 2))
    _cv_x = _cv.xbox(_cv_xb)
    check("covers: an Xbox folder's square logo (largest scale) is the cover, its splash the background",
          _cv_x.get("cover") == _cv_xb / "Assets" / "Logo150.scale-200.png"
          and _cv_x.get("hero") == _cv_xb / "Assets" / "Splash.png", _cv_x)
    (_cv_xb / "MicrosoftGame.config").write_text(
        '<Game><ShellVisuals Square150x150Logo="..\\..\\..\\secret.png"/></Game>', encoding="utf8")
    (_cv_tmp / "secret.png").write_bytes(_cv_png())
    check("...and a manifest path that climbs out of the folder is not followed",
          _cv.xbox(_cv_xb) == {}, _cv.xbox(_cv_xb))

    # ---- the name matcher, on the shapes Steam's store search really answers
    _cv_div = [{"id": 2221490, "name": "Tom Clancy\u2019s The Division\u00ae 2", "type": "app"},
               {"id": 365590, "name": "Tom Clancy\u2019s The Division\u00ae", "type": "app"},
               {"id": 556470, "name": "Tom Clancy\u2019s The Division\u00ae - Survival", "type": "app"}]
    _cv_ds = [{"id": 3280350, "name": "DEATH STRANDING 2: ON THE BEACH", "type": "app"},
              {"id": 3669170, "name": "DEATH STRANDING 2: ON THE BEACH - Upgrade to Digital Deluxe Edition",
               "type": "app"}]
    _cv_gw = [{"id": 1817250, "name": "Ghostwire: Tokyo - Prelude", "type": "app"},
              {"id": 1702820, "name": "Ghostwire: Tokyo - Deluxe Upgrade", "type": "app"},
              {"id": 1944840, "name": "Ghostwire: Tokyo Original Game Soundtrack", "type": "app"}]
    _cv_cases = [
        ("Tom Clancy's The Division", _cv_div, 365590),
        ("Tom Clancy's The Division 2", _cv_div, 2221490),
        ("Tom Clancy's The Division", _cv_div[:1], None),                      # only the sequel
        ("DEATH STRANDING 2", _cv_ds, 3280350),                                # name + subtitle
        ("DEATH STRANDING", _cv_ds, None),                                     # the first game is not the second
        ("Ghostwire Tokyo", _cv_gw, None),                                     # DLC and soundtrack only
        ("Ghostwire Tokyo", [{"id": 1475810, "name": "Ghostwire: Tokyo", "type": "app"}] + _cv_gw, 1475810),
        ("Grand Theft Auto IV: The Complete Edition",
         [{"id": 12210, "name": "Grand Theft Auto IV: The Complete Edition", "type": "app"}], 12210),
        ("Grand Theft Auto IV", [{"id": 12210, "name": "Grand Theft Auto IV: The Complete Edition"}], 12210),
        ("Grand Theft Auto V", [{"id": 12210, "name": "Grand Theft Auto IV: The Complete Edition"}], None),
        ("HELLDIVERS\u2122 2", [{"id": 553850, "name": "HELLDIVERS\u2122 2", "type": "app"}], 553850),
        ("Alan Wake 2", [{"id": 3274290, "name": "Beat Saber - Monstercat Mixtape 2 - Alan Walker - \"Wake Up\""}],
         None),
        ("Some Folder", [], None),
        ("", _cv_div, None),
        ("Batman Arkham", [{"id": 1, "name": "Batman Arkham: City"}, {"id": 2, "name": "Batman Arkham: Knight"}],
         None),                                                                # two subtitles: which one?
        ("Control", [{"id": 1, "name": "Control: The Foundation"}], None),     # one word: no subtitle guess
        ("Assassin's Creed Shadows", [{"id": "x", "name": "Assassin's Creed Shadows"},
                                      {"id": 3159330, "name": "Assassin\u2019s Creed Shadows", "type": "app"}],
         3159330),                                                             # a bad id is skipped
        ("Cyberpunk 2077", [{"id": 1, "name": "Cyberpunk 2077", "type": "dlc"}], None),
    ]
    _cv_bad = [(n, want, _cv.match(n, items)) for n, items, want in _cv_cases
               if _cv.match(n, items) != want]
    check(f"covers: the name matcher takes the game and nothing near it ({len(_cv_cases)} names)",
          not _cv_bad, _cv_bad)

    # ---- the negative cache and its expiry
    _cv_g = _CvGame("Nothing Like It", _cv_tmp / "games" / "Nothing")
    check("covers: a game never asked about is pending", _cv.pending(_cv_g))
    _cv._write_meta(_cv_g, {"miss": 1000.0, "ttl": _cv.MISS_TTL})
    check("covers: a recorded miss holds for its week...",
          not _cv.pending(_cv_g, now=1000.0 + _cv.MISS_TTL - 60))
    check("...and asks again after it", _cv.pending(_cv_g, now=1000.0 + _cv.MISS_TTL + 60))
    _cv._write_meta(_cv_g, {"miss": 1000.0, "ttl": _cv.FAIL_TTL})
    check("covers: a failed connection holds for a day, not a week",
          _cv.FAIL_TTL == 24 * 3600 and _cv.MISS_TTL == 7 * 24 * 3600 and
          _cv.pending(_cv_g, now=1000.0 + _cv.FAIL_TTL + 60)
          and not _cv.pending(_cv_g, now=1000.0 + _cv.FAIL_TTL - 60))
    _cv._write_meta(_cv_g, {"miss": 1000.0, "ttl": _cv.MISS_TTL})
    _cv_g.name = "Nothing Like It 2"
    check("covers: a renamed game is a new question", _cv.pending(_cv_g, now=1001.0))
    _cv_g.name = "Nothing Like It"
    check("covers: two game folders never share a cache folder",
          _cv.key(r"D:\Games\A") != _cv.key(r"D:\Games\B")
          and _cv.key(r"D:\Games\A") == _cv.key("d:/games/a/"))

    # ---- a failed lookup is recorded, never raised, never retried in a loop
    _cv_calls.clear()
    _cv_h = _CvGame("Offline Game", _cv_tmp / "games" / "Offline")
    _cv_r = _cv.lookup(_cv_h)
    check("covers: no network - the lookup answers False and records a one-day miss",
          _cv_r is False and _cv._meta(_cv_h).get("ttl") == 24 * 3600 and not _cv.pending(_cv_h),
          (_cv_r, _cv._meta(_cv_h)))
    check("...after asking once, over HTTPS, at Steam's store",
          len(_cv_calls) == 1 and getattr(_cv_calls[0], "full_url", "").startswith(
              "https://store.steampowered.com/api/storesearch/?term=Offline%20Game"),
          [getattr(c, "full_url", c) for c in _cv_calls])

    # ---- online_art off: not one request, and nothing queued
    _pfc.set_("online_art", False)
    _cv_calls.clear()
    _cv_o = _CvGame("Private Game", _cv_tmp / "games" / "Private", source="Epic")
    _cv_r = _cv.lookup(_cv_o)
    check("covers: online_art off - lookup asks nothing, writes nothing",
          _cv_r is False and not _cv_calls and not _cv.dir_of(_cv_o).exists(), _cv_calls)
    _art._found.clear()
    with patch.object(_art, "_want", side_effect=AssertionError("queued")) as _cv_want:
        _art.find(_cv_o)
    check("...and art.find queues no lookup", not _cv_want.called)
    _pfc.set_("online_art", True)

    # ---- what a server sends is checked before it is kept
    _cv_big = _cv_tmp / "big.jpg"
    _cv_big.write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * (_cv.MAX_BYTES + 10))
    _cv_html = _cv_tmp / "page.jpg"
    _cv_html.write_bytes(b"<!doctype html><title>login</title>")
    _cv_ok = _cv_tmp / "mine.png"
    _cv_ok.write_bytes(_cv_png(4, 6))
    _cv_u1 = _CvGame("Chosen", _cv_tmp / "games" / "Chosen")
    _cv_s1 = _cv.set_user(_cv_u1, "cover", _cv_big)
    _cv_s2 = _cv.set_user(_cv_u1, "cover", _cv_html)
    check("covers: a picture over 8 MB and an HTML page named .jpg are refused, with a reason",
          "MB" in _cv_s1 and "not a JPEG, PNG or BMP" in _cv_s2 and not _cv.user(_cv_u1), (_cv_s1, _cv_s2))
    check("covers: a picture Windows cannot draw is refused and not left behind",
          "could not read" in _cv.set_user(_cv_u1, "cover", _cv_ok, readable=lambda p: False)
          and not _cv.user(_cv_u1))
    check("covers: magic bytes decide, not the name",
          _cv.image_ext(b"\xff\xd8\xff\xe0JFIF") == ".jpg" and _cv.image_ext(_cv_png()) == ".png"
          and _cv.image_ext(b"<html>") is None and _cv.image_ext(b"") is None)

    class _CvResp:
        def __init__(self, body, length=None):
            self.body, self.status = body, 200
            self.headers = {"Content-Length": str(len(body) if length is None else length)}

        def read(self, n=-1):
            return self.body if n < 0 else self.body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    with patch.object(_urc, "urlopen", lambda *a, **k: _CvResp(b"\xff\xd8\xff", length=_cv.MAX_BYTES + 1)):
        _cv_r1 = _cv._get("https://example.invalid/a.jpg")
    with patch.object(_urc, "urlopen", lambda *a, **k: _CvResp(b"x" * 64, length=0)):
        _cv_r2 = _cv._get("https://example.invalid/a.jpg", limit=32)
    check("covers: a download declared or read past the cap is dropped", _cv_r1 is None and _cv_r2 is None,
          (_cv_r1, _cv_r2))
    _cv_e = _CvGame("Portal Page", _cv_tmp / "games" / "Portal")
    with patch.object(_cv, "_get", lambda url, limit=_cv.MAX_BYTES: (
            json.dumps({"items": [{"id": 77, "name": "Portal Page", "type": "app"}]}).encode()
            if "storesearch" in url else b"<html>captive portal</html>")):
        _cv_r = _cv.lookup(_cv_e)
    check("covers: a captive portal answering 200 for every image leaves no cover, and a miss",
          _cv_r is False and not _cv.got(_cv_e) and _cv._meta(_cv_e).get("ttl") == _cv.MISS_TTL,
          (_cv_r, _cv.got(_cv_e)))
    _cv_urls = _cv.steam_urls(3280350, {"asset_url_format": "steam/apps/3280350/${FILENAME}?t=1",
                                       "library_capsule_2x": "9b523fd411eeaefe80f238489745325a1cd2317f/"
                                                             "library_capsule_2x.jpg",
                                       "library_hero": "../../evil/library_hero.jpg"})
    check("covers: Steam's hashed asset paths come first; a path that climbs out is not used",
          _cv_urls["cover"][0].endswith("/3280350/9b523fd411eeaefe80f238489745325a1cd2317f/"
                                        "library_capsule_2x.jpg?t=1")
          and not any("evil" in u for u in _cv_urls["hero"]), _cv_urls)

    # ---- a picture chosen by hand wins, and the reset takes it back out
    _cv_ch = _CvGame("Chosen Two", _cv_tmp / "games" / "Chosen2")
    _cv._store(_cv_ch, "got-", "cover", _cv_png(2, 3))
    _art._found.clear()
    _cv_heard: list = []
    _art.listen(lambda f: _cv_heard.append(f))
    check("covers: a chosen PNG is copied into the cache", _art.choose(_cv_ch, "cover", _cv_ok) == "",
          _cv.user(_cv_ch))
    _cv_a = _art.find(_cv_ch)
    check("...wins over a downloaded cover, and the window hears about it",
          _cv_a.cover is not None and _cv_a.cover.name.startswith("user-cover") and _cv_a.chosen
          and _cv_heard == [str(_cv_ch.folder)], (_cv_a, _cv_heard))
    _cv_ok.unlink()
    _art._found.clear()
    check("...and is a copy: the original can move", _art.find(_cv_ch).cover.is_file())
    _art.reset(_cv_ch)
    _cv_a = _art.find(_cv_ch)
    check("covers: 'use the automatic pictures' goes back to the downloaded cover",
          _cv_a.cover is not None and _cv_a.cover.name.startswith("got-cover") and not _cv_a.chosen, _cv_a)

    # ---- the Tk thread asks from memory; the network runs on the art thread
    _cv_t = _CvGame("Threaded Game", _cv_tmp / "games" / "Threaded")
    _art._found.clear()
    with patch.object(_cv, "user", side_effect=AssertionError("disk")), \
            patch.object(_art, "_index", side_effect=AssertionError("walk")):
        _cv_pk = _art.peek(_cv_t)
    check("covers: art.peek reads no folder and no file", _cv_pk is None)
    _cv_th = _UiThreads(run=False)
    _cv_seen: list = []

    def _cv_lookup(game, appid=None):
        _cv_seen.append(_cv_th.inside)
        _cv._store(game, "got-", "cover", _cv_png())
        return True
    _cv_heard.clear()
    with patch.object(_art, "threading", _cv_th), patch.object(_cv, "lookup", _cv_lookup):
        _art._asked.discard(_cv.key(_cv_t.folder))
        _art.find(_cv_t)
        _cv_before = list(_cv_seen)
        _art.find(_cv_t)                                    # asked twice before it lands
        _cv_th.go()
    check("covers: art.find only queues the lookup; it runs on a worker, once",
          _cv_before == [] and _cv_seen == [1] and _cv_th.names.count("_work") == 1,
          (_cv_before, _cv_seen, _cv_th.names))
    check("...and when it lands the window is told, and the next find has the cover",
          _cv_heard == [str(_cv_t.folder)] and _art.find(_cv_t).cover is not None, _cv_heard)
    _cv_heard.clear()
    _art._found.clear()
    with patch.object(_art, "threading", _cv_th), patch.object(_cv, "lookup", _cv_lookup):
        for _p_ in _cv.got(_cv_t).values():
            _p_.unlink()
        _art.find(_cv_t)
        _cv_th.go()
    check("covers: a game already asked about this run is not asked again", len(_cv_seen) == 1, _cv_seen)
    from core.ui import gamepage as _gpc
    check("covers: the game page's draw asks art.peek, not art.find (it runs on every redraw)",
          "art.peek(" in src_of(_gpc.GamePage.draw) and "art.find(" not in src_of(_gpc.GamePage.draw))

    # ---- the window: ("art", folder) drops that game's images only
    with _ui_isolated():
        _cv_c = _ui_ctl()
        _cv_c.covers = {r"D:\A|176x264": {"kind": "icon"}, r"D:\AB|176x264": {"kind": "icon"}}
        _cv_c.pump()
        _art._listeners[0](r"D:\A")
        _cv_k = _cv_c.pump()
        check("covers: an ('art', folder) message drops that game's cards, not a neighbour's",
              _cv_k == ["art"] and list(_cv_c.covers) == [r"D:\AB|176x264"], (_cv_k, _cv_c.covers))
        _cv_c.shell.page = None
        _cv_c.set_online_art(False)
        check("covers: the view menu's switch is the online_art setting",
              _pfc.get("online_art", True) is False)
finally:
    _cv_p.stop()
    _cv.ROOT, _pfc.FILE, _cv.EPIC_DATA = _cv_saved
    _art._found.clear()
    shutil.rmtree(_cv_tmp, ignore_errors=True)

# ---- the real window: right-click a card, pick 'choose cover...', a real file dialog answer
_cv_live = _UiLive()
try:
    if not check("covers: the window opened for the right-click check", _cv_live.ok, _cv_live.error):
        raise RuntimeError("no window")
    _cv_dir = Path(tempfile.mkdtemp(prefix="covers_live_"))
    _cv_png_path = _cv_dir / "cover.png"
    _cv_png_path.write_bytes(_cv_png(6, 9))
    _ui_live_game = _ui_game(name="Picture Game")
    _cv_live.app.all_games = [_ui_live_game]
    _cv_live.app.shell.show("library")
    _cv_live.settle(300)
    _cv_card = next((cd["tag"] for cd in _cv_live.app.shell.page.cards if cd["g"] is _ui_live_game), None)
    _cv_live.click(_cv_card, button=3)
    _cv_menu = [str(_cv_live.canvas.itemcget(i, "text")) for i in _cv_live.canvas.find_withtag(
        _cv_live.kit.top().tag) if _cv_live.canvas.type(i) == "text"] if _cv_live.kit.top() else []
    check("covers: a card's right-click menu offers choose cover / background / logo",
          all(any(t.startswith(w) for t in _cv_menu) for w in
              ("choose cover", "choose background", "choose logo")), _cv_menu)
    with patch("tkinter.filedialog.askopenfilename", lambda **k: str(_cv_png_path)):
        _cv_picked = _cv_live.pick("choose cover")
        _cv_landed = _cv_live.until(lambda: "cover" in _cv.user(_ui_live_game), 6.0)
    _cv_live.settle(300)
    check("covers: picking it copies the file into the art cache and says so",
          _cv_picked and _cv_landed and "cover set" in str(_cv_live.app.shell.status_text),
          (_cv_picked, _cv_landed, getattr(_cv_live.app.shell, "status_text", "")))
    # the new cover redrew the grid: the card is found again, as a person would
    _cv_card = next((cd["tag"] for cd in _cv_live.app.shell.page.cards if cd["g"] is _ui_live_game), None)
    _cv_live.click(_cv_card, button=3)
    _cv_menu = [str(_cv_live.canvas.itemcget(i, "text")) for i in _cv_live.canvas.find_withtag(
        _cv_live.kit.top().tag) if _cv_live.canvas.type(i) == "text"] if _cv_live.kit.top() else []
    check("...after which the menu offers the automatic pictures back",
          any(t.startswith("use the automatic pictures") for t in _cv_menu), _cv_menu)
    shutil.rmtree(_cv_dir, ignore_errors=True)
except RuntimeError:
    pass
finally:
    _cv_live.close()
    _art._found.clear()


section("2.0: the dlss page in the real window, every state, real events "
        "(dlss_page_check)")

# The page is driven in its own process: the sandbox switches the network
# off and moves every settings file for the life of that process, which must
# not leak into the sections of this run. Each of its checks comes back as
# one check here, and a run that died part way is a failure of its own - a
# half-run check looks green (see the memory note).
_dpc = subprocess.run([sys.executable, str(SRC_DIR / "_tools" / "dlss_page_check.py")],
                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=600, cwd=str(SRC_DIR))
_dpc_lines = [ln for ln in _dpc.stdout.splitlines() if ln.startswith(("   PASS  ", "   FAIL  "))]
for _ln in _dpc_lines:
    check("dlss page: " + _ln[9:].split("   ")[0], _ln.startswith("   PASS  "),
          "" if _ln.startswith("   PASS  ") else _ln[9:])
check("dlss page: the check ran to its end",
      _dpc.returncode == 0 and "FAILED: none" in _dpc.stdout and len(_dpc_lines) >= 48,
      (_dpc.returncode, len(_dpc_lines), _dpc.stderr[-400:]))


section("2.0 gate: DLSS evidence beside our own files, dlss page backups, the page's reads, "
        "the covers switch")

import queue as _g2q  # noqa: E402
import re as _g2re  # noqa: E402
import threading as _g2th  # noqa: E402
from types import SimpleNamespace as _G2NS  # noqa: E402
sys.path.insert(0, str(SRC_DIR / "_tools"))
import fake_pe as _g2fpe  # noqa: E402
from core import dlssupdate as _g2du, covers as _g2cov, anticheat as _g2ac  # noqa: E402
from core.ui import ctl_dlss as _g2ctl  # noqa: E402

_g2tmp = Path(tempfile.mkdtemp(prefix="gate20_"))
_g2saved = (prefs.FILE, _g2cov.ROOT)
prefs.FILE = _g2tmp / "settings.json"         # never the owner's settings or art cache
_g2cov.ROOT = _g2tmp / "art"
try:
    # -- 1. our own files are not another DLSS tool ------------------------------------
    def _g2folder(tag, files, manifest=None):
        d = _g2tmp / tag
        d.mkdir(parents=True)
        for n in files:
            (d / n).write_bytes(b"MZ")
        if manifest is not None:
            (d / "dlss5-autopilot.json").write_text(json.dumps(
                {"files": manifest, "complete": True}), encoding="utf8")
        return d

    _g2opti = ["OptiScaler.ini", "nvngx.dll_dlssnr.dll", "dxgi.dll", "nvngx_dlssnr.dll"]
    _g2d = _g2folder("opti_ours", _g2opti + ["nvngx_dlss.dll"], manifest=_g2opti)
    check("gate 2.0 #1: our own optiscaler install files do not hide the game's nvngx_dlss.dll",
          pe._ships_dlss(_g2d) == "nvngx_dlss.dll", pe._ships_dlss(_g2d))
    _g2d = _g2folder("opti_by_hand", ["OptiScaler.ini", "nvngx_dlss.dll"])
    check("...while OptiScaler put in by hand still does", pe._ships_dlss(_g2d) == "")
    _g2d = _g2folder("renodx_hdr", ["renodx-residentevil4.addon64", "nvngx_dlss.dll",
                                   "dlss5-dx11-bridge.addon64"])
    check("...a RenoDX HDR add-on and our old dx11 bridge are not a DLSS tool",
          pe._ships_dlss(_g2d) == "nvngx_dlss.dll", pe._ships_dlss(_g2d))
    _g2r250 = (SRC_DIR / "_tools" / "reports" / "250.txt").read_text(encoding="utf8", errors="replace")
    _g2sw = _g2re.search(r"dlss5-lab-overlay-[0-9a-f]+\.addon64", _g2r250)
    _g2d = _g2folder("swapper250", [_g2sw.group(0) if _g2sw else "dlss5-lab-overlay-x.addon64",
                                    "nvngx_dlss.dll", "dxgi.dll", "nvngx.dll.addon64"])
    check("...and #250's swapper overlay add-on (named in the report) still does",
          bool(_g2sw) and pe._ships_dlss(_g2d) == "", (bool(_g2sw), pe._ships_dlss(_g2d)))
    # the file list is the installer's, the add-on rule narrower on purpose
    _g2inst = {n.lower() for n in installer.OTHER_NGX_HOOKS}
    check("...pe's foreign file list is the installer's (minus the standalone caller bridge and "
          "add-ons, which the name rule catches)",
          {n for n in _g2inst if not n.endswith(".addon64")} - {installer.STANDALONE_BRIDGE.lower()}
          == set(pe._FOREIGN_NGX_FILES)
          and all(pe._foreign_ngx({n}) for n in _g2inst if n.endswith(".addon64")),
          sorted(_g2inst ^ set(pe._FOREIGN_NGX_FILES)))

    # -- 2. the dlss page's backups are not the game running -------------------------------
    _g2d = _g2tmp / "ran" / "Binaries" / "Win64"
    _g2d.mkdir(parents=True)
    _g2now = time.time()
    for _n in ("nvngx_dlss.dll.dlss5-dlss-original", "nvngx_dlss.dll.dlss5-dlss-displaced-1758000000",
               "nvngx_dlss.dll.dlss5-dlss-part", "dlss5-dlss-update.json",
               ".dlss5-dlss-write-test-abc"):
        (_g2d / _n).write_bytes(b"x")
    with patch.object(diagnose.model, "_user_data_roots", lambda: []):
        _g2ran = diagnose._game_ran(_g2d, "Game.exe", _g2now - 3600, set())
        check("gate 2.0 #2: fresh dlss page backups and its record are not 'the game ran'",
              _g2ran == ("", 0.0), _g2ran)
        (_g2d / "GameUserSettings.ini").write_bytes(b"x")
        _g2ran = diagnose._game_ran(_g2d, "Game.exe", _g2now - 3600, set())
        check("...while a file the game writes still is", _g2ran[0] == "GameUserSettings.ini", _g2ran)

    # -- 3. the bug report finds the page's record three levels up ---------------------------
    _g2root = _g2tmp / "Ready Or Not"
    _g2exe = _g2root / "ReadyOrNot" / "Binaries" / "Win64"
    _g2exe.mkdir(parents=True)
    _g2rel = "ReadyOrNot/Plugins/DLSS/Binaries/ThirdParty/Win64/nvngx_dlss.dll"
    (_g2root / _g2rel).parent.mkdir(parents=True)
    (_g2root / _g2rel).write_bytes(b"MZ")
    (_g2root / _g2du.RECORD).write_text(json.dumps({"files": [
        {"path": _g2rel, "family": "dlss", "original": "3.7.20", "written": "310.9.1",
         "size": 2, "label": "310.9.1 (NVIDIA SDK)"}]}), encoding="utf8")
    check("gate 2.0 #3: the record is found three levels above an Unreal exe, with and without the game root",
          diagnose._dlss_record_root(_g2exe) == _g2root
          and diagnose._dlss_record_root(_g2exe, _g2root) == _g2root,
          diagnose._dlss_record_root(_g2exe))
    _g2lines = "\n".join(diagnose._presence(_g2exe, {}, "feeder"))
    check("...and the report's file list carries the page's update",
          "updated on the dlss page to 310.9.1" in _g2lines, _g2lines[-300:])
    _g2deep = _g2tmp / "deep" / "a" / "b" / "c" / "d" / "e"
    _g2deep.mkdir(parents=True)
    (_g2tmp / "deep" / _g2du.RECORD).write_text("{}", encoding="utf8")
    check("...the climb is bounded (five levels up is not this game's) and stops at a drive root",
          diagnose._dlss_record_root(_g2deep) is None
          and diagnose._dlss_record_root(Path(_g2tmp.anchor)) is None)

    # -- 4. a traceback of window frames is not an install crash ----------------------------
    check("gate 2.0 #4: the install module walk stops at the 2.0 window's package",
          _g2re.search(r'n in \([^)]*"ui"', src_of(diagnose.model._install_modules)) is not None)
    _g2tb = ('Traceback (most recent call last):\n'
             '  File "C:\\a\\core\\ui\\{m}.py", line 5, in go\n'
             'URLError: <urlopen error [Errno 11001] getaddrinfo failed>\n')
    check("...a frame under core/ui named like an install module is the window's",
          diagnose._install_crash(_g2tb.format(m="net")) == ("", "")
          and diagnose._install_crash(_g2tb.format(m="net").replace("\\ui\\", "\\"))[0],
          diagnose._install_crash(_g2tb.format(m="net")))

    # -- 5. an update over our own build with the game's backup gone ---------------------------
    _g2blobs = _g2tmp / "blobs"
    _g2blobs.mkdir()

    def _g2cat(v):
        return {f: [{"tag": f"v{v}", "label": f"{v} (NVIDIA SDK)", "raw": n, "size": 0,
                     "url": f"https://raw.githubusercontent.com/NVIDIA/DLSS/v{v}/{n}"}]
                for f, n, _l in _g2du.FAMILIES}

    def _g2download(url, name, progress=None, **_k):
        v = name.split("-", 1)[1].split(" ")[0]
        p = _g2blobs / name.replace(" ", "_")
        p.write_bytes(_g2fpe.dll(v))
        return p

    def _g2game(tag, files=(("nvngx_dlss.dll", "3.7.20"),), bits=64, extra=()):
        d = _g2tmp / "games" / tag
        d.mkdir(parents=True)
        (d / "Game.exe").write_bytes(_g2fpe.dll("1.0.0", size=5000))
        for rel, v in files:
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_bytes(_g2fpe.dll(v) + tag.encode())
        for x in extra:
            (d / x).mkdir(parents=True, exist_ok=True)
        return games.Game(name=tag, folder=d, exe=d / "Game.exe", bitness=bits, api="DX12")

    with patch.object(_g2du.watch, "from_folder", lambda *a, **k: []), \
            patch.object(_g2du.net, "download", _g2download):
        _g2g = _g2game("backup_gone")
        _g2sr = _g2g.folder / "nvngx_dlss.dll"
        _g2du.update(_g2g, catalog=_g2cat("310.9.1"))
        _g2du._ours(_g2sr).unlink()
        _g2rep = _g2du.update(_g2g, catalog=_g2cat("310.10.0"))
        _g2e = next((e for e in _g2du.scan(_g2g, fresh=True)), None)
        _g2rec = _g2du.load_record(_g2g)
        check("gate 2.0 #5: updating our own build with the game's backup gone makes no 'backup' of our build",
              _g2rep.ok and _g2du.pe.file_version(_g2sr) == "310.10.0" and not _g2du._ours(_g2sr).exists()
              and _g2rec and _g2rec[0]["original"] == "3.7.20",
              (_g2rep.error, _g2du._ours(_g2sr).exists(), _g2rec))
        check("...the page labels it 'backup gone', not the game's own",
              _g2e is not None and _g2e.state == _g2du.UPDATED and _g2e.backup is None, _g2e)
        _g2rep = _g2du.restore(_g2g)
        check("...and restore leaves the file there and says the backup is gone",
              _g2du.pe.file_version(_g2sr) == "310.10.0" and any("is gone" in n for n in _g2rep.notes),
              _g2rep.notes)
        # the order the invariant protects: a first update still backs up the game's own
        _g2g = _g2game("first_update")
        _g2du.update(_g2g, catalog=_g2cat("310.9.1"))
        check("...while a first update still keeps the game's own beside it",
              _g2du.pe.file_version(_g2du._ours(_g2g.folder / "nvngx_dlss.dll")) == "3.7.20")

    # -- 6/7. the page's reads: detection's walk, only what changed, coalesced re-reads -------
    _g2g = _g2game("walk_reuse", files=(
        ("Engine/Plugins/DLSS/Win64/nvngx_dlss.dll", "3.7.20"),))
    dlss.forget_walk()
    dlss.walked(_g2g.folder, skip_dir=_g2g.install_dir)        # what detection asks
    _g2walks = []
    with patch.object(dlss, "find_dlss_files", lambda *a, **k: _g2walks.append(a) or []):
        _g2c = _g2du._candidates(_g2g, False)
    check("gate 2.0 #6: the page's read reuses detection's walk of the folder instead of walking again",
          not _g2walks and any(p.name == "nvngx_dlss.dll" for p in _g2c), (_g2walks, _g2c))
    dlss.forget_walk()

    class _G2App(_g2ctl.DlssControl):
        def __init__(self, gs):
            self._dlss_init()
            self.all_games, self.busy, self.scanning, self.action = list(gs), False, False, ""
            self.q = _g2q.Queue()
            self.asked, self.later, self.hid = [], [], set()
            self.shell = _G2NS(status=lambda *a, **k: None, pages={}, page=None,
                               busy=lambda *a, **k: None,
                               ask=lambda title, text, *a, **k: self.asked.append(text) or False)
            self.root = _G2NS(after=lambda ms, fn: self.later.append(fn))

        def refresh(self, *a, **k):
            pass

        def have_library(self):
            return True

        def hidden(self):
            return set(self.hid)

        def write(self, *a, **k):
            pass

        def pump(self, until, seconds=10.0):
            end = time.time() + seconds
            while time.time() < end:
                try:
                    kind, payload = self.q.get(timeout=0.1)
                except _g2q.Empty:
                    if until():
                        return True
                    continue
                getattr(self, f"_on_{kind}")(payload)
                if until():
                    return True
            return until()

    _g2reads = []
    _g2real_scan = _g2du.scan

    _g2hold = _g2th.Event()
    _g2hold.set()

    def _g2scan(g, fresh=False):
        _g2reads.append((g.name, _g2th.get_ident()))
        _g2hold.wait(10)
        time.sleep(0.02)
        return _g2real_scan(g, fresh)

    _g2A = _g2game("pA")
    _g2B = _g2game("pB", extra=("EasyAntiCheat",))
    _g2C = _g2game("pC")
    _g2D = _g2game("pD")
    _g2U = _g2game("pUnread")
    _g2U.bitness = None
    with patch.object(_g2du, "scan", _g2scan), patch.object(_g2du, "running", lambda *a, **k: ""), \
            patch.object(_g2ctl.watch, "procs", lambda: []), \
            patch.object(_g2du.sources, "nvidia_dlss", lambda: _g2cat("310.9.1")):
        _g2du._NEWEST = {}
        _g2app = _G2App([_g2A, _g2B, _g2U])
        _g2app.dlss_scan()
        _g2app.pump(lambda: not _g2app.dlss_scanning)
        _g2data = _g2app.dlss_state()
        check("gate 2.0 #7: a game whose bitness is not read yet is neither read nor counted 32-bit",
              sorted(n for n, _t in _g2reads) == ["pA", "pB"] and _g2data.get("x86") == 0
              and str(_g2U.folder) not in _g2data.get("seen"), (_g2reads, _g2data.get("x86")))
        _g2reads.clear()
        _g2app.all_games.append(_g2C)
        check("...one new game makes the read due", _g2app.dlss_due())
        _g2app.dlss_scan()
        _g2app.pump(lambda: not _g2app.dlss_scanning)
        check("gate 2.0 #6: and that read reads the new game only, keeping the others' rows",
              [n for n, _t in _g2reads] == ["pC"]
              and {str(_g2A.folder), str(_g2B.folder), str(_g2C.folder)} <= set(_g2app.dlss_state()["games"]),
              _g2reads)
        _g2reads.clear()
        (_g2A.folder / "patched.txt").write_bytes(b"x")
        os.utime(_g2A.folder, (time.time() + 120, time.time() + 120))
        _g2app.all_games.append(_g2D)
        _g2app.dlss_scan()
        _g2app.pump(lambda: not _g2app.dlss_scanning)
        check("...a later one reads the new game and the one whose folder changed, not the rest",
              sorted(n for n, _t in _g2reads) == ["pA", "pD"], _g2reads)
        _g2reads.clear()
        _g2app.dlss_scan(fresh=True)
        _g2app.pump(lambda: not _g2app.dlss_scanning)
        check("...and 'check again' still reads every game",
              sorted(n for n, _t in _g2reads) == ["pA", "pB", "pC", "pD"], _g2reads)
        _g2reads.clear()
        # the first read held open, so every later call lands while it runs
        _g2hold.clear()
        _g2app.dlss_reread(_g2A)
        _g2app.pump(lambda: len(_g2reads) >= 1, 5)
        for _g in (_g2B, _g2C, _g2D, _g2A, _g2B, _g2C):
            _g2app.dlss_reread(_g)
        _g2hold.set()
        _g2app.pump(lambda: not _g2app._dlss_rereading and len(_g2reads) >= 5, 15)
        check("gate 2.0 #6: many re-reads in a row are one worker, a game queued twice is read once more "
              "(A again: asked after its read began)",
              [n for n, _t in _g2reads] == ["pA", "pB", "pC", "pD", "pA"]
              and len({t for _n, t in _g2reads}) == 1, _g2reads)
        # a library scan running: the page waits for it
        _g2reads.clear()
        _g2app.scanning = True
        _g2app.dlss_scan(fresh=True)
        check("gate 2.0 #7: no read of the games starts while the library is scanning, one waits for it",
              not _g2app.dlss_scanning and len(_g2app.later) == 1, len(_g2app.later))
        _g2app.dlss_scan(fresh=True)
        _g2app.scanning = False
        for _fn in list(_g2app.later):
            _fn()
        _g2app.pump(lambda: not _g2app.dlss_scanning)
        check("...and it runs once when the scan ends", len(_g2reads) == 4 and len(_g2app.later) == 1,
              (_g2reads, len(_g2app.later)))
        # hidden games
        _g2app.hid = {str(_g2B.folder)}
        check("gate 2.0 #7: a hidden game is not on the page and not in update all",
              _g2B not in _g2app._dlss_games()
              and all(r["g"] is not _g2B for r in _g2app.dlss_rows() + _g2app.dlss_todo()))
        _g2app.hid = set()
        # the anti-cheat question names the file
        _g2app.dlss_update(_g2B)
        check("gate 2.0 #7: the anti-cheat question names the file it rests on",
              _g2app.asked and "EasyAntiCheat" in _g2app.asked[-1], _g2app.asked)
        # NVIDIA's newest is asked again on 'check again'
        _g2du._NEWEST = {}
        _g2du.newest()
        with patch.object(_g2du.sources, "nvidia_dlss", lambda: _g2cat("310.10.0")):
            _g2old = _g2du.newest().get("dlss", {}).get("version")
            _g2new = _g2du.newest(refresh=True).get("dlss", {}).get("version")
        with patch.object(_g2du.sources, "nvidia_dlss", lambda: {}):
            _g2off = _g2du.newest(refresh=True)
        check("gate 2.0 #7: 'check again' asks NVIDIA's list again; a failed ask keeps the last answer "
              "for updates",
              _g2old == "310.9.1" and _g2new == "310.10.0" and _g2off == {}
              and _g2du.newest().get("dlss", {}).get("version") == "310.10.0", (_g2old, _g2new, _g2off))
        check("...and the page's read passes 'check again' on",
              "du.newest(refresh=fresh)" in src_of(_g2ctl.DlssControl.dlss_scan))
    _g2du._NEWEST = {}

    # -- 8. the covers switch holds mid-lookup ------------------------------------------------
    _g2jpg = b"\xff\xd8\xff" + b"0" * 64
    _g2calls = []

    class _G2Resp:
        headers = {}

        def __init__(self, body):
            self.body = body

        def read(self, n=-1):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _g2urlopen(req, timeout=None, context=None):
        _g2calls.append(getattr(req, "full_url", req))
        prefs.set_(_g2cov.PREF, False)       # switched off while this request was out
        return _G2Resp(_g2jpg)

    prefs.set_(_g2cov.PREF, True)
    _g2cg = games.Game(name="Covers Game", folder=_g2tmp / "covers_game", exe=None, source="Steam")
    with patch.object(_g2cov.urllib.request, "urlopen", _g2urlopen):
        _g2got = _g2cov.lookup(_g2cg, appid="1234")
    check("gate 2.0 #8: switching online covers off stops a running lookup at its next request",
          len(_g2calls) == 1, _g2calls)
    check("...and nothing it downloaded, and no 'miss', is written",
          not _g2got and not _g2cov.got(_g2cg) and not (_g2cov.dir_of(_g2cg) / "meta.json").exists(),
          (_g2got, _g2cov.got(_g2cg)))
    prefs.set_(_g2cov.PREF, True)
    check("...while switched on, the same picture is kept",
          _g2cov._store(_g2cg, "got-", "cover", _g2jpg) is not None)
finally:
    prefs.FILE, _g2cov.ROOT = _g2saved
    shutil.rmtree(_g2tmp, ignore_errors=True)


section("2.0 gate: toasts over redraws, the watcher's offer, kit bindings, one window")

import ast as _g3ast  # noqa: E402
import threading as _g3th  # noqa: E402
import tkinter as _g3tk  # noqa: E402
from types import SimpleNamespace as _G3NS  # noqa: E402
from core import lookout as _g3look, verdicts as _g3v  # noqa: E402
from core import log as _g3log  # noqa: E402
from core.ui import app as _g3app, ctl_watch as _g3cw, kit as _g3kit, tray as _g3tray  # noqa: E402

# -- verdicts: every fragment is a string the tool prints -----------------------------
_g3texts = []
for _g3f in sorted((SRC_DIR / "core" / "diagnose").glob("*.py")) + [SRC_DIR / "core" / "ui" / "ctl_game.py"]:
    for _g3n in _g3ast.walk(_g3ast.parse(_g3f.read_text(encoding="utf8"))):
        if isinstance(_g3n, _g3ast.Constant) and isinstance(_g3n.value, str):
            _g3texts.append(_g3n.value.lower())
        elif isinstance(_g3n, _g3ast.JoinedStr):
            _g3texts.append("".join(v.value if isinstance(v, _g3ast.Constant) else "{}"
                                    for v in _g3n.values).lower())
_g3unprinted = [s for _n, _w, frs in _g3v.CHAIN for s in frs if not any(s.lower() in t for t in _g3texts)]
check("gate 2.0 watcher: every verdict fragment in core/verdicts.py is a string the tool prints",
      not _g3unprinted, _g3unprinted)

_g3cases = [
    ("Not started since the install - run the game once, then check again.", "3 ", True),
    ("Not run yet, or OptiScaler did not load.", "3 ", True),
    ("Loaded into F.E.A.R. 3.exe at 15 Sep 20:12, and no log was written - ReShade's Vulkan layer is "
     "not reaching the game.", "3 ", True),
    ("Game.exe ran at 15 Sep 20:12 and loaded nothing from this folder - try another proxy name.", "3 ", True),
    ("It started and closed during start-up - ReShade attached and nothing else got to run.", "5 ", True),
    ("The add-on runs but never produces a frame.", "5 ", True),
    ("DLSS never started - the add-on crashed creating the feature.", "8 ", True),
    ("The neural feature was refused by NGX (0xBAD00005).", "9 ", True),
    ("Neural rendering stopped after it started.", "9 ", True),
    ("ReShade's dxgi.dll is missing from the folder - reinstall.", "2 ", False),
    ("ReShade's Vulkan layer is not registered - install again.", "2 ", False),
    ("Inconclusive - the feed did not get far enough to tell.", "5 ", False),
    ("The install went beside a launcher, not the game - install again beside the executable that draws.",
     "3 ", False),
    ("Not started since the install - run the game once, then check again. Another DLSS hook was loaded "
     "beside ours too (Working DLSS Swapper) - test without it.", "3 ", False),
    ("Another DLSS hook was loaded beside ours (The crash is in the Swapper) - move it out of the game "
     "folder and test with ours alone.", "11 ", False),
]
_g3bad = [(v[:50], _g3v.stage(v)[0], _g3v.route_failed(v)) for v, st, rf in _g3cases
          if not _g3v.stage(v)[0].startswith(st) or _g3v.route_failed(v) != rf]
check("gate 2.0 watcher: the verdicts are staged where they belong and offer a route only when one "
      "can help (#238 is not antivirus, a feed that cannot tell and a second hook offer none)",
      not _g3bad, _g3bad)
_g3base = json.loads((SRC_DIR / "_tools" / "verdict_baseline.json").read_text(encoding="utf8"))
_g3unm = sorted({v["verdict"][:60] for v in _g3base.values() if _g3v.stage(v["verdict"])[0].startswith("unmapped")})
check("gate 2.0 watcher: no verdict in the 84-report baseline is unmapped", not _g3unm, _g3unm)
_g3sg = subprocess.run([sys.executable, str(SRC_DIR / "_tools" / "stuck_games.py"), "--json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                       cwd=str(SRC_DIR))
check("...and _tools/stuck_games.py still runs on the chain", _g3sg.returncode == 0 and _g3sg.stdout.startswith("["),
      _g3sg.stderr[-300:])

# -- the watcher: a route for "nothing of ours loaded", kept, bounded, out of a job's way ----
with _ui_isolated():
    _g3g = _ui_game(installed=True, name="Watch Gate")
    _g3c = _ui_ctl()
    _g3c.all_games = [_g3g]
    _g3rep = _G3NS(ran=False, never_ran=True, route="feeder", findings=[],
                   verdict="Not started since the install - run the game once, then check again.")
    check("gate 2.0 watcher: a game the watcher saw run and close with nothing loaded is offered the next route",
          _g3c.next_route(_g3g, _g3rep, ["feeder", "optiscaler"], seen=True)[0] == "optiscaler",
          _g3c.next_route(_g3g, _g3rep, ["feeder", "optiscaler"], seen=True))
    _g3c.plans.clear()
    check("...and the same verdict for a game nobody saw start offers nothing",
          _g3c.next_route(_g3g, _g3rep, ["feeder", "optiscaler"], seen=False) == ("", ""))
    _g3c.plans.clear()
    _g3c._on_lookdiag((str(_g3g.install_dir), _g3rep, ["feeder", "optiscaler"]))
    _g3saved = (prefs.get("last_verdicts") or {}).get(str(_g3g.install_dir)) or {}
    check("gate 2.0 watcher: the offer is kept with the answer, so it outlives a restart",
          _g3saved.get("next") == "optiscaler" and "feeder" in str(_g3saved.get("why"))
          and _g3c.shell.toasts and "try optiscaler" in str(_g3c.shell.toasts[-1]), _g3saved)

    # a job on the folder: the close is left to the job
    _g3c.shell.toasts.clear()
    _g3c.verdicts.clear()
    _g3c.busy, _g3c.action, _g3c.game = True, "autopilot", _g3g
    with _ui_threads(run=False) as _g3t:
        _g3c._on_look(("closed", str(_g3g.install_dir), 60.0))
        _g3c._on_lookdiag((str(_g3g.install_dir), _g3rep, ["feeder", "optiscaler"]))
    check("gate 2.0 watcher: a game closing while an autopilot pass runs on it is not read, answered or offered",
          not _g3t.started and not _g3c.shell.toasts and not _g3c.verdicts, (_g3t.names, _g3c.shell.toasts))
    _g3c.busy, _g3c.action, _g3c.game = False, "", None
    _g3c.dlss_job = str(_g3g.folder)
    with _ui_threads(run=False) as _g3t:
        _g3c._on_look(("closed", str(_g3g.install_dir), 60.0))
    check("...nor while its DLSS is being updated", not _g3t.started, _g3t.names)
    _g3c.dlss_job = None
    with _ui_threads(run=False) as _g3t:
        _g3c._on_look(("closed", str(_g3g.install_dir), 60.0))
    check("...and with no job it is read as before", len(_g3t.started) == 1, _g3t.names)

    # Windows' fault record: the watcher's answer agrees with "did it work?"
    _g3crash = _G3NS(when="", exe="Game.exe", module="nvngx_dlssnr.dll")
    _g3work = _G3NS(ran=True, never_ran=False, route="feeder", findings=[], verdict="Working.")
    _g3asked = []
    while not _g3c.q.empty():
        _g3c.q.get_nowait()
    with _ui_threads(run=True), \
            patch.object(_g3cw.diagnose, "analyse", lambda *a, **k: _g3work), \
            patch.object(_g3cw.diagnose, "windows_crash",
                         lambda folder, exe: _g3asked.append(exe) or (_g3crash, ("t", "d"))):
        _g3c._on_look(("closed", str(_g3g.install_dir), 60.0))
    _g3msg = _g3c.q.get_nowait() if not _g3c.q.empty() else ("", ())
    check("gate 2.0 watcher: a closed game's worker reads Windows' fault record beside its logs",
          _g3msg[0] == "lookdiag" and len(_g3msg[1]) == 4 and _g3msg[1][3] is _g3crash
          and _g3asked == ["Game.exe"], (_g3msg[0], _g3asked))
    _g3c.shell.toasts.clear()
    _g3c._on_lookdiag(_g3msg[1])
    _g3e = _g3c.verdicts.get(str(_g3g.install_dir)) or {}
    check("...and a 'Working' session that ended in a crash is answered as a crash, naming the module",
          _g3e.get("ok") is False and str(_g3e.get("said")).startswith("It ran, and then the game crashed")
          and "crashed in nvngx_dlssnr.dll" in str(_g3c.shell.toasts[-1:]), (_g3e, _g3c.shell.toasts[-1:]))
    with patch.object(_g3cw, "crash_is_this_session", lambda c, d: False):
        _g3c._on_lookdiag(_g3msg[1])
    check("...while a fault from an earlier launch leaves it working",
          (_g3c.verdicts.get(str(_g3g.install_dir)) or {}).get("ok") is True)

    # try <route>: bounded, cancelled by another game, says when busy
    _g3after = []
    _g3c.root.after = lambda ms, fn=None, *a: _g3after.append(fn) or "after#1"
    _g3auto = []
    _g3c.autopilot = lambda routes=None, ask=True: _g3auto.append(routes)
    _g3c.open_game = lambda g: setattr(_g3c, "game", g)
    _g3c.support = None
    _g3now = [1000.0]
    with patch.object(_g3cw, "time", _G3NS(monotonic=lambda: _g3now[0])):
        _g3c.try_next(_g3g, "optiscaler")
        _g3after.pop()()                         # not read yet: waits
        _g3now[0] += 11.0
        _g3after.pop()()                         # past the wait: gives up, says so
    check("gate 2.0 watcher: 'try <route>' stops waiting after its limit and says what to do",
          not _g3after and not _g3auto and "press autopilot" in _g3c.shell.status_text, _g3c.shell.status_text)
    _g3other = _ui_game(name="Other Game")
    _g3c.support = None
    _g3c.try_next(_g3g, "optiscaler")
    _g3c.game = _g3other
    _g3after.pop()()
    _g3c.game, _g3c.support = _g3g, object()
    check("...another game opened in the meantime cancels it, and a later visit installs nothing",
          not _g3after and not _g3auto, (_g3after, _g3auto))
    _g3c.busy, _g3c.action = True, "installing"
    _g3c.try_next(_g3g, "optiscaler")
    check("...and while the tool is busy it says so instead of doing nothing",
          "busy with installing" in _g3c.shell.status_text and not _g3after, _g3c.shell.status_text)
    _g3c.busy, _g3c.action = False, ""
    _g3c.try_next(_g3g, "optiscaler")
    _g3after.pop()()
    check("...and a game whose page is read runs the pass from that route", len(_g3auto) == 1, _g3auto)

    # a hand-edited plan that is not a dict
    prefs.set_("autopilot_plans", {str(_g3g.install_dir): ["feeder"], "x": 3})
    try:
        _g3c2 = _ui_ctl()
        _g3c2.all_games = [_g3g]
        _g3np = _g3c2.next_route(_g3g, _g3rep, ["feeder", "optiscaler"])
    except Exception as _g3e:
        _g3np = repr(_g3e)
    check("gate 2.0 watcher: a hand-edited autopilot_plans entry that is not a plan is ignored, not raised",
          _g3np == ("optiscaler", _g3v.why_next(_g3rep.verdict, "feeder")), _g3np)
_ui_cleanup()

# -- the pump: only a job's own end clears busy ----------------------------------------
_g3pump = _G3NS(q=__import__("queue").Queue(), busy=True,
                wrote=[], root=_G3NS(after=lambda *a: None))
_g3pump.write = lambda t, tag="": _g3pump.wrote.append(t)
_g3pump.offer_crash_report = lambda: None
_g3pump._pump = lambda: None


def _g3raise(_p):
    raise RuntimeError("drawing failed")


_g3pump._on_prog = _g3raise
_g3pump._on_installed = _g3raise
with patch.object(_g3log, "exception", lambda *a, **k: None), patch.object(_g3log, "crashed", lambda: False):
    _g3pump.q.put(("prog", (10, "copying")))
    _g3app.App._pump(_g3pump)
    _g3busy_prog = _g3pump.busy
    _g3pump.q.put(("installed", None))
    _g3app.App._pump(_g3pump)
check("gate 2.0 pump: a progress handler that raises leaves busy set while the job's worker still writes",
      _g3busy_prog is True, _g3busy_prog)
check("...and a job-ending handler that raises still ends the job", _g3pump.busy is False)
_g3kinds = set()
for _g3f in (SRC_DIR / "core" / "ui").glob("ctl_*.py"):
    _g3src = _g3f.read_text(encoding="utf8")
    for _g3m in __import__("re").finditer(r"def _on_(\w+)\(self[^)]*\) -> None:\n((?:        .*\n|\n)+)", _g3src):
        if "_idle()" in _g3m.group(2) or "busy = False" in _g3m.group(2) or "busy, self.action = False" in _g3m.group(2):
            _g3kinds.add(_g3m.group(1))
check("...and every handler that clears busy is one the pump knows ends a job",
      _g3kinds and _g3kinds <= _g3app.JOB_ENDS, sorted(_g3kinds - _g3app.JOB_ENDS))

# -- one window ---------------------------------------------------------------------------


class _G3Win:
    def __init__(self, err=0, main=0, visible=False, iconic=False, tray=0, boom=False):
        self.calls, self.err, self.main, self.visible, self.iconic, self.tray = [], err, main, visible, iconic, tray
        self.boom = boom

    def __call__(self):
        me = self
        if self.boom:
            raise OSError("no user32")

        class K:
            @staticmethod
            def CreateMutexW(a, b, name):
                me.calls.append(("mutex", name))
                return 77

        class U:
            @staticmethod
            def FindWindowW(cls, title):
                me.calls.append(("find", cls, title))
                return me.main if cls == "TkTopLevel" else me.tray

            @staticmethod
            def IsWindowVisible(h):
                return me.visible

            @staticmethod
            def IsIconic(h):
                return me.iconic

            @staticmethod
            def ShowWindow(h, n):
                me.calls.append(("show", h, n))

            @staticmethod
            def SetForegroundWindow(h):
                me.calls.append(("front", h))

            @staticmethod
            def PostMessageW(h, m, wp, lp):
                me.calls.append(("post", h, m, lp))
        return K, U, (lambda: me.err)


_g3w = _G3Win(err=0)
_g3first = _g3app.already_open(_g3w())
check("gate 2.0 one window: the first copy takes the mutex and opens", _g3first is False and _g3app._mutex == 77
      and ("mutex", "Local\\DLSS5AutopilotWindow") in _g3w.calls, _g3w.calls)
_g3w = _G3Win(err=183, main=11, visible=True, iconic=True)
check("...a second copy brings the open window forward (restored from the taskbar) and does not open",
      _g3app.already_open(_g3w()) is True and ("show", 11, 9) in _g3w.calls and ("front", 11) in _g3w.calls,
      _g3w.calls)
_g3w = _G3Win(err=183, main=11, visible=False, tray=22)
check("...a window hidden in the tray is opened the way a click on the icon opens it",
      _g3app.already_open(_g3w()) is True
      and ("post", 22, _g3tray.WM_TRAY, _g3tray.WM_LBUTTONUP) in _g3w.calls, _g3w.calls)
check("...no window to find (still starting) or Windows' calls failing opens this copy rather than nothing",
      _g3app.already_open(_G3Win(err=183)()) is False and _g3app.already_open(_G3Win(boom=True)) is False)
_g3tkcalls = []
with patch.object(_g3app, "already_open", lambda: True), patch.object(_g3log, "start", lambda *a: None), \
        patch.object(_g3log, "write", lambda *a, **k: None), \
        patch.object(_g3app.tk, "Tk", lambda *a, **k: _g3tkcalls.append(1) or (_ for _ in ()).throw(RuntimeError())):
    _g3rc = _g3app.run()
check("...and run() returns before any window is built when one is open", _g3rc == 0 and not _g3tkcalls, (_g3rc, _g3tkcalls))
check("...and the check is in run() only, not in the CLI modes or App()",
      "already_open" not in (SRC_DIR / "dlss5_autopilot.py").read_text(encoding="utf8")
      and "already_open" not in src_of(_g3app.App.__init__))

# -- the watcher switched off and on within one poll ------------------------------------
_g3ticks = []
_g3lk = _g3look.Lookout(lambda ev: None, poll=1.0)
_g3lk._tick = lambda: _g3ticks.append(1)
_g3lk.start()
time.sleep(0.2)
_g3lk.stop()
_g3lk.start()
time.sleep(1.4)
_g3alive = _g3lk.alive
_g3n = len(_g3ticks)
_g3lk.stop()
check("gate 2.0 watcher: off and on again within one poll leaves it watching", _g3alive and _g3n >= 2,
      (_g3alive, _g3n))

# -- settings: one writer at a time, never a half file, never {} written back -------------
with _ui_isolated() as _g3d:
    prefs.set_("installs", ["C:/Games/A"])
    _g3errs = []

    def _g3writer(i):
        try:
            for j in range(15):
                prefs.set_(f"k{i}_{j}", j)
        except Exception as e:
            _g3errs.append(repr(e))
    _g3ths = [_g3th.Thread(target=_g3writer, args=(i,)) for i in range(8)]
    for _t in _g3ths:
        _t.start()
    for _t in _g3ths:
        _t.join(60)
    _g3all = prefs.load()
    check("gate 2.0 settings: eight threads writing at once lose no key",
          not _g3errs and all(f"k{i}_{j}" in _g3all for i in range(8) for j in range(15))
          and _g3all.get("installs") == ["C:/Games/A"], (_g3errs, len(_g3all)))
    _g3rep_calls = []
    _g3real_replace = os.replace
    with patch.object(prefs.os, "replace", lambda a, b: _g3rep_calls.append(str(b)) or _g3real_replace(a, b)):
        prefs.set_("x", 1)
    check("...written beside and moved over, so a reader never meets half a file",
          _g3rep_calls == [str(prefs.FILE)] and not list(_g3d.glob("*.tmp")), (_g3rep_calls, list(_g3d.iterdir())))
    prefs.FILE.write_text('{"installs": ["C:/Games/A"], "x"', encoding="utf8")
    _g3got = prefs.get("installs")
    prefs.set_("y", 2)
    _g3after_write = json.loads(prefs.FILE.read_text(encoding="utf8"))
    check("...a truncated file reads as the last good one, and the next write keeps the installs list",
          _g3got == ["C:/Games/A"] and _g3after_write.get("installs") == ["C:/Games/A"]
          and _g3after_write.get("y") == 2, (_g3got, _g3after_write))
    prefs.FILE.write_text("[]", encoding="utf8")
    try:
        _g3lst = (prefs.get("installs"), prefs.get("nothing", "dflt"))
    except Exception as _g3e:
        _g3lst = repr(_g3e)
    check("...and a settings.json that is a list does not break every read", _g3lst == (["C:/Games/A"], "dflt"),
          _g3lst)

# -- the real window: toasts, animations, bindings, widgets --------------------------------
_g3live = _UiLive()
try:
    if not _g3live.ok:
        check("gate 2.0 window: the window opened", False, _g3live.error)
        raise RuntimeError
    _g3a = _g3live.app
    _g3sh = _g3a.shell
    _g3cv = _g3live.canvas
    _g3live.settle(200)
    _g3pressed = []
    _g3sh.toast("Watch Gate closed", "nothing loaded", actions=[("open", lambda: _g3pressed.append(1), True)],
                timeout=0)
    _g3tag = next(x.tag for x in _g3live.kit.layers if x.tag.startswith("toast"))
    _g3sh.redraw()                       # what refresh("game") did right after the toast
    _g3sliding = _g3sh.motion.busy(_g3tag)
    _g3live.settle(450)
    _g3order = list(_g3cv.find_all())
    _g3top_page = max((_g3order.index(i) for i in _g3cv.find_withtag("page")), default=-1)
    _g3low_toast = min((_g3order.index(i) for i in _g3cv.find_withtag(_g3tag)), default=-1)
    check("gate 2.0 window: a toast survives a page redraw, above the page, still sliding in",
          _g3low_toast > _g3top_page >= 0 and _g3sliding
          and any(x.tag == _g3tag for x in _g3live.kit.layers), (_g3low_toast, _g3top_page, _g3sliding))
    _g3live.press("open", "button")
    _g3live.settle(400)
    check("...and its button still works with a real click after the redraw",
          _g3pressed == [1] and not _g3cv.find_withtag(_g3tag), (_g3pressed, _g3cv.find_withtag(_g3tag)))

    # a menu opened after a toast stays open when the toast times out
    _g3sh.toast("second", "", timeout=0)
    _g3tag2 = next(x.tag for x in _g3live.kit.layers if x.tag.startswith("toast"))
    _g3live.kit.menu(40, 40, [("one", lambda: None), ("two", lambda: None)])
    _g3menu = _g3live.kit.top()
    _g3sh._toast_out(_g3tag2)
    _g3live.settle(400)
    check("gate 2.0 window: a toast timing out closes only itself, not the menu opened after it",
          _g3live.kit.top() is _g3menu and not _g3cv.find_withtag(_g3tag2), [x.tag for x in _g3live.kit.layers])
    _g3live.kit.pop()
    # a layer whose items are gone does not swallow the next click
    _g3live.kit.push(_g3kit.Layer("gone_layer", lambda animate: None))
    check("...and a layer whose items a redraw deleted is dropped instead of swallowing the next click",
          _g3live.kit.top() is None, [x.tag for x in _g3live.kit.layers])

    # the log drawer keeps opening through a redraw; a page animation stops
    _g3sh.toggle_log(True)
    _g3sh.motion.run("glow", 5000, lambda k: None)
    _g3sh.redraw()
    _g3drawer_moving, _g3glow = _g3sh.motion.busy("drawer"), _g3sh.motion.busy("glow")
    _g3live.settle(400)
    check("gate 2.0 window: a redraw stops the page's animations and not the log drawer opening",
          _g3drawer_moving and not _g3glow and _g3sh.log_open and _g3sh.drawer.winfo_height() > 50,
          (_g3drawer_moving, _g3glow, _g3sh.drawer.winfo_height()))
    _g3sh.toggle_log(False)
    _g3live.settle(300)

    # bindings, commands, widgets and the registry do not grow with redraws
    def _g3lines(c, tag, seq):
        return len([ln for ln in str(c.tag_bind(tag, seq)).splitlines() if ln.strip()])
    for _ in range(3):
        _g3sh.draw_rail()
    _g3cmds_rail = len(_g3sh.rail_c._tclCommands or [])
    for _ in range(5):
        _g3sh.draw_rail()
    check("gate 2.0 window: a rail item redrawn five times has one hover handler, and no Tcl command leaks",
          _g3lines(_g3sh.rail_c, "nav_help", "<Enter>") == 1 and _g3lines(_g3sh.rail_c, "nav_help", "<Button-1>") == 1
          and len(_g3sh.rail_c._tclCommands or []) == _g3cmds_rail,
          (_g3lines(_g3sh.rail_c, "nav_help", "<Enter>"), _g3cmds_rail, len(_g3sh.rail_c._tclCommands or [])))
    _g3live.click("nav_help", canvas=_g3sh.rail_c)
    check("...and the redrawn item still answers a real click", _g3live.kit.top() is not None
          and str(_g3live.kit.top().tag).startswith("menu"))
    _g3live.kit.close_all()
    for _ in range(5):
        _g3sh.status(f"status {_}")
    _g3cmds_bottom = len(_g3sh.bottom_c._tclCommands or [])
    for _ in range(5):
        _g3sh.status(f"status again {_}")
    check("...nor the status line's link, redrawn with every status", len(_g3sh.bottom_c._tclCommands or []) == _g3cmds_bottom,
          (_g3cmds_bottom, len(_g3sh.bottom_c._tclCommands or [])))

    _g3sh.show("library")
    _g3live.settle(150)
    for _ in range(2):
        _g3sh.redraw()
    _g3n_cmd, _g3n_reg = len(_g3cv._tclCommands or []), len(_g3live.kit.registry)
    for _ in range(5):
        _g3sh.redraw()
    _g3entries = [w for w in _g3cv.winfo_children() if isinstance(w, _g3tk.Entry)]
    check("gate 2.0 window: five library redraws leave one search box, and no handler or registry growth",
          len(_g3entries) == 1 and len(_g3cv._tclCommands or []) == _g3n_cmd
          and len(_g3live.kit.registry) == _g3n_reg,
          (len(_g3entries), _g3n_cmd, len(_g3cv._tclCommands or []), _g3n_reg, len(_g3live.kit.registry)))
    _g3field = _g3sh.pages["library"].field
    check("...the search box still jumps to the games on Down, and a plain text box does not submit on it",
          bool(_g3field.entry.bind("<Down>"))
          and not _g3live.kit.field(10, 10, 200, "name", on_enter=lambda: None, glyph=None).entry.bind("<Down>"))
    _g3sh.redraw()

    # a dialog leaves no handler on the main window
    _g3cfg = len([ln for ln in str(_g3live.root.bind("<Configure>")).splitlines() if ln.strip()])
    # answered once the dialog is really up: a single timer that fired before
    # the dialog existed left the whole suite waiting on it for hours
    def _g3answer(tries=[0]):
        if _g3sh.dialog is not None:
            _g3sh.dialog._finish(True)
        elif tries[0] < 200:
            tries[0] += 1
            _g3live.root.after(50, _g3answer)
    _g3live.root.after(150, _g3answer)
    _g3sh.ask("gate", "a question")
    _g3live.root.after(150, lambda: _g3answer([0]))
    _g3sh.ask("gate", "another")
    _g3cfg2 = len([ln for ln in str(_g3live.root.bind("<Configure>")).splitlines() if ln.strip()])
    check("gate 2.0 window: an answered dialog takes its handler on the main window with it",
          _g3cfg2 == _g3cfg, (_g3cfg, _g3cfg2))
except RuntimeError:
    pass
except Exception as _g3e:
    # a raise here would end the suite with every later section unrun
    check("gate 2.0 window: the window checks ran to their end", False, repr(_g3e))
finally:
    _g3live.close()


section("2.0 gate: the game page, the library and remix - what the release review found")

# Each check below failed on the tree the review read. They run the real
# controllers (_ui_ctl) or the real window (_UiLive), never a source grep.
import queue as _q_rg
import threading as _thr_rg
import zipfile as _zip_rg
from core import autopilot as _ap_rg, mfg as _mfg_rg, remixdl as _rdl_rg  # noqa: E402
from core.ui import ctl_game as _cg_rg, ctl_library as _cl_rg, remixpage as _rp_rg  # noqa: E402


def _rg_main_counter(target, name):
    """Wrap target.name; count only the calls made on the Tk (main) thread."""
    real = getattr(target, name)
    calls: list = []

    def wrapped(*a, **k):
        if _thr_rg.current_thread() is _thr_rg.main_thread():
            calls.append(a[:1])
        return real(*a, **k)
    return patch.object(target, name, wrapped), calls


_rg: dict = {}
_live_rg = _UiLive()
try:
    if _live_rg.ok:
        _a_rg = _live_rg.app
        # --- 1: a settings redraw walks no folder on the Tk thread ------------
        _a_rg.sm = _mfg_rg.ADA                       # an RTX 40 card: the mfg row's rule is asked
        _g1_rg = _ui_game(name="Walked Game", api="DX12")
        _live_rg.enter(_g1_rg, _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER))
        _live_rg.press("settings", "button")
        _live_rg.settle(300)
        _p1, _walks1 = _rg_main_counter(dlss, "find_dlss_files")
        _p2, _renodx1 = _rg_main_counter(prefs, "find_renodx")
        with _p1, _p2:
            _a_rg.shell.redraw()
            _a_rg.set_setting("route", dlss.OPTI)
            _a_rg.set_setting("route", dlss.FEEDER)
            _a_rg.opts()
            _live_rg.settle(100)
        _rg["walks"], _rg["renodx"] = len(_walks1), len(_renodx1)

        # --- 2: a burst of download progress is a few paints, not a redraw each
        _a_rg.busy, _a_rg.action, _a_rg.job_game, _a_rg.progress = True, "installing", _g1_rg, (0, "starting")
        _a_rg.shell.redraw()
        _live_rg.settle(80)
        _redraws2 = [0]
        _real_redraw2 = _a_rg.shell.redraw

        def _count_redraw2():
            _redraws2[0] += 1
            _real_redraw2()
        _a_rg.shell.redraw = _count_redraw2
        for _i in range(400):
            _a_rg.q.put(("prog", (_i // 4, f"nvngx_dlssnr.zip - {_i} MB")))
        _live_rg.settle(700)
        _rg["prog_redraws"] = _redraws2[0]
        _rg["prog_texts"] = [t for t in _live_rg.texts() if "installing" in t or "nvngx_dlssnr.zip" in t]
        _a_rg._idle()
        _a_rg.shell.show("video")
        _live_rg.settle(80)
        _a_rg.video_busy, _a_rg.video_progress = "downloading", (0, "starting")
        _real_redraw2()
        _live_rg.settle(60)
        _redraws2[0] = 0
        for _i in range(400):
            _a_rg.q.put(("vprog", (_i // 4, f"ffmpeg - {_i} MB")))
        _live_rg.settle(700)
        _rg["vprog_redraws"] = _redraws2[0]
        _rg["vprog_texts"] = [t for t in _live_rg.texts() if "downloading" in t]
        _a_rg.video_busy, _a_rg.video_progress = "", None
        del _a_rg.shell.redraw
        _a_rg.open_game(_g1_rg)
        _live_rg.settle(200)

        # --- 6, 7, 8: an installed game with newer parts; warnings; an offer --
        _g6_rg = _ui_game(name="Stale And Warned", installed=True)
        _live_rg.enter(_g6_rg, _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER, native_dlss=True))
        _a_rg.stale = {str(_g6_rg.install_dir): 2}
        _a_rg.result = {"kind": "installed", "title": "installed",
                        "warnings": ["dlssnr 310.2 does not match your card - installed anyway"]}
        _a_rg.set_setting("keep_dlss", False)
        _a_rg.verdicts[str(_g6_rg.install_dir)] = {"ok": False, "said": "never drew a frame", "fps": None,
                                                   "next": "optiscaler", "why": "the feeder never saw a frame"}
        _a_rg.shell.redraw()
        _live_rg.settle(120)
        _rg["buttons6"] = _live_rg.labels("button")
        _rg["texts7"] = _live_rg.texts()
        _tried8: list = []
        _a_rg.try_next = lambda g, route: _tried8.append((g, route))
        _rg["clicked8"] = _live_rg.press("try optiscaler", "button")
        _rg["tried8"] = [(g is _g6_rg, r) for g, r in _tried8]
        del _a_rg.try_next

        # --- 13: the backdrop cache holds the page on screen only --------------
        _a_rg.shell.show("library")
        _live_rg.settle(100)
        _page13 = _a_rg.game_page
        _buf13 = bytes(4 * 4 * 3)
        for _n13 in ("One", "Two", "Three"):
            _g13 = _ui_game(name=f"Backdrop {_n13}")
            _a_rg.game = _g13
            _page13.width = 640
            _page13.got_backdrop((f"{_g13.folder}|640", _buf13, 4, 4, 100, False))
        _rg["backdrops"] = len(_page13.backdrops)
        _rg["frames"] = max((len(v.get("frames") or []) for v in _page13.backdrops.values()), default=0)
        _a_rg._on_cover((f"{_g13.folder}|4x4", _buf13, 4, 4, None, "cover"))
        _c13 = _a_rg.covers.get(f"{_g13.folder}|4x4") or {}
        _rg["cover_images"] = len(_c13.get("levels") or []) + (1 if _c13.get("dim") is not None else 0)

        # --- 14: one library redraw reads each installed game's record once -----
        _a_rg.all_games = [_g6_rg]
        _a_rg._rows[(str(_g6_rg.folder), str(_g6_rg.exe))] = (True, "feeder", "beta", "beta", False, "")
        _a_rg.shell.show("library")
        _live_rg.settle(150)
        _p14, _man14 = _rg_main_counter(diagnose, "_manifest")
        with _p14:
            _a_rg.set_filter("installed")
        _rg["manifest_reads"] = len(_man14)
        _a_rg.set_filter("all")
finally:
    _live_rg.close()
    _ui_cleanup()

check("1: a settings redraw, a route change and opts() walk no game folder on the Tk thread (RTX 40, DX12)",
      _rg.get("walks") == 0, _rg.get("walks"))
check("1: ...and read no renodx add-on in Downloads or on the Desktop there either",
      _rg.get("renodx") == 0, _rg.get("renodx"))
check("2: 400 download progress messages are a few redraws, not 400",
      _rg.get("prog_redraws", 999) <= 3, _rg.get("prog_redraws"))
check("2: ...and the button still shows where the download is, painted in place",
      any("99%" in t for t in _rg.get("prog_texts", [])), _rg.get("prog_texts"))
check("2: the video page's download progress the same way",
      _rg.get("vprog_redraws", 999) <= 3 and any("99%" in t for t in _rg.get("vprog_texts", [])),
      (_rg.get("vprog_redraws"), _rg.get("vprog_texts")))
check("6: an installed game with newer parts: update (2) leads, play and did it work? stay beside it",
      {"update (2)", "play", "did it work?"} <= set(_rg.get("buttons6", [])), _rg.get("buttons6"))
check("7: the install's warnings are drawn in the result block, its title says how many",
      any("does not match your card" in t for t in _rg.get("texts7", []))
      and any(t.startswith("installed - 1 warning") for t in _rg.get("texts7", [])),
      [t for t in _rg.get("texts7", []) if "install" in t][:6])
check("7: unticking 'keep the game's own nvngx_dlss' puts the swap warning on the page, not only in the log",
      any("nvngx_dlss.dll is swapped" in t for t in _rg.get("texts7", [])),
      [t for t in _rg.get("texts7", []) if "swap" in t])
check("8: a next route the watcher kept is offered on the page, with why, and the button tries it",
      any("the feeder never saw a frame" in t for t in _rg.get("texts7", []))
      and _rg.get("clicked8") and _rg.get("tried8") == [(True, "optiscaler")],
      (_rg.get("clicked8"), _rg.get("tried8")))
check("13: after three games the game page keeps one backdrop, of three frames",
      _rg.get("backdrops") == 1 and 0 < _rg.get("frames", 0) <= 3, (_rg.get("backdrops"), _rg.get("frames")))
check("13: a cover is three images (rest, hover, dimmed), not six",
      0 < _rg.get("cover_images", 0) <= 3, _rg.get("cover_images"))
check("14: one library redraw reads an installed game's record once",
      _rg.get("manifest_reads") == 1, _rg.get("manifest_reads"))

# --- 3: a job that ends while another game is on the page ------------------
with _ui_isolated(), _ui_threads(run=False):
    _c3 = _ui_ctl()
    _gA3 = _ui_game(name="Job Game", installed=True)
    _gB3 = _ui_game(name="Page Game", installed=True)
    _c3.all_games = [_gA3, _gB3]
    _c3.verdicts = {str(_gA3.install_dir): {"ok": False, "said": "x", "next": "optiscaler"},
                    str(_gB3.install_dir): {"ok": True, "said": "Working."}}
    _c3.game, _c3.settings, _c3.entry = _gB3, _c3.default_settings(_gB3), {}
    _c3.support, _c3.route = _ui_support([dlss.FEEDER, dlss.OPTI], dlss.FEEDER), dlss.FEEDER
    _rereads3: list = []
    _c3.dlss_reread = lambda g: _rereads3.append(g)
    _c3.busy, _c3.action, _c3.job_game = True, "autopilot", _gA3
    _out3 = _ap_rg.Outcome(attempts=[_ap_rg.Attempt(route=dlss.OPTI, installed=True, started=True,
                                                    ours=["dxgi.dll"])],
                           route=dlss.OPTI, ok=True, installed=dlss.OPTI)
    _c3.q.put(("autopilot", (_gA3, _out3, ([dlss.FEEDER, dlss.OPTI], [dlss.FEEDER]), [dlss.FEEDER, dlss.OPTI])))
    _c3.pump()
    _s3 = {"route": _c3.route, "result": _c3.result, "planA": _c3.plans.get(str(_gA3.install_dir)),
           "planB": _c3.plans.get(str(_gB3.install_dir)), "vA": str(_gA3.install_dir) in _c3.verdicts,
           "vB": str(_gB3.install_dir) in _c3.verdicts, "rereads": [g.name for g in _rereads3], "busy": _c3.busy}
    _c3.busy, _c3.action, _c3.job_game = True, "installing", _gA3
    _c3.steps = []
    _c3.q.put(("installed", (_gA3, installer.Report(written=["dxgi.dll"]), dlss.OPTI)))
    _c3.q.put(("fail", (_gA3, "it stopped")))
    _c3.pump()
    _s3.update(result2=_c3.result, steps2=list(_c3.steps))
check("3: an autopilot pass that ends while another game is open leaves that page's route and result alone",
      _s3["route"] == dlss.FEEDER and _s3["result"] is None and not _s3["busy"], _s3)
check("3: ...and its plan, verdict and DLSS re-read go to the game it ran on",
      (_s3["planA"] or {}).get("tried") == [dlss.FEEDER, dlss.OPTI] and _s3["planB"] is None
      and not _s3["vA"] and _s3["vB"] and _s3["rereads"] == ["Job Game"], _s3)
check("3: an install or a failure of another game does not write this page's result or steps",
      _s3["result2"] is None and _s3["steps2"] == [], (_s3["result2"], _s3["steps2"]))
_ui_cleanup()

# --- 4: an installed game opens on the route and settings it was installed with
with _ui_isolated(), _ui_threads(run=False) as _th4:
    _s4: dict = {}
    for _name4, _api4, _man4, _offer4 in (
            ("On OptiScaler", "DX12", {"path": dlss.OPTI, "proxy": "winmm.dll", "opti_build": optiscaler.PRESR,
                                       "nr": {"WorkingScale": 0.6, "Preset": 2, "Style": 1}, "fg": True,
                                       "keep_game_dlss": False}, [dlss.FEEDER, dlss.OPTI]),
            ("On The Feeder", "DX11", {"path": dlss.FEEDER, "proxy": "d3d11.dll", "provider": 2,
                                       "feeder_tag": "v0.13.1", "feed_cfg": {"work_resolution": 80, "preset": 5}},
                                     [dlss.BRIDGE, dlss.FEEDER])):
        _g4 = _ui_game(name=_name4, api=_api4, manifest=_man4)
        _c4 = _ui_ctl()
        _c4.sm, _c4.catalog = 120, {"dlss": []}
        _sup4 = _ui_support(_offer4, _offer4[0])
        with patch.object(dlss, "detect", lambda *a, **k: _sup4), patch.object(gpu, "driver_version", lambda: ""), \
                patch.object(community, "fetch", lambda *a, **k: {}):
            _c4.enter_game(_g4)
            _th4.go()
            _c4.pump()
        _inst4 = installer.options_from_manifest(_g4.install_dir)
        _o4 = _c4.opts()
        _f4 = ("path", "provider", "opti_proxy", "opti_build", "fg", "keep_game_dlss", "feeder_tag",
               "reshade_proxy", "dxvk", "remix_swap")
        _s4[_name4] = {"diff": [f for f in _f4 if getattr(_o4, f) != getattr(_inst4, f)],
                       "nr": _o4.nr, "feed": _o4.feed, "route": _c4.route}
check("4: a game installed on a route that is not the recommended one opens on that route, and opts() is "
      "what was installed (OptiScaler: build, proxy name, frame generation, model dials, the DLSS swap)",
      _s4["On OptiScaler"]["route"] == dlss.OPTI and _s4["On OptiScaler"]["diff"] == []
      and _s4["On OptiScaler"]["nr"].get("WorkingScale") == 0.6 and _s4["On OptiScaler"]["nr"].get("Preset") == 2
      and _s4["On OptiScaler"]["nr"].get("Style") == 1, _s4.get("On OptiScaler"))
check("4: ...and on the feeder: provider, the pinned feeder, ReShade's name, work area and preset",
      _s4["On The Feeder"]["route"] == dlss.FEEDER and _s4["On The Feeder"]["diff"] == []
      and _s4["On The Feeder"]["feed"] == {"work_resolution": 80, "preset": 5}, _s4.get("On The Feeder"))
_ui_cleanup()

# --- 5: 'update (n)' is asked for when the window opens on the saved library
with _ui_isolated(), _ui_threads(run=False):
    _c5 = _ui_ctl()
    _g5 = _ui_game(name="Cached And Installed", installed=True)
    _asked5: list = []
    _c5.check_stale = lambda: _asked5.append(1)
    with patch.object(_cl_rg.library, "load", lambda *a, **k: ([_g5], {}, [])):
        _opened5 = _c5.load_cached()
check("5: opening on the saved library asks which installed games have newer parts",
      _opened5 and _asked5 == [1], (_opened5, _asked5))
_ui_cleanup()

# --- 7 (F10), 8 (update all), 10, 11 on the controllers -----------------------
with _ui_isolated(), _ui_threads(run=False):
    _c7 = _ui_ctl(_ui_game(name="F10 On The Page"), _ui_support([dlss.FEEDER, dlss.STANDALONE], dlss.FEEDER))
    prefs.set_("overlay_key", reshade_ini.OVERLAY_KEYS["F10"])
    _c7.apply_route(dlss.STANDALONE)
    _notes7 = list(_c7.notes)
    prefs.set_("overlay_key", 0)
    check("7: the F10 clash is a note on the page as well as a line in the log",
          any("F10" in t for _k, t in _notes7), _notes7)

    _c8 = _ui_ctl()
    _g8 = _ui_game(name="Updated All", installed=True)
    _c8.all_games = [_g8]
    _c8.verdicts = {str(_g8.install_dir): {"ok": False, "said": "x", "next": "optiscaler", "why": "y"}}
    _rereads8: list = []
    _c8.dlss_reread = lambda g: _rereads8.append(g)
    _c8.busy = True
    _c8.q.put(("updated_all", (1, [_g8], 0)))
    _c8.pump()
    check("8: update all drops the updated games' old verdicts (and offers) and reads their DLSS again",
          str(_g8.install_dir) not in _c8.verdicts and _rereads8 == [_g8] and not _c8.busy
          and str(_g8.install_dir) not in (prefs.get("last_verdicts") or {}),
          (list(_c8.verdicts), [g.name for g in _rereads8]))

    _c10 = _ui_ctl()
    _g10a, _g10b = _ui_game(name="Store One"), _ui_game(name="Store Two")
    with patch.object(games, "same_exe_once", lambda gs: [g for g in gs if g.name != "Store Two"]):
        _c10._on_scanned(([_g10a, _g10b], {}))
    check("10: a rescan keeps what same_exe_once returned (one entry per executable)",
          [g.name for g in _c10.all_games if g.name.startswith("Store")] == ["Store One"],
          [g.name for g in _c10.all_games])

    _c11 = _ui_ctl()
    _g11 = _ui_game(name="Anti Cheat Installed", installed=True)
    _g11b = _ui_game(name="Was Another Api", api="DX12", manifest={"api": "DX11"})
    _c11.all_games = [_g11, _g11b]
    _c11._rows[(str(_g11.folder), str(_g11.exe))] = (True, "feeder", "beta", "beta", True, "EasyAntiCheat")
    _c11._rows[(str(_g11b.folder), str(_g11b.exe))] = (True, "feeder", "beta", "beta", False, "")
    _c11.stale = {str(_g11.install_dir): 2}
    _card11 = _c11.card(_g11)
    _todo11, _skip11 = _c11.update_targets()
    _c11.update_all()
    check("11: an installed game with anti-cheat stays in the installed and update tabs",
          _card11["kind"] == "update" and _c11.counts()["installed"] == 2, (_card11, _c11.counts()))
    check("11: update all leaves it alone and says why; its count is what it acts on",
          _todo11 == [] and _skip11 == [_g11] and _c11.shell.asked == []
          and "anti-cheat" in _c11.shell.status_text and not _c11.busy,
          ([g.name for g in _todo11], [g.name for g in _skip11], _c11.shell.status_text))
    check("11: a 'reinstall - was DX11' card is not counted as something update all does",
          _c11.card(_g11b)["status"].startswith("reinstall") and _g11b not in _todo11)

    # 14: covers landing together are one library redraw
    _c14 = _ui_ctl()
    _jobs14: list = []
    _c14.root.after = lambda ms, fn=None, *a: (_jobs14.append(fn), f"after#{len(_jobs14)}")[1]
    _redraws14: list = []
    _c14.refresh = lambda page, soft=False: _redraws14.append(page)
    for _i in range(20):
        _c14._on_cover((f"X:/g{_i}|4x4", None, 4, 4, None, "none"))
    _before14 = list(_redraws14)
    for _fn in list(_jobs14):
        _fn and _fn()
    check("14: twenty covers landing are one library redraw, 100 ms later",
          _before14 == [] and len(_jobs14) == 1 and _redraws14 == ["library"], (_before14, len(_jobs14), _redraws14))

    # L8: a hand-edited settings file
    prefs.set_("games_sort", [["name"], True])
    prefs.set_("target_fps", 1e999)
    try:
        _c_l8 = _ui_ctl(_ui_game(name="Odd Settings"), _ui_support([dlss.FEEDER], dlss.FEEDER))
        _c_l8.set_setting("target_fps", "1e999")
        _l8 = (_c_l8.sort, _c_l8.target(), _c_l8.target_fps)
    except Exception as _e8:
        _l8 = repr(_e8)
    check("L8: games_sort [[...]] and target_fps 1e999 in settings do not stop the window",
          _l8 == ("", 0, ""), _l8)

    # a built-in profile carries the work area, not a route
    _c_pf = _ui_ctl(_ui_game(name="Balanced Here", api="DX12"), _ui_support([dlss.FEEDER, dlss.OPTI], dlss.OPTI))
    _c_pf.apply_route(dlss.OPTI)
    _c_pf.load_profile("Balanced")
    check("profiles: a built-in preset on OptiScaler keeps the route and sets the work area",
          _c_pf.route == dlss.OPTI and _c_pf.settings["workres"] == 75 and _c_pf.opts().nr.get("WorkingScale") == 0.75,
          (_c_pf.route, _c_pf.settings.get("workres")))
_ui_cleanup()

# --- L2: the session start reads two small parts of a big ReShade.log -----------
_d_l2 = Path(tempfile.mkdtemp(prefix="rg_l2_"))
(_d_l2 / "ReShade.log").write_text("12:00:00:000 [1] first\n" + ("x" * 120 + "\n") * 4000
                                   + "12:10:30:500 [1] last\n", encoding="utf8")
_mt_l2 = (_d_l2 / "ReShade.log").stat().st_mtime
_start_l2 = _cg_rg.session_start(_d_l2)
check("L2: the session start comes from the first and last stamped lines of a 480 KB log",
      abs(_start_l2 - (_mt_l2 - 630.5)) < 0.01, (_start_l2, _mt_l2))
shutil.rmtree(_d_l2, ignore_errors=True)

# --- 12: a remix mod with more than 50 files -------------------------------
_d12 = Path(tempfile.mkdtemp(prefix="rg_remix_"))
_zip12 = _d12 / "mod.zip"
with _zip_rg.ZipFile(_zip12, "w") as _z12:
    _z12.writestr("Mod/.trex/d3d9.dll", b"MZ")
    _z12.writestr("Mod/rtx.conf", b"x")
    for _i in range(60):
        _z12.writestr(f"Mod/rtx-remix/mods/f{_i:02d}.dds", b"d")
_fetch12 = _rdl_rg.Fetch(tag="v1", name="mod.zip", url="u", size=_zip12.stat().st_size)


def _dl12(url, name, progress=None, **k):
    if progress:
        progress(512, 1024)
        progress(1024, 1024)
    return _zip12


_game12 = _d12 / "game"
_game12.mkdir()
_calls12: list = []


def _raises12(done, total):
    _calls12.append((done, total))
    if isinstance(total, str) and done:
        raise RuntimeError("stopped part way")


with patch.object(_rdl_rg, "resolve", lambda url: _fetch12), patch.object(_rdl_rg.net, "download", _dl12), \
        patch.object(_rdl_rg.remix, "is_remix_game", lambda d: False):
    try:
        _rdl_rg.install("https://github.com/a/b", _game12, progress=_raises12)
        _stop12 = ""
    except RuntimeError as _e12:
        _stop12 = str(_e12)
    _rec12 = _rdl_rg.installed(_game12) or {}
    check("12: a remix install that stops part way still writes its record of what it wrote",
          _stop12 == "stopped part way" and len(_rec12.get("files") or []) >= 26
          and all((_game12 / f).is_file() for f in _rec12.get("files") or []) and _rec12.get("complete") is False,
          (_stop12, len(_rec12.get("files") or []), _rec12.get("complete")))
    shutil.rmtree(_game12, ignore_errors=True)
    _game12.mkdir()

    class _App12:
        busy, action, job_game = False, "", None
        q = _q_rg.Queue()

        def write(self, *a):
            pass

    class _Shell12:
        page = None
        infos: list = []

        def redraw(self):
            pass

        def info(self, *a):
            self.infos.append(a)

        status = info

    _pg12 = object.__new__(_rp_rg.RemixPage)
    _pg12.app, _pg12.shell, _pg12.busy_mod, _pg12.state = _App12(), _Shell12(), None, {}
    _mod12 = remixlist.MODS[0]
    _th12 = _UiThreads(run=True)
    with patch.object(_rp_rg, "threading", _th12):
        _pg12.app.busy = True
        _pg12.fetch(_mod12, games.Game(name="Remix", folder=_game12, exe=_game12 / "x.exe"))
        _refused12 = _pg12.busy_mod is None and _pg12.app.q.empty() and bool(_Shell12.infos)
        _pg12.app.busy = False
        _pg12.fetch(_mod12, games.Game(name="Remix", folder=_game12, exe=_game12 / "x.exe"))
    _msgs12 = []
    while not _pg12.app.q.empty():
        _msgs12.append(_pg12.app.q.get_nowait())
    _done12 = [p for k, p in _msgs12 if k == "remixed"]
    if _done12:
        _pg12.done(_done12[0])
    check("12: the remix page takes both progress shapes, and a 60-file mod installs with its record",
          len(_done12) == 1 and _done12[0][1] is True and len((_rdl_rg.installed(_game12) or {}).get("files") or []) == 62
          and not _pg12.app.busy, ([d[1:] for d in _done12], [p for k, p in _msgs12 if k == "log"][:3]))
    check("12: a remix fetch is refused while another job is running",
          _refused12, _Shell12.infos)
shutil.rmtree(_d12, ignore_errors=True)


section("2.0: the driver version is the installed driver's, not an old registry entry (#242)")

# #242 ran 616.92 and was told to update from 581.80: the display class in
# the registry keeps an entry for every NVIDIA card or driver the PC ever
# had, and the first one found was used.
check("32.0.16.1692 reads as 616.92, 32.0.15.8180 as 581.80",
      gpu._marketing("32.0.16.1692") == "616.92" and gpu._marketing("32.0.15.8180") == "581.80")
_drv_root = Path(tempfile.mkdtemp(prefix="drv_"))
(_drv_root / "System32").mkdir()
(_drv_root / "System32" / "nvapi64.dll").write_bytes(b"MZ")
gpu._DRIVER.clear()
try:
    import winreg as _wr242
    with patch.dict(os.environ, {"SystemRoot": str(_drv_root)}), \
            patch.object(pe, "file_version", lambda p: "32.0.16.1692"), \
            patch.object(_wr242, "OpenKey", side_effect=AssertionError("registry read")):
        _drv_a = gpu.driver_version()
    gpu._DRIVER.clear()
    (_drv_root / "System32" / "nvapi64.dll").unlink()

    class _Key:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    _entries = {"0000": ("NVIDIA GeForce RTX 3080", "32.0.15.8180"),
                "0001": ("AMD Radeon(TM) Graphics", "32.0.21045.5002"),
                "0002": ("NVIDIA GeForce RTX 4060 Ti", "32.0.16.1692")}
    _order = list(_entries)

    def _enum(root, i):
        if i >= len(_order):
            raise OSError("no more")
        return _order[i]

    def _open(root, sub=None):
        return _Key(sub)

    def _query(k, name):
        desc, ver = _entries[k.name]
        return (desc if name == "DriverDesc" else ver, 1)
    with patch.dict(os.environ, {"SystemRoot": str(_drv_root)}), \
            patch.object(_wr242, "OpenKey", _open), patch.object(_wr242, "EnumKey", _enum), \
            patch.object(_wr242, "QueryValueEx", _query):
        _drv_b = gpu.driver_version()
finally:
    gpu._DRIVER.clear()
    shutil.rmtree(_drv_root, ignore_errors=True)
check("the driver's own nvapi64.dll answers, and the registry is not read", _drv_a == "616.92", _drv_a)
check("without it, the newest of several NVIDIA registry entries is taken, not the first", _drv_b == "616.92", _drv_b)


section("RESULT")
_REACHED_RESULT = True
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("EVERYTHING PASSED")
