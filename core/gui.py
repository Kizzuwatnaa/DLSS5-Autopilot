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

from . import (diagnose, dlss, feedcfg, games, gpu, installer, prefs,
               reshade_ini, selfupdate, sources, update)

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
        self.all_games: list[games.Game] = []
        self.shown: list[games.Game] = []
        self.game: games.Game | None = None
        self.catalog: dict[str, list[dict]] = {}
        self.renodx_local: Path | None = None
        self.update_url: str | None = None
        self.support: dlss.Support | None = None

        self.provider = tk.IntVar(value=3)
        self.keep_dlss = tk.BooleanVar(value=True)
        self.workres = tk.IntVar(value=100)

        self._build()
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
        self.verlbl.pack(fill="x", padx=20, pady=(0, 18))

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
        def work() -> None:
            newer, latest, url = update.check()
            if newer:
                self.q.put(("update", (latest, url)))
        threading.Thread(target=work, daemon=True).start()

    def _show_banner(self, latest: str, url: str) -> None:
        self.update_url = url
        self.bannerlbl.config(text=f"> version {latest} is out "
                                   f"(you are on {update.VERSION})")
        self.banner.pack(fill="x", before=self.body)

    def _do_update(self) -> None:
        if self.busy:
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

        warn = tk.Frame(f, bg=PANEL, highlightbackground=RUST, highlightthickness=1)
        warn.pack(fill="x", pady=(16, 0))
        wi = tk.Frame(warn, bg=PANEL)
        wi.pack(fill="x", padx=16, pady=13)
        tk.Label(wi, text="!! before you get your hopes up", bg=PANEL, fg=RUST,
                 font=font(10, "bold")).pack(anchor="w")
        self.realitylbl = tk.Label(
            wi, bg=PANEL, fg=DIM, font=font(9), justify="left", anchor="w",
            wraplength=680,
            text="dlss5 feeding is built around directx 10/11/12 and works "
                 "reliably there. directx 9, opengl and every 32-bit game go "
                 "through extra translation or a helper process, and the dlss "
                 "feature frequently fails to create on those paths. they are "
                 "offered here and labelled honestly - do not expect them to "
                 "work.\n\nnever use any of this online: anti-cheat flags "
                 "reshade add-ons.")
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
        self.tree.delete(*self.tree.get_children())
        self.scanlbl.config(text="scanning...")
        self.btn_next.config(state="disabled")

        def work() -> None:
            try:
                gs = games.scan_all(progress=lambda m: self.q.put(("scan", m)))
                self.q.put(("scanned", gs))
            except Exception:
                self.q.put(("error", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def _fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        a = self.arch.get()
        self.shown = [g for g in self.all_games
                      if g.exe and (a == "all" or str(g.bitness) == a)]
        for i, g in enumerate(self.shown):
            ok, _ = installer.check_supported(g)
            sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0)
            level, _ = installer.reliability(g, sup.recommended)
            outlook = {installer.STABLE: "reliable", installer.BETA: "beta",
                       installer.EXPERIMENTAL: "often fails"}[level]
            if not ok:
                status, tag, outlook = "unsupported", "unsupported", "-"
            elif g.installed:
                status, tag = "installed", "installed"
            else:
                status = "ready"
                tag = "shaky" if level == installer.EXPERIMENTAL else ""
            self.tree.insert("", "end", iid=str(i), text="  " + g.name,
                             values=(g.source.lower(), g.bit_label, g.api,
                                     sup.recommended, outlook, status),
                             tags=(tag,) if tag else ())
        hidden = len([g for g in self.all_games if not g.exe])
        msg = f"{len(self.shown)} games"
        if a != "all":
            msg += f"  ::  {a}-bit filter"
        if hidden:
            msg += f"  ::  {hidden} folders had no executable"
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
        sup = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0)
        level, why_rel = installer.reliability(g, sup.recommended)
        proxy = installer._proxy_name(g.api)
        lines = [f"exe    {g.exe}",
                 f"arch   {g.bit_label}  api {g.api}  ({g.api_why})",
                 f"route  {dlss.LABELS[sup.recommended]}  [{level}]"]
        if ok:
            lines.append(f"path   reshade as {proxy}"
                         + ("  +  host64/ helper" if g.bitness == 32 else "")
                         + ("  +  dgvoodoo2" if g.api == "DX9" else ""))
            if level != installer.STABLE:
                lines.append(f"note   {why_rel}")
        else:
            lines.append(f"note   {why}")
        if getattr(g, "emu", None):
            lines.append(f"emu    {g.emu.renderer_hint}")
        self.detail.config(text="\n".join(lines),
                           fg=(DIM if level == installer.STABLE else RUST)
                           if ok else RED)
        self.btn_next.config(state="normal" if ok else "disabled")

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

        def row(r: int, label: str, colour: str = DIM) -> None:
            tk.Label(inner, text=label, bg=PANEL, fg=colour,
                     font=font(9)).grid(row=r, column=0, sticky="w",
                                        padx=(0, 14), pady=5)

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

        row(3, "motion vectors")
        self.cb_prov = ttk.Combobox(inner, state="readonly",
                                    values=[v[0] for v in reshade_ini.PROVIDERS.values()])
        self.cb_prov.current(0)
        self.cb_prov.grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)
        self.cb_prov.bind("<<ComboboxSelected>>", self._on_prov)

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

        row(9, "dlss preset")
        self.cb_preset = ttk.Combobox(inner, state="readonly",
                                      values=list(feedcfg.PRESETS.values()))
        self.cb_preset.current(0)
        self.cb_preset.grid(row=9, column=1, columnspan=2, sticky="ew", pady=5)

        row(10, "hdr")
        self.cb_hdr = ttk.Combobox(inner, state="readonly", width=18,
                                   values=list(feedcfg.HDR.values()))
        self.cb_hdr.current(0)
        self.cb_hdr.grid(row=10, column=1, sticky="w", pady=5)
        self.dlaalbl = tk.Label(inner, text="", bg=PANEL, fg=FAINT, font=font(8))
        self.dlaalbl.grid(row=10, column=2, sticky="w", padx=(10, 0))

        self.reswarn = tk.Label(
            inner, bg=PANEL, fg=RUST, font=font(8), justify="left", anchor="w",
            wraplength=680,
            text="!! set your screen resolution BEFORE turning neural rendering "
                 "on. the feature is created for one backbuffer size; changing "
                 "resolution or display mode while it runs forces a rebuild that "
                 "can freeze or crash the game.")
        self.reswarn.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(12, 0))
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

    # ------------------------------------------------------------ diagnose
    def _diagnose(self) -> None:
        """Read the game's own logs back and say what happened."""
        if not self.game:
            return
        rep = diagnose.analyse(self.game.install_dir)
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

    # ---------------------------------------------------------------- bits
    def _work_applies(self) -> bool:
        """work_resolution is honoured on the 64-bit D3D11 path only.

        The add-on's own log line is "settled D3D11 work resolution=..%"; on
        DX12, OpenGL and the 32-bit helper the value is simply ignored, so the
        slider is disabled rather than lying about what it does.
        """
        g = self.game
        if getattr(self, "route", dlss.FEEDER) != dlss.FEEDER:
            return False          # only the feeder has a work area at all
        return bool(g and g.bitness == 64 and g.api == "DX11")

    def _on_workres(self, _v=None) -> None:
        if not self._work_applies():
            return
        v = self.workres.get()
        if v == 100:
            self.workhint.config(text="100% - full quality", fg=DIM)
        elif v >= 80:
            self.workhint.config(text=f"{v}% - a little faster", fg=DIM)
        else:
            self.workhint.config(text=f"{v}% - faster, softer", fg=RUST)

    def _sync_workres(self) -> None:
        if self._work_applies():
            self.sc_work.configure(state="normal", fg=BODY, troughcolor=FIELD)
            self._on_workres()
        else:
            self.workres.set(100)
            self.sc_work.configure(state="disabled", fg=FAINT, troughcolor=FIELD)
            if getattr(self, "route", dlss.FEEDER) != dlss.FEEDER:
                self.workhint.config(
                    text="n/a on this route - the work area is a feeder setting",
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
        self.routelbl.config(text=dlss.BLURB[path], fg=DIM)
        feeder = path == dlss.FEEDER
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
            self._log(f"  plan: {' -> '.join(installer.plan(self.game, self._opts()))}")

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
                 f"reshade = {installer._proxy_name(g.api)}{extra}")

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

        # Work out which routes exist for this game and preselect the best.
        self.support = dlss.detect(g.install_dir, g.folder, g.api, g.bitness or 0)
        self.cb_route["values"] = [dlss.LABELS[o] for o in self.support.options]
        self.cb_route.current(self.support.options.index(self.support.recommended))
        if self.support.native_dlss:
            self._log(f"> this game ships its own dlss "
                      f"({', '.join(self.support.evidence[:3])})", "ok")
        self._log(f"> {self.support.reason}")
        self._apply_route(self.support.recommended)
        if len(cands) > 1:
            self._log(f"!! this folder has {len(cands)} executables; selected "
                      f"{g.exe.name}", "warn")
            self._log("   if the game launches a different one, change it above "
                      "or the install does nothing", "warn")

        card, sm = gpu.detect()
        if card:
            self.gpulbl.config(text=f"{card}\n{gpu.label(sm)}")
        else:
            self._log("!! no nvidia card detected - dlss5 will not run", "warn")

        self._find_local_renodx()
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
        keep = self.cb_renodx.get()
        self.cb_renodx["values"] = ([keep] if keep.startswith("[local]") else []) + ren
        if keep.startswith("[local]"):
            self.cb_renodx.set(keep)
        elif ren:
            self.cb_renodx.current(0)
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
        return installer.Options(
            provider=self.provider.get(),
            renodx=None if local else clean(val),
            renodx_local=local,
            dlssnr=clean(self.cb_dlssnr.get()),
            dlss=clean(self.cb_dlss.get()),
            keep_game_dlss=self.keep_dlss.get(),
            feed=feed,
            path=getattr(self, 'route', dlss.FEEDER),
            native_dlss=bool(self.support and self.support.native_dlss),
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
        self.btn_next.config(state="normal", text="INSTALL")
        self.btn_back.config(state="normal")

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
                    self._fill()
                    self.status.config(text="scan complete")
                elif kind == "update":
                    self._show_banner(*payload)
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
                elif kind == "done":
                    self._finish_ok(payload)
                elif kind == "removed":
                    self._idle()
                    self._log(f"> uninstalled ({len(payload)} items)", "ok")
                    self.btn_remove.config(state="disabled")
                elif kind in ("fail", "error"):
                    self._idle()
                    self._log(payload, "err")
                    self.pblbl.config(text="")
                    self.status.config(text="failed")
                    messagebox.showerror(APP, payload.strip().splitlines()[-1])
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def _finish_ok(self, rep: installer.Report) -> None:
        self._idle()
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
        self._log("   1. press Home to open reshade")
        p = reshade_ini.PROVIDERS[self.provider.get()]
        if p[1]:
            self._log(f"   2. tick '{p[0]}' and 'DLSS 5 Feed', provider ABOVE "
                      f"the feed")
        else:
            self._log("   2. put your provider's technique ABOVE DLSS 5 Feed")
        self._log("   3. turn on neural rendering in the DLSS 5 panel")
        self._log("   4. turn OFF the game's own MSAA/SSAA")
        self._log("")
        self._log("!! set your resolution BEFORE enabling neural rendering - the "
                  "feature is built for one backbuffer size and rebuilding it "
                  "mid-session can crash the game. use borderless, not exclusive "
                  "fullscreen.", "warn")
        self._log("")
        self._log("> played it? come back and press 'did it work?' - it reads the "
                  "logs and tells you what happened.", "head")
        self.btn_remove.config(state="normal")
        self.status.config(text="install complete")


def run() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
