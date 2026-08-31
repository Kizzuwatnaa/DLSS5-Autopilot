"""tkinter arayuzu - 3 adimli sihirbaz.

Adim 1: mimari (64-bit / 32-bit / hepsi)
Adim 2: taranan oyunlardan secim
Adim 3: ayarlar + kurulum
"""
from __future__ import annotations

import queue
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import feedcfg, games, gpu, installer, net, pe, prefs, reshade_ini, sources

APP = "DLSS 5 Kurulum Aracı"

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

        self.arch = tk.StringVar(value="hepsi")
        self.all_games: list[games.Game] = []
        self.shown: list[games.Game] = []
        self.game: games.Game | None = None
        self.catalog: dict[str, list[dict]] = {}
        self.renodx_local: Path | None = None

        self.provider = tk.IntVar(value=3)
        self.renodx_v = tk.StringVar()
        self.dlssnr_v = tk.StringVar()
        self.dlss_v = tk.StringVar()
        self.keep_dlss = tk.BooleanVar(value=True)

        self._build()
        self._show(1)
        self.root.after(60, self._pump)

    # ------------------------------------------------------------- iskelet
    def _build(self) -> None:
        r = self.root
        r.title(APP)
        r.geometry("960x780")
        r.minsize(880, 660)
        r.configure(bg=BG)

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TXT, fieldbackground=PANEL2,
                     bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=TXT, font=("Segoe UI", 10))
        st.configure("Panel.TLabel", background=PANEL, foreground=TXT)
        st.configure("H1.TLabel", font=("Segoe UI Semibold", 17), foreground=TXT)
        st.configure("H2.TLabel", font=("Segoe UI Semibold", 12), foreground=TXT)
        st.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        st.configure("MutedP.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        st.configure("TButton", background=PANEL2, foreground=TXT, borderwidth=0,
                     focuscolor=PANEL2, padding=(14, 8), font=("Segoe UI", 10))
        st.map("TButton", background=[("active", LINE), ("disabled", PANEL)],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0b1220",
                     font=("Segoe UI Semibold", 10), padding=(18, 9))
        st.map("Accent.TButton", background=[("active", "#6cb6ff"), ("disabled", LINE)],
               foreground=[("disabled", MUTED)])
        st.configure("TRadiobutton", background=PANEL, foreground=TXT, font=("Segoe UI", 10))
        st.map("TRadiobutton", background=[("active", PANEL)])
        st.configure("TCheckbutton", background=BG, foreground=TXT, font=("Segoe UI", 10))
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TXT,
                     rowheight=27, borderwidth=0, font=("Segoe UI", 10))
        st.configure("Treeview.Heading", background=PANEL2, foreground=MUTED,
                     borderwidth=0, font=("Segoe UI Semibold", 9))
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", "#0b1220")])
        st.map("Treeview.Heading", background=[("active", LINE)])
        st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                     foreground=TXT, arrowcolor=TXT, padding=6)
        # readonly durumunda ttk metni soluklastiriyor; okunur hale getir
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

        # baslik
        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=22, pady=(18, 6))
        tk.Label(head, text="DLSS 5", bg=BG, fg=ACCENT,
                 font=("Segoe UI Black", 20)).pack(side="left")
        tk.Label(head, text="  Kurulum Aracı", bg=BG, fg=TXT,
                 font=("Segoe UI Light", 20)).pack(side="left")
        self.steplbl = tk.Label(head, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.steplbl.pack(side="right")

        tk.Frame(r, bg=LINE, height=1).pack(fill="x", padx=22, pady=(4, 0))

        # Alt cubuk govdeden ONCE ve alta sabitlenir: govde buyuse de
        # KUR dugmesi asla kirpilmaz.
        foot = tk.Frame(r, bg=BG)
        foot.pack(side="bottom", fill="x", padx=22, pady=12)
        tk.Frame(r, bg=LINE, height=1).pack(side="bottom", fill="x", padx=22)

        self.body = tk.Frame(r, bg=BG)
        self.body.pack(fill="both", expand=True, padx=22, pady=12)

        self.p1 = self._page_arch()
        self.p2 = self._page_games()
        self.p3 = self._page_install()
        self.status = tk.Label(foot, text="Hazır", bg=BG, fg=MUTED,
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.btn_back = ttk.Button(foot, text="< Geri", command=self._back)
        self.btn_back.pack(side="left", padx=(0, 8))
        self.btn_next = ttk.Button(foot, text="Devam >", style="Accent.TButton",
                                   command=self._next)
        self.btn_next.pack(side="right")

    def _show(self, step: int) -> None:
        self.step = step
        for p in (self.p1, self.p2, self.p3):
            p.pack_forget()
        [self.p1, self.p2, self.p3][step - 1].pack(fill="both", expand=True)
        self.steplbl.config(text=f"Adım {step} / 3")
        self.btn_back.config(state="normal" if step > 1 else "disabled")
        if step == 1:
            self.btn_next.config(text="Oyunları tara >", state="normal")
        elif step == 2:
            self.btn_next.config(text="Devam >",
                                 state="normal" if self.game else "disabled")
        else:
            self.btn_next.config(text="KUR", state="normal")

    # ------------------------------------------------------------- adim 1
    def _page_arch(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        ttk.Label(f, text="Hangi mimari için kuracaksın?", style="H1.TLabel")\
            .pack(anchor="w", pady=(10, 4))
        ttk.Label(f, text="Oyunun 32-bit mi 64-bit mi olduğunu bilmiyorsan "
                          "'Hepsini göster'i seç — araç her oyunun mimarisini "
                          "kendisi okuyup yazacak.", style="Muted.TLabel")\
            .pack(anchor="w", pady=(0, 18))

        opts = [
            ("hepsi", "Hepsini göster",
             "Bütün oyunları listele, mimarisini yanlarında göster. Önerilen."),
            ("64", "Sadece 64-bit oyunlar",
             "Standart yol. ReShade + DLSS 5 eklentisi doğrudan oyunun yanına kurulur."),
            ("32", "Sadece 32-bit oyunlar",
             "32-bit süreç 64-bit NGX yükleyemez; ayrıca bir host64 yardımcı "
             "süreci kurulur. DLSS5-Feeder bu yolu BETA olarak işaretliyor."),
        ]
        for val, title, desc in opts:
            card = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            card.pack(fill="x", pady=5)
            rb = ttk.Radiobutton(card, text=title, value=val, variable=self.arch)
            rb.pack(anchor="w", padx=16, pady=(12, 2))
            ttk.Label(card, text=desc, style="MutedP.TLabel", wraplength=800,
                      justify="left").pack(anchor="w", padx=38, pady=(0, 12))
        return f

    # ------------------------------------------------------------- adim 2
    def _page_games(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=BG)
        top = tk.Frame(f, bg=BG)
        top.pack(fill="x")
        ttk.Label(top, text="Oyunu seç", style="H1.TLabel").pack(side="left")
        ttk.Button(top, text="Klasör seç...", command=self._pick_folder)\
            .pack(side="right", padx=(8, 0))
        ttk.Button(top, text="Yeniden tara", command=self._scan).pack(side="right")

        self.scanlbl = ttk.Label(f, text="", style="Muted.TLabel")
        self.scanlbl.pack(anchor="w", pady=(4, 8))

        wrap = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        cols = ("kaynak", "mimari", "api", "durum")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings", height=13)
        self.tree.heading("#0", text="OYUN")
        self.tree.column("#0", width=340, anchor="w")
        for c, t, w in (("kaynak", "KAYNAK", 80), ("mimari", "MİMARİ", 80),
                        ("api", "API", 110), ("durum", "DURUM", 190)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("kurulu", foreground=OK)
        self.tree.tag_configure("yok", foreground=WARN)
        self.tree.bind("<<TreeviewSelect>>", self._on_pick)
        self.tree.bind("<Double-1>", lambda e: self._next())

        self.detail = tk.Label(f, text="", bg=BG, fg=MUTED, font=("Consolas", 9),
                               anchor="w", justify="left")
        self.detail.pack(fill="x", pady=(8, 0))
        return f

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Oyun klasörünü seç")
        if not d:
            return
        g = games.manual(Path(d))
        if not g.exe:
            messagebox.showwarning(APP, f"Bu klasörde çalıştırılabilir bulunamadı:\n{d}")
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
        self.scanlbl.config(text="Taranıyor...")
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
                      if g.exe and (a == "hepsi" or str(g.bitness) == a)]
        for i, g in enumerate(self.shown):
            ok, why = installer.check_supported(g)
            if g.installed:
                durum, tag = "Kurulu", "kurulu"
            elif not ok:
                durum, tag = "Desteklenmiyor", "yok"
            else:
                durum, tag = "Kurulabilir", ""
            self.tree.insert("", "end", iid=str(i), text="  " + g.name,
                             values=(g.source, g.bit_label, g.api, durum),
                             tags=(tag,) if tag else ())
        hidden = len([g for g in self.all_games if not g.exe])
        msg = f"{len(self.shown)} oyun listeleniyor"
        if a != "hepsi":
            msg += f" ({a}-bit filtresi açık)"
        if hidden:
            msg += f"  ·  {hidden} klasörde çalıştırılabilir bulunamadı (kurulu değil)"
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
        proxy = installer._proxy_name(g.api)
        lines = [f"exe    : {g.exe}",
                 f"mimari : {g.bit_label}   API: {g.api}  ({g.api_why})"]
        if ok:
            lines.append(f"yol    : ReShade '{proxy}' olarak kurulacak"
                         + ("   +  host64/ yardımcı süreç" if g.bitness == 32 else "")
                         + ("   +  dgVoodoo2 (DX9->D3D11)" if g.api == "DX9" else ""))
        if getattr(g, "emu", None):
            lines.append(f"EMÜLATÖR: {g.emu.renderer_hint}")
        else:
            lines.append("UYARI  : " + why)
        self.detail.config(text="\n".join(lines), fg=MUTED if ok else WARN)
        self.btn_next.config(state="normal" if ok else "disabled")

    # ------------------------------------------------------------- adim 3
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

        row(0, "Hareket vektörü")
        self.cb_prov = ttk.Combobox(inner, state="readonly",
                                    values=[v[0] for v in reshade_ini.PROVIDERS.values()])
        self.cb_prov.current(0)
        self.cb_prov.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        self.cb_prov.bind("<<ComboboxSelected>>", self._on_prov)

        row(1, "DLSS 5 eklentisi")
        self.cb_renodx = ttk.Combobox(inner, state="readonly", values=["yukleniyor..."])
        self.cb_renodx.grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Button(inner, text="Kendi dosyam...", command=self._pick_renodx)\
            .grid(row=1, column=2, sticky="w", padx=(8, 0), pady=5)

        row(2, "nvngx_dlssnr")
        self.cb_dlssnr = ttk.Combobox(inner, state="readonly", values=["yukleniyor..."])
        self.cb_dlssnr.grid(row=2, column=1, columnspan=2, sticky="ew", pady=5)

        row(3, "nvngx_dlss")
        self.cb_dlss = ttk.Combobox(inner, state="readonly", values=["yukleniyor..."])
        self.cb_dlss.grid(row=3, column=1, sticky="ew", pady=5)
        tk.Checkbutton(inner, text="oyununkini koru", variable=self.keep_dlss,
                       bg=PANEL, fg=TXT, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=TXT, font=("Segoe UI", 9), borderwidth=0)\
            .grid(row=3, column=2, sticky="w", padx=(8, 0))

        # --- gelismis ayarlar (dlss5-feed.cfg) ---------------------------
        row(4, "Kalite / hız")
        adv = tk.Frame(inner, bg=PANEL)
        adv.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(10, 2))
        adv.columnconfigure(1, weight=1)

        tk.Label(adv, text="İşleme alanı", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.workres = tk.IntVar(value=100)
        self.sc_work = tk.Scale(adv, from_=50, to=100, resolution=5, orient="horizontal",
                                variable=self.workres, bg=PANEL, fg=TXT, troughcolor=PANEL2,
                                highlightthickness=0, borderwidth=0, showvalue=True,
                                font=("Segoe UI", 8), length=240, sliderrelief="flat",
                                activebackground=ACCENT, command=self._on_workres)
        self.sc_work.grid(row=0, column=1, sticky="w", padx=(10, 10))
        self.workhint = tk.Label(adv, text="%100 — tam kalite", bg=PANEL, fg=MUTED,
                                 font=("Segoe UI", 8))
        self.workhint.grid(row=0, column=2, sticky="w")

        tk.Label(adv, text="DLSS preset", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cb_preset = ttk.Combobox(adv, state="readonly", width=46,
                                      values=list(feedcfg.PRESETS.values()))
        self.cb_preset.current(0)
        self.cb_preset.grid(row=1, column=1, columnspan=2, sticky="w",
                            padx=(10, 0), pady=(8, 0))

        tk.Label(adv, text="HDR", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.cb_hdr = ttk.Combobox(adv, state="readonly", width=20,
                                   values=list(feedcfg.HDR.values()))
        self.cb_hdr.current(0)
        self.cb_hdr.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        tk.Label(adv, text="Feeder yolu her zaman DLAA'dır (aşağıya bak)",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 8))\
            .grid(row=2, column=2, sticky="w", pady=(8, 0))

        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", pady=(12, 4))
        self.pb = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        self.pblbl = tk.Label(f, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w")
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
        self.btn_remove = ttk.Button(act, text="Kurulumu kaldır", command=self._uninstall)
        self.btn_remove.pack(side="left")
        ttk.Button(act, text="Oyun klasörünü aç",
                   command=lambda: self.game and webbrowser.open(str(self.game.install_dir)))\
            .pack(side="left", padx=8)
        return f

    def _on_workres(self, _v=None) -> None:
        v = self.workres.get()
        if v == 100:
            self.workhint.config(text="%100 - tam kalite", fg=MUTED)
        elif v >= 80:
            self.workhint.config(text=f"%{v} - biraz daha hizli", fg=MUTED)
        else:
            self.workhint.config(text=f"%{v} - belirgin hiz, detay kaybi", fg=WARN)

    def _feed_settings(self) -> dict:
        """Gelismis ayarlardan dlss5-feed.cfg sozlugu uret."""
        s: dict = {}
        wr = self.workres.get()
        if wr != 100:
            s["work_resolution"] = wr
        keys = list(feedcfg.PRESETS.keys())
        pi = self.cb_preset.current()
        if pi > 0:
            s["preset"] = keys[pi]
        hk = list(feedcfg.HDR.keys())
        hi = self.cb_hdr.current()
        if hi > 0:
            s["hdr"] = hk[hi]
        return s

    def _on_prov(self, _e=None) -> None:
        self.provider.set(list(reshade_ini.PROVIDERS.keys())[self.cb_prov.current()])

    def _find_local_renodx(self) -> None:
        """Yerel renodx addon dosyasini bul ve varsayilan yap.

        Discord'dan cekilen surumler aynada olmadigi icin, bir kez secilen
        dosya butun oyunlarda varsayilan olarak kullanilir (prefs.py).
        """
        found, cands = prefs.find_renodx()
        if not found:
            return
        self.renodx_local = found
        tag = f"[yerel] {found.name}"
        vals = [v for v in self.cb_renodx["values"] if not v.startswith(("yukleniyor", "[yerel]"))]
        self.cb_renodx["values"] = [tag] + vals
        self.cb_renodx.set(tag)
        self._log(f"renodx: yerel dosya kullanılacak -> {found.name} "
                  f"({found.stat().st_size/1048576:.1f} MB)", "ok")
        self._log(f"   {found.parent}")
        if len(cands) > 1:
            self._log(f"   ({len(cands)-1} yerel dosya daha bulundu; "
                      f"başkasını istersen 'Kendi dosyam...')")

    def _pick_renodx(self) -> None:
        p = filedialog.askopenfilename(
            title="renodx addon dosyasını seç (Discord'dan indirdiğin)",
            filetypes=[("ReShade eklentisi", "*.addon64 *.addon"), ("Tüm dosyalar", "*.*")])
        if not p:
            return
        self.renodx_local = Path(p)
        prefs.remember_renodx(self.renodx_local)
        vals = list(self.cb_renodx["values"])
        tagname = f"[yerel] {self.renodx_local.name}"
        if tagname not in vals:
            vals.insert(0, tagname)
            self.cb_renodx["values"] = vals
        self.cb_renodx.set(tagname)

    def _enter_install(self) -> None:
        g = self.game
        self.gamelbl.config(text=g.name)
        extra = "  +  host64/ yardimci surec" if g.bitness == 32 else ""
        self.pathlbl.config(
            text=f"{g.exe}     |     {g.bit_label}  {g.api}  ->  "
                 f"ReShade = {installer._proxy_name(g.api)}{extra}")
        self.btn_remove.config(state="normal" if g.installed else "disabled")
        card, sm = gpu.detect()
        if card:
            self._log(f"Ekran kartı: {card}  ({gpu.label(sm)})", "head")
            self._log("Seçtiğin nvngx_dlssnr sürümü indirildikten sonra bu kart için "
                      "gerçekten kod içeriyor mu diye denetlenecek.")
        else:
            self._log("NVIDIA ekran kartı tespit edilemedi - DLSS 5 çalışmayabilir.", "warn")
        self._log(f"Plan: {' -> '.join(installer.plan(g, self._opts()))}", "head")
        if g.bitness == 32:
            self._log("Not: 32-bit yol DLSS5-Feeder tarafından BETA olarak "
                      "işaretlenmiş. Sorun çıkarsa dlss5-feed.log'a bak.", "warn")
        if g.api == "DX9":
            self._log("DX9 oyunu: önce dgVoodoo2 kurulacak (DX9 -> D3D11), "
                      "sonrası 32-bit yolla aynı.", "head")
        emu = getattr(g, "emu", None)
        if emu:
            self._log(f"{emu.name} ({emu.system}) tespit edildi.", "head")
            self._log(f"  ÖNEMLİ: {emu.renderer_hint}", "warn")
            self._log("  Vulkan/OpenGL seçiliyse ReShade hiç devreye girmez.", "warn")
            if emu.note:
                self._log(f"  {emu.note}")
            self._log("  Emülatörlerde ReShade birden fazla derinlik tamponu görebilir; "
                      "görüntü bozuksa ReShade'in DX11/DX12 sekmesinden doğru tamponu seç.")
        self._find_local_renodx()
        if not self.catalog:
            self._load_catalog()

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
        self.cb_renodx["values"] = ([keep] if keep.startswith("[yerel]") else []) + ren
        if keep.startswith("[yerel]"):
            self.cb_renodx.set(keep)
        elif sources.RENODX_DEFAULT in ren:
            self.cb_renodx.set(sources.RENODX_DEFAULT)
        elif ren:
            self.cb_renodx.current(0)
        self.cb_dlssnr["values"] = nr
        if nr:
            self.cb_dlssnr.current(0)
        self.cb_dlss["values"] = ds
        if ds:
            self.cb_dlss.current(0)
        self._log(f"Sürüm listesi hazır  ·  renodx: {len(ren)}, "
                  f"dlssnr: {len(nr)}, dlss: {len(ds)} sürüm", "")
        self._log(f"Önerilen renodx sürümü {sources.RENODX_DEFAULT} "
                  f"(DLSS5-Feeder belgeleri bunu söylüyor). "
                  f"Discord'dan yeni bir sürüm indirdiysen 'Kendi dosyam' ile seç.", "")

    def _opts(self) -> installer.Options:
        val = self.cb_renodx.get() if hasattr(self, "cb_renodx") else ""
        local = self.renodx_local if val.startswith("[yerel]") else None
        return installer.Options(
            provider=self.provider.get(),
            renodx=None if local else (val or sources.RENODX_DEFAULT),
            renodx_local=local,
            dlssnr=self.cb_dlssnr.get() or None if hasattr(self, "cb_dlssnr") else None,
            dlss=self.cb_dlss.get() or None if hasattr(self, "cb_dlss") else None,
            keep_game_dlss=self.keep_dlss.get(),
            feed=self._feed_settings(),
        )

    # ------------------------------------------------------------- eylemler
    def _install(self) -> None:
        if self.busy or not self.game:
            return
        self.busy = True
        self.btn_next.config(state="disabled", text="Kuruluyor...")
        self.btn_back.config(state="disabled")
        self.btn_remove.config(state="disabled")
        self.pb["value"] = 0
        g, opt = self.game, self._opts()
        self._log("", "")
        self._log(f"=== {g.name} — kurulum başlıyor ===", "head")

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
                APP, f"{self.game.name}\n\nBu aracın kurduğu dosyalar silinecek. "
                     f"Oyunun kendi dosyalarına dokunulmaz. Devam?"):
            return
        self.busy = True
        self._log("", "")
        self._log("=== kaldırılıyor ===", "head")
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

    # ------------------------------------------------------------- kuyruk
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
                    self.status.config(text="Tarama bitti")
                elif kind == "catalog":
                    self._fill_catalog(payload)
                elif kind == "caterr":
                    self._log(f"Sürüm listesi alınamadı: {payload}", "warn")
                    self._log("İnternet bağlantını kontrol et; 'Kendi dosyam' ile "
                              "elle de devam edebilirsin.", "warn")
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
                    self.game.__dict__.pop("_", None)
                    self._log(f"Kaldırıldı ({len(payload)} öğe).", "ok")
                    self.btn_remove.config(state="disabled")
                    self.btn_next.config(state="normal", text="KUR")
                    self.btn_back.config(state="normal")
                elif kind in ("fail", "error"):
                    self.busy = False
                    self._log(payload, "err")
                    self.pblbl.config(text="")
                    self.btn_next.config(state="normal", text="KUR")
                    self.btn_back.config(state="normal")
                    self.status.config(text="Hata")
                    messagebox.showerror(APP, payload.strip().splitlines()[-1])
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def _finish_ok(self, rep: installer.Report) -> None:
        self.pb["value"] = 100
        self.pblbl.config(text="")
        self._log("", "")
        self._log(f"BİTTİ — {len(rep.written)} dosya yazıldı.", "ok")
        for n in rep.notes:
            self._log(f"  · {n}")
        if rep.skipped:
            self._log(f"  · dokunulmayan: {', '.join(rep.skipped)}")
        self._log("", "")
        self._log("Şimdi oyunu aç ve:", "head")
        self._log("  1. Home tuşuna bas (ReShade açılır)")
        p = reshade_ini.PROVIDERS[self.provider.get()]
        if p[1]:
            self._log(f"  2. '{p[0]}' tekniği ve 'DLSS 5 Feed' işaretli olmalı "
                      f"(sağlayıcı ÜSTTE)")
        else:
            self._log("  2. Seçtiğin sağlayıcının tekniğini DLSS 5 Feed'in ÜSTÜNE al")
        self._log("  3. 'DLSS 5 Neural Rendering' panelinden neural rendering'i aç")
        self._log("  4. Oyunun kendi MSAA/SSAA ayarını KAPAT")
        self._log("")
        self._log("Çalışmazsa oyun klasöründeki dlss5-feed.log dosyasına bak: "
                  "'feature ready … DLAA' ve 'frame N delivered' satırlarını görmelisin.",
                  "warn")
        self._log("Online oyunlarda kullanma — anti-cheat ReShade eklentilerine takılır.",
                  "warn")
        self.btn_next.config(state="normal", text="KUR")
        self.btn_back.config(state="normal")
        self.btn_remove.config(state="normal")
        self.status.config(text="Kurulum tamamlandı")


def run() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
