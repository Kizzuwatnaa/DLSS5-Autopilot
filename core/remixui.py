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
                 text="A Remix mod rebuilds an old game with path tracing. DLSS 5 "
                      "then runs inside the Remix runtime, after its own upscaler - "
                      "no ReShade, no feeder, nothing of ours in the game folder "
                      "except the runtime's neural library.\n\n"
                      "Install the mod yourself from its page below. Once it is in, "
                      "this tool sees the .trex folder and offers the remix route, "
                      "which is what switches DLSS 5 on."
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
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        self._canvas = canvas

        owned = {id(m) for _g, m in remixlist.for_library(library or [])}
        if owned:
            self._heading(f"in your library ({len(owned)})", AMBER)
            for g, m in remixlist.for_library(library or []):
                self._row(m, mine=g.name)
        self._heading("already Remix, nothing to install", TXT)
        for m in remixlist.BUILT_IN:
            self._row(m)
        self._heading("community mods", TXT)
        for m in remixlist.MODS:
            if id(m) in owned:
                continue
            self._row(m)

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

    def _row(self, m: "remixlist.RemixMod", mine: str = "") -> None:
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
        tk.Frame(self.body, bg=LINE, height=1).pack(fill="x", padx=14)


def show(parent, library=None) -> RemixWindow:
    return RemixWindow(parent, library)
