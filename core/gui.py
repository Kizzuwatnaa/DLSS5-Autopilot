"""Terminal-styled tkinter interface.

Monospace throughout, amber on near-black, bracket markers, square edges. A
persistent left rail holds the places, so you can always see where you are:

games    the library, where the app opens once there is one
install  settings, install, diagnose, for the game picked
video    the portable player, fed like a game
remix    RTX Remix mods, and which of your games have one
start    the welcome before a first scan, reached from the name in the corner
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import (anticheat, autotune, community, components, diagnose, dlss,
               dxvk, feedcfg, reengine, wincrash,
               games, gpu, library, pe, profiles, video,
               installer, log, optiscaler, prefs, reshade_ini, selfupdate,
               sources, update)
from . import mfg as _mfg

APP = "dlss5 autopilot"

BG      = "#0b0c0e"     # window ground
RAIL    = "#08090a"     # left rail
PANEL   = "#0e1013"     # cards / log
FIELD   = "#121519"     # inputs
LINE    = "#1c1f24"
EDGE    = "#2a2e35"
TXT     = "#e6e8ea"     # bright text
BODY    = "#b9bcc2"     # normal text
DIM     = "#5c6069"     # labels
FAINT   = "#454952"     # decoration
AMBER   = "#d8a657"     # accent
GREEN   = "#6f9f6f"
RUST    = "#b07a3c"     # warnings
SLIDER_TROUGH = "#7a5a2c"   # the work-area slider's track: dark amber, unmissable
SLIDER_HOT    = "#f0b25a"   # its handle under the mouse
RED     = "#c96a5a"

# Cascadia ships with Windows Terminal and VS; Consolas is on every Windows.
MONO = ("Cascadia Mono", "Consolas", "Courier New")
# First entry of the DLSS 5 add-on dropdown. _opts() turns anything that
# starts with "auto" into None, which is what lets installer.install() pin.
ADDON_AUTO = "auto - the newest build that works on this driver and route"
# The first entry of the ray-reconstruction dropdown: doing nothing is
# the right default, because the game already works with what it ships.
DLSSD_KEEP = "keep the game's own"


def _crash_is_this_session_impl(crash, install_dir) -> bool:
    """Was this fault recorded during the session the logs describe?

    The event search goes back a fortnight from the install, so without
    this a crash from last week would speak for a clean session played
    after the driver was rolled back - both in the verdict on screen and
    in the record posted to the shared list.
    """
    when = _event_epoch(getattr(crash, "when", ""))
    ran = _last_log_write(install_dir)
    if not when or not ran:
        return True             # nothing to date it against; take it as told
    return when >= ran - 300


def _first_line(e: Exception) -> str:
    """The first line of an exception's message, or its class name.

    A bare TimeoutError has an empty message, and `"".splitlines()` is the
    empty list - so indexing it raised inside a worker thread, after the
    window had been put in its busy state and before the message that takes
    it out again.
    """
    return (str(e).splitlines() or [""])[0] or type(e).__name__


def _event_epoch(when: str) -> float:
    """A Windows event time as an epoch, or 0 when it cannot be read."""
    import calendar
    import time as _time
    try:
        return calendar.timegm(_time.strptime(str(when)[:19],
                                              "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def _last_log_write(install_dir) -> float:
    """When the game's own logs were last written, as an epoch.

    The bound for "did this crash belong to the session just diagnosed":
    a fault recorded before the last line any add-on wrote belongs to an
    earlier run, and must not rewrite this run's verdict.
    """
    newest = 0.0
    d = Path(install_dir)
    names = [d / "ReShade.log", d / "dlss5-feed.log", d / "OptiScaler.log",
             d / "host64" / "dlss5-feed-host.log", d / "dlss5-bridge.log"]
    try:
        # The OptiScaler forks put theirs under Logs\ on some builds, and a
        # Remix game has no ReShade log at all: diagnose knows where both
        # live, and a route missing from this list would disable the check
        # rather than tighten it.
        from . import diagnose as _dg
        extra = _dg._opti_log(d)
        if extra:
            names.append(extra)
        from . import remix as _rx
        names.append(d / getattr(_rx, "LOG", "rtx-remix/logs/remix-dxvk.log"))
    except Exception:
        pass
    for f in names:
        try:
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    if not newest:
        # Nothing datable in the folder: fall back to the install time, so
        # the window is narrowed to "since the install" rather than turned
        # off altogether.
        try:
            from . import diagnose as _dg2
            newest = float(_dg2._installed_at(d) or 0.0)
        except Exception:
            newest = 0.0
    return newest


def font(size: int = 10, weight: str = "normal") -> tuple:
    return (MONO[0], size, weight)


# Pixels per 96-DPI pixel. Fonts are in points and follow Tk's scaling, but
# every bare integer Tk takes - row heights, frame widths, wrap lengths,
# column widths - is a pixel. At 4K/200% the fonts doubled while the rows
# stayed 26 px tall and the text was cut to its upper half (issue #40).
SCALE = 1.0


def px(n: int) -> int:
    return max(1, round(n * SCALE))


def lines(n: int, floor: int = 5) -> int:
    """A row count that keeps roughly its pixel height as the font scales.

    Text and Treeview heights are counted in rows, and a row is as tall as
    the font: 14 rows at 300% ask for three times the pixels they asked for
    at 100%. Scaling the fonts alone therefore turned "the log fits under
    the settings" into "the log is one and a half lines tall" (issue #40).
    """
    return max(floor, round(n / SCALE))


class Scroller(tk.Frame):
    """A frame whose contents scroll vertically when the window is too short.

    At 100% every page fits and this is invisible - no scrollbar, no change
    in layout. At 200%/300% the install page's settings alone are taller
    than the whole screen, and without somewhere to scroll they push the log
    and the buttons out of the window instead (issue #40).
    """

    def __init__(self, parent: tk.Misc, **kw) -> None:
        super().__init__(parent, bg=BG, **kw)
        self._h = 0
        # A canvas asks for 7 centimetres of height by default, which is
        # 795 pixels at 300% - enough on its own to push a page off the
        # window. It is given its room by the geometry manager instead.
        self._canvas = tk.Canvas(self, bg=BG, highlightthickness=0,
                                 borderwidth=0, takefocus=0, height=px(40))
        self._bar = ttk.Scrollbar(self, orient="vertical",
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_scroll)
        self._canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self._canvas, bg=BG)
        self._win = self._canvas.create_window((0, 0), window=self.inner,
                                               anchor="nw")
        self.inner.bind("<Configure>", self._resized)
        self._canvas.bind("<Configure>", self._canvas_resized)
        # Nothing is bound here. The window binds the wheel once and asks
        # which widget the pointer is actually over - see App._wheel_anywhere.
        # Binding on this canvas's own Enter/Leave did not work: the content
        # frame is a child window laid over the canvas, and Tk hands the
        # canvas a Leave as soon as the pointer crosses into a child, so the
        # binding disappeared the moment the pointer touched a control.

    def content_height(self) -> int:
        return self.inner.winfo_reqheight()

    def set_height(self, h: int) -> None:
        """How much room this area gets. The rest of it scrolls."""
        self._h = h
        if self._canvas.cget("height") != h:
            self._canvas.configure(height=h)
        self._update_bar()

    def _resized(self, _e=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._update_bar()

    def _canvas_resized(self, e) -> None:
        self._canvas.itemconfigure(self._win, width=e.width)
        self._update_bar()

    def _update_bar(self) -> None:
        # No scrollbar while everything fits - at 100% nothing about this
        # frame is visible at all. The height asked for is the one to
        # compare against: winfo_height() is still the old value until the
        # geometry manager has run, and the bar would appear a beat late.
        have = self._h or self._canvas.winfo_height()
        need = self.content_height() > have + 2
        if need and not self._bar.winfo_ismapped():
            # Before the canvas: the canvas is packed with expand=True and
            # claims the whole width, so a scrollbar packed after it is laid
            # out one pixel wide and never seen.
            self._bar.pack(side="right", fill="y", before=self._canvas)
        elif not need and self._bar.winfo_ismapped():
            self._bar.pack_forget()
            self._canvas.yview_moveto(0.0)

    def _on_scroll(self, first: str, last: str) -> None:
        self._bar.set(first, last)

    def wheel(self, e) -> bool:
        """Scroll if there is anywhere to scroll. True when it was used."""
        if not self._bar.winfo_ismapped():
            return False
        self._canvas.yview_scroll(int(-e.delta / 120), "units")
        return True


def _wrap_to_width(lbl: tk.Label, floor: int = 200) -> None:
    """Make a label wrap at whatever width it is given.

    A label with no wraplength asks for its whole text on one line, and a
    grid cell narrower than that simply cuts it off - which is what the
    feeder build note did at 150% and 200% (found by _tools/gui_lint.py).
    """
    def fit(e) -> None:
        want = max(px(floor), e.width - px(4))
        try:
            if int(str(lbl.cget("wraplength") or 0)) != want:
                lbl.configure(wraplength=want)
        except (tk.TclError, ValueError):
            pass
    lbl.bind("<Configure>", fit, add="+")


class Prose(tk.Text):
    """Read-only text that is as tall as what it says, with styled lines.

    The route description under the dropdown was one grey Label: fourteen
    lines of the same colour, where the warnings wrapped back to the left
    edge and could not be told from the description. This keeps the text
    and gives each kind of line its own look - and a hanging indent, so a
    wrapped warning stays under its own "!".
    """

    def __init__(self, parent: tk.Misc, **kw) -> None:
        super().__init__(parent, bg=PANEL, fg=DIM, font=font(8), wrap="word",
                         insertbackground=PANEL,
                         borderwidth=0, highlightthickness=0, relief="flat",
                         cursor="arrow", takefocus=0, height=1,
                         padx=0, pady=0, **kw)
        ind = px(26)
        self.tag_configure("blurb", foreground=DIM)
        self.tag_configure("note", foreground=DIM, spacing1=px(3))
        self.tag_configure("warn", foreground=BODY, lmargin1=px(6),
                           lmargin2=ind, spacing1=px(2))
        self.tag_configure("mark", foreground=AMBER)
        self.tag_configure("driver", foreground=AMBER, lmargin1=px(6),
                           lmargin2=ind, spacing1=px(2))
        self.tag_configure("bad", foreground=RUST, spacing3=px(3))
        self.configure(state="disabled")
        self.bind("<Configure>", lambda _e: self.after_idle(self._fit))

    def show(self, parts: list[tuple[str, str]]) -> None:
        """parts: (tag, text) pairs, one per line."""
        self.configure(state="normal")
        self.delete("1.0", "end")
        for i, (tag, text) in enumerate(parts):
            if i:
                self.insert("end", "\n")
            if tag in ("warn", "driver"):
                self.insert("end", "!  ", ("mark", tag))
            self.insert("end", text, tag)
        self.configure(state="disabled")
        self.after_idle(self._fit)

    def _fit(self) -> None:
        # By pixels, not by display lines: `height` is counted in lines of
        # the base font, but the spacing above each warning adds pixels that
        # a line count does not see - and the last wrapped row of the driver
        # warning came out two pixels tall.
        try:
            from tkinter import font as _tkfont
            n = self.count("1.0", "end", "ypixels")
            need = (n[0] if isinstance(n, tuple) else n) or 0
            step = _tkfont.Font(font=self.cget("font")).metrics("linespace") or 1
            lines_ = max(1, -(-int(need) // int(step)))
            if int(self.cget("height")) != lines_:
                self.configure(height=lines_)
        except tk.TclError:
            pass


FEEDER_CHOICES = ("stable - newest release",
                  "newest pre-release")

# The rail holds places, not steps. It used to read "1 architecture, 2 game,
# 3 install" - a wizard - while the app opened on step 2, and video and RTX
# Remix were two cards under the architecture question that most people
# never saw once the library became the home screen. Page 1 is only the
# welcome before a first scan; it is reached from the name in the corner.
PAGES = {1: ("start", "find your games"),
         2: ("games", "your library"),
         3: ("install", "settings and go"),
         4: ("video and youtube", "any file or a youtube link"),
         5: ("rtx remix", "path-traced classics")}
NAV = (2, 3, None, 4, 5)           # None: a rule between the two groups
ARCH_CHOICES = (("all", "every architecture"), ("64", "64-bit only"),
                ("32", "32-bit only (experimental)"))


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.step = 1

        self.arch = tk.StringVar(value="all")
        self.search = tk.StringVar(value="")
        self.all_games: list[games.Game] = []
        self.shown: list[games.Game] = []
        # One row costs several folder reads, so keep them: without this a
        # search box would re-read the whole library on every keystroke.
        self._rows: dict[tuple, object] = {}
        # Games a worker thread is re-reading right now: _fill lists them
        # without a compatibility row rather than reading them itself.
        self._recheck: set[tuple] = set()
        self._fill_job: str | None = None
        self.game: games.Game | None = None
        self.catalog: dict[str, list[dict]] = {}
        self.renodx_local: Path | None = None
        self.update_url: str | None = None
        self.support: dlss.Support | None = None

        self.provider = tk.IntVar(value=3)
        self.keep_dlss = tk.BooleanVar(value=True)
        self.workres = tk.IntVar(value=100)
        # Empty means off. Kept between runs: a target is a preference, not
        # a per-game decision, and retyping it every time would kill it.
        self.target_fps = tk.StringVar(value=str(prefs.get("target_fps") or ""))
        self._tune = None                # the last suggestion, if any
        self._last_crash = None          # what Windows recorded, if anything
        self.feeder_pre = tk.BooleanVar(value=False)
        self.dxvk = tk.BooleanVar(value=False)
        self.fg = tk.BooleanVar(value=False)
        self.mfg = tk.BooleanVar(value=False)
        self.vr = tk.BooleanVar(value=False)
        self.sm: int | None = None          # the card's architecture, once known
        self.stale: dict[str, int] = {}     # install folder -> outdated parts
        self.route_fit: dict[str, tuple[bool, str]] = {}
        self.update_ready: Path | None = None
        self._crash_shown = False
        self._last_diag: object | None = None
        self._target_after = None        # the pending "save the target" call
        # Routes whose compatibility and HDR notes have been written for
        # this game. A set, not the last route: switching away and back is
        # not new information.
        self._noted_routes: set[str] = set()

        self._build()
        # One wheel binding for the whole window, which then asks what the
        # pointer is over. Anything that scrolls itself - the log, the game
        # list - is left to its own class binding; everything else looks up
        # its parents for a Scroller and scrolls that.
        self.root.bind_all("<MouseWheel>", self._wheel_anywhere, add="+")
        # ttk's class binding changes a dropdown's selection on a wheel turn,
        # and it runs before the handler above - so the only way to stop a
        # scroll from picking a different route is to take the binding away.
        # The open drop-down list is a Listbox and keeps its own wheel.
        for cls in ("TCombobox",):
            self.root.unbind_class(cls, "<MouseWheel>")
        self.search.trace_add("write", self._search_changed)
        # The library is the home screen, not the third thing you reach.
        # Every launch used to open on the architecture question - a filter
        # most people answer once - and the games were not even read until
        # you pressed past it, so coming back to a game you had installed
        # meant walking the whole wizard again. When there is a library from
        # last time, open on it; the architecture is a filter on that page.
        self._show(1)
        opened_on_library = False
        try:
            if self.scan_on_start.get() and self._load_cached():
                # _load_cached() has already filled the list and written the
                # "from the last scan" line; filling again would replace it
                # with the generic count and re-read every manifest.
                self._show(2)
                opened_on_library = True
        except Exception:
            log.exception("opening on the saved library")
        # The saved library is version-gated, so after every update - and
        # this tool updates itself - it came back unusable and the app opened
        # on the architecture page as if it had never been run. Someone who
        # has scanned before goes to the library, and it is read again there.
        if not opened_on_library and self.scan_on_start.get() \
                and library.FILE.is_file():
            try:
                self._show(2)
                self._scan()
                opened_on_library = True
            except Exception:
                log.exception("rescanning the library at start")
        if not opened_on_library:
            self._show(1)
        self.root.after(60, self._pump)
        self._check_update()

    # ---------------------------------------------------------------- style
    def _style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=BODY, fieldbackground=FIELD,
                     bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=BODY, font=font(10))
        st.configure("H1.TLabel", font=font(15), foreground=TXT)
        st.configure("Dim.TLabel", foreground=DIM, font=font(9))
        st.configure("TButton", background=BG, foreground=BODY, borderwidth=1,
                     focuscolor=BG, padding=(px(14), px(7)), font=font(10),
                     relief="solid", bordercolor=EDGE)
        st.map("TButton", background=[("active", FIELD), ("disabled", BG)],
               foreground=[("disabled", FAINT)], bordercolor=[("disabled", LINE)])
        st.configure("Accent.TButton", background=AMBER, foreground=BG,
                     font=font(10, "bold"), padding=(px(22), px(8)), borderwidth=0)
        st.map("Accent.TButton", background=[("active", "#e8bd7a"),
                                             ("disabled", LINE)],
               foreground=[("disabled", FAINT)])
        st.configure("TRadiobutton", background=PANEL, foreground=TXT, font=font(10))
        st.map("TRadiobutton", background=[("active", PANEL)])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=BODY, rowheight=px(26), borderwidth=0, font=font(10))
        st.configure("Treeview.Heading", background=BG, foreground=DIM,
                     borderwidth=0, font=font(9))
        st.map("Treeview", background=[("selected", AMBER)],
               foreground=[("selected", BG)])
        st.map("Treeview.Heading", background=[("active", FIELD)])
        st.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                     foreground=TXT, arrowcolor=DIM, padding=6,
                     borderwidth=0, font=font(10))
        st.map("TCombobox",
               fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
               background=[("readonly", FIELD)],
               foreground=[("readonly", TXT), ("disabled", FAINT)],
               selectbackground=[("readonly", FIELD)],
               selectforeground=[("readonly", TXT)],
               arrowcolor=[("readonly", DIM), ("disabled", FAINT)])
        for k, v in (("background", FIELD), ("foreground", TXT),
                     ("selectBackground", AMBER), ("selectForeground", BG)):
            self.root.option_add(f"*TCombobox*Listbox.{k}", v)
        self.root.option_add("*TCombobox*Listbox.font", font(10))
        # The field itself, not only its drop-down list: ttk takes no font
        # from the style for this widget, so every dropdown on the page was
        # in Segoe UI while the rest of the window is monospaced.
        self.root.option_add("*TCombobox.font", font(10))
        # Classic Tk widgets read these from the option database when they
        # are made, so one line here covers every widget that does not set
        # its own. Without them a checkbox drew Windows' light-grey focus
        # frame round itself, and the fps box's arrows flashed white.
        for pattern, value in (("*highlightBackground", PANEL),
                               ("*highlightColor", EDGE),
                               ("*Checkbutton.highlightThickness", 0),
                               ("*Radiobutton.highlightThickness", 0),
                               ("*Spinbox.activeBackground", FIELD),
                               ("*Spinbox.disabledBackground", PANEL),
                               ("*Spinbox.disabledForeground", FAINT),
                               ("*Spinbox.readonlyBackground", FIELD),
                               ("*Entry.disabledBackground", PANEL),
                               ("*Entry.disabledForeground", FAINT),
                               ("*Entry.readonlyBackground", FIELD),
                               ("*Spinbox.highlightThickness", 0)):
            self.root.option_add(pattern, value)
        st.configure("TProgressbar", background=AMBER, troughcolor=FIELD,
                     borderwidth=0, thickness=px(4))
        # Trough and thumb only: clam draws arrows and a three-line grip on
        # the thumb, in light grey, and on this ground that looked like a
        # scrollbar that had broken in the middle.
        for orient in ("Vertical", "Horizontal"):
            st.layout(f"{orient}.TScrollbar", [
                (f"{orient}.Scrollbar.trough", {
                    "sticky": "ns" if orient == "Vertical" else "we",
                    "children": [(f"{orient}.Scrollbar.thumb",
                                  {"expand": "1", "sticky": "nswe"})]})])
            st.configure(f"{orient}.TScrollbar", troughcolor=PANEL,
                         background=FAINT, bordercolor=PANEL,
                         lightcolor=FAINT, darkcolor=FAINT,
                         gripcount=0, borderwidth=0, relief="flat",
                         width=px(10), arrowsize=px(10))
            st.map(f"{orient}.TScrollbar",
                   background=[("pressed", AMBER), ("active", DIM)],
                   lightcolor=[("pressed", AMBER), ("active", DIM)],
                   darkcolor=[("pressed", AMBER), ("active", DIM)])

    # ---------------------------------------------------------------- chrome
    def _build(self) -> None:
        r = self.root
        r.title(APP)
        r.after(0, lambda: _dark_titlebar(r))
        # Our own icon in the title bar and the taskbar, not Python's feather.
        # Two ways, because one is not enough on every machine: iconphoto
        # (a PNG, fine for the taskbar) and iconbitmap (a real .ico with the
        # 16/32 px sizes Windows wants for the title bar and alt-tab). The
        # .ico has to be a file, so it goes into the settings folder.
        try:
            import base64
            from .icon_png import ICON_PNG_B64
            self._icon = tk.PhotoImage(data=base64.b64decode(ICON_PNG_B64))
            r.iconphoto(True, self._icon)
        except Exception:
            pass
        try:
            from .icon_png import ICON_ICO_B64
            ico = prefs.FILE.parent / "dlss5-autopilot.ico"
            data = base64.b64decode(ICON_ICO_B64)
            if not ico.is_file() or ico.read_bytes() != data:
                ico.parent.mkdir(parents=True, exist_ok=True)
                ico.write_bytes(data)
            r.iconbitmap(default=str(ico))
        except Exception:
            pass
        # Scaling the window with the fonts asks for 3180x2490 at 300%, and
        # a 4K screen is 2160 tall: the window opened taller than the screen
        # and minsize kept it there, so the bottom of every page - the log,
        # INSTALL - was off the display and no amount of dragging helped
        # (issue #40). Never ask for more than the screen has.
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        r.geometry(f"{min(px(1060), int(sw * 0.95))}x{min(px(830), int(sh * 0.92))}")
        r.minsize(min(px(980), int(sw * 0.6)), min(px(720), int(sh * 0.5)))
        # Open filling the screen: the three-column layout reads better with
        # room, and a small window in a corner looked like a dialog box.
        try:
            r.state("zoomed")
        except tk.TclError:
            pass
        r.configure(bg=BG)
        self._style()

        rail = tk.Frame(r, bg=RAIL, width=px(236))
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        tk.Frame(r, bg=LINE, width=px(1)).pack(side="left", fill="y")

        # The mark, then the name. The icon was only ever set on the window
        # and the taskbar, so the application itself carried no logo at all
        # and the corner it belongs in was two lines of text.
        brand = tk.Frame(rail, bg=RAIL)
        brand.pack(fill="x", padx=20, pady=(px(22), px(18)))
        try:
            img = getattr(self, "_icon", None)
            if img is not None:
                # 64 px artwork, and the only thing on the page that does
                # not grow with the scaling on its own: half size normally,
                # full size once everything else is bigger, doubled again on
                # the displays where 64 px is a thumbnail (5K at 250%, 8K).
                self._logo = (img.zoom(2) if SCALE >= 2.5
                              else img if SCALE >= 1.5 else img.subsample(2))
                tk.Label(brand, image=self._logo, bg=RAIL,
                         borderwidth=0).pack(side="left", padx=(0, px(12)))
        except Exception:
            pass
        words = tk.Frame(brand, bg=RAIL)
        words.pack(side="left", anchor="w")
        tk.Label(words, text="dlss5", bg=RAIL, fg=AMBER, anchor="w",
                 font=(MONO[0], 15, "bold")).pack(anchor="w")
        self.brandname = tk.Label(words, text="autopilot", bg=RAIL, fg=TXT,
                                  anchor="w", font=(MONO[0], 15))
        self.brandname.pack(anchor="w")
        # The name in the corner goes home, as it does on every website:
        # the library when there is one, the first page when there is not.
        for w in (brand, words, *brand.winfo_children(),
                  *words.winfo_children()):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda e: self._go_home())
            w.bind("<Enter>", lambda e: self.brandname.configure(fg=AMBER))
            w.bind("<Leave>", lambda e: self.brandname.configure(fg=TXT))
        tk.Frame(rail, bg=LINE, height=px(1)).pack(fill="x", padx=20,
                                                   pady=(0, px(10)))

        self.rail_rows: list[dict] = []
        for i in NAV:
            if i is None:
                tk.Frame(rail, bg=LINE, height=px(1)).pack(
                    fill="x", padx=20, pady=px(8))
                continue
            title, sub = PAGES[i]
            row = tk.Frame(rail, bg=RAIL, cursor="hand2")
            row.pack(fill="x")
            marker = tk.Frame(row, bg=RAIL, width=px(3))
            marker.pack(side="left", fill="y")
            pad = tk.Frame(row, bg=RAIL)
            pad.pack(side="left", fill="x", expand=True, padx=(18, 12), pady=9)
            mark = tk.Label(pad, text="[ ]", bg=RAIL, fg=FAINT, font=font(10))
            mark.pack(side="left", padx=(0, 10))
            box = tk.Frame(pad, bg=RAIL)
            box.pack(side="left", fill="x", expand=True)
            t1 = tk.Label(box, text=title, bg=RAIL, fg=DIM, anchor="w", font=font(10))
            t1.pack(fill="x")
            t2 = tk.Label(box, text=sub, bg=RAIL, fg=FAINT, anchor="w", font=font(8))
            t2.pack(fill="x")
            e = {"row": row, "marker": marker, "pad": pad, "box": box,
                 "mark": mark, "t1": t1, "t2": t2, "n": i}
            self.rail_rows.append(e)
            for w in (row, pad, box, mark, t1, t2):
                w.bind("<Button-1>", lambda ev, n=i: self._jump(n))

        tk.Frame(rail, bg=RAIL).pack(fill="both", expand=True)
        tk.Frame(rail, bg=LINE, height=px(1)).pack(fill="x", padx=20,
                                                   pady=(0, px(10)))
        self.gpulbl = tk.Label(rail, text="", bg=RAIL, fg=DIM, anchor="w",
                               justify="left", font=font(8), wraplength=px(196))
        self.gpulbl.pack(fill="x", padx=20, pady=(0, 6))
        self.verlbl = tk.Label(rail, text=f"v{update.VERSION}", bg=RAIL, fg=DIM,
                               anchor="w", font=font(8))
        self.verlbl.pack(fill="x", padx=20, pady=(0, 4))
        # A bug report needs something to attach. This opens the log file so
        # it can be pasted somewhere useful.
        self.loglink = tk.Label(rail, text="[ open log file ]", bg=RAIL, fg=DIM,
                                anchor="w", cursor="hand2", font=font(8))
        self.loglink.pack(fill="x", padx=20, pady=(0, 2))
        self.loglink.bind("<Button-1>", lambda e: self._open_log())
        self.buglink = tk.Label(rail, text="[ report a bug ]", bg=RAIL, fg=DIM,
                                anchor="w", cursor="hand2", font=font(8))
        self.buglink.pack(fill="x", padx=20, pady=(0, 2))
        self.buglink.bind("<Button-1>", lambda e: self._report_bug())
        self.ideallink = tk.Label(rail, text="[ suggest a feature ]", bg=RAIL,
                                  fg=DIM, anchor="w", cursor="hand2", font=font(8))
        self.ideallink.pack(fill="x", padx=20, pady=(0, 2))
        self.ideallink.bind("<Button-1>", lambda e: self._suggest())
        self.howlink = tk.Label(rail, text="[ how it works ]", bg=RAIL, fg=DIM,
                                anchor="w", cursor="hand2", font=font(8))
        self.howlink.pack(fill="x", padx=20, pady=(0, 18))
        self.howlink.bind("<Button-1>", lambda e: webbrowser.open(
            f"https://github.com/{update.REPO}#which-route-a-game-gets"))

        right = tk.Frame(r, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self.banner = tk.Frame(right, bg="#1a1509")
        self.bannerlbl = tk.Label(self.banner, text="", bg="#1a1509", fg=AMBER,
                                  font=font(9), anchor="w")
        self.bannerlbl.pack(side="left", padx=18, pady=8)
        close = tk.Label(self.banner, text="[x]", bg="#1a1509", fg=FAINT,
                         cursor="hand2", font=font(9))
        close.pack(side="right", padx=(0, 14))
        close.bind("<Button-1>", lambda e: self.banner.pack_forget())
        self.updbtn = tk.Label(self.banner, text="[ update now ]", bg="#1a1509",
                               fg=AMBER, cursor="hand2", font=font(9, "bold"))
        self.updbtn.pack(side="right", padx=12)
        self.updbtn.bind("<Button-1>", lambda e: self._do_update())

        bar = tk.Frame(right, bg=BG)
        bar.pack(side="bottom", fill="x", padx=28, pady=14)
        tk.Frame(right, bg=LINE, height=px(1)).pack(side="bottom", fill="x", padx=px(28))

        self.body = tk.Frame(right, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28, pady=(20, 10))

        self.p1 = self._page_start()
        self.p2 = self._page_games()
        self.p3 = self._page_install()
        self.p4 = self._page_video()
        self.p5 = self._page_remix()
        self.pages = (self.p1, self.p2, self.p3, self.p4, self.p5)

        self.status = tk.Label(bar, text="ready", bg=BG, fg=DIM,
                               font=font(9), anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.btn_back = ttk.Button(bar, text="back", command=self._back)
        self.btn_back.pack(side="left", padx=(0, 10))
        self.btn_next = ttk.Button(bar, text="continue", style="Accent.TButton",
                                   command=self._next)
        self.btn_next.pack(side="right")

        r.bind("<Escape>", lambda e: self._go_home())
        # Not from a text box: Ctrl+H is backspace there, in Tk as in Windows.
        r.bind("<Control-h>", lambda e: None if isinstance(
            e.widget, (tk.Entry, tk.Text, tk.Spinbox, ttk.Entry))
            else self._go_home())

    def _go_home(self) -> None:
        """The library is the home screen; before any scan, the first page."""
        self._jump(2 if self._have_library() else 1)

    def _can_install_page(self) -> bool:
        """A game is picked and has the metadata needed to install."""
        if not self.game:
            return False
        try:
            return bool(installer.check_supported(self.game)[0])
        except Exception:
            return False

    def _have_library(self) -> bool:
        """Games have been found. The video player on its own is not a
        library: set up before any scan, it made every "is there a library"
        check true, and nothing ever scanned again."""
        return any(getattr(g, "kind", "game") != "video" for g in self.all_games)

    def _jump(self, n: int) -> None:
        if self.busy or n == self.step:
            return
        if n in (1, 4, 5):
            self._show(n)
        elif n == 2:
            self._show(2)
            if not self._have_library():
                self._enter_library()
        elif n == 3 and self._can_install_page():
            self._show(3)
            self._enter_install()

    def _paint_rail(self) -> None:
        for e in self.rail_rows:
            active = e["n"] == self.step
            # The install page needs a game; until one is picked its row
            # says so by being dimmer, and a click on it does nothing.
            off = e["n"] == 3 and not self._can_install_page()
            bg = PANEL if active else RAIL
            e["marker"].configure(bg=AMBER if active else RAIL)
            for k in ("row", "pad", "box"):
                e[k].configure(bg=bg)
            e["mark"].configure(bg=bg, text="[>]" if active else "[ ]",
                                fg=AMBER if active else FAINT)
            e["t1"].configure(bg=bg, fg=TXT if active else (FAINT if off else BODY))
            e["t2"].configure(bg=bg, fg=FAINT)
            e["row"].configure(cursor="arrow" if off else "hand2")

    def _show(self, step: int) -> None:
        self.step = step
        for p in self.pages:
            p.pack_forget()
        self.pages[step - 1].pack(fill="both", expand=True)
        self._paint_rail()
        if step == 5:
            self._remix_mine()
        self.btn_back.config(state="normal" if step >= 3 else "disabled")
        # Video and Remix have their own buttons on the page; "continue"
        # under them would lead nowhere.
        if step in (4, 5):
            self.btn_next.pack_forget()
        elif not self.btn_next.winfo_manager():
            self.btn_next.pack(side="right")
        if step == 1:
            self.btn_next.config(text="scan games", state="normal")
        elif step == 2:
            # The selected row has to BE the game: after the video player was
            # set up, another game's row stayed selected under it.
            sel = self.tree.selection()
            try:
                picked = bool(sel) and self.shown[int(sel[0])] is self.game
            except (ValueError, IndexError, TypeError):
                picked = False
            self.btn_next.config(text="continue",
                                 state="normal" if picked else "disabled")
        elif step == 3:
            self.btn_next.config(text="INSTALL", state="normal")

    def _card(self, parent, pad=(16, 14), hover: bool = False) -> tk.Frame:
        c = tk.Frame(parent, bg=PANEL, highlightbackground=LINE,
                     highlightthickness=1)
        inner = tk.Frame(c, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=pad[0], pady=pad[1])
        c.inner = inner                       # type: ignore[attr-defined]
        if hover:
            # A card you can click should say so before you click it. The
            # border only, not the fill: a whole panel changing colour under
            # the pointer is noise, an edge lifting is an invitation.
            def paint(colour):
                def _(_e=None):
                    try:
                        c.configure(highlightbackground=colour)
                    except tk.TclError:
                        pass
                return _
            def leave(e):
                # Tk sends <Leave> to the parent when the pointer moves onto
                # a child, and the labels cover most of the card - so the
                # edge dropped the moment you actually looked at it.
                if getattr(e, "detail", "") != "NotifyInferior":
                    paint(LINE)()

            def arm(w):
                w.bind("<Enter>", paint(EDGE), add="+")
                w.bind("<Leave>", leave, add="+")
                for kid in w.winfo_children():
                    arm(kid)
            c.after_idle(lambda: arm(c))
        return c

    # ---------------------------------------------------------------- update
    def _check_update(self) -> None:
        """Look for a newer build and, when running as an .exe, fetch it.

        The download happens quietly in the background; nothing is replaced
        until the person presses the button, because a running install must
        never be interrupted by a restart.
        """
        def work() -> None:
            newer, latest, url = update.check()
            if not newer:
                return
            self.q.put(("update", (latest, url)))
            if selfupdate.running_exe() is None or not prefs.get("auto_update", True):
                return
            try:
                exe = selfupdate.fetch()
                self.q.put(("update_ready", (latest, exe)))
            except Exception as e:
                log.write(f"auto-update download failed: {e}", "warn")
        threading.Thread(target=work, daemon=True).start()

    def _show_banner(self, latest: str, url: str) -> None:
        self.update_url = url
        self.bannerlbl.config(text=f"> version {latest} is out "
                                   f"(you are on {update.VERSION})"
                                   + (" - downloading..." if selfupdate.running_exe()
                                      and prefs.get("auto_update", True) else ""))
        self.banner.pack(fill="x", before=self.body)

    def _update_ready(self, latest: str, exe: Path) -> None:
        self.update_ready = exe
        self.bannerlbl.config(text=f"> version {latest} downloaded and verified")
        self.updbtn.config(text="[ restart into it ]")

    def _do_update(self) -> None:
        if self.busy:
            return
        if self.update_ready is not None:
            self.bannerlbl.config(text="> restarting into the new build...")
            self.root.update()
            selfupdate.apply_and_restart(self.update_ready)
            return
        if selfupdate.running_exe() is None:
            webbrowser.open(self.update_url or update.RELEASES_PAGE)
            return
        if not messagebox.askyesno(
                APP,
                "Download the new version and restart?\n\n"
                "The current build is kept alongside as .old.exe so you can go "
                "back if anything is wrong."):
            return
        self.busy = True
        self.bannerlbl.config(text="> downloading update...")

        def work() -> None:
            try:
                exe = selfupdate.fetch(
                    progress=lambda d, t: self.q.put(
                        ("prog", (int(d * 100 / t) if t else 0,
                                  f"update - {d/1048576:.1f} MB"))))
                self.q.put(("swap", exe))
            except Exception as e:
                self.q.put(("updfail", str(e)))
        threading.Thread(target=work, daemon=True).start()


    def _eyebrow(self, parent, step: int) -> tk.Label:
        """The rail's name for this page over its title, tying the two."""
        lbl = tk.Label(parent, text=f"{PAGES[step][0]}   ::   {PAGES[step][1]}",
                       bg=BG, fg=FAINT, anchor="w", font=font(8))
        lbl.pack(anchor="w", pady=(0, px(4)))
        return lbl

    # ---------------------------------------------------------------- start
    def _page_start(self) -> tk.Frame:
        outer = tk.Frame(self.body, bg=BG)
        # The first page anyone sees, and at 200% scaling it was 448
        # pixels taller than the window with the bottom of it simply
        # gone - no scrollbar, no way to reach it (issue #40).
        scroll = Scroller(outer)
        scroll.pack(fill="both", expand=True)
        f = scroll.inner
        self._eyebrow(f, 1)
        ttk.Label(f, text="dlss 5 for the games you have", style="H1.TLabel")\
            .pack(anchor="w")
        self.startlbl = tk.Label(
            f, bg=BG, fg=DIM, font=font(9), justify="left", anchor="w",
            wraplength=px(680),
            text="[ scan games ] (bottom right) finds the games your stores "
                 "know about and the usual game folders, reads each executable "
                 "for its architecture and renderer, and picks the route that "
                 "fits it. for one game, use [ choose folder ] on the games "
                 "page. video and rtx remix are on the "
                 "left.")
        self.startlbl.pack(anchor="w", pady=(6, 0))

        # What the publishers ship right now - the tool always fetches these.
        self.boardlbl = tk.Label(f, text="", bg=BG, fg=DIM, font=font(8),
                                 anchor="w", justify="left", wraplength=px(680))
        self.boardlbl.pack(fill="x", pady=(10, 0))
        self._load_board()

        warn = tk.Frame(f, bg=PANEL, highlightbackground=RUST, highlightthickness=1)
        warn.pack(fill="x", pady=(16, 0))
        wi = tk.Frame(warn, bg=PANEL)
        wi.pack(fill="x", padx=16, pady=13)
        tk.Label(wi, text="!! before you get your hopes up", bg=PANEL, fg=RUST,
                 font=font(10, "bold")).pack(anchor="w")
        self.realitylbl = tk.Label(
            wi, bg=PANEL, fg=DIM, font=font(9), justify="left", anchor="w",
            wraplength=px(680),
            text="dlss5 works reliably on 64-bit directx 11/12. directx 9, "
                 "opengl, vulkan and every 32-bit game go through extra "
                 "translation, a layer or a helper process, and the dlss "
                 "feature fails to create there far more often. directx 10 goes "
                 "through the feeder only (build 0.13.1 and later). each game is labelled - "
                 "do not expect the long shots to work.\n\nnever use any of "
                 "this online: anti-cheat flags reshade add-ons.")
        self.realitylbl.pack(anchor="w", pady=(6, 0))
        wi.bind("<Configure>",
                lambda e: self.realitylbl.configure(wraplength=max(px(380), e.width - px(10))))
        f.bind("<Configure>",
               lambda e: (self.startlbl.configure(wraplength=max(px(380), e.width - px(10))),
                          self.boardlbl.configure(wraplength=max(px(380), e.width - px(10)))),
               add="+")
        return outer

    def _page_side(self, n: int, title: str, button: str, command,
                   text: str) -> tuple[tk.Frame, tk.Label, tk.Frame]:
        """A page of its own for video or Remix: what it is, and one button."""
        outer = tk.Frame(self.body, bg=BG)
        scroll = Scroller(outer)
        scroll.pack(fill="both", expand=True)
        f = scroll.inner
        self._eyebrow(f, n)
        top = tk.Frame(f, bg=BG)
        top.pack(fill="x")
        ttk.Label(top, text=title, style="H1.TLabel").pack(side="left")
        ttk.Button(top, text=button, style="Accent.TButton",
                   command=command).pack(side="right")
        card = tk.Frame(f, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        card.pack(fill="x", pady=(14, 0))
        ci = tk.Frame(card, bg=PANEL)
        ci.pack(fill="x", padx=16, pady=12)
        lbl = tk.Label(ci, bg=PANEL, fg=DIM, font=font(9), justify="left",
                       anchor="w", wraplength=px(660), text=text)
        lbl.pack(anchor="w")
        ci.bind("<Configure>",
                lambda e: lbl.configure(wraplength=max(px(380), e.width - px(10))))
        return outer, lbl, f

    # Video is the same feed with no depth buffer: a D3D11 player set up
    # once, after which any file or a YouTube link plays through DLSS 5.
    def _page_video(self) -> tk.Frame:
        page, self.videolbl, _f = self._page_side(
            4, "video and youtube", "set up the video player", self._video_setup,
            "a portable MPC-HC in a folder of your choice, with dlss5 fed "
            "into it. play any file, or File > Open URL with a youtube "
            "link - it streams live, nothing is downloaded. F6 switches "
            "neural rendering on and off while it plays. tested: 60 fps, "
            "the feed costs about 5% of the frame.")
        return page

    # Old games rebuilt with path tracing: DLSS 5 goes inside the Remix
    # runtime there, so it is a route of its own rather than an add-on.
    def _page_remix(self) -> tk.Frame:
        page, self.remixlbl, f = self._page_side(
            5, "rtx remix", "every remix project", self._show_remix,
            "RTX Remix is a separate, free NVIDIA mod that rebuilds an old "
            "game (2000s-era, fixed-function DirectX - GTA IV, Portal, Deus "
            "Ex, Vampire Bloodlines...) with real-time ray tracing: a full "
            "graphics overhaul, not a filter. A handful of those mods "
            "run DLSS 5 inside that engine.\n\n"
            "1. get the mod: for GTA IV and Need for Speed: Underground 2, "
            "once the game is in your library, the list behind the button "
            "has download & install, which puts it in for you; for the "
            "rest, install it from its own page.\n"
            "2. rescan on the games page: the game comes up on the remix "
            "route.\n"
            "3. INSTALL: this tool finds the mod's .trex runtime and switches "
            "DLSS 5 on in it. no reshade, no feeder, nothing else changes. "
            "proven on GTA IV: path tracing and DLSS 5 running together.")
        # Which of the games in the library have a mod, on the page itself:
        # "download & install" was two clicks away and nobody found it.
        self.remixmine = tk.Label(f, text="", bg=BG, fg=GREEN, font=font(9),
                                  anchor="w", justify="left", wraplength=px(680))
        self.remixmine.pack(fill="x", pady=(12, 0))
        f.bind("<Configure>", lambda e: self.remixmine.configure(
            wraplength=max(px(380), e.width - px(10))), add="+")
        return page

    def _remix_mine(self) -> None:
        """Name the library's games that have a Remix mod, on the remix page."""
        try:
            from . import remixlist
            found = remixlist.for_library(self.all_games)
        except Exception:
            found = []
        if not self._have_library():
            text = "scan your games first to see which of them have a mod."
        elif not found:
            text = "none of the games in your library has a remix mod listed."
        else:
            text = "in your library: " + ", ".join(
                g.name + (" (download & install)" if m.installable else "")
                for g, m in found)
        self.remixmine.config(text=text, fg=GREEN if found else DIM)

    # ---------------------------------------------------------------- games
    def _page_games(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        self._eyebrow(f, 2)
        # Title and the things you DO on the left-to-right line; the things
        # that change what the list SHOWS on the line below, with the search
        # box. Seven controls on one line with the title read as clutter,
        # and this page is the first thing the tool opens on now.
        top = tk.Frame(f, bg=BG)
        top.pack(fill="x")
        ttk.Label(top, text="pick a game", style="H1.TLabel").pack(side="left")
        ttk.Button(top, text="choose folder", command=self._pick_folder)\
            .pack(side="right", padx=(8, 0))
        ttk.Button(top, text="rescan", command=self._scan).pack(side="right")
        # Removing an install should not mean walking the whole wizard again.
        self.btn_rm2 = ttk.Button(top, text="uninstall", state="disabled",
                                  command=self._uninstall)
        self.btn_rm2.pack(side="right", padx=(0, 8))
        ttk.Button(top, text="update all", command=self._update_all)\
            .pack(side="right", padx=(0, 8))

        # A library of two hundred games with no way to search reads as "the
        # list is broken" - the only filter here used to be 32/64-bit.
        srow = tk.Frame(f, bg=BG)
        srow.pack(fill="x", pady=(10, 0))
        ttk.Label(srow, text="search", style="Dim.TLabel").pack(side="left")
        # Some libraries are huge, and some people only ever want to point
        # at one folder (issue #18): the automatic scan can be switched off.
        self.scan_on_start = tk.BooleanVar(value=bool(prefs.get("scan_on_start", True)))
        tk.Checkbutton(srow, text="scan library at start", variable=self.scan_on_start,
                       command=lambda: prefs.set_("scan_on_start", bool(self.scan_on_start.get())),
                       bg=BG, fg=DIM, selectcolor=FIELD, activebackground=BG,
                       activeforeground=TXT, font=font(8), borderwidth=0)\
            .pack(side="right", padx=(0, 2))
        self.only_installed = tk.BooleanVar(value=False)
        tk.Checkbutton(srow, text="installed only", variable=self.only_installed,
                       command=self._fill, bg=BG, fg=BODY, selectcolor=FIELD,
                       activebackground=BG, activeforeground=TXT,
                       font=font(9), borderwidth=0)\
            .pack(side="right", padx=(0, 16))
        # The 32/64-bit filter was a page of its own in front of the list;
        # a filter belongs beside the other filters.
        self.archbox = ttk.Combobox(srow, state="readonly", font=font(9),
                                    width=max(len(t) for _v, t in ARCH_CHOICES),
                                    values=[t for _v, t in ARCH_CHOICES])
        self.archbox.set(dict(ARCH_CHOICES)[self.arch.get()])

        def _arch_picked(_e=None) -> None:
            label = self.archbox.get()
            self.arch.set(next(v for v, t in ARCH_CHOICES if t == label))
            self._fill()
        self.archbox.bind("<<ComboboxSelected>>", _arch_picked)
        # ...and the other way, when something else sets the filter.
        self.arch.trace_add("write", lambda *_a: self.archbox.set(
            dict(ARCH_CHOICES).get(self.arch.get(), ARCH_CHOICES[0][1])))
        self.archbox.pack(side="right", padx=(0, 16))
        ent = tk.Entry(srow, textvariable=self.search, bg=FIELD, fg=TXT,
                       insertbackground=AMBER, relief="flat", font=font(10),
                       highlightthickness=1, highlightbackground=LINE,
                       highlightcolor=EDGE)
        ent.pack(side="left", fill="x", expand=True, padx=(10, 0), ipady=3)
        # "break": Esc here clears the box and stops there, instead of
        # also taking the window home.
        ent.bind("<Escape>", lambda e: (self.search.set(""), "break")[1])
        ent.bind("<Return>", lambda e: self._focus_first())
        ent.bind("<Down>", lambda e: self._focus_first())
        self.searchbox = ent

        self.scanlbl = ttk.Label(f, text="", style="Dim.TLabel")
        self.scanlbl.pack(anchor="w", pady=(6, 10))

        # The detail line under the list is packed first, from the bottom:
        # packed after the list it was squeezed to one pixel on a short
        # window and the selected game's details were simply not there
        # (issue #40, at 300%).
        wrap = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        cols = ("source", "arch", "api", "route", "outlook", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings",
                                 height=lines(13, 4))
        self.tree.heading("#0", text="  game", anchor="w")
        self.tree.column("#0", width=px(250), anchor="w")
        for c, t, w in (("source", "source", 76), ("arch", "arch", 62),
                        ("api", "api", 80), ("route", "route", 74),
                        ("outlook", "outlook", 96), ("status", "status", 92)):
            # Headings on the same edge as what is under them: centred
            # over left-aligned text, every column looked out of line.
            self.tree.heading(c, text=t, anchor="w")
            self.tree.column(c, width=px(w), anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=2)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("installed", foreground=GREEN)
        self.tree.tag_configure("unsupported", foreground=RED)
        self.tree.tag_configure("shaky", foreground=RUST)
        self.tree.tag_configure("stale", foreground=AMBER)
        self.tree.bind("<<TreeviewSelect>>", self._on_pick)
        self.tree.bind("<Double-1>", lambda e: self._next())

        det = self._card(f, pad=(14, 11))
        det.pack(side="bottom", fill="x", pady=(10, 0))
        wrap.pack(fill="both", expand=True)
        self.detail = tk.Label(det.inner, text="select a game for details",
                               bg=PANEL, fg=FAINT, font=font(9),
                               anchor="w", justify="left")
        self.detail.pack(fill="x")
        self.protected_details = tk.Frame(det.inner, bg=PANEL)
        tk.Label(self.protected_details, text=games.XBOX_EXE_HINT, bg=PANEL,
                 fg=RUST, font=font(9), anchor="w", justify="left",
                 wraplength=px(800)).pack(fill="x", pady=(6, 0))
        choices = tk.Frame(self.protected_details, bg=PANEL)
        choices.pack(anchor="w", pady=(6, 0))
        ttk.Label(choices, text="architecture").pack(side="left")
        self.cb_bitness = ttk.Combobox(choices, state="readonly", width=31,
                                      values=["auto - protected EXE", "64-bit", "32-bit"])
        self.cb_bitness.pack(side="left", padx=(8, 16))
        self.cb_bitness.bind("<<ComboboxSelected>>", self._on_bitness)
        ttk.Label(choices, text="graphics api").pack(side="left")
        self.cb_protected_api = ttk.Combobox(choices, state="readonly", width=25)
        self.cb_protected_api.pack(side="left", padx=(8, 0))
        self.cb_protected_api.bind("<<ComboboxSelected>>", self._on_api)
        return f

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="select the game folder")
        if not d:
            return
        g = games.manual(Path(d))
        if not g.exe:
            messagebox.showwarning(APP, f"no executable found in:\n{d}")
            return
        self.all_games.insert(0, g)
        self.root.after(1, self._remember_library)
        # A search still in the box could hide the folder just chosen.
        self.search.set("")
        self._cancel_fill()
        self._fill()
        for iid in self.tree.get_children():
            if self.shown[int(iid)] is g:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                break

    # ------------------------------------------------------------- video
    def _show_remix(self) -> None:
        """The Remix window: what has a mod, and which of them you own."""
        try:
            from . import remixui       # imports gui's colours: not at top level
            remixui.show(self.root, self.all_games)
        except Exception as e:
            log.exception("remix window")
            messagebox.showerror(APP, f"could not open the list:\n{e}")

    def _video_setup(self) -> None:
        """Fetch a portable player into a folder, then treat it as a game."""
        if self.busy:
            return
        known = video.known()
        folder = known.install_dir if known else video.default_dir()
        if not known:
            if not messagebox.askyesno(
                    APP, f"the video player ({video.PLAYER}, portable) goes into:\n\n"
                         f"{folder}\n\nnothing is written anywhere else. ok?\n\n"
                         f"'no' lets you pick another folder."):
                d = filedialog.askdirectory(
                    title="folder for the video player (a new, empty one is fine)")
                if not d:
                    return
                folder = Path(d)
        self.busy = True
        self.game = None
        self._show(3)
        self.gamelbl.config(text=f"Video player ({video.PLAYER})")
        self.btn_next.config(state="disabled", text="preparing")
        self.btn_back.config(state="disabled")
        self.pb["value"] = 0
        self._log("")
        self._log("=== video player ===", "head")
        self._log(f"> fetching {video.PLAYER} and yt-dlp into {folder}")

        def work() -> None:
            try:
                g = video.prepare(
                    folder,
                    on_prog=lambda p_, m: self.q.put(("prog", (p_, m))),
                    on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("video_ready", g))
            except (sources.RateLimited, sources.Unavailable) as e:
                self.q.put(("fail", str(e)))
            except Exception:
                log.exception("setting up the video player")
                self.q.put(("fail", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------- profiles / preview
    def _refresh_profiles(self, select: str = "") -> None:
        names = ["(none)"] + profiles.list_profiles()
        self.cb_profile["values"] = names
        self.cb_profile.current(names.index(select) if select in names else 0)

    def _on_profile(self, _e=None) -> None:
        """Load a profile into the visible settings.

        The widgets stay the source of truth: the profile only moves them,
        so anything changed afterwards still wins at install time. Values
        with no widget of their own (extra feed keys) ride along in
        self.profile_extra and are merged underneath the widgets' values.
        """
        name = self.cb_profile.get()
        self.profile_extra = None
        if name == "(none)":
            return
        try:
            opt = profiles.load(name)
        except Exception as e:
            messagebox.showerror(APP, str(e))
            return
        self.profile_extra = opt
        if self.support and opt.path in self.support.options:
            self.cb_route.current(self.support.options.index(opt.path))
            self._apply_route(opt.path)
        elif opt.path != getattr(self, "route", None):
            self._log(f"!! profile wants the {opt.path} route, which this game "
                      f"does not offer - keeping {getattr(self, 'route', '?')}",
                      "warn")
        if opt.provider in reshade_ini.PROVIDERS:
            self.provider.set(opt.provider)
            try:
                self.cb_prov.current(list(reshade_ini.PROVIDERS).index(opt.provider))
            except Exception:
                pass
        wr = opt.feed.get("work_resolution")
        ws = opt.nr.get("WorkingScale")
        if getattr(self, "route", None) == dlss.OPTI and ws is not None:
            self.workres.set(int(round(float(ws) * 100)))
        elif wr is not None:
            self.workres.set(int(wr))
        self._on_workres()
        self.keep_dlss.set(bool(opt.keep_game_dlss))
        self.dxvk.set(bool(opt.dxvk))
        try:
            pr = int(opt.feed.get("preset", 0) or 0)
            self.cb_preset.current(list(feedcfg.PRESETS).index(pr))
        except Exception:
            pass
        # The build is part of the profile, and applying everything else
        # while leaving this on the first entry means the person gets
        # Dagherbou's build with a profile they saved on a fork.
        try:
            self.cb_optibuild.current(list(optiscaler.BUILDS).index(opt.opti_build))
        except Exception:
            pass
        self._log(f"> profile '{name}': " + ", ".join(profiles.describe(opt)), "ok")

    def _save_profile(self) -> None:
        from tkinter import simpledialog
        name = simpledialog.askstring(APP, "profile name (these settings, for "
                                           "any game):", parent=self.root)
        if not name:
            return
        try:
            p = profiles.save(name.strip(), self._opts())
        except Exception as e:
            messagebox.showerror(APP, str(e))
            return
        self._refresh_profiles(name.strip())
        self._log(f"> profile saved: {p.name} - pick it from the list on any "
                  f"game", "ok")

    def _delete_profile(self) -> None:
        name = self.cb_profile.get()
        if name == "(none)" or profiles.is_builtin(name):
            messagebox.showinfo(APP, "pick one of your own profiles to delete")
            return
        if messagebox.askyesno(APP, f"delete the profile '{name}'?"):
            profiles.delete(name)
            self._refresh_profiles()

    def _preview(self) -> None:
        """Say what INSTALL would do, without doing it."""
        if not self.game or self.busy:
            return
        try:
            pv = installer.preview(self.game, self._opts())
        except Exception as e:
            log.exception("preview")
            self._log(f"!! preview failed: {e}", "err")
            return
        self._log("")
        self._log("=== what will happen ===", "head")
        for line in installer.preview_lines(pv):
            tag = ("err" if line.startswith("cannot") else
                   "warn" if line.startswith(("warning", "outside")) else "")
            self._log(f"   {line}", tag)
        self._log("   nothing is downloaded or written by this preview")

    def _compare(self) -> None:
        if not self.game:
            return
        try:
            from . import compareui   # imports gui's colours: not at top level
            compareui.show(self.root, self.game.install_dir, self.game.name)
        except Exception as e:
            log.exception("compare window")
            messagebox.showerror(APP, f"could not open the comparison:\n{e}")

    def _open_player(self) -> None:
        if self.game and getattr(self.game, "kind", "") == "video":
            try:
                video.launch(self.game.install_dir)
            except Exception as e:
                messagebox.showerror(APP, f"could not start the player:\n{e}")

    def _play_url(self) -> None:
        if not self.game or getattr(self.game, "kind", "") != "video":
            return
        u = self.url.get().strip() or video.clipboard_url(self.root)
        if not video.looks_like_url(u):
            messagebox.showinfo(APP, "paste a link first (https://...)")
            return
        if not self.game.installed:
            self._log("!! dlss5 is not installed into the player yet - press "
                      "INSTALL first, the link still plays but plain", "warn")
        self.url.set(u)
        try:
            video.play_url(self.game.install_dir, u)
            self._log(f"> playing {u[:90]}", "ok")
        except Exception as e:
            messagebox.showerror(APP, f"could not start the player:\n{e}")

    def _refresh_cameras(self, then=None) -> None:
        """Enumerate cameras in the background: DirectShow can stall for
        seconds on a virtual camera driver, and the window must not freeze."""
        if not self.game or getattr(self.game, "kind", "") != "video":
            return
        folder = self.game.install_dir
        self.cb_camera["values"] = ["(looking...)"]
        self.cb_camera.current(0)

        def work() -> None:
            cams = video.list_cameras(folder)
            self.q.put(("cameras", (cams, then)))
        threading.Thread(target=work, daemon=True).start()

    def _cameras_found(self, cams: list, then=None) -> None:
        self.cb_camera["values"] = cams or ["(no camera found - ffmpeg must be in "
                                            "tools/, run one download first)"]
        self.cb_camera.current(0)
        if cams:
            self._log(f"> cameras: {', '.join(cams)}")
        if then and cams:
            then()

    def _start_webcam(self) -> None:
        if not self.game or getattr(self.game, "kind", "") != "video":
            return
        cam = self.cb_camera.get()
        if not cam or cam.startswith("("):
            # No list yet: enumerate first, then come back here.
            self._refresh_cameras(then=self._start_webcam)
            return
        import time
        t0 = time.time()
        folder = self.game.install_dir
        self._log(f"> webcam '{cam}': starting the stream...")

        def work() -> None:
            try:
                video.start_webcam(folder, cam)
                self.q.put(("webcam_started", (cam, t0)))
            except Exception as e:
                self.q.put(("webcam_failed", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _webcam_started(self, cam: str, t0: float) -> None:
        self._log(f"> '{cam}' started - the player opens the stream; "
                  f"checking in 10 s whether the feed is processing it...")
        folder = self.game.install_dir if self.game else None
        if folder is None:
            return
        if True:
            def verify() -> None:
                frames, mv = video.feed_frames_since(folder, t0)
                sent = video.capture_frames_sent()
                if sent:
                    self._log(f"> capture: {sent} frames sent to the player so far")
                if frames >= 3:
                    self._log(f"> '{cam}' through DLSS 5: yes - {frames} frames "
                              f"processed so far" + (", motion alive" if mv else "")
                              + ". F6 toggles it, 'stop' ends the stream.", "ok")
                else:
                    self._log("!! the feed has not processed any frames yet. "
                              "if the player shows the picture, give it a few "
                              "seconds and press 'did it work?'; if it shows "
                              "nothing, another app may hold the camera, or the "
                              "window is minimised.", "warn")
            self.root.after(10000, verify)

    def _stop_webcam(self) -> None:
        video.stop_webcam()
        self._log("> stream stopped")

    def _refresh_screens(self, then=None) -> None:
        items = video.list_screens()
        self.cb_screen["values"] = items
        self.cb_screen.current(0)
        self._log(f"> {len(items)} screens and windows listed")
        if then:
            then()

    def _start_screen(self) -> None:
        if not self.game or getattr(self.game, "kind", "") != "video":
            return
        target = self.cb_screen.get()
        if not target or target.startswith("("):
            self._refresh_screens(then=self._start_screen)
            return
        import time
        t0 = time.time()
        folder = self.game.install_dir
        self._log(f"> '{target}': starting the capture...")

        def work() -> None:
            try:
                video.start_screen(folder, target)
                self.q.put(("webcam_started", (target, t0)))
            except Exception as e:
                self.q.put(("webcam_failed", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _open_processed(self) -> None:
        if not self.game or getattr(self.game, "kind", "") != "video":
            return
        d = self.game.install_dir / video.PROCESSED
        d.mkdir(exist_ok=True)
        webbrowser.open(str(d))

    def _process_file(self) -> None:
        if self.busy or not self.game or getattr(self.game, "kind", "") != "video":
            return
        if not self.game.installed:
            messagebox.showinfo(APP, "press INSTALL first - the processor uses the "
                                     "DLSS runtimes the install puts beside the player")
            return
        f = filedialog.askopenfilename(
            title="video to render through DLSS 5",
            filetypes=[("video", "*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.ts"),
                       ("all files", "*.*")])
        if not f:
            return
        self.busy = True
        self.btn_next.config(state="disabled", text="rendering")
        self.pb["value"] = 0
        folder = self.game.install_dir
        scale = self.cb_scale.get()
        style = list(video.STYLES.values()).index(self.cb_style.get())
        self._log("")
        self._log(f"=== render: {Path(f).name} ===", "head")
        self._log("> this runs the model on every frame; a minute of 1080p takes "
                  "a minute or two on an RTX 40. the player stays usable meanwhile.")

        def work() -> None:
            try:
                out = video.process(
                    folder, Path(f), scale=scale, style=style,
                    on_prog=lambda p_, m: self.q.put(("prog", (p_, m))),
                    on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("processed", out))
            except Exception as e:
                log.exception("rendering a video")
                self.q.put(("fail", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _open_downloads(self) -> None:
        if not self.game or getattr(self.game, "kind", "") != "video":
            return
        d = self.game.install_dir / video.DOWNLOADS
        d.mkdir(exist_ok=True)
        webbrowser.open(str(d))

    def _open_file(self) -> None:
        """A video already on disk, whoever downloaded it: play it here."""
        if not self.game or getattr(self.game, "kind", "") != "video":
            return
        f = filedialog.askopenfilename(
            title="video to play through DLSS 5",
            filetypes=[("video", "*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.ts *.wmv"),
                       ("all files", "*.*")])
        if not f:
            return
        try:
            video.launch(self.game.install_dir, f)
            self._log(f"> playing {Path(f).name} - F6 toggles neural rendering", "ok")
        except Exception as e:
            messagebox.showerror(APP, "could not start the player:\n" + str(e))

    def _download_url(self) -> None:
        if self.busy or not self.game or getattr(self.game, "kind", "") != "video":
            return
        u = self.url.get().strip() or video.clipboard_url(self.root)
        if not video.looks_like_url(u):
            messagebox.showinfo(APP, "paste a link first (https://...)")
            return
        self.url.set(u)
        self.busy = True
        self.btn_next.config(state="disabled", text="downloading")
        self.pb["value"] = 0
        folder = self.game.install_dir
        full = self.fullq.get()
        self._log("")
        self._log(f"=== download: {u[:90]} ===", "head")
        self._log(f"> saving under {folder / video.DOWNLOADS} - the 'downloads "
                  f"folder' button opens it")

        def work() -> None:
            try:
                if not video.has_ffmpeg(folder):
                    self.q.put(("log", "      fetching ffmpeg once (170 MB) - "
                                       "youtube only serves video and audio "
                                       "apart, it joins them"))
                    video.ensure_ffmpeg(
                        folder,
                        on_prog=lambda p_, m: self.q.put(("prog", (p_, m))),
                        on_log=lambda t: self.q.put(("log", t)))
                f_ = video.download(
                    folder, u, full_quality=full,
                    on_prog=lambda p_, m: self.q.put(("prog", (p_, m))),
                    on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("downloaded", f_))
            except Exception as e:
                log.exception("downloading a video")
                self.q.put(("fail", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _toggle_nr(self) -> None:
        """Send the add-on's toggle key to the running player."""
        if not video.toggle_nr():
            messagebox.showinfo(APP, "the player is not running - open it and "
                                     "start a video first. F6 inside the "
                                     "player does the same thing.")

    def _forget_row(self, g) -> None:
        """Drop one game's compatibility row - it is about to be re-read."""
        if g is not None and getattr(g, "exe", None):
            self._rows.pop((str(g.folder), str(g.exe)), None)
        else:
            self._rows.clear()

    def _remember_library(self) -> None:
        """Write the library back after something changed it outside a scan.

        A folder chosen by hand, a different executable picked, an install
        or an uninstall: without this the next launch would show the library
        as it was before, and the person would have to rescan to get their
        own change back.
        """
        # Not while a worker is still re-reading games: their rows are
        # deliberately absent and their Game objects still hold the OLD
        # renderer, so saving now would write that with a fresh stamp and
        # the next launch would never look at them again. The "rechecked"
        # handler saves once the worker lands.
        if self._recheck:
            return
        # Never write an empty set of rows over a full one: that would cost
        # the next launch the whole compatibility pass.
        if self._have_library() and (self._rows or not library.FILE.is_file()):
            library.save(self.all_games, self._rows, update.VERSION, self._sm())

    def _load_cached(self) -> bool:
        """Show the library found last time, without walking the disks again.

        Everything that has not changed since is on screen at once. Games
        whose folder or executable moved on are read again - their renderer
        may be what changed - and that costs a folder walk each, so it
        happens on a worker thread while the list is already usable.
        "rescan" always does the full walk (issue #67).
        """
        try:
            got = library.load(update.VERSION, self._sm())
        except Exception:
            log.exception("reading the library cache")
            got = None
        if not got:
            return False
        gs, rows, changed = got
        self.all_games = list(gs)
        kp = video.known()
        if kp and not any(x.install_dir == kp.install_dir for x in gs):
            self.all_games.insert(0, kp)
        self._rows.clear()
        self._rows.update(rows)
        # Rows for the changed games are filled in by the worker below; until
        # then they are simply not in _rows, and _fill leaves them to it.
        # Keyed on the folder alone: enrich() can reassign g.exe (it
        # adopts a previous install's executable), and a key built from the
        # old one would stop matching half way through.
        self._recheck = {str(g.folder) for g in changed}
        self._recheck_id = getattr(self, "_recheck_id", 0) + 1
        self._fill()
        self.scanlbl.config(
            text=f"{len(self.shown)} games  ::  from the last scan"
                 + (f"  ::  reading {len(self._recheck)} that changed"
                    if self._recheck else "")
                 + "  ::  press rescan to look for new ones")
        if changed:
            self._recheck_changed(changed)
        return True

    def _recheck_changed(self, changed: list) -> None:
        """Read the games whose folder moved on, off the Tk thread.

        Both halves matter: games.enrich() re-reads the executable, so a
        game that switched renderer in an update is not installed for the
        old one, and _inspect_row walks the folder for what is already in
        it. Doing either inline is what froze the window before the scan
        was moved to a worker (issues #8, #18, #32).
        """
        sm = self._sm()

        gen = self._recheck_id

        def work() -> None:
            rows = {}
            for g in changed:
                try:
                    games.enrich(g)
                except Exception:
                    log.exception(f"re-reading {g.name}")
                if g.exe:
                    rows[(str(g.folder), str(g.exe))] = self._inspect_row(g, sm)
            self.q.put(("rechecked", (gen, rows)))

        threading.Thread(target=work, daemon=True).start()

    def _scan(self) -> None:
        if self.busy:
            return
        self.busy = True
        # Whatever a recheck worker is holding is about to be replaced.
        self._recheck = set()
        self._recheck_id = getattr(self, "_recheck_id", 0) + 1
        self._rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.scanlbl.config(text="scanning...")
        self.btn_next.config(state="disabled")

        def work() -> None:
            try:
                gs = games.scan_all(progress=lambda m: self.q.put(("scan", m)))
                # Compatibility checks walk folders too. Doing them in _fill
                # blocked Tk just as it received the last scan messages, so
                # the window stayed painted at e.g. 95/97 for minutes.
                rows = {}
                sm = self._sm()
                for i, g in enumerate(gs, 1):
                    if not g.exe:
                        continue
                    self.q.put(("scan", f"Checking compatibility... {i}/{len(gs)}: {g.name}"))
                    started = time.monotonic()
                    rows[(str(g.folder), str(g.exe))] = self._inspect_row(g, sm)
                    if time.monotonic() - started >= 1:
                        log.write(f"checked {g.name} in {time.monotonic() - started:.1f}s")
                library.save(gs, rows, update.VERSION, sm)
                self.q.put(("scanned", (gs, rows)))
            except (sources.RateLimited, sources.Unavailable) as e:
                self.q.put(("error", str(e)))
            except Exception:
                log.exception("scanning the library")
                self.q.put(("error", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def _load_board(self) -> None:
        """Current upstream versions, one line, from the cached API answers."""
        def work() -> None:
            parts = []
            for key, label in (("feeder", "feeder"), ("bridge", "bridge"),
                               ("optiscaler", "optiscaler"), ("renodx", "renodx-dlss5"),
                               ("renodx_sf", "renodx-dlss SF"), ("dlssnr", "dlssnr"),
                               ("reshade", "reshade")):
                try:
                    v = components._latest(key)
                    if v:
                        parts.append(f"{label} {v}")
                except Exception:
                    continue
            if parts:
                self.q.put(("board", "publishers ship:  " + "  |  ".join(parts)))
        threading.Thread(target=work, daemon=True).start()

    def _update_all(self) -> None:
        """Reinstall every game whose parts have moved on, same choices as before."""
        if self.busy:
            return
        targets = [g for g in self.all_games
                   if g.exe and g.installed and self.stale.get(str(g.install_dir))]
        if not targets:
            self.scanlbl.config(text="nothing to update - every installed game is current")
            return
        if not messagebox.askyesno(
                APP, f"update {len(targets)} game(s)?\n\nEach is installed again "
                     f"with the same route and settings, fetching the newest "
                     f"components. Backups and your own files are kept."):
            return
        self.busy = True
        self.btn_next.config(state="disabled")

        def work() -> None:
            done = 0
            for g in targets:
                opt = installer.options_from_manifest(g.install_dir)
                if opt is None:
                    continue
                self.q.put(("scan", f"updating {g.name}..."))
                try:
                    installer.install(g, opt, on_log=lambda t: log.write(t))
                    done += 1
                except Exception as e:
                    log.exception(f"updating {g.name}", e)
                    self.q.put(("scan", f"{g.name}: {_first_line(e)}"))
            self.stale = {}
            self.q.put(("updated_all", done))
        threading.Thread(target=work, daemon=True).start()

    def _check_stale(self) -> None:
        """Which installed games have parts that moved on since?

        Runs after every scan, in the background, one source lookup each.
        The result is a word in the status column - the fix is just to press
        install again on that game.
        """
        roots = [g.install_dir for g in self.all_games if g.exe and g.installed]
        if not roots:
            return

        def work() -> None:
            try:
                self.q.put(("stale", components.stale_counts(roots)))
            except Exception:
                log.exception("checking installed games for updates")
        threading.Thread(target=work, daemon=True).start()

    def _cancel_fill(self) -> None:
        if self._fill_job is not None:
            try:
                self.root.after_cancel(self._fill_job)
            except tk.TclError:
                pass
            self._fill_job = None

    def _search_changed(self, *_args) -> None:
        # Refilling reads folders, so coalesce a burst of typing into one pass.
        if not hasattr(self, "tree"):
            return
        self._cancel_fill()
        self._fill_job = self.root.after(180, self._fill)

    def _focus_first(self) -> None:
        """Enter (or Down) in the search box picks the first match."""
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])
            self.tree.see(kids[0])

    @staticmethod
    def _inspect_row(g: games.Game, sm: int | None):
        """Read the compatibility columns, normally on the scan worker."""
        try:
            ok, _ = installer.check_supported(g)
            if not ok:
                # Missing metadata or a fatal scan error: show its reason.
                return False, "-", installer.EXPERIMENTAL, "-", False, ""
            sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm)
            level, _ = installer.reliability(g, sup.recommended)
            outlook = {installer.STABLE: "reliable",
                       installer.BETA: "beta",
                       installer.EXPERIMENTAL: "often fails"}[level]
            ac = anticheat.detect(g.install_dir, g.folder)
            return ok, sup.recommended, level, outlook, ac.present, ac.summary
        except Exception as e:
            log.exception(f"inspecting {g.name}", e)
            return False

    @staticmethod
    def _matches(g: games.Game, terms: list[str]) -> bool:
        """Every word typed has to appear - in the name, folder or store."""
        if not terms:
            return True
        hay = f"{g.name} {g.folder} {g.source}".lower()
        return all(t in hay for t in terms)

    def _fill(self) -> None:
        self._cancel_fill()
        self.tree.delete(*self.tree.get_children())
        a = self.arch.get()
        only = getattr(self, "only_installed", None)
        q = self.search.get().strip().lower()
        terms = q.split()
        # A game whose architecture could not be read (the executable was
        # locked by antivirus or a running updater, or it is a cloud
        # placeholder) used to vanish under a 32/64 filter with no explanation.
        # Show it: hiding it is the one outcome the user cannot act on.
        self.shown = [g for g in self.all_games
                      if g.exe and (a == "all" or g.bitness is None
                                    or str(g.bitness) == a)
                      and (not (only and only.get()) or g.installed)
                      and self._matches(g, terms)]
        for i, g in enumerate(self.shown):
            key = (str(g.folder), str(g.exe))
            row = self._rows.get(key)
            if row is None and str(g.folder) in self._recheck:
                # A worker is walking this folder; reading it here as well
                # would block the window, which is the whole reason the
                # compatibility pass is not done on this thread.
                self.tree.insert("", "end", iid=str(i), text="  " + g.name,
                                 values=(g.source.lower(), g.bit_label, g.api,
                                         "-", "-", "reading..."))
                continue
            if row is None:
                # Newly chosen folders and changed installs need fresh rows;
                # the library scan supplies its rows before asking Tk to fill.
                row = self._inspect_row(g, self._sm())
                self._rows[key] = row
            if row is False:
                self.tree.insert("", "end", iid=str(i), text="  " + g.name,
                                 values=(g.source.lower(), g.bit_label, g.api,
                                         "-", "-", "unreadable"),
                                 tags=("unsupported",))
                continue
            ok, route, level, outlook, ac_present, ac_summary = row
            if not ok:
                status = "needs metadata" if g.exe_warning and not g.error else "unsupported"
                tag, outlook = "unsupported", "-"
            elif ac_present:
                # Not refused - it is their machine - but said up front, and
                # asked again before INSTALL.
                status, tag, outlook = f"{ac_summary}!", "shaky", "online - at your own risk"
            elif g.installed:
                # Read fresh every time: this changes on install and uninstall.
                n_stale = self.stale.get(str(g.install_dir), 0)
                man = diagnose._manifest(g.install_dir)
                man_api = man.get("api") or ""
                # A DXVK install is recorded as Vulkan on purpose: the game
                # was relabelled for the layer, not misdetected.
                if man_api and man_api != g.api and not man.get("dxvk") \
                        and man.get("proxy") != diagnose.VULKAN_LAYER:
                    # Installed for one renderer, detected as another (a
                    # Unity game installed as OpenGL by 1.7.1): the files
                    # in the folder are never loaded.
                    status, tag = f"reinstall - was {man_api}", "stale"
                elif n_stale:
                    status, tag = f"update ({n_stale} newer)", "stale"
                else:
                    status, tag = "installed", "installed"
            else:
                status = "ready"
                tag = "shaky" if level == installer.EXPERIMENTAL else ""
            self.tree.insert("", "end", iid=str(i), text="  " + g.name,
                             values=(g.source.lower(), g.bit_label, g.api,
                                     route, outlook, status),
                             tags=(tag,) if tag else ())
        hidden = len([g for g in self.all_games if not g.exe])
        n_inst = len([g for g in self.all_games if g.exe and g.installed])
        msg = f"{len(self.shown)} games  ::  {n_inst} installed"
        if self.stale:
            msg += f"  ::  {len(self.stale)} with newer parts - press install again"
        if a != "all":
            msg += f"  ::  {a}-bit filter"
        if q:
            msg += f'  ::  matching "{q}"'
        if only and only.get():
            msg += "  ::  showing installed only"
        if hidden:
            msg += f"  ::  {hidden} folders had no executable"
        # An empty list used to say "0 games" and leave you stuck. Say what to
        # do instead: not every launcher can be discovered from the registry.
        if not self.shown:
            if q and self.all_games:
                listed = len([g for g in self.all_games if g.exe])
                msg = (f'nothing matches "{q}"  ::  clear the search box for '
                       f'all {listed} games')
            elif self.all_games and a != "all":
                msg = (f"no {a}-bit games  ::  set the filter to 'every "
                       f"architecture', or use [choose folder]")
            elif self.all_games and only and only.get():
                msg = "nothing installed yet  ::  untick 'installed only'"
            elif self.all_games:
                msg = ("no games to list  ::  use [choose folder] and point "
                       "at the game's own folder")
            else:
                msg = ("no games found  ::  use [choose folder] and point at "
                       "the game's own folder - see the log for what each "
                       "store returned")
        self.scanlbl.config(text=msg)
        self.game = None
        self.protected_details.pack_forget()
        self.detail.config(text="select a game for details", fg=FAINT)
        self.btn_next.config(state="disabled")
        self._paint_rail()

    def _on_pick(self, _evt=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        g = self.shown[int(sel[0])]
        if g is not self.game:
            self._forget_last_session()
        self.game = g
        self._paint_rail()
        ok, why = installer.check_supported(g)
        self.protected_details.pack_forget()
        if g.exe_warning:
            self.cb_bitness.current({64: 1, 32: 2}.get(g.bitness, 0))
            self.cb_protected_api["values"] = [f"auto - {g.api_detected}"] + list(games.APIS)
            forced = games.api_override(g.folder)
            self.cb_protected_api.current(games.APIS.index(forced) + 1 if forced in games.APIS else 0)
            self.protected_details.pack(fill="x")
        sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, self._sm())
        level, why_rel = installer.reliability(g, sup.recommended)
        proxy = installer._proxy_name(g.api, self._opts().reshade_proxy)
        lines = [f"exe    {g.exe}",
                 f"arch   {g.bit_label}  api {g.api}  ({g.api_why})",
                 f"route  {dlss.LABELS[sup.recommended]}  [{level}]"
                 + ("  -  ships its own dlss" if sup.native_dlss else "")]
        n_stale = self.stale.get(str(g.install_dir), 0)
        if n_stale:
            lines.append(f"update {n_stale} installed part(s) have a newer version "
                         f"- press install again to bring them up to date")
        if ok:
            need = installer.wants_dxvk(g)
            lines.append(f"path   reshade as {installer.VULKAN_LAYER if need else proxy}"
                         + ("  +  host64/ helper" if g.bitness == 32 else "")
                         + ("  +  dxvk (vulkan)" if g.api == "DX9" else "")
                         + ("  +  dxvk (this game quits when reshade hooks it)"
                            if need else ""))
            if level != installer.STABLE:
                lines.append(f"note   {why_rel}")
        else:
            lines.append(f"note   {why}")
        ac = anticheat.detect(g.install_dir, g.folder)
        if ac.present:
            lines.append(f"WARN   {ac.summary} is installed here. This is an online "
                         f"game: its anti-cheat can refuse to start, remove the "
                         f"files, or ban the account. Installing is your decision")
        if sup.recommended != dlss.REMIX and reengine.detected(g.install_dir):
            lines.append("note   RE Engine (Capcom) game - ReShade's add-ons "
                         "are documented to crash titles like this, worst on "
                         "Denuvo'd ones (Requiem); this install puts "
                         "REFramework in first so ReShade survives, the way "
                         "players report doing it on Requiem itself - "
                         "'did it work?' after a try explains if it didn't")
        other = games._recorded_exe(g.install_dir)
        if other and g.exe and other.lower() != g.exe.name.lower():
            lines.append(f"shared this folder is already set up for {other}; both "
                         f"executables use the same files, so installing or "
                         f"uninstalling here affects both")
        if getattr(g, "emu", None):
            lines.append(f"emu    {g.emu.renderer_hint}")
        self.detail.config(text="\n".join(lines),
                           fg=(DIM if level == installer.STABLE else RUST)
                           if ok else RED)
        self.btn_next.config(state="normal" if ok else "disabled")
        if hasattr(self, "btn_rm2"):
            self.btn_rm2.config(state="normal" if g.installed else "disabled")

    # ---------------------------------------------------------------- install
    def _page_install(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        # Everything above the buttons goes in here: the settings are taller
        # than the screen once the fonts are scaled, and this is what keeps
        # them from squeezing the log out of the window (issue #40).
        self.installscroll = Scroller(f)
        top = self.installscroll.inner
        self._eyebrow(top, 3)
        self.gamelbl = ttk.Label(top, text="", style="H1.TLabel")
        self.gamelbl.pack(anchor="w")
        self.pathlbl = ttk.Label(top, text="", style="Dim.TLabel")
        self.pathlbl.pack(anchor="w", pady=(4, 12))

        card = self._card(top)
        card.pack(fill="x")
        inner = card.inner
        inner.columnconfigure(1, weight=1)

        def row(r: int, label: str, colour: str = DIM) -> tk.Label:
            lbl = tk.Label(inner, text=label, bg=PANEL, fg=colour, font=font(9))
            lbl.grid(row=r, column=0, sticky="w", padx=(0, 14), pady=5)
            return lbl

        self.exerow = tk.Label(inner, text="target exe", bg=PANEL, fg=AMBER,
                               font=font(9))
        self.cb_exe = ttk.Combobox(inner, state="readonly", values=[])
        self.cb_exe.bind("<<ComboboxSelected>>", self._on_exe)

        row(1, "route")
        self.cb_route = ttk.Combobox(inner, state="readonly", values=[])
        self.cb_route.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        self.cb_route.bind("<<ComboboxSelected>>", self._on_route)

        self.routelbl = Prose(inner)
        self.routelbl.grid(row=2, column=0, columnspan=3, sticky="ew",
                           pady=(px(2), px(10)))

        self.lbl_mv = row(3, "motion vectors")
        self.cb_prov = ttk.Combobox(inner, state="readonly",
                                    values=[v[0] for v in reshade_ini.PROVIDERS.values()])
        self.cb_prov.current(0)
        self.cb_prov.grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)
        self.cb_prov.bind("<<ComboboxSelected>>", self._on_prov)

        # Shares row 3 with motion vectors: the feeder needs one, OptiScaler
        # needs the other, and no route needs both.
        self.lbl_proxy = tk.Label(inner, text="loads as", bg=PANEL, fg=DIM,
                                  font=font(9))
        self.cb_proxy = ttk.Combobox(
            inner, state="readonly",
            values=["auto - pick a free name"] +
                   [f"{n}  -  {optiscaler.PROXY_HELP[n]}"
                    for n in optiscaler.PROXY_NAMES])
        self.cb_proxy.current(0)
        self.cb_proxy.bind("<<ComboboxSelected>>", self._on_proxy)

        # ReShade is loaded the same way, under the name of a system DLL. When
        # a game will not start at all, this is the first thing worth changing.
        self.lbl_rproxy = tk.Label(inner, text="reshade loads as", bg=PANEL,
                                   fg=DIM, font=font(9))
        self.cb_rproxy = ttk.Combobox(
            inner, state="readonly",
            values=["auto - from the graphics api"] +
                   [f"{n}  -  {installer.RESHADE_PROXY_HELP[n]}"
                    for n in installer.RESHADE_PROXIES])
        self.cb_rproxy.current(0)

        row(4, "dlss5 add-on")
        self.cb_renodx = ttk.Combobox(inner, state="readonly", values=["loading..."])
        self.cb_renodx.grid(row=4, column=1, sticky="ew", pady=5)
        ttk.Button(inner, text="use my file", command=self._pick_renodx)\
            .grid(row=4, column=2, sticky="w", padx=(10, 0), pady=5)

        row(5, "nvngx_dlssnr")
        self.cb_dlssnr = ttk.Combobox(inner, state="readonly",
                                      values=["auto - match my gpu"])
        self.cb_dlssnr.current(0)
        self.cb_dlssnr.grid(row=5, column=1, columnspan=2, sticky="ew", pady=5)

        row(6, "nvngx_dlss")
        self.cb_dlss = ttk.Combobox(inner, state="readonly", values=["loading..."])
        self.cb_dlss.grid(row=6, column=1, sticky="ew", pady=5)
        tk.Checkbutton(inner, text="keep the game's own",
                       variable=self.keep_dlss, command=self._on_keep_dlss,
                       bg=PANEL, fg=BODY, selectcolor=FIELD, activebackground=PANEL,
                       activeforeground=TXT, font=font(9), borderwidth=0)\
            .grid(row=6, column=2, sticky="w", padx=(10, 0))

        # Ray reconstruction, for a game that ships it. NVIDIA publishes
        # this runtime as well, so a game stuck on an old build can be moved
        # on the same way DLSS itself can. Hidden for every game that does
        # not have one: installing it would change nothing.
        self.rr_row = tk.Frame(inner, bg=PANEL)
        self.lbl_dlssd = tk.Label(self.rr_row, text="ray reconstruction",
                                  bg=PANEL, fg=DIM, font=font(9))
        self.lbl_dlssd.pack(side="left", padx=(0, px(10)))
        self.cb_dlssd = ttk.Combobox(self.rr_row, state="readonly", width=28,
                                     values=["loading..."])
        self.cb_dlssd.bind("<<ComboboxSelected>>", self._on_dlssd)
        self.cb_dlssd.pack(side="left")
        self.dlssdhint = tk.Label(self.rr_row, text="", bg=PANEL, fg=DIM,
                                  font=font(8))
        self.dlssdhint.pack(side="left", padx=(px(10), 0))

        tk.Frame(inner, bg=LINE, height=px(1)).grid(row=7, column=0, columnspan=3,
                                                sticky="ew", pady=(12, 9))

        row(8, "work area", TXT)
        wrap = tk.Frame(inner, bg=PANEL)
        wrap.grid(row=8, column=1, columnspan=2, sticky="ew", pady=3)
        self.sc_work = tk.Scale(wrap, from_=50, to=100, resolution=5,
                                orient="horizontal", variable=self.workres,
                                # The handle is drawn in `bg`: on the panel
                                # colour it vanished until pressed. Amber at
                                # rest, brighter under the mouse, a trough
                                # you can actually see.
                                bg=AMBER, fg=TXT, troughcolor=SLIDER_TROUGH,
                                highlightthickness=0, borderwidth=0,
                                showvalue=True, font=font(9), length=px(230),
                                sliderlength=22, sliderrelief="raised",
                                activebackground=SLIDER_HOT,
                                command=self._on_workres)
        self.sc_work.pack(side="left")
        # The dial above is what everyone sets by feel. This is the same
        # dial, set from what the last session actually measured instead:
        # say what you want, play, and press "did it work?".
        aim = tk.Frame(wrap, bg=PANEL)
        aim.pack(side="left", padx=(14, 0))
        tk.Label(aim, text="aim for", bg=PANEL, fg=DIM,
                 font=font(9)).pack(side="left")
        self.sp_target = tk.Spinbox(aim, from_=30, to=240, increment=5,
                                    width=4, textvariable=self.target_fps,
                                    bg=FIELD, fg=TXT, buttonbackground=PANEL,
                                    insertbackground=TXT, font=font(9),
                                    highlightthickness=0, borderwidth=0,
                                    command=self._on_target)
        self.sp_target.pack(side="left", padx=px(4))
        self.sp_target.bind("<FocusOut>", lambda _e: self._on_target())
        # The two bindings above miss a value typed straight before a button
        # press: the session was right and the next launch had forgotten it.
        self.target_fps.trace_add("write", lambda *_a: self._target_typed())
        tk.Label(aim, text="fps", bg=PANEL, fg=DIM,
                 font=font(9)).pack(side="left")
        self.workhint = tk.Label(wrap, text="", bg=PANEL, fg=DIM, font=font(8),
                                 justify="left", wraplength=px(250))
        self.workhint.pack(side="left", padx=(14, 0))

        self.lbl_preset = row(9, "dlss preset")
        self.cb_preset = ttk.Combobox(inner, state="readonly",
                                      values=list(feedcfg.PRESETS.values()))
        self.cb_preset.current(0)
        self.cb_preset.grid(row=9, column=1, columnspan=2, sticky="ew", pady=5)

        self.lbl_hdr = row(10, "hdr")
        self.cb_hdr = ttk.Combobox(inner, state="readonly", width=18,
                                   values=list(feedcfg.HDR.values()))
        self.cb_hdr.current(0)
        self.cb_hdr.grid(row=10, column=1, sticky="w", pady=5)
        self.dlaalbl = tk.Label(inner, text="", bg=PANEL, fg=DIM, font=font(8))
        self.dlaalbl.grid(row=10, column=2, sticky="w", padx=(10, 0))

        # OptiScaler's own dials sit in the same two rows; only one route's
        # controls are ever shown at a time.
        self.lbl_nrpreset = tk.Label(inner, text="model preset", bg=PANEL, fg=DIM,
                                     font=font(9))
        self.cb_nrpreset = ttk.Combobox(
            inner, state="readonly",
            values=[f"{v}" + ("  -  the author's default" if k == 0 else "")
                    for k, v in optiscaler.NR_PRESETS.items()])
        self.cb_nrpreset.current(0)
        self.lbl_nrstyle = tk.Label(inner, text="style", bg=PANEL, fg=DIM,
                                    font=font(9))
        self.cb_nrstyle = ttk.Combobox(
            inner, state="readonly", width=18,
            values=[f"{v}" for v in optiscaler.NR_STYLES.values()])
        self.cb_nrstyle.current(0)
        self.nrhint = tk.Label(inner, text="the rest is on the overlay",
                               bg=PANEL, fg=DIM, font=font(8))
        # Which OptiScaler + DLSS-NR build goes in (#21).
        self.lbl_optibuild = tk.Label(inner, text="optiscaler build", bg=PANEL,
                                      fg=DIM, font=font(9))
        self.cb_optibuild = ttk.Combobox(inner, state="readonly",
                                         values=list(optiscaler.BUILDS.values()))
        self.cb_optibuild.current(0)
        # FSR 3.1 frame generation from the libraries OptiScaler ships; any
        # RTX card, D3D12 games. NVIDIA's multi-frame (3x/4x) is RTX 50
        # hardware and is not offered as if it were this.
        self.ck_fg = tk.Checkbutton(
            inner, text="frame generation  -  FSR 3.1 through OptiScaler, 2x, "
                        "any RTX card (D3D12; game's own frame gen off)",
            variable=self.fg, bg=PANEL, fg=DIM, selectcolor=FIELD,
            activebackground=PANEL, activeforeground=TXT, font=font(8),
            borderwidth=0)

        # The key that opens the overlay. ReShade uses Home and OptiScaler
        # Insert, and plenty of keyboards - laptops, 60% boards - have
        # neither, which leaves no way at all into the panel where neural
        # rendering is switched on (#88). A preference, not a per-game
        # setting: it is about the keyboard.
        self.lbl_overlaykey = tk.Label(inner, text="overlay key", bg=PANEL,
                                       fg=DIM, font=font(9))
        self.cb_overlaykey = ttk.Combobox(
            inner, state="readonly", width=16,
            values=list(reshade_ini.OVERLAY_KEYS))
        _want = int(prefs.get("overlay_key") or 0)
        _i = next((n for n, k in enumerate(reshade_ini.OVERLAY_KEYS.values())
                   if k == _want), 0)
        self.cb_overlaykey.current(_i)
        self.cb_overlaykey.bind("<<ComboboxSelected>>", self._on_overlaykey)
        self.overlaykeyhint = tk.Label(
            inner, text="written at install and kept for every game; "
                        "reshade opens on home, optiscaler on insert",
            bg=PANEL, fg=DIM, font=font(8))

        # The feeder's pre-releases carry support for the newer add-on builds;
        # any exact release can be pinned when the newest one breaks a game.
        self.lbl_feederver = row(11, "feeder build")
        self.cb_feederver = ttk.Combobox(inner, state="readonly",
                                         values=list(FEEDER_CHOICES))
        self.cb_feederver.current(0)
        self.cb_feederver.bind("<<ComboboxSelected>>", self._on_feederver)
        self.feeder_tags: list[str] = []
        self.feederhint = tk.Label(
            inner, bg=PANEL, fg=DIM, font=font(8), anchor="w", justify="left",
            text="stable = what GitHub marks as the latest release; or pin an "
                 "exact build when the newest one breaks a game. builds "
                 "before 0.8 pair with DLSS 5 add-on 4.55 and have a settings "
                 "tab; 0.10 and later use add-on 4.7 and take preset and work "
                 "area from here")
        _wrap_to_width(self.feederhint)

        # Some D3D11 games quit the moment ReShade hooks them (MGS V). Through
        # DXVK they render on Vulkan and ReShade loads as a layer instead.
        self.ck_dxvk = tk.Checkbutton(
            inner, text="run the game through DXVK (D3D11 -> Vulkan): for "
                        "games that close when ReShade loads inside them (MGS V). "
                        "DirectX 9 always goes through DXVK, ticked or not",
            variable=self.dxvk, bg=PANEL, fg=DIM, selectcolor=FIELD,
            activebackground=PANEL, activeforeground=TXT, font=font(8),
            borderwidth=0, command=lambda: self._set_pathlbl(self.game))
        # The executable's import table decides the API; when it lies (a
        # game that links D3D11 and renders with D3D9, or one whose renderer
        # is a launcher setting) the person can say so here. Remembered per
        # folder.
        self.lbl_api = tk.Label(inner, text="graphics api", bg=PANEL, fg=DIM, font=font(9))
        self.cb_api = ttk.Combobox(inner, state="readonly", width=44, values=["auto"])
        self.cb_api.bind("<<ComboboxSelected>>", self._on_api)
        # RTX 40 only, and only when the folder shows DLSS Frame Generation:
        # dashdogy's unlock raises the multiplier of a feature the game has.
        self.ck_mfg = tk.Checkbutton(
            inner, text="multi-frame generation on this RTX 40 (3x/4x): dashdogy's "
                        "RTX40MFG-Unlock + Ultimate ASI Loader. Needs the game's own "
                        "DLSS Frame Generation ON. Research software - can crash",
            variable=self.mfg, bg=PANEL, fg=DIM, selectcolor=FIELD,
            activebackground=PANEL, activeforeground=TXT, font=font(8),
            borderwidth=0)

        # ReShade routes: the OpenXR layer, so a VR game's headset image gets
        # the pass and not only the desktop mirror (#33). Untested here.
        self.ck_vr = tk.Checkbutton(
            inner, text="VR headset (OpenXR): register ReShade's OpenXR layer as "
                        "well, so the pass runs on the image the headset shows. "
                        "OpenXR games only, not OpenVR/SteamVR. Experimental - "
                        "not tried with a headset; please report",
            variable=self.vr, bg=PANEL, fg=DIM, selectcolor=FIELD,
            activebackground=PANEL, activeforeground=TXT, font=font(8),
            borderwidth=0)

        self.reswarn = tk.Label(
            inner, bg=PANEL, fg=RUST, font=font(8), justify="left", anchor="w",
            wraplength=px(680),
            text="!! set your screen resolution BEFORE turning neural rendering "
                 "on. the feature is created for one backbuffer size; changing "
                 "resolution or display mode while it runs forces a rebuild that "
                 "can freeze or crash the game.")
        # Row 19: a paragraph, not a control, and it belongs under the
        # settings rather than between two of them. It shared row 13 with
        # the DXVK checkbox, which painted over it on every DX11 feeder
        # game - found by the walkthrough's grid-overlap check.
        self.reswarn.grid(row=19, column=0, columnspan=3, sticky="ew", pady=(12, 0))

        inner.bind("<Configure>",
                   lambda e: self.reswarn.configure(wraplength=max(px(360), e.width - px(8))))

        # The video player's link box: paste, play. Packed in _enter_install.
        self.urlrow = tk.Frame(top, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        ui = tk.Frame(self.urlrow, bg=PANEL)
        ui.pack(fill="x", padx=12, pady=8)
        tk.Label(ui, text="link", bg=PANEL, fg=DIM, font=font(9)).pack(side="left")
        self.url = tk.StringVar()
        self.urlbox = tk.Entry(ui, textvariable=self.url, bg=FIELD, fg=TXT,
                               insertbackground=AMBER, relief="flat", font=font(10),
                               highlightthickness=1, highlightbackground=LINE,
                               highlightcolor=EDGE)
        self.urlbox.pack(side="left", fill="x", expand=True, padx=(10, 10), ipady=3)
        self.urlbox.bind("<Return>", lambda e: self._play_url())
        ttk.Button(ui, text="play", style="Accent.TButton",
                   command=self._play_url).pack(side="left")
        ttk.Button(ui, text="download, then play",
                   command=self._download_url).pack(side="left", padx=(8, 0))
        ttk.Button(ui, text="open a video file...",
                   command=self._open_file).pack(side="left", padx=(8, 0))
        ttk.Button(ui, text="downloads folder",
                   command=self._open_downloads).pack(side="left", padx=(8, 0))
        self.fullq = tk.BooleanVar(value=False)
        tk.Checkbutton(ui, text="4K", variable=self.fullq, bg=PANEL, fg=DIM,
                       selectcolor=FIELD, activebackground=PANEL,
                       activeforeground=TXT, font=font(8), borderwidth=0)            .pack(side="left", padx=(10, 0))
        # Second line: neural-render a clip on disk and keep the result.
        pr = tk.Frame(self.urlrow, bg=PANEL)
        pr.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(pr, text="process a file", bg=PANEL, fg=DIM, font=font(9)).pack(side="left")
        ttk.Button(pr, text="pick a video and render it",
                   command=self._process_file).pack(side="left", padx=(10, 10))
        tk.Label(pr, text="size", bg=PANEL, fg=DIM, font=font(9)).pack(side="left")
        self.cb_scale = ttk.Combobox(pr, state="readonly", width=14,
                                     values=list(video.SCALES))
        self.cb_scale.current(0)
        self.cb_scale.pack(side="left", padx=(6, 12))
        tk.Label(pr, text="style", bg=PANEL, fg=DIM, font=font(9)).pack(side="left")
        self.cb_style = ttk.Combobox(pr, state="readonly", width=10,
                                     values=list(video.STYLES.values()))
        self.cb_style.current(0)
        self.cb_style.pack(side="left", padx=(6, 12))
        ttk.Button(pr, text="processed folder",
                   command=self._open_processed).pack(side="left")
        # Third line: a webcam through the feed - the thing people show off.
        wr = tk.Frame(self.urlrow, bg=PANEL)
        wr.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(wr, text="webcam", bg=PANEL, fg=DIM, font=font(9)).pack(side="left")
        self.cb_camera = ttk.Combobox(wr, state="readonly", width=34, values=["(press refresh)"])
        self.cb_camera.current(0)
        self.cb_camera.pack(side="left", padx=(10, 6))
        ttk.Button(wr, text="refresh", command=self._refresh_cameras).pack(side="left")
        ttk.Button(wr, text="start", style="Accent.TButton",
                   command=self._start_webcam).pack(side="left", padx=(10, 6))
        ttk.Button(wr, text="stop", command=self._stop_webcam).pack(side="left")
        tk.Label(wr, text="live camera through DLSS 5 in the player, about half "
                          "a second behind; F6 to compare",
                 bg=PANEL, fg=DIM, font=font(8)).pack(side="left", padx=(12, 0))
        # Fourth line: a screen or a window - the browser, a stream, an
        # emulator, a game nothing should be injected into.
        sr = tk.Frame(self.urlrow, bg=PANEL)
        sr.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(sr, text="screen", bg=PANEL, fg=DIM, font=font(9)).pack(side="left")
        self.cb_screen = ttk.Combobox(sr, state="readonly", width=34, values=["(press refresh)"])
        self.cb_screen.current(0)
        self.cb_screen.pack(side="left", padx=(10, 6))
        ttk.Button(sr, text="refresh", command=self._refresh_screens).pack(side="left")
        ttk.Button(sr, text="start", style="Accent.TButton",
                   command=self._start_screen).pack(side="left", padx=(10, 6))
        ttk.Button(sr, text="stop", command=self._stop_webcam).pack(side="left")
        tk.Label(sr, text="screen: the left part of the desktop, player parked on the right  "
                          "|  window: that window only - the player can go fullscreen over it",
                 bg=PANEL, fg=DIM, font=font(8)).pack(side="left", padx=(12, 0))
        self.urlhint = tk.Label(
            self.urlrow, bg=PANEL, fg=DIM, font=font(8), anchor="w",
            text="a youtube (or any yt-dlp) link: 'play' streams it live in the "
                 "player; a file already on disk opens with 'open a video "
                 "file' (or drop it on the player); 'download' saves it under the player's downloads "
                 "folder; 'process a file' renders a clip through DLSS 5 offline (and DLSS "
                 "upscales it when a bigger size is picked) into the player's processed "
                 "folder - the first run fetches the small video2dlssnr tool. downloads "
                 "folder first (up to 1440p, or 4K when ticked; the first "
                 "download fetches ffmpeg once, 170 MB). a link on the "
                 "clipboard is picked up by itself.")
        self.urlhint.pack(fill="x", padx=12, pady=(0, 8))

        barwrap = tk.Frame(top, bg=BG)
        barwrap.pack(fill="x", pady=(12, 2))
        self.pb = ttk.Progressbar(barwrap, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        self.pblbl = tk.Label(top, text="", bg=BG, fg=DIM, font=font(8), anchor="w")
        self.pblbl.pack(fill="x")

        # Packed to the bottom BEFORE the log, so a growing log can never push
        # these buttons out of the window.
        act = tk.Frame(f, bg=BG)
        act.pack(side="bottom", fill="x", pady=(9, 0))
        self.btn_diag = ttk.Button(act, text="did it work?", command=self._diagnose)
        self.btn_diag.pack(side="left")
        # Enabled once there is a diagnosis to share: what happened here is
        # the only thing the next person with this game cannot look up.
        self.btn_share = ttk.Button(act, text="share the result",
                                    command=self._share_result,
                                    state="disabled")
        self.btn_share.pack(side="left", padx=(10, 0))
        self.btn_tune = ttk.Button(act, text="apply the change",
                                   command=self._apply_tune, state="disabled")
        self.btn_tune.pack(side="left", padx=(10, 0))
        self.btn_remove = ttk.Button(act, text="uninstall", command=self._uninstall)
        self.btn_remove.pack(side="left", padx=10)
        ttk.Button(act, text="check versions", command=self._check_components)\
            .pack(side="left", padx=(0, 10))
        ttk.Button(act, text="open folder",
                   command=lambda: self.game and webbrowser.open(str(self.game.install_dir)))\
            .pack(side="left")
        act2 = tk.Frame(f, bg=BG)
        act2.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(act2, text="what will happen?", command=self._preview)\
            .pack(side="left")
        ttk.Button(act2, text="before / after", command=self._compare)\
            .pack(side="left", padx=10)
        tk.Label(act2, text="profile", bg=BG, fg=DIM, font=font(9))\
            .pack(side="left", padx=(14, 6))
        self.cb_profile = ttk.Combobox(act2, state="readonly", width=22,
                                       values=["(none)"] + profiles.list_profiles())
        self.cb_profile.current(0)
        self.cb_profile.bind("<<ComboboxSelected>>", self._on_profile)
        self.cb_profile.pack(side="left")
        ttk.Button(act2, text="save as...", command=self._save_profile)\
            .pack(side="left", padx=(6, 0))
        ttk.Button(act2, text="delete", command=self._delete_profile)\
            .pack(side="left", padx=(6, 0))
        # Only shown for the video player; packed in _enter_install.
        self.btn_play = ttk.Button(act, text="open the player",
                                   style="Accent.TButton", command=self._open_player)
        self.btn_toggle = ttk.Button(act, text="neural rendering on/off (F6)",
                                     command=self._toggle_nr)

        # Packed before the settings, and from the bottom: whatever room is
        # left after the buttons is the log's, and the settings above it
        # take what remains - and scroll when that is not enough. Packed the
        # other way round, a tall settings card left the log a line and a
        # half at 300% scaling (issue #40).
        logwrap = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        logwrap.pack(side="bottom", fill="both", expand=True, pady=(6, 0))
        # The log takes half the page and had nothing to say it was a log.
        # A title bar the width of the panel is the cheapest way to make a
        # box read as a thing rather than as leftover space.
        loghead = tk.Frame(logwrap, bg=PANEL)
        loghead.pack(side="top", fill="x", padx=12, pady=(px(5), 0))
        tk.Label(loghead, text="what the tool is doing", bg=PANEL, fg=DIM,
                 font=font(8)).pack(side="left")
        self.logclear = tk.Label(loghead, text="[ clear ]", bg=PANEL, fg=FAINT,
                                 font=font(8), cursor="hand2")
        self.logclear.pack(side="right")
        self.logclear.bind("<Button-1>", lambda _e: self._clear_log())
        self.log = tk.Text(logwrap, bg=PANEL, fg=BODY, insertbackground=BODY,
                           font=font(9), borderwidth=0, height=lines(14, 6),
                           wrap="word", state="disabled", spacing1=1)
        lsb = ttk.Scrollbar(logwrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=12,
                      pady=(px(3), px(8)))
        lsb.pack(side="right", fill="y")
        self.log.tag_configure("ok", foreground=GREEN)
        self.log.tag_configure("err", foreground=RED)
        self.log.tag_configure("warn", foreground=RUST)
        self.log.tag_configure("head", foreground=AMBER)
        self.installscroll.pack(side="top", fill="x")

        # Pack hands a widget everything it asks for before the next one
        # gets anything, so at 300% the settings took the window and left
        # the log a line and a half - and reversing the order only moved the
        # problem to the settings (22 pixels of a 1221-pixel form). Split
        # what the window actually has: the settings get what they need, up
        # to two thirds of it, and the log keeps the rest (issue #40).
        logwrap.pack_propagate(False)

        def _split(_e=None) -> None:
            h = f.winfo_height()
            if h < 50:
                return
            spare = h - act.winfo_reqheight() - act2.winfo_reqheight() - px(16)
            rows = max(1, int(self.log.cget("height")))
            row = max(1, (self.log.winfo_reqheight() + px(20)) // rows)
            # The settings get what they need, but never more than the
            # window minus a readable log - at 300% the settings alone are
            # twice the height of the whole page. Whatever they do not need
            # is the log's, which is why a roomy window still opens with a
            # tall log and no scrollbar anywhere.
            # ...and never fewer than three lines of log: the settings
            # can scroll for what they lose, the log cannot be read at
            # one and a half lines. The panel's own title bar is not log:
            # counted here, or the three lines quietly become two (the
            # scaling test caught exactly that at 200%).
            chrome = loghead.winfo_reqheight() + px(11)
            keep = max(row * 3 + chrome, min(row * 8 + chrome,
                                             int(spare * 0.38)))
            settings = max(0, min(self.installscroll.content_height(),
                                  spare - keep))
            self.installscroll.set_height(settings)
            logwrap.configure(height=max(row, spare - settings))

        f.bind("<Configure>", _split)
        self._split_install = _split
        return f

    # --------------------------------------------------------- components
    def _check_components(self) -> None:
        """Compare what is installed here against what the sources offer now."""
        if self.busy or not self.game:
            return
        g = self.game
        if not g.installed:
            self._log("> nothing is installed in this folder yet", "warn")
            return
        self.busy = True
        self._log("")
        self._log("=== component versions ===", "head")
        self._log("  asking each source for its current version...")

        def work():
            try:
                self.q.put(("components", components.check(g.install_dir)))
            except Exception:
                log.exception("checking component versions")
                self.q.put(("components", []))
        threading.Thread(target=work, daemon=True).start()

    def _show_components(self, items) -> None:
        self.busy = False
        if not items:
            self._log("  could not read any recorded versions - reinstall to "
                      "record them", "warn")
            return
        for it in items:
            if it.outdated:
                self._log(f"[!!]   {it.name}: {it.installed} -> {it.latest}", "warn")
            elif it.installed != it.latest:
                self._log(f"[--]   {it.name}: {it.installed} "
                          f"(current is {it.latest}, a different build)")
            else:
                self._log(f"[ok]   {it.name}: {it.installed}", "ok")
        stale = [i for i in items if i.outdated]
        self._log("")
        self._log(f"> {components.summary(items)}",
                  "warn" if stale else "ok")
        if stale:
            self._log("  press install again to update - your settings and "
                      "backups are kept")

    # ------------------------------------------------------------ diagnose
    def _diagnose(self) -> None:
        """Read the game's own logs back and say what happened."""
        if not self.game:
            return
        rep = diagnose.analyse(self.game.install_dir)
        self._last_diag = rep
        try:
            # Enabled for any diagnosis, not only for a session that logged:
            # "it never started and wrote nothing" is the largest group of
            # reports there is, and it was the one that could not be shared.
            self.btn_share.configure(state="normal")
        except Exception:
            pass
        self._log("")
        self._log(f"=== diagnosis{f' :: log {rep.log_time}' if rep.log_time else ''} "
                  f"===", "head")
        self._log(f"> {rep.verdict}",
                  "ok" if rep.verdict.startswith("Working") else
                  ("warn" if rep.ran else "err"))
        for f_ in rep.findings:
            mark = {"ok": "[ok]  ", "warn": "[!!]  ",
                    "bad": "[fail]", "info": "[--]  "}[f_.level]
            tag = {"ok": "ok", "warn": "warn", "bad": "err", "info": ""}[f_.level]
            self._log(f"{mark} {f_.title}", tag)
            if f_.detail:
                self._log(f"        {f_.detail}")
        if not rep.verdict.startswith("Working"):
            self._log("")
            self._log("> stuck? press [ report a bug ] on the left - the diagnosis "
                      "above and the log tail go into the report, you post it.",
                      "head")
        # After the verdict, not before it: what it cost is the second
        # question, and only worth reading once the first one is answered.
        self._autotune(rep)
        self._windows_crash(rep)

    def _forget_last_session(self) -> None:
        """Whatever the last diagnosis found belonged to the game it read.

        Diagnose game A, pick game B, press "share the result" and the
        record posted for B carried A's verdict - into a published list.
        The same crash event went into B's bug report.
        """
        self._last_diag = None
        self._last_crash = None
        self._tune = None
        self._noted_routes = set()
        for name, text in (("btn_share", "share the result"),
                           ("btn_tune", "apply the change")):
            w = getattr(self, name, None)
            if w is not None:
                try:
                    w.configure(state="disabled", text=text)
                except tk.TclError:
                    pass

    def _crash_is_this_session(self, crash) -> bool:
        """Does this recorded fault belong to the session just diagnosed?"""
        g = self.game
        return g is None or _crash_is_this_session_impl(crash, g.install_dir)

    def _crash_overrides(self, crash) -> None:
        """A recorded fault outranks a log that stopped in a good place.

        The logs are written while the game runs, so the last line of a
        healthy-looking session is not evidence that the session ended
        well. Windows' own record is, and when the two disagree the record
        wins - saying "Working." to somebody who watched the game close is
        the fastest way to lose their trust (#98).
        """
        d = getattr(self, "_last_diag", None)
        if d is None or not str(getattr(d, "verdict", "")).startswith("Working"):
            return
        if not self._crash_is_this_session(crash):
            return
        mod = str(getattr(crash, "module", "") or "")
        d.verdict = ("It ran, and then the game crashed - Windows recorded "
                     "the fault" + (f" in {mod}." if mod else "."))
        self._log("")
        self._log("> the neural pass did run, so the install is right - but "
                  "Windows recorded this game faulting afterwards, and a "
                  "session that ends in a crash is not a working one.",
                  "warn")

    def _windows_crash(self, rep) -> None:
        """What Windows itself recorded when the game closed.

        The biggest group of reports is a game that closes itself, and one
        that dies before ReShade loads writes nothing at all - but Windows
        writes an Application Error event naming the faulting module. Read
        off the Tk thread: it starts a PowerShell process.
        """
        g = self.game
        if g is None:
            return
        # Asked on every verdict, "Working." included. The logs can only
        # say what happened up to the last line they wrote: in #98 the model
        # dispatched, the game then died after the logos, and the tool
        # announced "Working." at somebody who had just watched it crash.
        # Windows recorded the fault either way - it only had to be asked.
        self._last_crash = None
        exe = getattr(getattr(g, "exe", None), "name", "") or ""
        if not exe:
            return
        where = g.install_dir
        proxy = ""
        try:
            since = diagnose._installed_at(where) or 0.0
            _man = diagnose._manifest(where) or {}
            proxy = str(_man.get("proxy") or "")
            # Every file this install wrote, so a fault in one of them is
            # recognised as ours whatever it is called.
            written = tuple(str(f) for f in (_man.get("files") or [])
                            if isinstance(f, str))
        except Exception:
            since = 0.0
            written = ()

        def work() -> None:
            try:
                c = wincrash.last_crash(exe, since=since)
                said = wincrash.describe(c, proxy, written)
            except Exception:
                return
            if said:
                self.q.put(("wincrash", (where, c, said)))
        threading.Thread(target=work, daemon=True).start()

    def _wheel_anywhere(self, e):
        """Send the wheel to the scrolling area the pointer is inside."""
        try:
            w = self.root.winfo_containing(e.x_root, e.y_root)
        except Exception:
            w = None
        if w is None:
            # winfo_containing answers from the real stacking on screen, and
            # returns nothing when another program's window covers that
            # point. The event itself still says which widget it reached.
            w = getattr(e, "widget", None)
        while w is not None:
            # These scroll themselves, and taking the wheel from them would
            # freeze the log and the game list.
            if isinstance(w, (tk.Text, tk.Listbox, ttk.Treeview,
                              ttk.Scrollbar, tk.Scrollbar)) \
                    and not isinstance(w, Prose):
                return None
            if isinstance(w, Scroller):
                # A dropdown does NOT keep the wheel: ttk.Combobox changes
                # its selection on a wheel turn, so scrolling a page whose
                # pointer passed over "route" or "add-on build" silently
                # picked a different one. A scroll must never be a choice.
                w.wheel(e)
                return "break"
            w = getattr(w, "master", None)
        return None

    def _target_typed(self) -> None:
        """A keystroke in the box. Save a moment after the typing stops.

        Writing on every character rewrote prefs.json per keystroke on the
        Tk thread, and widened the window in which a non-atomic write can
        be interrupted. The two bindings still save at once; this only
        catches a value typed straight before a button press.
        """
        try:
            if self._target_after is not None:
                self.root.after_cancel(self._target_after)
        except Exception:
            pass
        self._target_after = self.root.after(600, self._on_target)

    def _on_target(self) -> None:
        """Remember the target, and only a sane one."""
        self._target_after = None
        raw = (self.target_fps.get() or "").strip()
        try:
            value = int(float(raw)) if raw else 0
        except ValueError:
            value = 0
        if value and not 20 <= value <= 480:
            value = 0
        prefs.set_("target_fps", value or "")

    def _target(self) -> int:
        try:
            return int(float((self.target_fps.get() or "0").strip() or 0))
        except ValueError:
            return 0

    def _autotune(self, rep) -> None:
        """After a session: what it cost, and the resolution to run next.

        Nothing is written here. The suggestion is printed with the numbers
        it came from, and applying it is a separate press - a tool that
        changes settings behind your back is not one people keep.
        """
        self._tune = None
        try:
            self.btn_tune.configure(state="disabled", text="apply the change")
        except Exception:
            pass
        g, target = self.game, self._target()
        if g is None or not target or not rep.ran:
            return
        # The slider is disabled where the work area is ignored (DX12, OpenGL,
        # the 32-bit helper, every route but feeder and optiscaler). Suggesting
        # a number there would be advice the add-on never reads.
        if not self._work_applies():
            return
        d = g.install_dir
        route = getattr(self, "route", "") or ""
        try:
            feed_txt = diagnose._last_run(diagnose._tail(d / diagnose.FEED_LOG,
                                                         100_000))
            opti_p = diagnose._opti_log(d)
            opti_txt = (diagnose._last_run(diagnose._tail(opti_p, 100_000))
                        if opti_p else "")
            # The resolution the SESSION ran at, read from the file the
            # add-on read - not from the slider. The slider is live UI: a
            # route change rewrites it (_sync_workres), so a session played
            # at 100% could be stored against 75% and poison the two-point
            # solve for good, because the history keeps one sample per
            # resolution.
            ran_at = autotune.ran_at(d, route, self.workres.get())
            m = autotune.measure(feed_txt, opti_txt, route, ran_at)
        except Exception:
            return
        if m is None:
            return
        # Recorded BEFORE the suggestion is worked out, or the newest
        # session is never one of the two points: session two then reported
        # "first measurement for this game" and only session three solved.
        autotune.remember(d, m)
        # `current` is what the SESSION ran at - the slider may have been
        # rewritten by a route change since.
        sug = autotune.suggest(autotune.history(d), target,
                               m.resolution, route, m)
        if sug is None:
            return
        self._log("")
        self._log(f"=== aiming for {target} fps ===", "head")
        for ln in sug.lines:
            self._log(f"> {ln}")
        if sug.resolution != m.resolution:
            self._tune = sug
            try:
                self.btn_tune.configure(
                    state="normal", text=f"set the work area to {sug.resolution}%")
            except Exception:
                pass

    def _apply_tune(self) -> None:
        """Write the suggested work area into the config that is already there.

        Both add-ons read their configuration once, when the game starts, so
        this takes effect the next time it is launched - not in a running
        game.
        """
        sug, g = self._tune, self.game
        if sug is None or g is None:
            return
        d, route = g.install_dir, getattr(self, "route", "") or ""
        self.workres.set(sug.resolution)
        self._on_workres()
        try:
            if route == dlss.OPTI:
                optiscaler.enable_nr(d, log=lambda t: self._log(t),
                                     settings={"WorkingScale":
                                               round(sug.resolution / 100.0, 3)})
            else:
                feedcfg.write(d, {"work_resolution": sug.resolution})
                self._log(f"      dlss5-feed.cfg: work_resolution="
                          f"{sug.resolution}%")
        except Exception as e:
            self._log(f"[fail] could not write the setting ({e}) - press "
                      f"INSTALL instead, it writes the same value.", "err")
            return
        self._log(f"> set to {sug.resolution}%. It is read when the game "
                  f"starts, so it applies to the next run - play again and "
                  f"press 'did it work?' to see where it landed.", "ok")
        self._tune = None
        try:
            self.btn_tune.configure(state="disabled", text="apply the change")
        except Exception:
            pass

    def _share_result(self) -> None:
        """Offer the last diagnosis to the compatibility list.

        Nothing is sent from here: this opens a pre-filled issue in the
        person's own browser, with the record visible in it, and they decide
        whether to post it. The outcome comes from the diagnosis rather than
        from a question, because the diagnosis has read the logs and the
        person has not.
        """
        rep, g = self._last_diag, self.game
        if rep is None or g is None:
            return
        # The Windows event read runs on a worker thread and can land
        # after the button is pressed. A record posted in that gap would
        # say "worked" about a session that crashed - and it goes into a
        # published list, where it becomes somebody else's advice.
        worked = (str(getattr(rep, "verdict", "")).startswith("Working")
                  and getattr(self, "_last_crash", None) is None)
        try:
            name, sm = gpu.detect()
        except Exception:
            name, sm = "unknown", None
        man = {}
        try:
            man = diagnose._manifest(g.install_dir) or {}
        except Exception:
            pass
        rec = community.record(
            g, getattr(self, "route", "") or str(man.get("path") or ""),
            "worked" if worked else "failed",
            api=str(man.get("api") or getattr(g, "api", "") or ""),
            build=str(man.get("opti_build") or ""),
            gpu_sm=sm, gpu_name=name or "", driver=gpu.driver_version() or "",
            version=update.VERSION)
        self._log("")
        self._log("> a browser window opens with the result in it - nothing "
                  "is sent unless you post it. it carries the game's name "
                  "and executable, the route and build, the graphics api, "
                  "your card and driver, this tool's version, whether it "
                  "worked, and the one-line verdict. no paths, no user name, "
                  "nothing else.", "head")
        try:
            webbrowser.open(community.issue_url(rec, str(getattr(rep, "verdict", ""))))
        except Exception as e:
            self._log(f"[fail] could not open the browser ({e})", "err")

    def _community_note(self) -> None:
        """What other people found in this game, before the install runs.

        Off the Tk thread: it is a download, and a download on the Tk thread
        is how the window froze in #8, #18 and #32. A cached copy makes it
        instant after the first time, and no answer at all simply means no
        note - nothing about this blocks an install.
        """
        g = self.game
        if g is None:
            return
        route, drv_g = getattr(self, "route", "") or "", g

        def work() -> None:
            try:
                entry = community.for_game(community.fetch(), drv_g)
                lines = community.advice(entry, route,
                                         gpu.driver_version() or "")
            except Exception:
                return
            if lines:
                self.q.put(("community", (drv_g.install_dir, lines)))
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- bits
    def _sm(self) -> int | None:
        if self.sm is None:
            try:
                _, self.sm = gpu.detect()
            except Exception:
                self.sm = None
        return self.sm

    def _work_applies(self) -> bool:
        """Is there a resolution dial on this route, for this game?

        OptiScaler: always - its model resolution (25-100%) is the fps lever.
        Feeder: work_resolution is honoured on the 64-bit D3D11 path only.
        The add-on's own log line is "settled D3D11 work resolution=..%"; on
        DX12, OpenGL and the 32-bit helper the value is simply ignored, so the
        slider is disabled rather than lying about what it does.
        """
        g = self.game
        route = getattr(self, "route", dlss.FEEDER)
        if route == dlss.OPTI:
            return True
        if route != dlss.FEEDER:
            return False          # the other routes have no work area at all
        return bool(g and g.bitness == 64 and g.api == "DX11")

    def _on_workres(self, _v=None) -> None:
        if not self._work_applies():
            return
        v = self.workres.get()
        if getattr(self, "route", None) == dlss.OPTI:
            cost = int(round(v * v / 100))
            if v == 100:
                self.workhint.config(text="100% - full size; the pass costs about "
                                          "half your fps", fg=RUST)
            elif v >= 75:
                self.workhint.config(text=f"{v}% - about {cost}% of the full-size "
                                          f"cost, hard to tell apart", fg=DIM)
            elif v >= 50:
                self.workhint.config(text=f"{v}% - about {cost}% of the cost; "
                                          f"fine detail softens a little", fg=DIM)
            else:
                self.workhint.config(text=f"{v}% - about {cost}% of the cost; "
                                          f"broad shading survives, fine "
                                          f"structure does not", fg=RUST)
            return
        if v == 100:
            self.workhint.config(text="100% - full quality", fg=DIM)
        elif v >= 80:
            self.workhint.config(text=f"{v}% - a little faster", fg=DIM)
        else:
            self.workhint.config(text=f"{v}% - faster, softer", fg=RUST)

    def _sync_workres(self) -> None:
        route = getattr(self, "route", dlss.FEEDER)
        if self._work_applies():
            if route == dlss.OPTI:
                self.sc_work.configure(from_=optiscaler.NR_SCALE_MIN,
                                       to=optiscaler.NR_SCALE_MAX)
                if self.workres.get() == 100 or self.workres.get() < 50:
                    self.workres.set(optiscaler.NR_SCALE_DEFAULT)
            else:
                self.sc_work.configure(from_=50, to=100)
                if self.workres.get() < 50:
                    self.workres.set(100)
            self.sc_work.configure(state="normal", fg=TXT, bg=AMBER,
                                   troughcolor=SLIDER_TROUGH)
            self.sp_target.configure(state="normal", fg=TXT, bg=FIELD)
            self._on_workres()
        else:
            self.workres.set(100)
            # Disabled, but still legible: the reason is in the hint next to it.
            self.sc_work.configure(state="disabled", fg=DIM, bg=FAINT,
                                   troughcolor=LINE)
            # The target frame rate sets this same dial, so it goes with it.
            # A disabled Spinbox paints with its disabled pair, not fg/bg.
            self.sp_target.configure(state="disabled",
                                     disabledbackground=PANEL,
                                     disabledforeground=FAINT)
            if route != dlss.FEEDER:
                self.workhint.config(
                    text="n/a on this route - the game's own dlss quality mode "
                         "is the performance setting here",
                    fg=FAINT)
            else:
                api = self.game.api if self.game else "this api"
                self.workhint.config(
                    text=f"n/a on {api} - the add-on applies the work area on "
                         f"the 64-bit d3d11 path only", fg=FAINT)

    def _on_exe(self, _e=None) -> None:
        g = self.game
        if not g or not g.candidates:
            return
        i = self.cb_exe.current()
        if i < 0 or i >= len(g.candidates):
            return
        # enrich(chosen=True) can move the api, the bitness and the
        # install folder, so the last verdict, the last crash and the tuning
        # suggestion all belonged to a game this no longer is.
        self._forget_last_session()
        g.exe = g.candidates[i]
        g.emu = None
        games.enrich(g, chosen=True)
        self._log(f"> target exe -> {g.exe.name}  ({g.bit_label} {g.api}); "
                  f"installing into {g.install_dir}", "head")
        ok, why = installer.check_supported(g)
        if not ok:
            self._log(f"  not supported: {why}", "err")
        # Not a partial repaint: enrich(chosen=True) can change the api, the
        # bitness and the install folder, and the route list, the blurb, the
        # reliability line, the plan, the checkboxes and the
        # ray-reconstruction row were all worked out from the old three.
        # Rebuilding the page is the only way they agree with each other.
        self._enter_install()
        self.btn_next.config(state="normal" if ok else "disabled")

    def _on_route(self, _e=None) -> None:
        """The user picked a different route; re-tune what is shown."""
        if not self.support:
            return
        i = self.cb_route.current()
        if 0 <= i < len(self.support.options):
            self._apply_route(self.support.options[i])

    def _apply_route(self, path: str) -> None:
        """Show only the settings this route actually uses."""
        self.route = path
        usable, note = self.route_fit.get(path, (True, ""))
        parts: list[tuple[str, str]] = []
        if self.game and self.game.exe_warning:
            parts.append(("warn", self.game.exe_warning))
        if not usable:
            parts.append(("bad", f"NOT FOR THIS PC - {note}."))
        parts.append(("blurb", dlss.BLURB[path]))
        if usable and note:
            parts.append(("note", note))
        # What this route will not tolerate, in plain words, before INSTALL.
        for line in getattr(dlss, "CONFLICTS", {}).get(path, ()):
            parts.append(("warn", line))
        for line in dlss.quirks(self.game.exe if self.game else None,
                                self.game.api if self.game else ""):
            parts.append(("warn", line))
        # The driver, before the install rather than in the diagnosis after
        # it: 616.64 is behind 31 of the first 87 reports. Its own colour,
        # because it is the warning most likely to decide the evening.
        try:
            warn = dlss.driver_warning(path, gpu.driver_version(),
                                       offered=getattr(self.support, "options",
                                                       None))
        except Exception:
            warn = None
        if warn:
            parts.append(("driver", warn))
        self.routelbl.show(parts)
        feeder = path == dlss.FEEDER
        opti = path == dlss.OPTI
        # The add-on dropdown lists the family this route installs.
        self._fill_addon_list(sf=path == dlss.RENODX)
        # Rows 9/10: the feeder's preset + hdr, or OptiScaler's preset + style.
        for w in (self.lbl_preset, self.cb_preset, self.lbl_hdr, self.cb_hdr,
                  self.dlaalbl, self.lbl_nrpreset, self.cb_nrpreset,
                  self.lbl_nrstyle, self.cb_nrstyle, self.nrhint, self.ck_fg,
                  self.lbl_optibuild, self.cb_optibuild,
                  self.lbl_feederver, self.cb_feederver, self.feederhint):
            w.grid_remove()
        if opti:
            self.lbl_optibuild.grid(row=12, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_optibuild.grid(row=12, column=1, columnspan=2, sticky="ew", pady=5)
            self.lbl_nrpreset.grid(row=9, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_nrpreset.grid(row=9, column=1, columnspan=2, sticky="ew", pady=5)
            self.lbl_nrstyle.grid(row=10, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_nrstyle.grid(row=10, column=1, sticky="w", pady=5)
            self.nrhint.grid(row=10, column=2, sticky="w", padx=(10, 0))
            if self.game and self.game.api == "DX12":
                self.ck_fg.grid(row=11, column=0, columnspan=3, sticky="w", pady=(0, 5))
            else:
                self.fg.set(False)
        else:
            self.lbl_preset.grid(row=9, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_preset.grid(row=9, column=1, columnspan=2, sticky="ew", pady=5)
            self.lbl_hdr.grid(row=10, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_hdr.grid(row=10, column=1, sticky="w", pady=5)
            self.dlaalbl.grid(row=10, column=2, sticky="w", padx=(10, 0))
        if feeder:
            self.lbl_feederver.grid(row=11, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_feederver.grid(row=11, column=1, columnspan=2, sticky="ew", pady=5)
            self.feederhint.grid(row=12, column=0, columnspan=3, sticky="ew")

        # OptiScaler is loaded by the game under one of several names; the
        # feeder's motion-vector provider sits in the same place on screen.
        # Row 3 carries whichever of the three this route actually needs:
        # OptiScaler's proxy name, or the feeder's motion-vector provider,
        # or - on the routes that use ReShade but not the feeder - the name
        # ReShade itself is loaded under.
        for w in (self.lbl_mv, self.cb_prov, self.lbl_proxy, self.cb_proxy,
                  self.lbl_rproxy, self.cb_rproxy):
            w.grid_remove()
        if opti:
            pair = (self.lbl_proxy, self.cb_proxy)
        elif feeder:
            pair = (self.lbl_mv, self.cb_prov)
        else:
            pair = (self.lbl_rproxy, self.cb_rproxy)
        pair[0].grid(row=3, column=0, sticky="w", padx=(0, 14), pady=5)
        pair[1].grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)
        # Motion vectors, work area and DLSS preset belong to the feeder's
        # synthetic contract; the other routes hook the game's real DLSS calls
        # and ignore all three.
        self.cb_prov.configure(state="readonly" if feeder else "disabled")
        # neural-upstream does the neural rendering itself: no renodx add-on
        # goes in, so its version dropdown would only mislead.
        self.cb_renodx.configure(
            state="disabled" if path == getattr(dlss, "UPSTREAM", "upstream")
            else "readonly")
        self.cb_preset.configure(state="readonly" if feeder else "disabled")
        self.dlaalbl.config(
            text="the feeder path is always dlaa" if feeder
            else "the game's own dlss quality mode applies")
        self.reswarn.grid() if feeder else self.reswarn.grid_remove()
        if self.game and not opti and path != dlss.RENODX \
                and self.game.api in dxvk.APIS:
            self.ck_dxvk.grid(row=13, column=0, columnspan=3, sticky="w",
                              pady=(6, 0))
        else:
            self.ck_dxvk.grid_remove()
            self.dxvk.set(False)
        if self.game and not opti and path != dlss.REMIX and \
                _mfg.applies(self._sm(), self.game.api, self.game.install_dir,
                             self.game.folder)[0]:
            self.ck_mfg.grid(row=14, column=0, columnspan=3, sticky="w",
                             pady=(6, 0))
        else:
            self.ck_mfg.grid_remove()
            self.mfg.set(False)
        # Ray reconstruction: only for a game that ships one, and only on
        # the routes whose install can actually act on it - the OptiScaler
        # and Remix branches return before the swap step, so offering the
        # choice there would take a decision and then ignore it.
        has_rr = (self._has_rr() and path not in (dlss.OPTI, dlss.REMIX))
        if has_rr:
            self.rr_row.grid(row=16, column=0, columnspan=3, sticky="w",
                             pady=(6, 0))
            # _fill_catalog disables the dropdown when it could list no
            # build, and says so next to it. A route change must not paint
            # over that with a promise, beside a control nobody can use.
            if str(self.cb_dlssd["state"]) == "disabled":
                self.dlssdhint.configure(
                    text="no build could be listed - the game keeps its own")
            else:
                self.dlssdhint.configure(
                    text="this game ships one; if you swap it, the original "
                         "is backed up and comes back on uninstall")
        else:
            self.rr_row.grid_remove()
            try:
                self.cb_dlssd.current(0)      # back to "keep the game's own"
            except tk.TclError:
                pass

        # The overlay key: every route that has an overlay. OptiScaler is
        # the one the request came from (#88 - its default is Insert), so
        # hiding it there was the wrong way round; Remix has no overlay of
        # ours at all.
        if self.game and path != dlss.REMIX:
            self.lbl_overlaykey.grid(row=17, column=0, sticky="w",
                                     padx=(0, 14), pady=(6, 0))
            self.cb_overlaykey.grid(row=17, column=1, sticky="w", pady=(6, 0))
            self.overlaykeyhint.grid(row=17, column=2, sticky="w", padx=(10, 0))
        else:
            for w in (self.lbl_overlaykey, self.cb_overlaykey,
                      self.overlaykeyhint):
                w.grid_remove()
        if self.game and not opti and path != dlss.REMIX and (self.game.bitness or 64) == 64:
            self.ck_vr.grid(row=18, column=0, columnspan=3, sticky="w",
                            pady=(6, 0))
        else:
            self.ck_vr.grid_remove()
            self.vr.set(False)
        self._sync_workres()
        if self.game:
            level, why = installer.reliability(self.game, path)
            self._log(f"> route: {dlss.LABELS[path]}  [{level}]", "head")
            self._log(f"  {why}")
            if not usable:
                self._log(f"  !! not for this pc: {note}", "warn")
            self._log(f"  plan: {' -> '.join(installer.plan(self.game, self._opts()))}")
            # Guarded on the route having really changed: _apply_route runs
            # on every page rebuild, and a note repainted each time is
            # noise, not information.
            if path not in self._noted_routes:
                self._noted_routes.add(path)
                self._community_note()
                self._hdr_note()

    def _route_label(self, o: str) -> str:
        """One dropdown line: what it is, whether it fits, if it is the pick."""
        usable, note = self.route_fit.get(o, (True, ""))
        rec = self.support and o == self.support.recommended
        tail = "  <-  recommended for this game and card" if rec else ""
        if not usable:
            tail = f"  (not for this pc: {note})"
        return dlss.LABELS[o] + tail

    def _fill_addon_list(self, sf: bool) -> None:
        """The DLSS 5 add-on dropdown for the route: SF or renodx-dlss5.

        Opens on "auto": the installer then applies its pins (4.55 on driver
        616.64+, 4.60 on OpenGL, the feeder's accepted build). A label picked
        from the list is an explicit choice and skips them.
        """
        fam = "renodx_sf" if sf else "renodx"
        vals = [e["label"] for e in self.catalog.get(fam, [])]
        if vals:
            vals = [ADDON_AUTO] + vals
        found, _ = prefs.find_renodx(sf=sf)
        self.renodx_local = found
        if found:
            tag = f"[local] {found.name}"
            self.cb_renodx["values"] = [tag] + vals
            self.cb_renodx.set(tag)
        elif vals:
            self.cb_renodx["values"] = vals
            self.cb_renodx.current(0)
        else:
            self.cb_renodx["values"] = ["loading..."]
            self.cb_renodx.set("loading...")

    def _open_log(self) -> None:
        """Show the log file in Explorer, creating it if this run was clean."""
        p = log.path()
        try:
            if not p.is_file():
                log.write("log opened from the interface")
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(p)])
        except Exception:
            messagebox.showinfo(APP, f"The log file is at:\n\n{p}")

    def _report_bug(self, kind: str = "bug") -> None:
        """Open a pre-filled issue with the machine details already in it.

        "It crashes a lot" is unactionable. Filling in the version, the card,
        the route, the last error and the tail of the log means a report
        arrives with the parts that matter, without asking anyone to hunt for
        them. Nothing is sent by itself: the person sees the text in the
        browser and decides whether to post it - and can edit it first.
        """
        try:
            name, sm = gpu.detect()
        except Exception:
            name, sm = "unknown", None
        drv = gpu.driver_version() or "?"
        g = self.game
        title = {"crash": "crash: ", "notwork": "not working: "}.get(kind, "bug: ")
        if g:
            title += g.name
        # The body is assembled in diagnose, where the log formats live: it
        # reads the folder and the add-on logs so the report carries the
        # evidence, not just the verdict.
        try:
            install_dir = g.install_dir if g else None
        except Exception:
            install_dir = None
        # The two things the tool cannot know. Closing the dialog cancels the
        # report rather than opening a blank one - nobody edits it afterwards.
        asked = True
        try:
            from . import reportui
            answers = reportui.ask(self.root, getattr(g, "name", "") or "")
        except Exception:
            # If the dialog itself cannot open, the report still must: a
            # blank template is worse than this one, but far better than no
            # way to report anything at all.
            answers, asked = None, False
        if asked and answers is None:
            return                  # cancelled on purpose
        # The route on the install page is the one being looked at; from
        # anywhere else it was whatever that page last showed, or "-" (#134).
        # The install record in the folder says what is actually there.
        route = getattr(self, "route", "-") if self.step == 3 else "-"
        if route == "-" and install_dir:
            try:
                route = diagnose._manifest(Path(install_dir)).get("path") or "-"
            except Exception:
                pass
        body = diagnose.issue_body(
            update.VERSION, name, sm, drv, g, route,
            self._last_diag, log.tail(60, 6000), log.path(), install_dir,
            last_error=log.last_error(), answers=answers,
            crash=getattr(self, "_last_crash", None))
        try:
            from urllib.parse import quote
            url = (f"https://github.com/{update.REPO}/issues/new"
                   f"?title={quote(title)}&body={quote(body)}")
            if len(url) > 7800:
                # Browsers and GitHub cap the URL; hand over the long form
                # through the clipboard instead of silently truncating it.
                self.root.clipboard_clear()
                self.root.clipboard_append(body)
                url = (f"https://github.com/{update.REPO}/issues/new"
                       f"?title={quote(title)}&body="
                       + quote("(the details are on your clipboard - paste them here)"))
            webbrowser.open(url)
        except Exception:
            self.root.clipboard_clear()
            self.root.clipboard_append(body)
            messagebox.showinfo(APP, "Details copied to the clipboard - paste "
                                     "them into a new issue on GitHub.")

    def _suggest(self) -> None:
        """A feature request lands in the same place as bugs, labelled apart."""
        try:
            name, sm = gpu.detect()
        except Exception:
            name, sm = "unknown", None
        body = ("**What would you like it to do**\n\n\n"
                "**Why / which game**\n\n\n---\n"
                f"- version: {update.VERSION}\n- gpu: {name} (sm_{sm})\n")
        try:
            from urllib.parse import quote
            webbrowser.open(f"https://github.com/{update.REPO}/issues/new"
                            f"?labels=enhancement&title={quote('idea: ')}&body={quote(body)}")
        except Exception:
            webbrowser.open(f"https://github.com/{update.REPO}/issues/new")

    def _offer_crash_report(self) -> None:
        """Something went wrong this run: say so once, with a one-click report."""
        if self._crash_shown:
            return
        self._crash_shown = True
        self.update_url = None
        self.bannerlbl.config(text="> something went wrong - the details are in "
                                   "the log file")
        self.updbtn.config(text="[ report it ]")
        self.updbtn.bind("<Button-1>", lambda e: self._report_bug("crash"))
        self.banner.pack(fill="x", before=self.body)

    def _on_proxy(self, _e=None) -> None:
        """Warn when the chosen name is already something else's file."""
        i = self.cb_proxy.current()
        if i <= 0 or not self.game:
            return
        name = optiscaler.PROXY_NAMES[i - 1]
        p = self.game.install_dir / name
        if p.is_file() and not optiscaler.is_optiscaler(p):
            self._log(f"  note: this game already has its own {name}. It will "
                      f"be backed up and restored on uninstall, but another "
                      f"name is safer.")

    def _on_prov(self, _e=None) -> None:
        self.provider.set(list(reshade_ini.PROVIDERS.keys())[self.cb_prov.current()])

    def _pick_renodx(self) -> None:
        p = filedialog.askopenfilename(
            title="select the renodx add-on you downloaded",
            filetypes=[("reshade add-on", "*.addon64 *.addon"), ("all files", "*.*")])
        if not p:
            return
        self.renodx_local = Path(p)
        prefs.remember_renodx(self.renodx_local)
        tag = f"[local] {self.renodx_local.name}"
        vals = [v for v in self.cb_renodx["values"]
                if not v.startswith(("loading", "[local]"))]
        self.cb_renodx["values"] = [tag] + vals
        self.cb_renodx.set(tag)

    def _set_pathlbl(self, g: games.Game) -> None:
        if g is None:
            return
        extra = "  +  host64/ helper" if g.bitness == 32 else ""
        if (self.dxvk.get() or g.api == "DX9") and g.api in dxvk.APIS:
            extra += "  +  dxvk (vulkan)"
        # Long install paths ran off the right edge; show the path relative to
        # the game folder instead, the full one is in the log.
        try:
            short = str(g.exe.relative_to(g.folder))
        except ValueError:
            short = g.exe.name
        api = "Vulkan" if (self.dxvk.get() and g.api in dxvk.APIS) else g.api
        self.pathlbl.config(
            text=f"{short}   ::   {g.bit_label} {g.api}  ->  "
                 f"reshade = {installer._proxy_name(api, self._opts().reshade_proxy)}{extra}")

    def _on_api(self, _e=None) -> None:
        g = self.game
        if not g or not hasattr(self, "cb_api"):
            return
        cb = _e.widget if _e is not None else self.cb_api
        i = cb.current()
        chosen = "" if i <= 0 else games.APIS[i - 1]
        games.set_api_override(g.folder, chosen or None)
        detected = getattr(g, "api_detected", "") or g.api
        if chosen:
            g.api, g.api_why = chosen, f"set by hand (detected {detected})"
        else:
            # Read it again rather than writing a placeholder: the reason is
            # what a bug report shows, and "detected from the executable"
            # hides which evidence decided it.
            try:
                g.api, g.api_why = pe.detect_api(g.exe)
            except Exception:
                g.api, g.api_why = detected, "detected from the executable"
        self._log(f"> graphics api: {g.api}" + ("" if chosen else " (auto)"))
        if cb is self.cb_protected_api:
            self._refresh_metadata()
            return
        self.root.after(1, self._remember_library)
        self._forget_row(g)
        self._enter_install()

    def _on_bitness(self, _e=None) -> None:
        if not self.game or not self.game.exe_warning:
            return
        chosen = {1: 64, 2: 32}.get(self.cb_bitness.current())
        games.set_bitness_override(self.game.folder, chosen)
        self.game.bitness = chosen
        self._refresh_metadata()

    def _refresh_metadata(self) -> None:
        g = self.game
        self._forget_row(g)
        self._fill()
        if g in self.shown:
            self.tree.selection_set(str(self.shown.index(g)))
            self._on_pick()
        self._remember_library()

    def _enter_install(self) -> None:
        g = self.game
        self.gamelbl.config(text=g.name)
        if getattr(g, "kind", "") != "video":
            detected = getattr(g, "api_detected", "") or g.api
            origin = "other files (protected EXE)" if g.exe_warning else "the executable"
            self.cb_api["values"] = [f"auto  -  {detected} from {origin}"] + \
                [{"DX9": "DirectX 9", "DX10": "DirectX 10", "DX11": "DirectX 11",
                  "DX12": "DirectX 12"}.get(a, a) for a in games.APIS]
            forced = games.api_override(g.folder)
            self.cb_api.current(games.APIS.index(forced) + 1 if forced in games.APIS else 0)
            self.lbl_api.grid(row=15, column=0, sticky="w", padx=(0, 14), pady=(6, 0))
            self.cb_api.grid(row=15, column=1, columnspan=2, sticky="w", pady=(6, 0))
        else:
            self.lbl_api.grid_remove()
            self.cb_api.grid_remove()
        self.profile_extra = None
        self._refresh_profiles()
        is_video = getattr(g, "kind", "") == "video"
        if is_video:
            self.btn_toggle.pack(side="right")
            self.btn_play.pack(side="right", padx=(0, 10))
            self.urlrow.pack(fill="x", pady=(10, 0), before=self.pb.master)
            cb = video.clipboard_url(self.root)
            if cb and not self.url.get():
                self.url.set(cb)
                self._log(f"> link on the clipboard picked up: {cb[:80]}")
            self._log("> video player: there is no depth buffer here, the feed "
                      "runs on colour and motion only - that is all dlss5 needs "
                      "for video.", "ok")
        else:
            self.btn_play.pack_forget()
            self.btn_toggle.pack_forget()
            self.urlrow.pack_forget()
        need = installer.wants_dxvk(g)
        self.dxvk.set(bool(need))
        if need:
            self._log(f"> {need} closes itself when reshade loads inside it - "
                      f"it will run through dxvk (vulkan) instead, with reshade "
                      f"as a vulkan layer. you can untick that below.", "ok")
        self._set_pathlbl(g)
        self._sync_workres()

        cands = g.candidates or ([g.exe] if g.exe else [])
        if len(cands) > 1:
            labels = []
            for c in cands:
                try:
                    labels.append(str(c.relative_to(g.folder)))
                except ValueError:
                    labels.append(str(c))
            self.cb_exe["values"] = labels
            self.cb_exe.current(cands.index(g.exe) if g.exe in cands else 0)
            self.exerow.grid(row=0, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_exe.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        else:
            self.exerow.grid_forget()
            self.cb_exe.grid_forget()

        self.btn_remove.config(state="normal" if g.installed else "disabled")

        card, sm = gpu.detect()
        self.sm = sm
        if card:
            drv = gpu.driver_version()
            self.gpulbl.config(text=f"{card}\n{gpu.label(sm)}"
                                    + (f"\ndriver {drv}" if drv else ""))
        else:
            # "no nvidia card" is not an answer to "does it work on my RX
            # 7600" (#53). Name the vendor and say what actually exists.
            vendor = None
            try:
                vendor = gpu.other_vendor()
            except Exception:
                pass
            if vendor == "AMD":
                self._log("!! no nvidia card detected - dlss5 will not run "
                          "here", "warn")
                for line in gpu.AMD_ANSWER.splitlines():
                    self._log("     " + line.strip() if line.startswith("  ")
                              else "   " + line)
            else:
                self._log("!! no nvidia card detected - dlss5 will not run",
                          "warn")

        # Work out which routes exist for this game, which of them fit this
        # card, and preselect the best. The dropdown says so on every line,
        # and the choice stays the user's.
        self.support = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0, sm)
        self.route_fit = {o: dlss.fit(o, g.api, self.support.native_dlss, sm,
                                       upscaler=getattr(self.support, 'upscaler', ''))
                          for o in self.support.options}
        self.cb_route["values"] = [self._route_label(o) for o in self.support.options]
        self.cb_route.current(self.support.options.index(self.support.recommended))
        if self.support.native_dlss:
            self._log(f"> this game ships its own dlss "
                      f"({', '.join(self.support.evidence[:3])})", "ok")
        elif getattr(self.support, "upscaler", ""):
            self._log(f"> no dlss, but the game ships {self.support.upscaler.upper()} "
                      f"({', '.join(self.support.upscaler_evidence[:2])}) - optiscaler "
                      f"can redirect those calls into dlss, then neural rendering", "ok")
        elif g.api in ("DX11", "DX12", "Unknown") and g.bitness == 64:
            self._log(f"> no dlss files found under {g.folder} - the native and "
                      f"optiscaler routes need the game's own dlss. if this game "
                      f"does have dlss, press [ report a bug ] and say where the "
                      f"nvngx_dlss.dll is; the log tail goes with it.", "warn")
        self._log(f"> {self.support.reason}")
        tier = gpu.tier_note(sm)
        if tier:
            self._log(f"> {tier}")
        self._apply_route(self.support.recommended)
        if len(cands) > 1:
            self._log(f"!! this folder has {len(cands)} executables; selected "
                      f"{g.exe.name}", "warn")
            self._log("   if the game launches a different one, change it above "
                      "or the install does nothing", "warn")

        if not self.catalog:
            self._load_catalog()
        # This page's rows change height here - the video player's link box
        # is packed and unpacked inside the scrolling area - and that does
        # not resize the page itself, so no <Configure> fires and the split
        # between the settings and the log would keep the previous game's
        # measurements.
        self.root.after_idle(self._split_install)

    def _find_local_renodx(self) -> None:
        found, cands = prefs.find_renodx()
        if not found:
            return
        self.renodx_local = found
        tag = f"[local] {found.name}"
        vals = [v for v in self.cb_renodx["values"]
                if not v.startswith(("loading", "[local]"))]
        self.cb_renodx["values"] = [tag] + vals
        self.cb_renodx.set(tag)
        self._log(f"> renodx: using your local build - {found.name} "
                  f"({found.stat().st_size/1048576:.1f} MB)", "ok")

    def _load_catalog(self) -> None:
        def work() -> None:
            try:
                self.q.put(("catalog", sources.rhi_catalog()))
            except Exception as e:
                self.q.put(("caterr", str(e)))
            try:
                self.q.put(("feeders", sources.feeder_releases()))
            except Exception as e:
                log.write(f"feeder release list: {e}", "warn")
        threading.Thread(target=work, daemon=True).start()

    def _fill_feeders(self, rels: list) -> None:
        self.feeder_tags = [t for t, _ in rels]
        # GitHub's pre-release flag is not set consistently upstream; the tag
        # itself says what a build is.
        self.cb_feederver["values"] = list(FEEDER_CHOICES) + [
            f"{t}{'  (pre-release)' if pre or 'beta' in t.lower() else ''}"
            for t, pre in rels]

    def _on_overlaykey(self, _e=None) -> None:
        """Remember the key.

        'route default' is stored as 0 and writes nothing - ReShade then
        opens on Home and OptiScaler on Insert. Home is its own entry, with
        its own key code, so asking for it is not the same request.
        """
        name = self.cb_overlaykey.get()
        vk = reshade_ini.OVERLAY_KEYS.get(name, 0)
        prefs.set_("overlay_key", 0 if "default" in name else vk)
        if "default" not in name:
            self._log(f"> the overlay will open on {name} - press INSTALL to "
                      f"write it into this game")

    def _has_rr(self) -> bool:
        """Does this game ship a ray-reconstruction runtime?

        Read out of the route detection's own evidence rather than by
        walking the folder again: `dlss.DLSS_FILES` already includes
        nvngx_dlssd.dll, so `dlss.detect()` found it when the game was
        picked. Walking here as well put a six-second budget on the Tk
        thread on every route change, which is the shape of #8, #18 and #32
        - and it looked in a different folder than the installer, so a
        nested executable was offered a swap the install then skipped.
        """
        sup = getattr(self, "support", None)
        if sup is None or self.game is None:
            return False
        return any(str(e).lower().endswith("nvngx_dlssd.dll")
                   for e in (getattr(sup, "evidence", None) or []))

    def _warn_swap(self, name: str) -> None:
        """Say what replacing a file the game shipped means, before it is."""
        self._log("")
        for line in anticheat.swap_message(name).splitlines():
            self._log(f"!! {line}" if line.strip() else "", "warn")

    def _on_keep_dlss(self) -> None:
        """Unticking this replaces the game's own nvngx_dlss.dll - the same
        act the ray-reconstruction dropdown warns about, and it had no
        warning of its own."""
        if not self.keep_dlss.get() and self.game is not None \
                and (self.game.install_dir / "nvngx_dlss.dll").is_file():
            self._warn_swap("nvngx_dlss.dll")

    def _on_dlssd(self, _e=None) -> None:
        """Chosen a ray-reconstruction build: say what a swap means first."""
        if self.cb_dlssd.get() == DLSSD_KEEP:
            return
        self._warn_swap("nvngx_dlssd.dll")

    def _on_feederver(self, _e=None) -> None:
        i = self.cb_feederver.current()
        if i >= 2:
            tag = self.feeder_tags[i - 2]
            self._log(f"> feeder pinned to {tag} - the "
                      f"matching DLSS 5 add-on build is chosen for it")
            self._hdr_note(tag)

    def _hdr_note(self, tag: str = "") -> None:
        """Say it when this display is in HDR and the build is too old for it.

        Only when HDR is actually on: an SDR display does not care, and a
        warning everybody sees is a warning nobody reads.
        """
        if getattr(self, "route", "") not in ("", dlss.FEEDER):
            return              # no feeder here, no feeder build to talk about
        try:
            if gpu.hdr_on() is not True:
                return
            if tag and sources.feeder_key(tag) >= sources.feeder_key(
                    sources.FEEDER_HDR_MIN):
                return
        except Exception:
            return
        if tag:
            self._log(f"!! this display is in HDR, and feeder {tag} is older "
                      f"than {sources.FEEDER_HDR_MIN} - the feeder's own "
                      f"0.15.1 notes say the neural pass was wrecking HDR "
                      f"highlights before it (blown or flat bright areas). "
                      f"Put the feeder build back on '{FEEDER_CHOICES[0]}', "
                      f"or turn HDR off in Windows while you play.", "warn")
        else:
            self._log(f"> this display is in HDR: feeder "
                      f"{sources.FEEDER_HDR_MIN} is the build that handles an "
                      f"HDR swapchain properly, and "
                      f"'{FEEDER_CHOICES[0]}' will normally be that or newer. "
                      f"An older pinned build wrecks the highlights.")

    def _fill_catalog(self, cat: dict) -> None:
        self.catalog = cat
        ren = [e["label"] for e in cat.get("renodx", [])]
        nr = [e["label"] for e in cat.get("dlssnr", [])]
        ds = [e["label"] for e in cat.get("dlss", [])]
        self._fill_addon_list(sf=getattr(self, "route", None) == dlss.RENODX)
        self.cb_dlssnr["values"] = ["auto - match my gpu"] + nr
        self.cb_dlssnr.current(0)
        self.cb_dlss["values"] = ds
        if ds:
            self.cb_dlss.current(0)
        dd = [e["label"] for e in cat.get("dlssd", [])]
        self.cb_dlssd["values"] = [DLSSD_KEEP] + dd
        self.cb_dlssd.current(0)
        # One publisher, no mirror behind it: with nothing listed there is
        # nothing to choose, and saying so beats an empty dropdown.
        self.cb_dlssd.configure(state="readonly" if dd else "disabled")
        if not dd:
            self.dlssdhint.configure(
                text="no build could be listed - the game keeps its own")
        if sources.last_fallback:
            self._log(f"!! {sources.last_fallback}", "warn")
        self._log(f"> versions: renodx {len(ren)}, dlssnr {len(nr)}, "
                  f"dlss {len(ds)}"
                  + (f", ray reconstruction {len(dd)}" if dd else ""))

    def _opts(self) -> installer.Options:
        if not hasattr(self, "cb_renodx"):
            return installer.Options()
        val = self.cb_renodx.get()
        local = self.renodx_local if val.startswith("[local]") else None
        feed: dict = {}
        if self._work_applies() and self.workres.get() != 100:
            feed["work_resolution"] = self.workres.get()
        pi = self.cb_preset.current()
        if pi > 0:
            feed["preset"] = list(feedcfg.PRESETS.keys())[pi]
        hi = self.cb_hdr.current()
        if hi > 0:
            feed["hdr"] = list(feedcfg.HDR.keys())[hi]
        clean = lambda v: None if (not v or v.startswith(("loading", "auto"))) else v
        nr: dict = {}
        if getattr(self, "route", None) == dlss.OPTI and hasattr(self, "cb_nrpreset"):
            nr["WorkingScale"] = round(self.workres.get() / 100, 2)
            if self.cb_nrpreset.current() > 0:
                nr["Preset"] = list(optiscaler.NR_PRESETS.keys())[self.cb_nrpreset.current()]
            if self.cb_nrstyle.current() > 0:
                nr["Style"] = list(optiscaler.NR_STYLES.keys())[self.cb_nrstyle.current()]
        extra = getattr(self, "profile_extra", None)
        if extra is not None:
            feed = {**extra.feed, **feed}
            nr = {**extra.nr, **nr}
        return installer.Options(
            provider=self.provider.get(),
            renodx=None if local else clean(val),
            renodx_local=local,
            dlssnr=clean(self.cb_dlssnr.get()),
            dlss=clean(self.cb_dlss.get()),
            # "" means leave the game's own alone, which is the default and
            # the only safe answer for anything played online.
            dlssd=("" if self.cb_dlssd.get() in (DLSSD_KEEP, "loading...")
                   else self.cb_dlssd.get()),
            keep_game_dlss=self.keep_dlss.get(),
            feed=feed,
            nr=nr,
            feeder_prerelease=self.cb_feederver.current() == 1,
            feeder_tag=(self.feeder_tags[self.cb_feederver.current() - 2]
                        if self.cb_feederver.current() >= 2 else ""),
            dxvk=self.dxvk.get(),
            fg=bool(self.fg.get()) and getattr(self, 'route', None) == dlss.OPTI,
            mfg=bool(self.mfg.get()),
            vr=bool(self.vr.get()),
            path=getattr(self, 'route', dlss.FEEDER),
            native_dlss=bool(self.support and self.support.native_dlss),
            upscaler=str(getattr(self.support, 'upscaler', '') or ''),
            opti_proxy=("" if self.cb_proxy.current() <= 0
                        else optiscaler.PROXY_NAMES[self.cb_proxy.current() - 1]),
            opti_build=list(optiscaler.BUILDS)[max(0, self.cb_optibuild.current())],
            reshade_proxy=("" if self.cb_rproxy.current() <= 0
                           else installer.RESHADE_PROXIES[self.cb_rproxy.current() - 1]),
        )

    # ---------------------------------------------------------------- actions
    def _install(self) -> None:
        if self.busy or not self.game:
            return
        try:
            ac = anticheat.detect(self.game.install_dir, self.game.folder)
        except Exception:
            ac = None
        if ac is not None and ac.present and not messagebox.askyesno(
                APP,
                f"{ac.summary} is installed in this game.\n\n"
                f"This is an online game. ReShade add-ons and anti-cheat do not "
                f"coexist: the game may refuse to start, delete the files, or "
                f"ban the account. Nobody but you carries that risk.\n\n"
                f"Install anyway?"):
            return
        self.busy = True
        self.btn_next.config(state="disabled", text="installing")
        self.btn_back.config(state="disabled")
        self.btn_remove.config(state="disabled")
        self.pb["value"] = 0
        g, opt = self.game, self._opts()
        self._log("")
        self._log(f"=== {g.name} ===", "head")

        def work() -> None:
            try:
                rep = installer.install(
                    g, opt,
                    on_step=lambda i, n, name: self.q.put(("step", (i, n, name))),
                    on_prog=lambda p, m: self.q.put(("prog", (p, m))),
                    on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("done", rep))
            except installer.InstallError as e:
                self.q.put(("fail", str(e)))
            except Exception:
                log.exception("installing")
                self.q.put(("fail", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def _uninstall(self) -> None:
        if self.busy or not self.game:
            return
        if not messagebox.askyesno(
                APP, f"{self.game.name}\n\nremove the files this tool installed? "
                     f"the game's own files are restored and left alone."):
            return
        self.busy = True
        self._log("")
        self._log("=== uninstalling ===", "head")
        g = self.game

        def work() -> None:
            try:
                rm = installer.uninstall(g, on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("removed", rm))
            except (sources.RateLimited, sources.Unavailable) as e:
                self.q.put(("fail", str(e)))
            except Exception:
                log.exception("uninstalling")
                self.q.put(("fail", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def _back(self) -> None:
        if self.busy or self.step < 3:
            return
        self._jump(2)

    def _enter_library(self, asked: bool = False) -> None:
        """The games page with nothing on it yet: fill it the way asked.

        `asked` is a press of "scan games": the setting is about scanning at
        START, and a button that says scan and then does not was the one
        thing it must never do.
        """
        if self._have_library():
            self._fill()
            return
        if self.scan_on_start.get() and self._load_cached():
            return
        # Once per run unless asked: a scan that found nothing would
        # otherwise run again on every visit to the games page.
        if asked or (self.scan_on_start.get()
                     and not getattr(self, "_scanned_once", False)):
            self._scan()
            return
        # "scan library at start" off means no library at start, cache or
        # not: someone who unticked it asked for an empty list, and handing
        # them one anyway would make the box mean nothing. The line below
        # tells them the fast path exists - after _fill, which writes its
        # own line and used to hide this one.
        kp = video.known()
        if kp and not any(getattr(x, "install_dir", None) == kp.install_dir
                          for x in self.all_games):
            self.all_games.insert(0, kp)
        self._fill()
        self.scanlbl.config(
            text="library scan is off - press rescan, or choose folder "
                 "for one game"
            + (" (there is a saved library from last time: tick 'scan "
               "library at start' and it opens straight away)"
               if library.FILE.is_file() else ""))

    def _next(self) -> None:
        if self.busy:
            return
        if self.step == 1:
            self._show(2)
            self._enter_library(asked=True)
        elif self.step == 2:
            if not self._can_install_page():
                return
            self._show(3)
            self._enter_install()
        elif self.step == 3:
            self._install()

    # ---------------------------------------------------------------- queue
    def _log(self, text: str, tag: str = "") -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear_log(self) -> None:
        """Empty the pane. The file on disk is untouched - that is the one
        a bug report needs, and clearing the view must not throw it away."""
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _idle(self) -> None:
        self.busy = False
        self.btn_next.config(
            state="normal",
            text="INSTALL" if self.step == 3
            else ("continue" if self.step == 2 else "scan games"))
        self.btn_back.config(state="normal" if self.step >= 3 else "disabled")

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "scan":
                    self.scanlbl.config(text=payload.lower())
                    self.status.config(text=payload.lower())
                elif kind == "wincrash":
                    where, crash, said = payload
                    if self.game is None or self.game.install_dir != where:
                        continue
                    # The same window the verdict rule uses: a fault the
                    # verdict would not accept must not reach the record
                    # either, or the screen and the published list disagree.
                    self._last_crash = (crash if self._crash_is_this_session(crash)
                                        else None)
                    self._log("")
                    self._log("=== what Windows recorded ===", "head")
                    self._log(f"[fail] {said[0]}", "err")
                    self._log(f"        {said[1]}")
                    self._crash_overrides(crash)
                elif kind == "community":
                    where, lines = payload
                    # The person may have moved on to another game while the
                    # file was downloading; a note about the previous one
                    # would read as being about this one.
                    if self.game is None or self.game.install_dir != where:
                        continue
                    self._log("")
                    self._log("=== what other people found ===", "head")
                    for ln in lines:
                        self._log(f"> {ln}")
                elif kind == "rechecked":
                    gen, rows = payload
                    if gen != getattr(self, "_recheck_id", 0):
                        # A rescan started after this worker did; its rows
                        # are the current ones and these are stale.
                        continue
                    self._rows.update(rows)
                    self._recheck = set()
                    # _fill lets go of the picked game: not on the install
                    # page, and not under someone who picked one and went
                    # to look at video or remix.
                    if self.step == 2 or (self.step != 3 and self.game is None):
                        self._fill()
                        self.scanlbl.config(
                            text=f"{len(self.shown)} games  ::  from the last "
                                 f"scan  ::  {len(rows)} changed and were "
                                 f"read again  ::  press rescan to look for "
                                 f"new ones")
                    self._remember_library()
                elif kind == "scanned":
                    payload, rows = payload
                    self.busy = False
                    self._scanned_once = True
                    # A folder chosen while the scan was still running used
                    # to vanish when the scan finished (issue #18).
                    found = {str(g.folder).lower() for g in payload}
                    kept = [g for g in self.all_games
                            if g.source == "Manual" and str(g.folder).lower() not in found]
                    # ...but a folder picked by hand that a store also
                    # reports under another folder is the same game twice.
                    self.all_games = games.same_exe_once(kept + payload)
                    kp = video.known()
                    if kp and not any(x.install_dir == kp.install_dir
                                      for x in payload):
                        self.all_games.insert(0, kp)
                    self._rows.clear()
                    self._rows.update(rows)
                    self._fill()
                    self._remember_library()
                    self.status.config(text="scan complete")
                    self._check_stale()
                    if self.step == 5:
                        self._remix_mine()
                elif kind == "update":
                    self._show_banner(*payload)
                elif kind == "update_ready":
                    self._update_ready(*payload)
                elif kind == "stale":
                    self.stale = payload
                    if self.all_games and (self.step == 2 or (
                            self.step != 3 and self.game is None)):
                        self._fill()
                elif kind == "board":
                    self.boardlbl.config(text=payload)
                elif kind == "updated_all":
                    self.busy = False
                    self._rows.clear()
                    self._fill()
                    self.scanlbl.config(text=f"updated {payload} game(s)")
                    self._check_stale()
                elif kind == "swap":
                    self.bannerlbl.config(text="> restarting into the new build...")
                    self.root.update()
                    selfupdate.apply_and_restart(payload)
                elif kind == "updfail":
                    self.busy = False
                    self.bannerlbl.config(text=f"> update failed: {payload}")
                    self.pblbl.config(text="")
                elif kind == "catalog":
                    self._fill_catalog(payload)
                elif kind == "feeders":
                    self._fill_feeders(payload)
                elif kind == "caterr":
                    self._log(f"!! could not fetch the version list: {payload}",
                              "warn")
                elif kind == "step":
                    i, n, name = payload
                    self.status.config(text=f"[{i + 1}/{n}] {name}")
                elif kind == "prog":
                    p, m = payload
                    self.pb["value"] = p
                    self.pblbl.config(text=m)
                elif kind == "log":
                    self._log(payload)
                elif kind == "components":
                    self._show_components(payload)
                elif kind == "done":
                    self._finish_ok(payload)
                    self._remember_library()
                elif kind == "cameras":
                    cams, then = payload
                    self._cameras_found(cams, then)
                elif kind == "webcam_started":
                    self._webcam_started(*payload)
                elif kind == "webcam_failed":
                    self._log(f"!! capture: {payload}", "err")
                    messagebox.showerror(APP, payload)
                elif kind == "processed":
                    self._idle()
                    self.pblbl.config(text="")
                    self._log(f"> rendered: {payload}", "ok")
                    self._log("> opening it in the player - compare with the original "
                              "side by side if you like", "ok")
                    try:
                        video.launch(self.game.install_dir, str(payload))
                    except Exception as e:
                        self._log(f"!! could not start the player: {e}", "err")
                elif kind == "downloaded":
                    self._idle()
                    self.pblbl.config(text="")
                    self._log(f"> saved: {payload}", "ok")
                    try:
                        video.launch(self.game.install_dir, str(payload))
                        self._log("> opening it in the player", "ok")
                    except Exception as e:
                        self._log(f"!! could not start the player: {e}", "err")
                elif kind == "video_ready":
                    self._forget_last_session()
                    self._idle()
                    self.pb["value"] = 0
                    self.pblbl.config(text="")
                    g = payload
                    self.all_games = [x for x in self.all_games
                                      if x.install_dir != g.install_dir]
                    self.all_games.insert(0, g)
                    self.game = g
                    self._log("> player ready. press INSTALL to feed dlss5 "
                              "into it.", "ok")
                    self._enter_install()
                    self._show(3)
                elif kind == "removed":
                    self._idle()
                    self._forget_row(self.game)
                    self._remember_library()
                    self._log(f"> uninstalled ({len(payload)} items)", "ok")
                    self.btn_remove.config(state="disabled")
                    if hasattr(self, "btn_rm2"):
                        self.btn_rm2.config(state="disabled")
                    if self.step == 2:
                        self._fill()
                elif kind in ("fail", "error"):
                    self._idle()
                    # A video setup that failed left an install page with
                    # nothing on it and an INSTALL that did nothing.
                    if self.step == 3 and self.game is None:
                        self._show(4)
                    self._log(payload, "err")
                    self.pblbl.config(text="")
                    self.status.config(text="failed")
                    messagebox.showerror(APP, payload.strip().splitlines()[-1])
                    if "Traceback" in payload:
                        self._offer_crash_report()
        except queue.Empty:
            pass
        except Exception:
            # Anything escaping here used to skip the reschedule below, which
            # killed the pump for good: progress, results and errors all
            # stopped arriving and the window looked frozen. Report it and
            # keep running.
            log.exception("handling a background result")
            try:
                self._idle()
                self._log("!! internal error - see the log file "
                          f"({log.path()})", "err")
            except Exception:
                pass
        finally:
            # Rescheduling is not optional: it is the only thing keeping the
            # interface connected to its worker threads.
            if log.crashed() and not self._crash_shown:
                self._offer_crash_report()
            self.root.after(60, self._pump)

    def _finish_ok(self, rep: installer.Report) -> None:
        self._idle()
        # Only this game's row changed. Clearing them all used to be free;
        # now that the rows are kept for the next launch it would throw the
        # whole library's compatibility pass away and make the next start
        # re-read every folder on the Tk thread (issues #8, #18, #32).
        self._forget_row(self.game)
        self.pb["value"] = 100
        self.pblbl.config(text="")
        self._log("")
        self._log(f"> done - {len(rep.written)} files written", "ok")
        for n in rep.notes:
            self._log(f"    {n}")
        for w in rep.warnings:
            self._log(f"!!  {w}", "warn")
        if rep.skipped:
            self._log(f"    left untouched: {', '.join(rep.skipped)}")
        self._log("")
        route = getattr(self, "route", dlss.FEEDER)
        if self.game and getattr(self.game, "kind", "") == "video":
            self._log("> now open the player and:", "head")
            for line in video.CHECKLIST:
                self._log(f"   {line}")
            self._log("")
            self._log("!! neural rendering re-draws EVERYTHING in the window, "
                      "menus and subtitles included - use the player fullscreen "
                      "(double-click the video). the first seconds after a "
                      "seek or a resolution change look smeared while the "
                      "history rebuilds.", "warn")
            self._log("")
            self._log("> watched something? come back and press 'did it work?' - "
                      "it reads the logs and tells you what happened.", "head")
            self.btn_remove.config(state="normal")
            self.status.config(text="install complete - open the player")
            return
        self._log("> now launch the game and:", "head")
        if route == dlss.OPTI:
            self._log(f"   1. press {reshade_ini.overlay_key_name('Insert')} to open "
                      f"the optiscaler overlay")
            self._log("   2. neural rendering is switched on already; if the "
                      "overlay says it refused, it tells you why right there")
            self._log(f"   3. model resolution is set to {self.workres.get()}% - "
                      f"the slider in the overlay changes it live")
            if self.game and self.game.api == "DX11":
                self._log("   4. on d3d11 the upscaler is FSR on D3D12 - leave it, "
                          "dlss cannot be the upscaler on this route")
            self._log("   !  set the game's dlss quality mode as you like - it "
                      "still applies")
        elif route == dlss.RENODX:
            self._log("   !  reshade's overlay will say 'no .fx files found' - "
                      "normal on this route, it uses no shaders")
            self._log(f"   1. press {reshade_ini.overlay_key_name()} to open reshade, "
                      f"then the RenoDX DLSS tab")
            self._log("   2. neural rendering is enabled; the tab shows its status "
                      "and lets you tune intensity and style")
            self._log("   3. turn OFF the game's own MSAA/SSAA")
        elif route == getattr(dlss, "REMIX", "remix"):
            self._log("   !  there is no reshade here and Home does nothing - "
                      "everything lives in the remix menu")
            self._log("   1. press Alt+X in game, then 'Developer Settings Menu'")
            self._log("   2. open the Post-Processing tab and tick "
                      "'Enable Neural Uplift (DLSS-NR)'")
            self._log("   3. style, intensity and the structure sliders are right "
                      "under it; the status line says whether the feature was "
                      "created")
            self._log("   !  leave the game's own dlss and ray reconstruction as "
                      "they are - the neural pass runs after them, not instead")
        elif route == getattr(dlss, "STANDALONE", "standalone"):
            self._log("   1. in the game turn OFF its own DLSS, frame generation "
                      "and anti-aliasing - this add-on brings all three")
            self._log("   2. it shows the result in its own window on top; set "
                      "resolution and display mode BEFORE starting, changes "
                      "need a restart")
            self._log(f"   3. press {reshade_ini.overlay_key_name()} for reshade, then "
                      f"the 'Standalone DLSS-NR + SR' "
                      "tab: neural rendering and frame generation toggle there")
            self._log("   4. F10 flips between the processed and the original picture")
            self._log("   !  a lower in-game resolution than your monitor = DLSS "
                      "super resolution up to native; same resolution = DLAA")
        elif route == getattr(dlss, "UPSTREAM", "upstream"):
            self._log("   !  reshade's overlay will say 'no .fx files found' - "
                      "normal on this route, it uses no shaders")
            self._log("   1. keep the game's own DLSS ON - the network runs "
                      "before it, at render resolution")
            self._log(f"   2. press {reshade_ini.overlay_key_name()} to open reshade, "
                      f"then the 'NR Pre-Upscale' "
                      "tab: strength and cadence live there")
            self._log("   3. using DLSS Frame Generation? set cadence to Quality "
                      "(every frame) or it stutters")
            self._log("   4. turn OFF the game's own MSAA/SSAA")
        elif route in (dlss.NATIVE, dlss.BRIDGE):
            self._log("   !  reshade's overlay will say 'no .fx files found' - "
                      "normal on this route, it uses no shaders; the add-on "
                      "tabs are what matter")
            self._log(f"   1. press {reshade_ini.overlay_key_name()} to open reshade, "
                      f"then the DLSS 5 tab")
            self._log("   2. turn on neural rendering there (F5 toggles it in the "
                      "4.6+ builds)")
            self._log("   3. keep the game's dlss ON - the add-on hooks it")
            self._log("   4. turn OFF the game's own MSAA/SSAA")
        else:
            self._log(f"   1. press {reshade_ini.overlay_key_name()} to open reshade")
            p = reshade_ini.PROVIDERS[self.provider.get()]
            if p[1]:
                self._log(f"   2. tick '{p[0]}' and 'DLSS 5 Feed', provider ABOVE "
                          f"the feed")
            else:
                self._log("   2. put your provider's technique ABOVE DLSS 5 Feed")
            if self.game and self.game.bitness == 32:
                # The 64-bit add-on runs in the host64 helper, and that helper
                # has its own window - but the feed's own DLSS 5 page IS in
                # this game's overlay and drives the helper from there
                # ("settings reloaded from the overlay page" in its log). Say
                # so, because alt-tabbing to the helper window is actively
                # harmful: it minimizes an exclusive-fullscreen game, the swap
                # chain comes back 160x28 and DLSS is torn down every time.
                self._log("   3. turn on neural rendering in the DLSS 5 page "
                          "of this overlay - it drives the 64-bit helper for "
                          "you")
                self._log("   !  the helper has a window of its own; do NOT "
                          "alt-tab to it while playing. Alt-tab minimizes the "
                          "game, the swap chain comes back tiny and DLSS is "
                          "rebuilt from scratch every time")
            else:
                self._log("   3. turn on neural rendering in the DLSS 5 panel")
            self._log("   4. turn OFF the game's own MSAA/SSAA")
            self._log("   !  play BORDERLESS or true fullscreen, at your "
                      "display's own resolution. In a bordered window the "
                      "game presents a few pixels short (1920x1071 instead "
                      "of 1920x1080) and the neural result never lands on "
                      "screen - everything looks fine in the logs and "
                      "nothing changes. Proven on Bayonetta", "warn")
            self._log("   5. NVIDIA Smooth Motion and this feeder do not mix - "
                      "turn it off for this game if the picture flickers")
        self._log("")
        self._log("!! set your resolution BEFORE enabling neural rendering - the "
                  "feature is built for one backbuffer size and rebuilding it "
                  "mid-session can crash the game. use borderless, not exclusive "
                  "fullscreen.", "warn")
        if self.dxvk.get():
            self._log("!! dxvk: the game renders on vulkan now. exclusive "
                      "fullscreen re-creates the swap chain on every mode "
                      "change and the feature with it - set the game to "
                      "borderless/windowed first, then enable neural rendering.",
                      "warn")
        if self.sm is not None and self.sm < 89:
            self._log("!! rtx 20/30: the pass is heavy on your card. if the fps "
                      "drop is too much, lower the work area / model resolution "
                      "or turn v-sync off.", "warn")
        self._log("")
        self._log("> played it? come back and press 'did it work?' - it reads the "
                  "logs and tells you what happened.", "head")
        self.btn_remove.config(state="normal")
        self.status.config(text="install complete")


def _own_icon(win) -> None:
    """Our icon on a window of our own.

    The main window sets it as the default, but a Toplevel opened later - the
    Remix list, the report dialog - came up with Tk's blank one. Every window
    of ours comes through _dark_titlebar, so it is set there, from the .ico
    the main window wrote.
    """
    try:
        ico = prefs.FILE.parent / "dlss5-autopilot.ico"
        if ico.is_file():
            win.iconbitmap(str(ico))
    except Exception:
        pass


def _dark_titlebar(win) -> None:
    """Paint the Windows title bar in the app's own colours.

    Tk draws only the client area; Windows draws the frame in the system
    theme, so a dark app came with a white strip on top. DWM attributes
    since Windows 10 20H1 (dark mode) and Windows 11 (caption / text
    colour) fix that; on anything older the calls fail and nothing changes.
    """
    _own_icon(win)
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            # Not realised yet - a Toplevel opened before its first paint
            # can still return 0 here even after update_idletasks(). Retry
            # once the window manager actually maps it, instead of leaving
            # the title bar white for good.
            win.bind("<Map>", lambda _e: _dark_titlebar(win), add="+")
            return
        dwm = ctypes.windll.dwmapi

        def colorref(hexcolour: str) -> int:
            r, g, b = (int(hexcolour[i:i + 2], 16) for i in (1, 3, 5))
            return (b << 16) | (g << 8) | r

        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)      # dark mode
        cap = ctypes.c_int(colorref(RAIL))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)     # caption
        txt = ctypes.c_int(colorref(BODY))
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)     # text
        bdr = ctypes.c_int(colorref(LINE))
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(bdr), 4)     # border
    except Exception:
        pass


def run() -> int:
    log.start(update.VERSION)
    try:
        import ctypes
        # Per-monitor DPI awareness: without it Windows stretches the whole
        # window like a bitmap on 125%/150% displays and the text goes soft.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    root = tk.Tk()
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForWindow(root.winfo_id()) or 96
        root.tk.call("tk", "scaling", dpi / 72.0)
        global SCALE
        SCALE = max(1.0, dpi / 96.0)
    except Exception:
        pass
    # Installed before the window is built: a failure while building it is
    # exactly the kind that used to disappear without trace.
    log.install_handlers(root)
    try:
        App(root)
    except Exception as e:
        log.exception("building the main window", e)
        raise
    root.mainloop()
    try:
        from . import video as _video
        _video.stop_webcam()
    except Exception:
        pass
    log.write("closed normally")
    return 0
