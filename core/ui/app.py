"""The 2.0 application: the shell, the pages, and the queue workers talk through.

Every long job runs on a worker thread and answers through `self.q` as
(kind, payload); `_pump` hands each kind to the method named `_on_<kind>`,
so a message with no handler is a bug the log names instead of a silent
drop. Nothing in this file walks a folder on the Tk thread.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path

from .. import gpu, library, log, prefs, selfupdate, update
from . import theme as T
from . import win
from .ctl_dlss import DlssControl
from .ctl_game import GameControl
from .ctl_library import LibraryControl
from .ctl_video import VideoControl
from .ctl_watch import WatchControl
from .dlsspage import DlssPage
from .gamepage import GamePage
from .libpage import LibraryPage
from .remixpage import RemixPage
from .vidpage import VideoPage
from .shell import TITLE, Shell

REPO_URL = f"https://github.com/{update.REPO}"

# The queue kinds whose handler ends a job that set `busy`. A handler of one
# of these that raises still ends the job; any other handler that raises (a
# progress line, a cover landing) runs while the job's worker is still
# writing, and clearing `busy` there let a second install start beside it.
JOB_ENDS = frozenset(("installed", "fail", "autopilot", "autofail", "removed", "preview",
                      "components", "diagnosed", "diagfail", "updated_all", "dlssdone",
                      "updfail", "swap"))


class App(LibraryControl, GameControl, VideoControl, WatchControl, DlssControl):
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.game = None
        self.update_url: str | None = None
        self.update_ready: Path | None = None
        self._crash_shown = False
        self._library_init()
        self._game_init()
        self._video_init()
        self._watch_init()
        self._dlss_init()
        self.shell = Shell(root)
        self.shell.on_help = self.help_items
        self.shell.on_watch = self.toggle_watch
        self.shell.watching = (False, "watch off")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.shell.add(LibraryPage(self.shell, self))
        self.game_page = GamePage(self.shell, self)
        self.shell.add(self.game_page)
        self.shell.add(DlssPage(self.shell, self))
        self.shell.add(VideoPage(self.shell, self))
        self.remix_page = RemixPage(self.shell, self)
        self.shell.add(self.remix_page)
        self._open_start()
        self.watch_refresh()
        root.after(60, self._pump)
        self.check_update()
        self.load_board()
        self.load_shared()
        # read which DLSS the games ship once the library is up (a day-old read only)
        root.after(5000, self.dlss_background)

    # ------------------------------------------------------------ start
    def _open_start(self) -> None:
        opened = False
        at_start = prefs.get("scan_on_start", True)
        try:
            if at_start and self.load_cached():
                opened = True
        except Exception:
            log.exception("opening on the saved library")
        self.shell.show("library", remember=False)
        if not opened and at_start and library.FILE.is_file():
            self.scan()

    # ------------------------------------------------------------ plumbing
    def write(self, text: str, tag: str = "") -> None:
        self.shell.write(text, tag)

    def refresh(self, page: str, soft: bool = False) -> None:
        """Redraw `page` if it is the one on screen."""
        sp = self.shell.page
        if sp is None or sp.name != page:
            return
        if soft and hasattr(sp, "refresh"):
            self.shell.set_height(sp.refresh())
        else:
            self.shell.redraw()

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                handler = getattr(self, f"_on_{kind}", None)
                if handler is None:
                    log.write(f"no handler for queue message '{kind}'", "warn")
                    continue
                try:
                    handler(payload)
                except Exception as e:
                    log.exception(f"handling '{kind}'", e)
                    if kind in JOB_ENDS:
                        self.busy = False
                    self.write(f"!! internal error - see the log file ({log.path()})", "err")
                    self.offer_crash_report()
        except queue.Empty:
            pass
        finally:
            try:
                if log.crashed():
                    self.offer_crash_report()
            except Exception:
                pass
            self.root.after(60, self._pump)

    def _on_log(self, payload) -> None:
        text, tag = payload if isinstance(payload, tuple) else (payload, "")
        self.write(text, tag)

    # ------------------------------------------------------------ navigation
    def open_game(self, g) -> None:
        self.enter_game(g)
        self.shell.show("game")

    def _on_backdrop(self, payload) -> None:
        self.game_page.got_backdrop(payload)

    def _on_remixed(self, payload) -> None:
        self.remix_page.done(payload)

    def _on_remixhave(self, payload) -> None:
        self.remix_page.got_have(payload)

    def video_launch(self) -> None:
        from .. import video
        g = self.game
        if g is None:
            return
        try:
            video.launch(g.install_dir)
        except Exception as e:
            self.shell.error("the player", f"could not start the player:\n{e}")

    # ------------------------------------------------------------ help / watch
    def help_items(self) -> list:
        try:
            name, sm = gpu.detect()
            card = f"{name}  \u00b7  driver {gpu.driver_version() or '?'}"
        except Exception:
            card = "graphics card not read"
        return [
            ("how it works", lambda: webbrowser.open(f"{REPO_URL}#which-route-a-game-gets")),
            ("report a bug", lambda: self.report_bug("bug")),
            ("suggest a feature", self.suggest),
            ("open the log file", self.shell.open_log_file),
            ("keyboard shortcuts", self.shortcuts),
            None,
            (f"v{update.VERSION}  \u00b7  {card}", lambda: None, False),
        ]

    def shortcuts(self) -> None:
        self.shell.info("keyboard", "arrows      move through the games\n"
                                    "enter       open the game\n"
                                    "esc         close what is open, then go back\n"
                                    "backspace   go back\n"
                                    "ctrl+f      search (or just start typing)\n"
                                    "shift+f10   a game's menu (or the menu key)\n"
                                    "ctrl+h      games")

    def report_bug(self, kind: str = "bug") -> None:
        """A pre-filled issue with the machine details in it. Nothing is sent:
        the browser shows the text and the person decides.

        Works with no game picked - the 1.9 window read the picked game's
        install folder outside any guard and raised from the help menu."""
        from urllib.parse import quote
        from .. import diagnose, installer, reportui
        try:
            name, sm = gpu.detect()
        except Exception:
            name, sm = "unknown", None
        drv = gpu.driver_version() or "?"
        g = self.game if self.shell.page is not None and self.shell.page.name == "game" else None
        title = {"crash": "crash: ", "notwork": "not working: "}.get(kind, "bug: ") + (g.name if g else "")
        install_dir = g.install_dir if g is not None else None
        asked = True
        try:
            answers = reportui.ask(self.root, getattr(g, "name", "") or "")
        except Exception:
            answers, asked = None, False
        if asked and answers is None:
            return
        # what is installed first: the dropdown may show a route the person
        # was only looking at, and the report's helper log follows the route
        route = "-"
        if install_dir:
            try:
                route = diagnose._manifest(Path(install_dir)).get("path") or "-"
            except Exception:
                pass
        if route == "-" and g is not None and self.route:
            route = self.route
        last_error = None
        if install_dir is not None:
            try:
                last_error = installer.last_failure(install_dir)
            except Exception:
                last_error = None
        body = diagnose.issue_body(
            update.VERSION, name, sm, drv, g, route, self._last_diag if g is not None else None,
            log.tail(60, 6000), log.path(), install_dir, last_error=last_error,
            session_error=log.last_error(), answers=answers,
            crash=self._last_crash if g is not None else None)
        try:
            url = f"{REPO_URL}/issues/new?title={quote(title)}&body={quote(body)}"
            if len(url) > 7800:
                self.root.clipboard_clear()
                self.root.clipboard_append(body)
                url = (f"{REPO_URL}/issues/new?title={quote(title)}&body="
                       + quote("(the details are on your clipboard - paste them here)"))
            webbrowser.open(url)
        except Exception:
            self.root.clipboard_clear()
            self.root.clipboard_append(body)
            self.shell.info("report a bug", "The details are on the clipboard - paste them into a new issue on "
                                            "GitHub.")

    def suggest(self) -> None:
        from urllib.parse import quote
        try:
            name, sm = gpu.detect()
        except Exception:
            name, sm = "unknown", None
        body = ("**What would you like it to do**\n\n\n**Why / which game**\n\n\n---\n"
                f"- version: {update.VERSION}\n- gpu: {name} (sm_{sm})\n")
        webbrowser.open(f"{REPO_URL}/issues/new?labels=enhancement&title={quote('idea: ')}&body={quote(body)}")

    def offer_crash_report(self) -> None:
        if self._crash_shown:
            return
        self._crash_shown = True
        self.shell.banner("crash", "something went wrong - the details are in the log file",
                          actions=[("report it", lambda: self.report_bug("crash"), True)],
                          colour=T.WARN)

    # ------------------------------------------------------------ update
    def check_update(self) -> None:
        def work():
            try:
                newer, latest, url = update.check()
            except Exception:
                return
            if not newer:
                return
            self.q.put(("update", (latest, url)))
            if selfupdate.running_exe() is None or not prefs.get("auto_update", True):
                return
            try:
                self.q.put(("update_ready", (latest, selfupdate.fetch())))
            except Exception as e:
                log.write(f"auto-update download failed: {e}", "warn")
        threading.Thread(target=work, daemon=True).start()

    def _on_update(self, payload) -> None:
        latest, url = payload
        self.update_url = url
        downloading = selfupdate.running_exe() and prefs.get("auto_update", True)
        self.shell.banner("update", f"version {latest} is out (you are on {update.VERSION})"
                          + (" - downloading..." if downloading else ""),
                          actions=[("update now", self.do_update, True)])

    def _on_update_ready(self, payload) -> None:
        latest, exe = payload
        self.update_ready = exe
        self.shell.banner("update", f"version {latest} downloaded and verified",
                          actions=[("restart into it", self.do_update, True)])

    def do_update(self) -> None:
        if self.busy:
            return
        if self.update_ready is not None and not Path(self.update_ready).is_file():
            self.update_ready = None
        if self.update_ready is not None:
            self.shell.banner("update", "restarting into the new build...")
            self.root.update()
            self._restart_into(self.update_ready)
            return
        if selfupdate.running_exe() is None:
            webbrowser.open(self.update_url or update.RELEASES_PAGE)
            return
        if not self.shell.ask("update and restart?",
                              "The new version is downloaded and the tool restarts into it. "
                              "The current build is kept beside it as .old.exe, so you can go back.",
                              "update", "not now"):
            return
        self.busy = True
        self.shell.banner("update", "downloading the update...")

        def work():
            try:
                exe = selfupdate.fetch(progress=lambda d, t: self.q.put(
                    ("scan", f"update - {d / 1048576:.1f} MB")))
                self.q.put(("swap", exe))
            except Exception as e:
                self.q.put(("updfail", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _on_swap(self, exe) -> None:
        self.shell.banner("update", "restarting into the new build...")
        self.root.update()
        self._restart_into(exe)

    def _restart_into(self, exe) -> None:
        try:
            selfupdate.apply_and_restart(exe)
        except (selfupdate.UpdateError, OSError) as e:
            log.write(f"update not applied: {e}", "warn")
            self.busy = False
            self.update_ready = None
            self.shell.busy("")
            self.shell.banner("update", f"update failed: {e}",
                              actions=[("try again", self.do_update, True)], colour=T.WARN)

    def _on_updfail(self, text: str) -> None:
        self.busy = False
        self.shell.busy("")
        self.shell.banner("update", f"update failed: {text}",
                          actions=[("try again", self.do_update, True)], colour=T.WARN)


MUTEX_NAME = "Local\\DLSS5AutopilotWindow"
_mutex = None          # held for the life of the process; Windows frees it on exit


def _win32():
    """(kernel32, user32, last_error) with the prototypes used below - the seam
    the checks replace."""
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u = ctypes.WinDLL("user32", use_last_error=True)
    k.CreateMutexW.restype = w.HANDLE
    k.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
    u.FindWindowW.restype = w.HWND
    u.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
    for name in ("IsWindowVisible", "IsIconic", "SetForegroundWindow"):
        getattr(u, name).argtypes = [w.HWND]
    u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
    u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    return k, u, ctypes.get_last_error


def already_open(win32=None) -> bool:
    """Is the window open in another process? Then that one comes forward and
    this one does not open.

    Two copies meant two watchers, two answers for every game that closed,
    and each overwriting the other's last answers and autopilot plans. Any
    failure here opens the window as before - a second copy is better than
    none."""
    global _mutex
    try:
        k, u, last_error = win32 or _win32()
        handle = k.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return False
        if last_error() != 183:               # ERROR_ALREADY_EXISTS
            _mutex = handle
            return False
        from .tray import WM_LBUTTONUP, WM_TRAY
        main = u.FindWindowW("TkTopLevel", TITLE)
        if main and u.IsWindowVisible(main):
            if u.IsIconic(main):
                u.ShowWindow(main, 9)            # SW_RESTORE
            u.SetForegroundWindow(main)
            return True
        # hidden in the tray: the message a click on its icon sends
        tray = u.FindWindowW("dlss5AutopilotTray", None)
        if tray:
            u.PostMessageW(tray, WM_TRAY, 0, WM_LBUTTONUP)
            return True
        # the mutex is there and no window is: still starting, or stuck -
        # open this one rather than nothing
        return False
    except Exception:
        return False


def run() -> int:
    log.start(update.VERSION)
    if already_open():
        log.write("already open - brought the running window forward")
        return 0
    win.dpi_aware()
    root = tk.Tk()
    try:
        win.apply_scale(root)
    except Exception:
        pass
    log.install_handlers(root)
    try:
        App(root)
    except Exception as e:
        log.exception("building the main window", e)
        raise
    root.mainloop()
    try:
        from .. import video as _video
        _video.stop_webcam()
    except Exception:
        pass
    log.write("closed normally")
    return 0
