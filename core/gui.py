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

from . import (anticheat, components, diagnose, dlss, feedcfg, games, gpu,
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
FAINT   = "#3a3d43"     # decoration
AMBER   = "#d8a657"     # accent
GREEN   = "#6f9f6f"
RUST    = "#b07a3c"     # warnings
RED     = "#c96a5a"

# Cascadia ships with Windows Terminal and VS; Consolas is on every Windows.
MONO = ("Cascadia Mono", "Consolas", "Courier New")


def font(size: int = 10, weight: str = "normal") -> tuple:
    return (MONO[0], size, weight)


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
        r.geometry("1060x830")
        r.minsize(980, 720)
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
        self.gpulbl = tk.Label(rail, text="", bg=RAIL, fg=FAINT, anchor="w",
                               justify="left", font=font(8), wraplength=196)
        self.gpulbl.pack(fill="x", padx=20, pady=(0, 6))
        self.verlbl = tk.Label(rail, text=f"v{update.VERSION}", bg=RAIL, fg=FAINT,
                               anchor="w", font=font(8))
        self.verlbl.pack(fill="x", padx=20, pady=(0, 4))
        # A bug report needs something to attach. This opens the log file so
        # it can be pasted somewhere useful.
        self.loglink = tk.Label(rail, text="[ open log file ]", bg=RAIL, fg=FAINT,
                                anchor="w", cursor="hand2", font=font(8))
        self.loglink.pack(fill="x", padx=20, pady=(0, 2))
        self.loglink.bind("<Button-1>", lambda e: self._open_log())
        self.buglink = tk.Label(rail, text="[ report a bug ]", bg=RAIL, fg=FAINT,
                                anchor="w", cursor="hand2", font=font(8))
        self.buglink.pack(fill="x", padx=20, pady=(0, 2))
        self.buglink.bind("<Button-1>", lambda e: self._report_bug())
        self.howlink = tk.Label(rail, text="[ how it works ]", bg=RAIL, fg=FAINT,
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

        self.status = tk.Label(bar, text="ready", bg=BG, fg=FAINT,
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

        # What the publishers ship right now - the tool always fetches these.
        self.boardlbl = tk.Label(f, text="", bg=BG, fg=FAINT, font=font(8),
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
            lines.append(f"path   reshade as {proxy}"
                         + ("  +  host64/ helper" if g.bitness == 32 else "")
                         + ("  +  dgvoodoo2" if g.api == "DX9" else ""))
            if level != installer.STABLE:
                lines.append(f"note   {why_rel}")
        else:
            lines.append(f"note   {why}")
        ac = anticheat.detect(g.install_dir, g.folder)
        if ac.present:
            lines.append(f"BLOCK  {ac.summary} is installed here - ReShade "
                         f"add-ons will be blocked or get you banned")
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

        row(8, "work area")
        wrap = tk.Frame(inner, bg=PANEL)
        wrap.grid(row=8, column=1, columnspan=2, sticky="ew", pady=3)
        self.sc_work = tk.Scale(wrap, from_=50, to=100, resolution=5,
                                orient="horizontal", variable=self.workres,
                                bg=PANEL, fg=BODY, troughcolor=FIELD,
                                highlightthickness=0, borderwidth=0,
                                showvalue=True, font=font(8), length=210,
                                sliderrelief="flat", activebackground=AMBER,
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
        self.dlaalbl = tk.Label(inner, text="", bg=PANEL, fg=FAINT, font=font(8))
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
                               bg=PANEL, fg=FAINT, font=font(8))

        # The feeder's pre-releases carry support for the newer add-on builds.
        self.ck_feederpre = tk.Checkbutton(
            inner, text="use the feeder's pre-release build (supports the newer "
                        "DLSS 5 add-on generations; less tested)",
            variable=self.feeder_pre, bg=PANEL, fg=DIM, selectcolor=FIELD,
            activebackground=PANEL, activeforeground=TXT, font=font(8),
            borderwidth=0)

        self.reswarn = tk.Label(
            inner, bg=PANEL, fg=RUST, font=font(8), justify="left", anchor="w",
            wraplength=680,
            text="!! set your screen resolution BEFORE turning neural rendering "
                 "on. the feature is created for one backbuffer size; changing "
                 "resolution or display mode while it runs forces a rebuild that "
                 "can freeze or crash the game.")
        self.reswarn.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        inner.bind("<Configure>",
                   lambda e: self.reswarn.configure(wraplength=max(360, e.width - 8)))

        barwrap = tk.Frame(f, bg=BG)
        barwrap.pack(fill="x", pady=(12, 2))
        self.pb = ttk.Progressbar(barwrap, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        self.pblbl = tk.Label(f, text="", bg=BG, fg=FAINT, font=font(8), anchor="w")
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

        logwrap = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        logwrap.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(logwrap, bg=PANEL, fg=BODY, insertbackground=BODY,
                           font=font(9), borderwidth=0, height=9,
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
            self.sc_work.configure(state="normal", fg=BODY, troughcolor=FIELD)
            self._on_workres()
        else:
            self.workres.set(100)
            self.sc_work.configure(state="disabled", fg=FAINT, troughcolor=FIELD)
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
        self.routelbl.config(text=text, fg=RUST if not usable else DIM)
        feeder = path == dlss.FEEDER
        opti = path == dlss.OPTI
        # The add-on dropdown lists the family this route installs.
        self._fill_addon_list(sf=path == dlss.RENODX)
        # Rows 9/10: the feeder's preset + hdr, or OptiScaler's preset + style.
        for w in (self.lbl_preset, self.cb_preset, self.lbl_hdr, self.cb_hdr,
                  self.dlaalbl, self.lbl_nrpreset, self.cb_nrpreset,
                  self.lbl_nrstyle, self.cb_nrstyle, self.nrhint,
                  self.ck_feederpre):
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
            self.ck_feederpre.grid(row=11, column=0, columnspan=3, sticky="w",
                                   pady=(4, 0))
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
        self.cb_preset.configure(state="readonly" if feeder else "disabled")
        self.dlaalbl.config(
            text="the feeder path is always dlaa" if feeder
            else "the game's own dlss quality mode applies")
        self.reswarn.grid() if feeder else self.reswarn.grid_remove()
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
        diag = ""
        d = self._last_diag
        if d is not None:
            try:
                diag = f"\n**Diagnosis**: {d.verdict}\n" + "".join(
                    f"- [{f_.level}] {f_.title}\n" for f_ in d.findings)
            except Exception:
                diag = ""
        err = log.last_error()
        body = (
            "**What happened**\n\n\n"
            "**What I expected**\n\n\n"
            "---\n"
            f"- version: {update.VERSION}\n"
            f"- gpu: {name} (sm_{sm}), driver {drv}\n"
            f"- game: {g.name if g else '-'}\n"
            f"- exe: {g.exe.name if g and g.exe else '-'}\n"
            f"- arch/api: {g.bit_label if g else '-'} / {g.api if g else '-'}\n"
            f"- route: {getattr(self, 'route', '-')}\n"
            + diag
            + (f"\n**Last error**\n```\n{err[-1500:]}\n```\n" if err else "")
            + f"\n**Log tail** (`{log.path()}`)\n```\n{log.tail(30, 2500)}\n```\n")
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
        extra = "  +  host64/ helper" if g.bitness == 32 else ""
        if g.api == "DX9":
            extra += "  +  dgvoodoo2"
        # Long install paths ran off the right edge; show the path relative to
        # the game folder instead, the full one is in the log.
        try:
            short = str(g.exe.relative_to(g.folder))
        except ValueError:
            short = g.exe.name
        self.pathlbl.config(
            text=f"{short}   ::   {g.bit_label} {g.api}  ->  "
                 f"reshade = {installer._proxy_name(g.api, self._opts().reshade_proxy)}{extra}")

    def _enter_install(self) -> None:
        g = self.game
        self.gamelbl.config(text=g.name)
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
        self.route_fit = {o: dlss.fit(o, g.api, self.support.native_dlss, sm)
                          for o in self.support.options}
        self.cb_route["values"] = [self._route_label(o) for o in self.support.options]
        self.cb_route.current(self.support.options.index(self.support.recommended))
        if self.support.native_dlss:
            self._log(f"> this game ships its own dlss "
                      f"({', '.join(self.support.evidence[:3])})", "ok")
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
        threading.Thread(target=work, daemon=True).start()

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
        return installer.Options(
            provider=self.provider.get(),
            renodx=None if local else clean(val),
            renodx_local=local,
            dlssnr=clean(self.cb_dlssnr.get()),
            dlss=clean(self.cb_dlss.get()),
            keep_game_dlss=self.keep_dlss.get(),
            feed=feed,
            nr=nr,
            feeder_prerelease=self.feeder_pre.get(),
            path=getattr(self, 'route', dlss.FEEDER),
            native_dlss=bool(self.support and self.support.native_dlss),
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
        self._log("> now launch the game and:", "head")
        route = getattr(self, "route", dlss.FEEDER)
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
            self._log("   1. press Home to open reshade, then the RenoDX DLSS tab")
            self._log("   2. neural rendering is enabled; the tab shows its status "
                      "and lets you tune intensity and style")
            self._log("   3. turn OFF the game's own MSAA/SSAA")
        elif route in (dlss.NATIVE, dlss.BRIDGE):
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
        if self.sm is not None and self.sm < 89:
            self._log("!! rtx 20/30: the pass is heavy on your card. if the fps "
                      "drop is too much, lower the work area / model resolution "
                      "or turn v-sync off.", "warn")
        self._log("")
        self._log("> played it? come back and press 'did it work?' - it reads the "
                  "logs and tells you what happened.", "head")
        self.btn_remove.config(state="normal")
        self.status.config(text="install complete")


def run() -> int:
    log.start(update.VERSION)
    root = tk.Tk()
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
