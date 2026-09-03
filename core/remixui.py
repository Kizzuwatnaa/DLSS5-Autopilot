"""The RTX Remix window: which of your games have a mod, and where it is.

Kept out of gui.py because it is a side trip, not part of the three-step
install. It answers one question - "can I try this, and with what?" - and
then gets out of the way. Nothing here installs a mod: the projects listed
are other people's work with their own instructions, and the honest thing
is a link, not a download button.
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk

from . import remixlist


class RemixWindow:
    def __init__(self, parent, library=None) -> None:
        from .gui import AMBER, BG, DIM, EDGE, FAINT, LINE, PANEL, TXT, font
        self.font = font
        self.win = tk.Toplevel(parent)
        self.win.title("RTX Remix + DLSS 5")
        self.win.configure(bg=BG)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{int(sw * 0.62)}x{int(sh * 0.72)}"
                          f"+{int(sw * 0.19)}+{int(sh * 0.14)}")
        try:
            from .gui import _dark_titlebar
            self.win.after(0, lambda: _dark_titlebar(self.win))
        except Exception:
            pass

        head = tk.Frame(self.win, bg=BG)
        head.pack(fill="x", padx=22, pady=(18, 6))
        tk.Label(head, text="RTX Remix + DLSS 5", bg=BG, fg=TXT,
                 font=font(15)).pack(anchor="w")
        tk.Label(head, bg=BG, fg=DIM, font=font(9), justify="left", anchor="w",
                 wraplength=int(sw * 0.55),
                 text="What RTX Remix is: a free NVIDIA tool that replaces an old "
                      "game's entire graphics pipeline with real-time ray tracing - "
                      "not a filter or a mod menu, a different renderer. Someone "
                      "builds one of these per game (below); it is a serious "
                      "project, often gigabytes of rebuilt textures and geometry.\n\n"
                      "How the two fit together: DLSS 5 is not injected into a "
                      "Remix game the way it is everywhere else in this tool. A few "
                      "Remix builds carry DLSS 5 inside their own renderer already - "
                      "this tool just switches that on. Nothing of ours goes into "
                      "the game folder except one small file and one line of text.\n\n"
                      "1. Pick a game below and install ITS mod, from its own page - "
                      "that part is not this tool's job.\n"
                      "2. Press rescan here. The game shows up with the remix route "
                      "chosen for you.\n"
                      "3. Press INSTALL like any other game."
                 ).pack(anchor="w", pady=(6, 0))
        tk.Label(head, bg=BG, fg=FAINT, font=font(8), justify="left", anchor="w",
                 wraplength=int(sw * 0.55), text=remixlist.RULE_OF_THUMB)\
            .pack(anchor="w", pady=(8, 0))

        wrap = tk.Frame(self.win, bg=PANEL, highlightbackground=LINE,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=22, pady=(12, 8))
        canvas = tk.Canvas(wrap, bg=PANEL, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        self.body = tk.Frame(canvas, bg=PANEL)
        self.body.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb.pack(side="right", fill="y")
        # Mousewheel scroll for this canvas only. bind_all() is process-wide -
        # it used to steal scrolling from the main window's own log and game
        # list the moment this window opened, and kept doing it (against a
        # destroyed canvas, raising an error on every scroll) after the
        # window was closed, because nothing ever unbound it. Bind only
        # while the pointer is actually over this window, and clean up when
        # it leaves or the window closes.
        def _wheel(e):
            try:
                canvas.yview_scroll(int(-e.delta / 120), "units")
            except tk.TclError:
                pass

        def _grab(_e=None):
            self.win.bind_all("<MouseWheel>", _wheel)

        def _release(_e=None):
            try:
                self.win.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass

        self.win.bind("<Enter>", _grab)
        self.win.bind("<Leave>", _release)
        self.win.bind("<Destroy>", _release)
        self._canvas = canvas

        owned = {id(m) for _g, m in remixlist.for_library(library or [])}
        if owned:
            self._heading(f"in your library ({len(owned)})", AMBER)
            for g, m in remixlist.for_library(library or []):
                self._row(m, mine=g.name, game=g)
        self._heading("already Remix, nothing to install", TXT)
        for m in remixlist.BUILT_IN:
            self._row(m)
        self._heading("community mods", TXT)
        for m in remixlist.MODS:
            if id(m) in owned:
                continue
            self._row(m)

        self.status = tk.Label(self.win, text="", bg=BG, fg=DIM, font=font(9),
                               anchor="w", justify="left",
                               wraplength=int(sw * 0.55))
        self.status.pack(fill="x", padx=22, pady=(0, 4))

        foot = tk.Frame(self.win, bg=BG)
        foot.pack(fill="x", padx=22, pady=(0, 16))
        tk.Label(foot, text="more projects, including ones with no repository:",
                 bg=BG, fg=DIM, font=font(9)).pack(side="left")
        for label, url in remixlist.MORE:
            lk = tk.Label(foot, text=f"[ {label} ]", bg=BG, fg=AMBER,
                          font=font(9), cursor="hand2")
            lk.pack(side="left", padx=(10, 0))
            lk.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
        ttk.Button(foot, text="close", command=self.win.destroy).pack(side="right")

    def _heading(self, text: str, colour: str) -> None:
        from .gui import PANEL, font
        tk.Label(self.body, text=text.upper(), bg=PANEL, fg=colour,
                 font=font(9, "bold")).pack(anchor="w", padx=14, pady=(14, 4))

    def _row(self, m: "remixlist.RemixMod", mine: str = "", game=None) -> None:
        from .gui import AMBER, BODY, DIM, GREEN, LINE, PANEL, font
        row = tk.Frame(self.body, bg=PANEL)
        row.pack(fill="x", padx=14, pady=3)
        left = tk.Frame(row, bg=PANEL)
        left.pack(side="left", fill="x", expand=True)
        title = tk.Frame(left, bg=PANEL)
        title.pack(anchor="w", fill="x")
        tk.Label(title, text=m.game, bg=PANEL, fg=GREEN if mine else BODY,
                 font=self.font(10)).pack(side="left")
        if mine:
            tk.Label(title, text="you own this", bg=PANEL, fg=GREEN,
                     font=self.font(8)).pack(side="left", padx=(10, 0))
        line = m.mod + (" - " + m.note if m.note else "")
        tk.Label(left, text=line, bg=PANEL, fg=DIM, font=self.font(8),
                 anchor="w", justify="left", wraplength=760).pack(anchor="w")
        lk = tk.Label(row, text="[ open page ]", bg=PANEL, fg=AMBER,
                      font=self.font(9), cursor="hand2")
        lk.pack(side="right", padx=(12, 4))
        lk.bind("<Button-1>", lambda e, u=m.url: webbrowser.open(u))
        # Fetching is offered only where BOTH are true: the project publishes
        # a complete install (renderer included), and the game was found by
        # the scan so there is a folder to put it in without asking.
        if m.installable and game is not None:
            btn = tk.Label(row, text="[ download & install ]", bg=PANEL,
                           fg=GREEN, font=self.font(9), cursor="hand2")
            btn.pack(side="right", padx=(12, 0))
            btn.bind("<Button-1>",
                     lambda e, mm=m, gg=game, b=btn: self._fetch(mm, gg, b))
        tk.Frame(self.body, bg=LINE, height=1).pack(fill="x", padx=14)

    # ------------------------------------------------------------ fetching

    def _say(self, text: str, colour: str | None = None) -> None:
        from .gui import DIM
        try:
            self.status.config(text=text, fg=colour or DIM)
        except tk.TclError:
            pass

    def _fetch(self, mod, game, button) -> None:
        """Download the mod and put it in, on a thread, reporting progress.

        Everything the download does is somebody else's file going into
        somebody's game, so the work happens in remixdl where it can refuse:
        an incomplete release, a folder that already has a Remix mod, an
        archive that tries to write outside the game folder.
        """
        import threading
        from . import net, remixdl
        if getattr(self, "_busy", False):
            return
        self._busy = True
        try:
            button.config(text="[ working... ]")
        except tk.TclError:
            pass

        def ui(fn, *a):
            try:
                self.win.after(0, lambda: fn(*a))
            except tk.TclError:
                pass

        def prog(done, total):
            pct = int(done * 100 / total) if total else 0
            ui(self._say, f"{mod.game}: {pct}%  "
                          f"{net.human(done)} / {net.human(total)}")

        def work():
            from .gui import GREEN, RED
            try:
                written = remixdl.install(mod.url, game.install_dir,
                                          log=lambda t: ui(self._say, t.strip()),
                                          progress=prog)
                ui(self._say, f"{mod.game}: installed, {len(written)} files. "
                              f"Press rescan in the main window - the game "
                              f"should come up on the remix route now.", GREEN)
                ui(button.config, {"text": "[ installed ]"})
            except remixdl.NotAModError as e:
                ui(self._say, f"{mod.game}: {e}", RED)
                ui(button.config, {"text": "[ open page ]"})
            except Exception as e:
                ui(self._say, f"{mod.game}: {type(e).__name__}: {e}", RED)
                ui(button.config, {"text": "[ retry ]"})
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()


def show(parent, library=None) -> RemixWindow:
    return RemixWindow(parent, library)
