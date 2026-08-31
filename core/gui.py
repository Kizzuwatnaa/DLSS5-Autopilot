"""tkinter interface - a three step wizard.

Step 1: architecture filter (64-bit / 32-bit / all)
Step 2: pick a game from the scan
Step 3: settings + install
"""
from __future__ import annotations

import queue
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import (feedcfg, games, gpu, installer, prefs, reshade_ini, sources,
               update)

APP = "DLSS 5 Autopilot"

BG      = "#14161a"
PANEL   = "#1c1f26"
PANEL2  = "#22262f"
LINE    = "#2e3440"
TXT     = "#e8eaed"
MUTED   = "#9aa0a6"
ACCENT  = "#4da3ff"
OK      = "#3ecf8e"
WARN    = "#ffb454"
ERR     = "#ff6b6b"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False

        self.arch = tk.StringVar(value="all")
        self.all_games: list[games.Game] = []
        self.shown: list[games.Game] = []
        self.game: games.Game | None = None
        self.catalog: dict[str, list[dict]] = {}
        self.renodx_local: Path | None = None

        self.provider = tk.IntVar(value=3)
        self.keep_dlss = tk.BooleanVar(value=True)
        self.workres = tk.IntVar(value=100)

        self._build()
        self._show(1)
        self.root.after(60, self._pump)
        self._check_update()

    # ------------------------------------------------------------- chrome
    def _build(self) -> None:
        r = self.root
        r.title(APP)
        r.geometry("980x800")
        r.minsize(900, 680)
        r.configure(bg=BG)

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TXT, fieldbackground=PANEL2,
                     bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TXT, font=("Segoe UI", 10))
        st.configure("H1.TLabel", font=("Segoe UI Semibold", 17), foreground=TXT)
        st.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        st.configure("MutedP.TLabel", background=PANEL, foreground=MUTED,
                     font=("Segoe UI", 9))
        st.configure("TButton", background=PANEL2, foreground=TXT, borderwidth=0,
                     focuscolor=PANEL2, padding=(14, 8), font=("Segoe UI", 10))
        st.map("TButton", background=[("active", LINE), ("disabled", PANEL)],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0b1220",
                     font=("Segoe UI Semibold", 10), padding=(18, 9))
        st.map("Accent.TButton", background=[("active", "#6cb6ff"), ("disabled", LINE)],
               foreground=[("disabled", MUTED)])
        st.configure("TRadiobutton", background=PANEL, foreground=TXT,
                     font=("Segoe UI", 10))
        st.map("TRadiobutton", background=[("active", PANEL)])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=TXT, rowheight=27, borderwidth=0, font=("Segoe UI", 10))
        st.configure("Treeview.Heading", background=PANEL2, foreground=MUTED,
                     borderwidth=0, font=("Segoe UI Semibold", 9))
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", "#0b1220")])
        st.map("Treeview.Heading", background=[("active", LINE)])
        st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                     foreground=TXT, arrowcolor=TXT, padding=6)
        st.map("TCombobox",
               fieldbackground=[("readonly", PANEL2), ("disabled", PANEL)],
               background=[("readonly", PANEL2)],
               foreground=[("readonly", TXT), ("disabled", MUTED)],
               selectbackground=[("readonly", PANEL2)],
               selectforeground=[("readonly", TXT)],
               arrowcolor=[("readonly", TXT)])
        r.option_add("*TCombobox*Listbox.background", PANEL2)
        r.option_add("*TCombobox*Listbox.foreground", TXT)
        r.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        r.option_add("*TCombobox*Listbox.selectForeground", "#0b1220")
        st.configure("TProgressbar", background=ACCENT, troughcolor=PANEL2,
                     borderwidth=0, thickness=8)

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=22, pady=(18, 6))
        logo = tk.Label(head, text="DLSS 5", bg=BG, fg=ACCENT,
                        font=("Segoe UI Black", 20), cursor="hand2")
        logo.pack(side="left")
        name = tk.Label(head, text="  Autopilot", bg=BG, fg=TXT,
                        font=("Segoe UI Light", 20), cursor="hand2")
        name.pack(side="left")
        for w in (logo, name):                      # clicking the logo goes home
            w.bind("<Button-1>", lambda e: self._home())
        self.steplbl = tk.Label(head, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.steplbl.pack(side="right")

        # Update banner, hidden until a newer release is found
        self.banner = tk.Frame(r, bg="#1e3a5f")
        self.bannerlbl = tk.Label(self.banner, text="", bg="#1e3a5f", fg=TXT,
                                  font=("Segoe UI", 9), anchor="w")
        self.bannerlbl.pack(side="left", padx=14, pady=7)
        close = tk.Label(self.banner, text="X", bg="#1e3a5f", fg=MUTED,
                         cursor="hand2", font=("Segoe UI", 9))
        close.pack(side="right", padx=(0, 12))
        close.bind("<Button-1>", lambda e: self.banner.pack_forget())
        self.bannerbtn = tk.Label(self.banner, text="Open download page >",
                                  bg="#1e3a5f", fg=ACCENT, cursor="hand2",
                                  font=("Segoe UI Semibold", 9))
        self.bannerbtn.pack(side="right", padx=14)

        tk.Frame(r, bg=LINE, height=1).pack(fill="x", padx=22, pady=(4, 0))

        # The footer is packed BEFORE the body and pinned to the bottom, so a
        # growing body can never clip the action button.
        foot = tk.Frame(r, bg=BG)
        foot.pack(side="bottom", fill="x", padx=22, pady=12)
        tk.Frame(r, bg=LINE, height=1).pack(side="bottom", fill="x", padx=22)

        self.body = tk.Frame(r, bg=BG)
        self.body.pack(fill="both", expand=True, padx=22, pady=12)

        self.p1 = self._page_arch()
        self.p2 = self._page_games()
        self.p3 = self._page_install()

        self.status = tk.Label(foot, text="Ready", bg=BG, fg=MUTED,
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.btn_home = ttk.Button(foot, text="Start over", command=self._home)
        self.btn_home.pack(side="left", padx=(0, 8))
        self.btn_back = ttk.Button(foot, text="< Back", command=self._back)
        self.btn_back.pack(side="left", padx=(0, 8))
        self.btn_next = ttk.Button(foot, text="Continue >", style="Accent.TButton",
                                   command=self._next)
        self.btn_next.pack(side="right")

        r.bind("<Escape>", lambda e: self._home())
        r.bind("<Control-h>", lambda e: self._home())

    def _home(self) -> None:
        """Return to step 1 from anywhere: logo, button, Esc or Ctrl+H."""
        if self.busy:
            return
        self._show(1)

    def _show(self, step: int) -> None:
        self.step = step
        for p in (self.p1, self.p2, self.p3):
            p.pack_forget()
        [self.p1, self.p2, self.p3][step - 1].pack(fill="both", expand=True)
        self.steplbl.config(text=f"Step {step} of 3   ·   Esc = start over")
        self.btn_back.config(state="normal" if step > 1 else "disabled")
        self.btn_home.config(state="normal" if step > 1 else "disabled")
        if step == 1:
            self.btn_next.config(text="Scan games >", state="normal")
        elif step == 2:
            self.btn_next.config(text="Continue >",
                                 state="normal" if self.game else "disabled")
        else:
            self.btn_next.config(text="INSTALL", state="normal")

    # ------------------------------------------------------------- updates
    def _check_update(self) -> None:
        def work() -> None:
            newer, latest, url = update.check()
            if newer:
                self.q.put(("update", (latest, url)))
        threading.Thread(target=work, daemon=True).start()

    def _show_banner(self, latest: str, url: str) -> None:
        self.bannerlbl.config(
            text=f"Version {latest} is available - you are running {update.VERSION}.")
        self.bannerbtn.bind("<Button-1>", lambda e: webbrowser.open(url))
        self.banner.pack(fill="x", padx=22, pady=(2, 0), before=self.body)

    # ------------------------------------------------------------- step 1
    def _page_arch(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        ttk.Label(f, text="Which architecture are you installing for?",
                  style="H1.TLabel").pack(anchor="w", pady=(10, 4))
        ttk.Label(f, text="If you do not know whether the game is 32-bit or 64-bit, "
                          "choose \"Show everything\" - the tool reads each game's "
                          "architecture itself.", style="Muted.TLabel")\
            .pack(anchor="w", pady=(0, 16))

        opts = [
            ("all", "Show everything",
             "List every game with its architecture beside it. Recommended."),
            ("64", "64-bit games only",
             "The standard path. ReShade and the DLSS 5 add-on go straight next "
             "to the game."),
            ("32", "32-bit games only",
             "A 32-bit process cannot load 64-bit NGX, so a host64 helper process "
             "is installed too. Experimental - it often fails to start."),
        ]
        for val, title, desc in opts:
            card = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            card.pack(fill="x", pady=5)
            ttk.Radiobutton(card, text=title, value=val, variable=self.arch)\
                .pack(anchor="w", padx=16, pady=(12, 2))
            ttk.Label(card, text=desc, style="MutedP.TLabel", wraplength=820,
                      justify="left").pack(anchor="w", padx=38, pady=(0, 12))

        note = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        note.pack(fill="x", pady=(14, 0))
        tk.Label(note, text="Reality check", bg=PANEL, fg=WARN,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=16, pady=(12, 2))
        tk.Label(note, bg=PANEL, fg=MUTED, font=("Segoe UI", 9), justify="left",
                 wraplength=870,
                 text="DLSS 5 feeding works reliably on DirectX 10/11/12 only. "
                      "DirectX 9, OpenGL and every 32-bit game go through extra "
                      "translation or a helper process, and the DLSS feature often "
                      "fails to create on those paths. They are offered here, but "
                      "expect them to be hit and miss.")\
            .pack(anchor="w", padx=16, pady=(0, 12))
        return f

    # ------------------------------------------------------------- step 2
    def _page_games(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        top = tk.Frame(f, bg=BG)
        top.pack(fill="x")
        ttk.Label(top, text="Pick a game", style="H1.TLabel").pack(side="left")
        ttk.Button(top, text="Choose folder...", command=self._pick_folder)\
            .pack(side="right", padx=(8, 0))
        ttk.Button(top, text="Rescan", command=self._scan).pack(side="right")

        self.scanlbl = ttk.Label(f, text="", style="Muted.TLabel")
        self.scanlbl.pack(anchor="w", pady=(4, 8))

        wrap = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        cols = ("source", "arch", "api", "outlook", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings", height=13)
        self.tree.heading("#0", text="GAME")
        self.tree.column("#0", width=290, anchor="w")
        for c, t, w in (("source", "SOURCE", 80), ("arch", "ARCH", 70),
                        ("api", "API", 90), ("outlook", "OUTLOOK", 110),
                        ("status", "STATUS", 110)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("installed", foreground=OK)
        self.tree.tag_configure("unsupported", foreground=ERR)
        self.tree.tag_configure("shaky", foreground=WARN)
        self.tree.bind("<<TreeviewSelect>>", self._on_pick)
        self.tree.bind("<Double-1>", lambda e: self._next())

        self.detail = tk.Label(f, text="", bg=BG, fg=MUTED, font=("Consolas", 9),
                               anchor="w", justify="left")
        self.detail.pack(fill="x", pady=(8, 0))
        return f

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Select the game folder")
        if not d:
            return
        g = games.manual(Path(d))
        if not g.exe:
            messagebox.showwarning(APP, f"No executable found in:\n{d}")
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
        self.scanlbl.config(text="Scanning...")
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
            level, _ = installer.reliability(g)
            outlook = {installer.STABLE: "reliable", installer.BETA: "beta",
                       installer.EXPERIMENTAL: "often fails"}[level]
            if not ok:
                status, tag, outlook = "Not supported", "unsupported", "-"
            elif g.installed:
                status, tag = "Installed", "installed"
            else:
                status = "Ready"
                tag = "shaky" if level == installer.EXPERIMENTAL else ""
            self.tree.insert("", "end", iid=str(i), text="  " + g.name,
                             values=(g.source, g.bit_label, g.api, outlook, status),
                             tags=(tag,) if tag else ())
        hidden = len([g for g in self.all_games if not g.exe])
        msg = f"{len(self.shown)} games listed"
        if a != "all":
            msg += f" ({a}-bit filter on)"
        if hidden:
            msg += f"  -  {hidden} folders had no executable (not installed)"
        self.scanlbl.config(text=msg)
        self.game = None
        self.detail.config(text="")
        self.btn_next.config(state="disabled")

    def _on_pick(self, _evt=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        g = self.shown[int(sel[0])]
        self.game = g
        ok, why = installer.check_supported(g)
        level, why_rel = installer.reliability(g)
        proxy = installer._proxy_name(g.api)
        lines = [f"exe   : {g.exe}",
                 f"arch  : {g.bit_label}   API: {g.api}  ({g.api_why})"]
        if ok:
            lines.append(f"path  : ReShade installs as '{proxy}'"
                         + ("   +  host64/ helper" if g.bitness == 32 else "")
                         + ("   +  dgVoodoo2 (DX9->D3D11)" if g.api == "DX9" else ""))
            if level != installer.STABLE:
                lines.append(f"NOTE  : {why_rel}")
        else:
            lines.append("NOTE  : " + why)
        if getattr(g, "emu", None):
            lines.append(f"EMU   : {g.emu.renderer_hint}")
        self.detail.config(text="\n".join(lines),
                           fg=(MUTED if level == installer.STABLE else WARN) if ok else ERR)
        self.btn_next.config(state="normal" if ok else "disabled")

    # ------------------------------------------------------------- step 3
    def _page_install(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        self.gamelbl = ttk.Label(f, text="", style="H1.TLabel")
        self.gamelbl.pack(anchor="w")
        self.pathlbl = ttk.Label(f, text="", style="Muted.TLabel")
        self.pathlbl.pack(anchor="w", pady=(2, 12))

        grid = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        grid.pack(fill="x")
        inner = tk.Frame(grid, bg=PANEL)
        inner.pack(fill="x", padx=16, pady=14)
        inner.columnconfigure(1, weight=1)

        def row(r: int, label: str) -> None:
            tk.Label(inner, text=label, bg=PANEL, fg=MUTED,
                     font=("Segoe UI", 9)).grid(row=r, column=0, sticky="w",
                                                padx=(0, 14), pady=5)

        self.exerow = tk.Label(inner, text="Target exe", bg=PANEL, fg=MUTED,
                               font=("Segoe UI", 9))
        self.cb_exe = ttk.Combobox(inner, state="readonly", values=[])
        self.cb_exe.bind("<<ComboboxSelected>>", self._on_exe)

        row(1, "Motion vectors")
        self.cb_prov = ttk.Combobox(inner, state="readonly",
                                    values=[v[0] for v in reshade_ini.PROVIDERS.values()])
        self.cb_prov.current(0)
        self.cb_prov.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        self.cb_prov.bind("<<ComboboxSelected>>", self._on_prov)

        row(2, "DLSS 5 add-on")
        self.cb_renodx = ttk.Combobox(inner, state="readonly", values=["loading..."])
        self.cb_renodx.grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Button(inner, text="Use my file...", command=self._pick_renodx)\
            .grid(row=2, column=2, sticky="w", padx=(8, 0), pady=5)

        row(3, "nvngx_dlssnr")
        self.cb_dlssnr = ttk.Combobox(inner, state="readonly",
                                      values=["Auto (match my GPU)"])
        self.cb_dlssnr.current(0)
        self.cb_dlssnr.grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)

        row(4, "nvngx_dlss")
        self.cb_dlss = ttk.Combobox(inner, state="readonly", values=["loading..."])
        self.cb_dlss.grid(row=4, column=1, sticky="ew", pady=5)
        tk.Checkbutton(inner, text="keep the game's own", variable=self.keep_dlss,
                       bg=PANEL, fg=TXT, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=TXT, font=("Segoe UI", 9), borderwidth=0)\
            .grid(row=4, column=2, sticky="w", padx=(8, 0))

        row(5, "Quality / speed")
        adv = tk.Frame(inner, bg=PANEL)
        adv.grid(row=5, column=1, columnspan=2, sticky="ew", pady=(10, 2))
        tk.Label(adv, text="Work area", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.sc_work = tk.Scale(adv, from_=50, to=100, resolution=5,
                                orient="horizontal", variable=self.workres,
                                bg=PANEL, fg=TXT, troughcolor=PANEL2,
                                highlightthickness=0, borderwidth=0, showvalue=True,
                                font=("Segoe UI", 8), length=230, sliderrelief="flat",
                                activebackground=ACCENT, command=self._on_workres)
        self.sc_work.grid(row=0, column=1, sticky="w", padx=(10, 10))
        self.workhint = tk.Label(adv, text="100% - full quality", bg=PANEL, fg=MUTED,
                                 font=("Segoe UI", 8))
        self.workhint.grid(row=0, column=2, sticky="w")

        tk.Label(adv, text="DLSS preset", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cb_preset = ttk.Combobox(adv, state="readonly", width=44,
                                      values=list(feedcfg.PRESETS.values()))
        self.cb_preset.current(0)
        self.cb_preset.grid(row=1, column=1, columnspan=2, sticky="w",
                            padx=(10, 0), pady=(8, 0))

        tk.Label(adv, text="HDR", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.cb_hdr = ttk.Combobox(adv, state="readonly", width=18,
                                   values=list(feedcfg.HDR.values()))
        self.cb_hdr.current(0)
        self.cb_hdr.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        tk.Label(adv, text="the feeder path is always DLAA", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).grid(row=2, column=2, sticky="w", pady=(8, 0))

        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", pady=(12, 4))
        self.pb = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        self.pblbl = tk.Label(f, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9),
                              anchor="w")
        self.pblbl.pack(fill="x")

        logwrap = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        logwrap.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(logwrap, bg=PANEL, fg=TXT, insertbackground=TXT,
                           font=("Consolas", 9), borderwidth=0, height=9,
                           wrap="word", state="disabled")
        lsb = ttk.Scrollbar(logwrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=10, pady=8)
        lsb.pack(side="right", fill="y")
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("head", foreground=ACCENT)

        act = tk.Frame(f, bg=BG)
        act.pack(fill="x", pady=(8, 0))
        self.btn_remove = ttk.Button(act, text="Uninstall", command=self._uninstall)
        self.btn_remove.pack(side="left")
        ttk.Button(act, text="Open game folder",
                   command=lambda: self.game and webbrowser.open(str(self.game.install_dir)))\
            .pack(side="left", padx=8)
        ttk.Button(act, text="Pick another game",
                   command=lambda: self._show(2)).pack(side="left")
        return f

    def _on_workres(self, _v=None) -> None:
        v = self.workres.get()
        if v == 100:
            self.workhint.config(text="100% - full quality", fg=MUTED)
        elif v >= 80:
            self.workhint.config(text=f"{v}% - a little faster", fg=MUTED)
        else:
            self.workhint.config(text=f"{v}% - noticeably faster, softer", fg=WARN)

    def _on_exe(self, _e=None) -> None:
        """A different executable was chosen: re-detect architecture and API."""
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
        self._log(f"target exe changed -> {g.exe.name}  ({g.bit_label} {g.api}); "
                  f"installing into {g.install_dir}", "head")
        ok, why = installer.check_supported(g)
        if not ok:
            self._log(f"  not supported: {why}", "err")
        self.btn_next.config(state="normal" if ok else "disabled")

    def _on_prov(self, _e=None) -> None:
        self.provider.set(list(reshade_ini.PROVIDERS.keys())[self.cb_prov.current()])

    def _pick_renodx(self) -> None:
        p = filedialog.askopenfilename(
            title="Select the renodx add-on you downloaded",
            filetypes=[("ReShade add-on", "*.addon64 *.addon"), ("All files", "*.*")])
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
        extra = "   +  host64/ helper" if g.bitness == 32 else ""
        if g.api == "DX9":
            extra += "   +  dgVoodoo2"
        self.pathlbl.config(
            text=f"{g.exe}     |     {g.bit_label}  {g.api}  ->  "
                 f"ReShade = {installer._proxy_name(g.api)}{extra}")

    def _enter_install(self) -> None:
        g = self.game
        self.gamelbl.config(text=g.name)
        self._set_pathlbl(g)

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

        level, why_rel = installer.reliability(g)
        if level != installer.STABLE:
            self._log(f"HEADS UP ({level}): {why_rel}", "warn")
        if len(cands) > 1:
            self._log(f"This folder has {len(cands)} executables. "
                      f"Selected: {g.exe.name}", "warn")
            self._log("  If the game launches a different one, change it above - "
                      "otherwise the install does nothing.", "warn")

        card, sm = gpu.detect()
        if card:
            self._log(f"Graphics card: {card}  ({gpu.label(sm)})", "head")
            self._log("The nvngx_dlssnr build is verified against this card after "
                      "download; incompatible builds are skipped automatically.")
        else:
            self._log("No NVIDIA card detected - DLSS 5 will not run.", "warn")

        self._log(f"Plan: {' -> '.join(installer.plan(g, self._opts()))}", "head")
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
        self._log(f"renodx: using your local build -> {found.name} "
                  f"({found.stat().st_size/1048576:.1f} MB)", "ok")
        self._log(f"   {found.parent}")
        if len(cands) > 1:
            self._log(f"   ({len(cands)-1} more local builds found; "
                      f"use 'Use my file...' to switch)")

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
        self.cb_dlssnr["values"] = ["Auto (match my GPU)"] + nr
        self.cb_dlssnr.current(0)
        self.cb_dlss["values"] = ds
        if ds:
            self.cb_dlss.current(0)
        self._log(f"Version list ready - renodx: {len(ren)}, dlssnr: {len(nr)}, "
                  f"dlss: {len(ds)}")

    def _opts(self) -> installer.Options:
        if not hasattr(self, "cb_renodx"):
            return installer.Options()
        val = self.cb_renodx.get()
        local = self.renodx_local if val.startswith("[local]") else None
        nr = self.cb_dlssnr.get()
        feed: dict = {}
        wr = self.workres.get()
        if wr != 100:
            feed["work_resolution"] = wr
        pi = self.cb_preset.current()
        if pi > 0:
            feed["preset"] = list(feedcfg.PRESETS.keys())[pi]
        hi = self.cb_hdr.current()
        if hi > 0:
            feed["hdr"] = list(feedcfg.HDR.keys())[hi]
        return installer.Options(
            provider=self.provider.get(),
            renodx=None if local else (val if val and not val.startswith("loading") else None),
            renodx_local=local,
            dlssnr=None if (not nr or nr.startswith("Auto")) else nr,
            dlss=self.cb_dlss.get() or None,
            keep_game_dlss=self.keep_dlss.get(),
            feed=feed,
        )

    # ------------------------------------------------------------- actions
    def _install(self) -> None:
        if self.busy or not self.game:
            return
        self.busy = True
        self.btn_next.config(state="disabled", text="Installing...")
        self.btn_back.config(state="disabled")
        self.btn_home.config(state="disabled")
        self.btn_remove.config(state="disabled")
        self.pb["value"] = 0
        g, opt = self.game, self._opts()
        self._log("")
        self._log(f"=== {g.name} - installing ===", "head")

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
                APP, f"{self.game.name}\n\nRemove the files this tool installed? "
                     f"The game's own files are restored and left alone."):
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

    # ------------------------------------------------------------- queue
    def _log(self, text: str, tag: str = "") -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "scan":
                    self.scanlbl.config(text=payload)
                    self.status.config(text=payload)
                elif kind == "scanned":
                    self.busy = False
                    self.all_games = payload
                    self._fill()
                    self.status.config(text="Scan complete")
                elif kind == "update":
                    self._show_banner(*payload)
                elif kind == "catalog":
                    self._fill_catalog(payload)
                elif kind == "caterr":
                    self._log(f"Could not fetch the version list: {payload}", "warn")
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
                    self.busy = False
                    self._finish_ok(payload)
                elif kind == "removed":
                    self.busy = False
                    self._log(f"Uninstalled ({len(payload)} items).", "ok")
                    self.btn_remove.config(state="disabled")
                    self.btn_next.config(state="normal", text="INSTALL")
                    self.btn_back.config(state="normal")
                    self.btn_home.config(state="normal")
                elif kind in ("fail", "error"):
                    self.busy = False
                    self._log(payload, "err")
                    self.pblbl.config(text="")
                    self.btn_next.config(state="normal", text="INSTALL")
                    self.btn_back.config(state="normal")
                    self.btn_home.config(state="normal")
                    self.status.config(text="Failed")
                    messagebox.showerror(APP, payload.strip().splitlines()[-1])
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def _finish_ok(self, rep: installer.Report) -> None:
        self.pb["value"] = 100
        self.pblbl.config(text="")
        self._log("")
        self._log(f"DONE - {len(rep.written)} files written.", "ok")
        for n in rep.notes:
            self._log(f"  - {n}")
        for w in rep.warnings:
            self._log(f"  ! {w}", "warn")
        if rep.skipped:
            self._log(f"  - left untouched: {', '.join(rep.skipped)}")
        self._log("")
        self._log("Now launch the game and:", "head")
        self._log("  1. Press Home to open ReShade")
        p = reshade_ini.PROVIDERS[self.provider.get()]
        if p[1]:
            self._log(f"  2. '{p[0]}' and 'DLSS 5 Feed' must both be ticked, "
                      f"with the provider ABOVE the feed")
        else:
            self._log("  2. Place your provider's technique ABOVE DLSS 5 Feed")
        self._log("  3. Turn on neural rendering in the DLSS 5 Neural Rendering panel")
        self._log("  4. Turn OFF the game's own MSAA/SSAA")
        self._log("")
        self._log("If it does not work, open dlss5-feed.log in the game folder. You "
                  "want to see 'feature ready ... DLAA' and 'frame N delivered'. "
                  "'CreateFeature raised exception' means the add-on and the "
                  "nvngx_dlssnr build do not get along - try another combination.",
                  "warn")
        self._log("Never use this in online games - anti-cheat will flag ReShade "
                  "add-ons.", "warn")
        self.btn_next.config(state="normal", text="INSTALL")
        self.btn_back.config(state="normal")
        self.btn_home.config(state="normal")
        self.btn_remove.config(state="normal")
        self.status.config(text="Install complete")


def run() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
