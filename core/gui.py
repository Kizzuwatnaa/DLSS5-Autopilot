"""Terminal-styled tkinter interface.

Monospace throughout, amber on near-black, bracket markers, square edges. A
persistent left rail carries the three steps so you can always see where you
are and click back.

Step 1: architecture filter
Step 2: pick a game from the scan
Step 3: settings, install, diagnose
"""
from __future__ import annotations

import queue
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import (anticheat, components, diagnose, dlss, dxvk, feedcfg,
               games, gpu, profiles, video,
               installer, log, optiscaler, prefs, reshade_ini, selfupdate,
               sources, update)

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


def font(size: int = 10, weight: str = "normal") -> tuple:
    return (MONO[0], size, weight)


FEEDER_CHOICES = ("stable - newest release",
                  "newest pre-release")

STEPS = (("architecture", "what to install for"),
         ("game", "pick from your library"),
         ("install", "settings and go"))


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
        self._fill_job: str | None = None
        self.game: games.Game | None = None
        self.catalog: dict[str, list[dict]] = {}
        self.renodx_local: Path | None = None
        self.update_url: str | None = None
        self.support: dlss.Support | None = None

        self.provider = tk.IntVar(value=3)
        self.keep_dlss = tk.BooleanVar(value=True)
        self.workres = tk.IntVar(value=100)
        self.feeder_pre = tk.BooleanVar(value=False)
        self.dxvk = tk.BooleanVar(value=False)
        self.sm: int | None = None          # the card's architecture, once known
        self.stale: dict[str, int] = {}     # install folder -> outdated parts
        self.route_fit: dict[str, tuple[bool, str]] = {}
        self.update_ready: Path | None = None
        self._crash_shown = False
        self._last_diag: object | None = None

        self._build()
        self.search.trace_add("write", self._search_changed)
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
                     focuscolor=BG, padding=(14, 7), font=font(10),
                     relief="solid", bordercolor=EDGE)
        st.map("TButton", background=[("active", FIELD), ("disabled", BG)],
               foreground=[("disabled", FAINT)], bordercolor=[("disabled", LINE)])
        st.configure("Accent.TButton", background=AMBER, foreground=BG,
                     font=font(10, "bold"), padding=(22, 8), borderwidth=0)
        st.map("Accent.TButton", background=[("active", "#e8bd7a"),
                                             ("disabled", LINE)],
               foreground=[("disabled", FAINT)])
        st.configure("TRadiobutton", background=PANEL, foreground=TXT, font=font(10))
        st.map("TRadiobutton", background=[("active", PANEL)])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=BODY, rowheight=26, borderwidth=0, font=font(10))
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
        st.configure("TProgressbar", background=AMBER, troughcolor=FIELD,
                     borderwidth=0, thickness=4)

    # ---------------------------------------------------------------- chrome
    def _build(self) -> None:
        r = self.root
        r.title(APP)
        r.after(0, lambda: _dark_titlebar(r))
        # Our own icon in the title bar and the taskbar, not Python's feather.
        try:
            import base64
            from .icon_png import ICON_PNG_B64
            self._icon = tk.PhotoImage(data=base64.b64decode(ICON_PNG_B64))
            r.iconphoto(True, self._icon)
        except Exception:
            pass
        r.geometry("1060x830")
        r.minsize(980, 720)
        # Open filling the screen: the three-column layout reads better with
        # room, and a small window in a corner looked like a dialog box.
        try:
            r.state("zoomed")
        except tk.TclError:
            pass
        r.configure(bg=BG)
        self._style()

        rail = tk.Frame(r, bg=RAIL, width=236)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        tk.Frame(r, bg=LINE, width=1).pack(side="left", fill="y")

        brand = tk.Frame(rail, bg=RAIL)
        brand.pack(fill="x", padx=20, pady=(24, 22))
        tk.Label(brand, text="dlss5", bg=RAIL, fg=AMBER,
                 font=(MONO[0], 16)).pack(anchor="w")
        tk.Label(brand, text="autopilot", bg=RAIL, fg=DIM,
                 font=(MONO[0], 16)).pack(anchor="w")

        self.rail_rows: list[dict] = []
        for i, (title, sub) in enumerate(STEPS, start=1):
            row = tk.Frame(rail, bg=RAIL, cursor="hand2")
            row.pack(fill="x")
            marker = tk.Frame(row, bg=RAIL, width=2)
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
        self.gpulbl = tk.Label(rail, text="", bg=RAIL, fg=DIM, anchor="w",
                               justify="left", font=font(8), wraplength=196)
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
            f"https://github.com/{update.REPO}#who-does-what-the-five-routes"))

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
        tk.Frame(right, bg=LINE, height=1).pack(side="bottom", fill="x", padx=28)

        self.body = tk.Frame(right, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28, pady=(20, 10))

        self.p1 = self._page_arch()
        self.p2 = self._page_games()
        self.p3 = self._page_install()

        self.status = tk.Label(bar, text="ready", bg=BG, fg=DIM,
                               font=font(9), anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.btn_back = ttk.Button(bar, text="back", command=self._back)
        self.btn_back.pack(side="left", padx=(0, 10))
        self.btn_next = ttk.Button(bar, text="continue", style="Accent.TButton",
                                   command=self._next)
        self.btn_next.pack(side="right")

        r.bind("<Escape>", lambda e: self._jump(1))
        r.bind("<Control-h>", lambda e: self._jump(1))

    def _jump(self, n: int) -> None:
        if self.busy or n == self.step:
            return
        if n < self.step:
            self._show(n)
        elif n == 2 and self.all_games:
            self._show(2)
        elif n == 3 and self.game:
            self._show(3)
            self._enter_install()

    def _paint_rail(self) -> None:
        for e in self.rail_rows:
            active = e["n"] == self.step
            done = e["n"] < self.step
            bg = PANEL if active else RAIL
            e["marker"].configure(bg=AMBER if active else RAIL)
            for k in ("row", "pad", "box"):
                e[k].configure(bg=bg)
            e["mark"].configure(bg=bg, text="[x]" if done else ("[>]" if active else "[ ]"),
                                fg=GREEN if done else (AMBER if active else FAINT))
            e["t1"].configure(bg=bg, fg=TXT if active else (BODY if done else DIM))
            e["t2"].configure(bg=bg, fg=FAINT)

    def _show(self, step: int) -> None:
        self.step = step
        for p in (self.p1, self.p2, self.p3):
            p.pack_forget()
        [self.p1, self.p2, self.p3][step - 1].pack(fill="both", expand=True)
        self._paint_rail()
        self.btn_back.config(state="normal" if step > 1 else "disabled")
        if step == 1:
            self.btn_next.config(text="scan games", state="normal")
        elif step == 2:
            self.btn_next.config(text="continue",
                                 state="normal" if self.game else "disabled")
        else:
            self.btn_next.config(text="INSTALL", state="normal")

    def _card(self, parent, pad=(16, 14)) -> tk.Frame:
        c = tk.Frame(parent, bg=PANEL, highlightbackground=LINE,
                     highlightthickness=1)
        inner = tk.Frame(c, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=pad[0], pady=pad[1])
        c.inner = inner                       # type: ignore[attr-defined]
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

    # ---------------------------------------------------------------- step 1
    def _page_arch(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        ttk.Label(f, text="what are you installing for?", style="H1.TLabel")\
            .pack(anchor="w")
        ttk.Label(f, text="not sure if a game is 32- or 64-bit? leave it on "
                          "everything - the architecture is read from each "
                          "executable.", style="Dim.TLabel")\
            .pack(anchor="w", pady=(6, 18))

        opts = [
            ("all", "everything", "reliable + experimental",
             "list every game with its architecture and outlook shown"),
            ("64", "64-bit only", "the reliable path",
             "reshade and the dlss5 add-on install straight next to the game"),
            ("32", "32-bit only", "experimental",
             "a 32-bit process cannot load 64-bit ngx, so a host64 helper runs "
             "alongside. it often fails to start."),
        ]
        for val, title, tag, desc in opts:
            card = self._card(f, pad=(16, 4))
            card.pack(fill="x", pady=3)
            top = tk.Frame(card.inner, bg=PANEL)
            top.pack(fill="x", pady=(9, 0))
            ttk.Radiobutton(top, text=title, value=val, variable=self.arch)\
                .pack(side="left")
            tk.Label(top, text=tag, bg=PANEL,
                     fg=RUST if "experimental" in tag else FAINT,
                     font=font(8)).pack(side="left", padx=10)
            tk.Label(card.inner, text=desc, bg=PANEL, fg=DIM, anchor="w",
                     justify="left", wraplength=660, font=font(9))\
                .pack(anchor="w", padx=22, pady=(2, 11))

        # Video is the same feed with no depth buffer: a D3D11 player set up
        # once, after which any file or a YouTube link plays through DLSS 5.
        vcard = tk.Frame(f, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        vcard.pack(fill="x", pady=(10, 0))
        vi = tk.Frame(vcard, bg=PANEL)
        vi.pack(fill="x", padx=16, pady=12)
        vtop = tk.Frame(vi, bg=PANEL)
        vtop.pack(fill="x")
        tk.Label(vtop, text="video and youtube", bg=PANEL, fg=TXT,
                 font=font(10, "bold")).pack(side="left")
        tk.Label(vtop, text="new", bg=PANEL, fg=AMBER, font=font(8))\
            .pack(side="left", padx=10)
        ttk.Button(vtop, text="set up the video player",
                   command=self._video_setup).pack(side="right")
        self.videolbl = tk.Label(
            vi, bg=PANEL, fg=DIM, font=font(9), justify="left", anchor="w",
            wraplength=660,
            text="a portable MPC-HC in a folder of your choice, with dlss5 fed "
                 "into it. play any file, or File > Open URL with a youtube "
                 "link - it streams live, nothing is downloaded. F6 switches "
                 "neural rendering on and off while it plays. tested: 60 fps, "
                 "the feed costs about 5% of the frame.")
        self.videolbl.pack(anchor="w", pady=(4, 0))
        vi.bind("<Configure>",
                lambda e: self.videolbl.configure(wraplength=max(380, e.width - 10)))

        # What the publishers ship right now - the tool always fetches these.
        self.boardlbl = tk.Label(f, text="", bg=BG, fg=DIM, font=font(8),
                                 anchor="w", justify="left", wraplength=680)
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
            wraplength=680,
            text="dlss5 works reliably on 64-bit directx 11/12. directx 9, "
                 "opengl, vulkan and every 32-bit game go through extra "
                 "translation, a layer or a helper process, and the dlss "
                 "feature fails to create there far more often. directx 10 is "
                 "not supported by anything. each game is labelled honestly - "
                 "do not expect the long shots to work.\n\nnever use any of "
                 "this online: anti-cheat flags reshade add-ons.")
        self.realitylbl.pack(anchor="w", pady=(6, 0))
        wi.bind("<Configure>",
                lambda e: self.realitylbl.configure(wraplength=max(380, e.width - 10)))
        return f

    # ---------------------------------------------------------------- step 2
    def _page_games(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
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
        self.only_installed = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="installed only", variable=self.only_installed,
                       command=self._fill, bg=BG, fg=BODY, selectcolor=FIELD,
                       activebackground=BG, activeforeground=TXT,
                       font=font(9), borderwidth=0)\
            .pack(side="right", padx=(0, 14))

        # A library of two hundred games with no way to search reads as "the
        # list is broken" - the only filter here used to be 32/64-bit.
        srow = tk.Frame(f, bg=BG)
        srow.pack(fill="x", pady=(10, 0))
        ttk.Label(srow, text="search", style="Dim.TLabel").pack(side="left")
        ent = tk.Entry(srow, textvariable=self.search, bg=FIELD, fg=TXT,
                       insertbackground=AMBER, relief="flat", font=font(10),
                       highlightthickness=1, highlightbackground=LINE,
                       highlightcolor=EDGE)
        ent.pack(side="left", fill="x", expand=True, padx=(10, 0), ipady=3)
        ent.bind("<Escape>", lambda e: self.search.set(""))
        ent.bind("<Return>", lambda e: self._focus_first())
        ent.bind("<Down>", lambda e: self._focus_first())
        self.searchbox = ent

        self.scanlbl = ttk.Label(f, text="", style="Dim.TLabel")
        self.scanlbl.pack(anchor="w", pady=(6, 10))

        wrap = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        cols = ("source", "arch", "api", "route", "outlook", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings", height=13)
        self.tree.heading("#0", text="  game")
        self.tree.column("#0", width=250, anchor="w")
        for c, t, w in (("source", "source", 76), ("arch", "arch", 62),
                        ("api", "api", 80), ("route", "route", 74),
                        ("outlook", "outlook", 96), ("status", "status", 92)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
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
        det.pack(fill="x", pady=(10, 0))
        self.detail = tk.Label(det.inner, text="select a game for details",
                               bg=PANEL, fg=FAINT, font=font(9),
                               anchor="w", justify="left")
        self.detail.pack(fill="x")
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

    def _scan(self) -> None:
        if self.busy:
            return
        self.busy = True
        self._rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.scanlbl.config(text="scanning...")
        self.btn_next.config(state="disabled")

        def work() -> None:
            try:
                gs = games.scan_all(progress=lambda m: self.q.put(("scan", m)))
                self.q.put(("scanned", gs))
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
                self.q.put(("board", "components today:  " + "  |  ".join(parts)))
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
                    self.q.put(("scan", f"{g.name}: {type(e).__name__}"))
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
            if row is None:
                # Each of these reads the game folder, so any one of them can
                # fail on a folder that has gone away or become unreadable.
                # Letting that escape would abandon the whole list half-drawn.
                try:
                    ok, _ = installer.check_supported(g)
                    sup = dlss.detect(g.install_dir, g.folder, g.api,
                                      g.bitness or 0, self._sm())
                    level, _ = installer.reliability(g, sup.recommended)
                    outlook = {installer.STABLE: "reliable",
                               installer.BETA: "beta",
                               installer.EXPERIMENTAL: "often fails"}[level]
                    ac = anticheat.detect(g.install_dir, g.folder)
                    row = (ok, sup.recommended, level, outlook,
                           ac.present, ac.summary)
                except Exception as e:
                    log.exception(f"inspecting {g.name}", e)
                    row = False
                self._rows[key] = row
            if row is False:
                self.tree.insert("", "end", iid=str(i), text="  " + g.name,
                                 values=(g.source.lower(), g.bit_label, g.api,
                                         "-", "-", "unreadable"),
                                 tags=("unsupported",))
                continue
            ok, route, level, outlook, ac_present, ac_summary = row
            if not ok:
                status, tag, outlook = "unsupported", "unsupported", "-"
            elif ac_present:
                status, tag, outlook = f"{ac_summary}!", "unsupported", "blocked"
            elif g.installed:
                # Read fresh every time: this changes on install and uninstall.
                n_stale = self.stale.get(str(g.install_dir), 0)
                if n_stale:
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
            elif self.all_games:
                msg = ("no games match this filter  ::  set architecture to "
                       "'all', or use [choose folder]")
            else:
                msg = ("no games found  ::  use [choose folder] and point at "
                       "the game's own folder - see the log for what each "
                       "store returned")
        self.scanlbl.config(text=msg)
        self.game = None
        self.detail.config(text="select a game for details", fg=FAINT)
        self.btn_next.config(state="disabled")

    def _on_pick(self, _evt=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        g = self.shown[int(sel[0])]
        self.game = g
        ok, why = installer.check_supported(g)
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
                         + ("  +  dgvoodoo2" if g.api == "DX9" else "")
                         + ("  +  dxvk (this game quits when reshade hooks it)"
                            if need else ""))
            if level != installer.STABLE:
                lines.append(f"note   {why_rel}")
        else:
            lines.append(f"note   {why}")
        ac = anticheat.detect(g.install_dir, g.folder)
        if ac.present:
            lines.append(f"BLOCK  {ac.summary} is installed here - ReShade "
                         f"add-ons will be blocked or get you banned")
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

    # ---------------------------------------------------------------- step 3
    def _page_install(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        self.gamelbl = ttk.Label(f, text="", style="H1.TLabel")
        self.gamelbl.pack(anchor="w")
        self.pathlbl = ttk.Label(f, text="", style="Dim.TLabel")
        self.pathlbl.pack(anchor="w", pady=(4, 12))

        card = self._card(f)
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

        self.routelbl = tk.Label(inner, bg=PANEL, fg=DIM, font=font(8),
                                 justify="left", anchor="w", wraplength=680)
        self.routelbl.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        inner.bind("<Configure>",
                   lambda e: self.routelbl.configure(wraplength=max(360, e.width - 8)),
                   add="+")

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
        tk.Checkbutton(inner, text="keep the game's own", variable=self.keep_dlss,
                       bg=PANEL, fg=BODY, selectcolor=FIELD, activebackground=PANEL,
                       activeforeground=TXT, font=font(9), borderwidth=0)\
            .grid(row=6, column=2, sticky="w", padx=(10, 0))

        tk.Frame(inner, bg=LINE, height=1).grid(row=7, column=0, columnspan=3,
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
                                showvalue=True, font=font(9), length=230,
                                sliderlength=22, sliderrelief="raised",
                                activebackground=SLIDER_HOT,
                                command=self._on_workres)
        self.sc_work.pack(side="left")
        self.workhint = tk.Label(wrap, text="", bg=PANEL, fg=DIM, font=font(8),
                                 justify="left", wraplength=340)
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
        self.nrhint = tk.Label(inner, text="the rest is on the overlay (Insert)",
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

        # Some D3D11 games quit the moment ReShade hooks them (MGS V). Through
        # DXVK they render on Vulkan and ReShade loads as a layer instead.
        self.ck_dxvk = tk.Checkbutton(
            inner, text="run the game through DXVK (D3D11/D3D9 -> Vulkan): for "
                        "games that close when ReShade loads inside them (MGS V); "
                        "on DX9 an experimental alternative to dgVoodoo2",
            variable=self.dxvk, bg=PANEL, fg=DIM, selectcolor=FIELD,
            activebackground=PANEL, activeforeground=TXT, font=font(8),
            borderwidth=0, command=lambda: self._set_pathlbl(self.game))

        self.reswarn = tk.Label(
            inner, bg=PANEL, fg=RUST, font=font(8), justify="left", anchor="w",
            wraplength=680,
            text="!! set your screen resolution BEFORE turning neural rendering "
                 "on. the feature is created for one backbuffer size; changing "
                 "resolution or display mode while it runs forces a rebuild that "
                 "can freeze or crash the game.")
        self.reswarn.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        inner.bind("<Configure>",
                   lambda e: self.reswarn.configure(wraplength=max(360, e.width - 8)))

        # The video player's link box: paste, play. Packed in _enter_install.
        self.urlrow = tk.Frame(f, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
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

        barwrap = tk.Frame(f, bg=BG)
        barwrap.pack(fill="x", pady=(12, 2))
        self.pb = ttk.Progressbar(barwrap, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        self.pblbl = tk.Label(f, text="", bg=BG, fg=DIM, font=font(8), anchor="w")
        self.pblbl.pack(fill="x")

        # Packed to the bottom BEFORE the log, so a growing log can never push
        # these buttons out of the window.
        act = tk.Frame(f, bg=BG)
        act.pack(side="bottom", fill="x", pady=(9, 0))
        self.btn_diag = ttk.Button(act, text="did it work?", command=self._diagnose)
        self.btn_diag.pack(side="left")
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

        logwrap = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        logwrap.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(logwrap, bg=PANEL, fg=BODY, insertbackground=BODY,
                           font=font(9), borderwidth=0, height=14,
                           wrap="word", state="disabled", spacing1=1)
        lsb = ttk.Scrollbar(logwrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=12, pady=10)
        lsb.pack(side="right", fill="y")
        self.log.tag_configure("ok", foreground=GREEN)
        self.log.tag_configure("err", foreground=RED)
        self.log.tag_configure("warn", foreground=RUST)
        self.log.tag_configure("head", foreground=AMBER)
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
            self._on_workres()
        else:
            self.workres.set(100)
            # Disabled, but still legible: the reason is in the hint next to it.
            self.sc_work.configure(state="disabled", fg=DIM, bg=FAINT,
                                   troughcolor=LINE)
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
        g.exe = g.candidates[i]
        g.emu = None
        games.enrich(g)
        self._set_pathlbl(g)
        self._sync_workres()
        self._log(f"> target exe -> {g.exe.name}  ({g.bit_label} {g.api}); "
                  f"installing into {g.install_dir}", "head")
        ok, why = installer.check_supported(g)
        if not ok:
            self._log(f"  not supported: {why}", "err")
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
        text = dlss.BLURB[path]
        if not usable:
            text = f"NOT FOR THIS PC - {note}.\n{text}"
        elif note:
            text = f"{text}\n({note})"
        # What this route will not tolerate, in plain words, before INSTALL.
        for line in getattr(dlss, "CONFLICTS", {}).get(path, ()):
            text += "\n  !  " + line
        self.routelbl.config(text=text, fg=RUST if not usable else DIM)
        feeder = path == dlss.FEEDER
        opti = path == dlss.OPTI
        # The add-on dropdown lists the family this route installs.
        self._fill_addon_list(sf=path == dlss.RENODX)
        # Rows 9/10: the feeder's preset + hdr, or OptiScaler's preset + style.
        for w in (self.lbl_preset, self.cb_preset, self.lbl_hdr, self.cb_hdr,
                  self.dlaalbl, self.lbl_nrpreset, self.cb_nrpreset,
                  self.lbl_nrstyle, self.cb_nrstyle, self.nrhint,
                  self.lbl_feederver, self.cb_feederver, self.feederhint):
            w.grid_remove()
        if opti:
            self.lbl_nrpreset.grid(row=9, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_nrpreset.grid(row=9, column=1, columnspan=2, sticky="ew", pady=5)
            self.lbl_nrstyle.grid(row=10, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_nrstyle.grid(row=10, column=1, sticky="w", pady=5)
            self.nrhint.grid(row=10, column=2, sticky="w", padx=(10, 0))
        else:
            self.lbl_preset.grid(row=9, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_preset.grid(row=9, column=1, columnspan=2, sticky="ew", pady=5)
            self.lbl_hdr.grid(row=10, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_hdr.grid(row=10, column=1, sticky="w", pady=5)
            self.dlaalbl.grid(row=10, column=2, sticky="w", padx=(10, 0))
        if feeder:
            self.lbl_feederver.grid(row=11, column=0, sticky="w", padx=(0, 14), pady=5)
            self.cb_feederver.grid(row=11, column=1, columnspan=2, sticky="ew", pady=5)
            self.feederhint.grid(row=12, column=0, columnspan=3, sticky="w")
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
        self._sync_workres()
        if self.game:
            level, why = installer.reliability(self.game, path)
            self._log(f"> route: {dlss.LABELS[path]}  [{level}]", "head")
            self._log(f"  {why}")
            if not usable:
                self._log(f"  !! not for this pc: {note}", "warn")
            self._log(f"  plan: {' -> '.join(installer.plan(self.game, self._opts()))}")

    def _route_label(self, o: str) -> str:
        """One dropdown line: what it is, whether it fits, if it is the pick."""
        usable, note = self.route_fit.get(o, (True, ""))
        rec = self.support and o == self.support.recommended
        tail = "  <-  recommended for this game and card" if rec else ""
        if not usable:
            tail = f"  (not for this pc: {note})"
        return dlss.LABELS[o] + tail

    def _fill_addon_list(self, sf: bool) -> None:
        """The DLSS 5 add-on dropdown for the route: SF or renodx-dlss5."""
        fam = "renodx_sf" if sf else "renodx"
        vals = [e["label"] for e in self.catalog.get(fam, [])]
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
        body = diagnose.issue_body(
            update.VERSION, name, sm, drv, g, getattr(self, "route", "-"),
            self._last_diag, log.tail(60, 6000), log.path(), install_dir,
            last_error=log.last_error())
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
        if self.dxvk.get() and g.api in dxvk.APIS:
            extra += "  +  dxvk (vulkan)"
        elif g.api == "DX9":
            extra += "  +  dgvoodoo2"
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

    def _enter_install(self) -> None:
        g = self.game
        self.gamelbl.config(text=g.name)
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
            self._log("!! no nvidia card detected - dlss5 will not run", "warn")

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

    def _on_feederver(self, _e=None) -> None:
        i = self.cb_feederver.current()
        if i >= 2:
            self._log(f"> feeder pinned to {self.feeder_tags[i - 2]} - the "
                      f"matching DLSS 5 add-on build is chosen for it")

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
        if sources.last_fallback:
            self._log(f"!! {sources.last_fallback}", "warn")
        self._log(f"> versions: renodx {len(ren)}, dlssnr {len(nr)}, dlss {len(ds)}")

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
            keep_game_dlss=self.keep_dlss.get(),
            feed=feed,
            nr=nr,
            feeder_prerelease=self.cb_feederver.current() == 1,
            feeder_tag=(self.feeder_tags[self.cb_feederver.current() - 2]
                        if self.cb_feederver.current() >= 2 else ""),
            dxvk=self.dxvk.get(),
            path=getattr(self, 'route', dlss.FEEDER),
            native_dlss=bool(self.support and self.support.native_dlss),
            upscaler=str(getattr(self.support, 'upscaler', '') or ''),
            opti_proxy=("" if self.cb_proxy.current() <= 0
                        else optiscaler.PROXY_NAMES[self.cb_proxy.current() - 1]),
            reshade_proxy=("" if self.cb_rproxy.current() <= 0
                           else installer.RESHADE_PROXIES[self.cb_rproxy.current() - 1]),
        )

    # ---------------------------------------------------------------- actions
    def _install(self) -> None:
        if self.busy or not self.game:
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
            except Exception:
                log.exception("uninstalling")
                self.q.put(("fail", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def _back(self) -> None:
        if self.busy:
            return
        self._show(max(1, self.step - 1))

    def _next(self) -> None:
        if self.busy:
            return
        if self.step == 1:
            self._show(2)
            if not self.all_games:
                self._scan()
            else:
                self._fill()
        elif self.step == 2:
            if not self.game:
                return
            self._show(3)
            self._enter_install()
        else:
            self._install()

    # ---------------------------------------------------------------- queue
    def _log(self, text: str, tag: str = "") -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def _idle(self) -> None:
        self.busy = False
        self.btn_next.config(
            state="normal",
            text="INSTALL" if self.step == 3
            else ("continue" if self.step == 2 else "scan games"))
        self.btn_back.config(state="normal" if self.step > 1 else "disabled")

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "scan":
                    self.scanlbl.config(text=payload.lower())
                    self.status.config(text=payload.lower())
                elif kind == "scanned":
                    self.busy = False
                    self.all_games = payload
                    kp = video.known()
                    if kp and not any(x.install_dir == kp.install_dir
                                      for x in payload):
                        self.all_games.insert(0, kp)
                    self._rows.clear()
                    self._fill()
                    self.status.config(text="scan complete")
                    self._check_stale()
                elif kind == "update":
                    self._show_banner(*payload)
                elif kind == "update_ready":
                    self._update_ready(*payload)
                elif kind == "stale":
                    self.stale = payload
                    if self.step == 2 and self.all_games:
                        self._fill()
                elif kind == "board":
                    self.boardlbl.config(text=payload)
                elif kind == "updated_all":
                    self.busy = False
                    self._rows.clear()
                    self._fill()
                    self.btn_next.config(state="normal")
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
                    self._rows.clear()
                    self._log(f"> uninstalled ({len(payload)} items)", "ok")
                    self.btn_remove.config(state="disabled")
                    if hasattr(self, "btn_rm2"):
                        self.btn_rm2.config(state="disabled")
                    if self.step == 2:
                        self._fill()
                elif kind in ("fail", "error"):
                    self._idle()
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
        self._rows.clear()
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
            self._log("   1. press Insert to open the optiscaler overlay")
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
            self._log("   1. press Home to open reshade, then the RenoDX DLSS tab")
            self._log("   2. neural rendering is enabled; the tab shows its status "
                      "and lets you tune intensity and style")
            self._log("   3. turn OFF the game's own MSAA/SSAA")
        elif route == getattr(dlss, "STANDALONE", "standalone"):
            self._log("   1. in the game turn OFF its own DLSS, frame generation "
                      "and anti-aliasing - this add-on brings all three")
            self._log("   2. it shows the result in its own window on top; set "
                      "resolution and display mode BEFORE starting, changes "
                      "need a restart")
            self._log("   3. press Home for reshade, then the 'Standalone DLSS-NR + SR' "
                      "tab: neural rendering and frame generation toggle there")
            self._log("   4. F10 flips between the processed and the original picture")
            self._log("   !  a lower in-game resolution than your monitor = DLSS "
                      "super resolution up to native; same resolution = DLAA")
        elif route == getattr(dlss, "UPSTREAM", "upstream"):
            self._log("   !  reshade's overlay will say 'no .fx files found' - "
                      "normal on this route, it uses no shaders")
            self._log("   1. keep the game's own DLSS ON - the network runs "
                      "before it, at render resolution")
            self._log("   2. press Home to open reshade, then the 'NR Pre-Upscale' "
                      "tab: strength and cadence live there")
            self._log("   3. using DLSS Frame Generation? set cadence to Quality "
                      "(every frame) or it stutters")
            self._log("   4. turn OFF the game's own MSAA/SSAA")
        elif route in (dlss.NATIVE, dlss.BRIDGE):
            self._log("   !  reshade's overlay will say 'no .fx files found' - "
                      "normal on this route, it uses no shaders; the add-on "
                      "tabs are what matter")
            self._log("   1. press Home to open reshade, then the DLSS 5 tab")
            self._log("   2. turn on neural rendering there (F5 toggles it in the "
                      "4.6+ builds)")
            self._log("   3. keep the game's dlss ON - the add-on hooks it")
            self._log("   4. turn OFF the game's own MSAA/SSAA")
        else:
            self._log("   1. press Home to open reshade")
            p = reshade_ini.PROVIDERS[self.provider.get()]
            if p[1]:
                self._log(f"   2. tick '{p[0]}' and 'DLSS 5 Feed', provider ABOVE "
                          f"the feed")
            else:
                self._log("   2. put your provider's technique ABOVE DLSS 5 Feed")
            self._log("   3. turn on neural rendering in the DLSS 5 panel")
            self._log("   4. turn OFF the game's own MSAA/SSAA")
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


def _dark_titlebar(win) -> None:
    """Paint the Windows title bar in the app's own colours.

    Tk draws only the client area; Windows draws the frame in the system
    theme, so a dark app came with a white strip on top. DWM attributes
    since Windows 10 20H1 (dark mode) and Windows 11 (caption / text
    colour) fix that; on anything older the calls fail and nothing changes.
    """
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
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
    log.write("closed normally")
    return 0
