"""The before/after window: two ReShade screenshots side by side.

Opened from the main window for the selected game. It only reads files, so
it is safe to leave open while the game runs; "refresh" picks up new shots.
Colours and fonts come from gui so it looks like part of the same tool.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from . import compare
from .gui import AMBER, BG, DIM, EDGE, LINE, PANEL, TXT, font

TOGGLE_KEY = "F6"
_FIT_DELAY_MS = 120


def _open_folder(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


class CompareWindow:
    def __init__(self, parent, install_dir: Path, game_name: str) -> None:
        self.install_dir = Path(install_dir)
        self.game_name = game_name
        self.win = tk.Toplevel(parent)
        self.win.title(f"before / after - {game_name}")
        self.win.configure(bg=BG)
        self.win.minsize(720, 420)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{int(sw * 0.8)}x{int(sh * 0.7)}")

        self.files: list[Path] = []
        self.shots: list[Path] = []          # [left, right] when paired
        self.labels = ["DLSS 5 off", "DLSS 5 on"]
        self._full: list = [None, None]      # decoded originals
        self._shown: list = [None, None]     # subsampled copies on canvas
        self._fit_job: str | None = None

        self._build()
        self.refresh()

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        w = self.win
        top = tk.Frame(w, bg=BG)
        top.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(top, text="before / after", bg=BG, fg=TXT, font=font(15))\
            .pack(side="left")
        tk.Label(top, text=self.game_name, bg=BG, fg=DIM, font=font(9))\
            .pack(side="left", padx=(12, 0), pady=(6, 0))
        ttk.Button(top, text="refresh", command=self.refresh).pack(side="right")
        ttk.Button(top, text="open folder",
                   command=lambda: _open_folder(compare.save_path(self.install_dir)))\
            .pack(side="right", padx=(0, 8))
        self.btn_export = ttk.Button(top, text="export png", command=self._export)
        self.btn_export.pack(side="right", padx=(0, 8))
        self.btn_swap = ttk.Button(top, text="swap", command=self._swap)
        self.btn_swap.pack(side="right", padx=(0, 8))

        self.body = tk.Frame(w, bg=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        # Two equal columns; each is a card with a canvas and two lines under.
        self.panes = []
        for i in range(2):
            self.body.columnconfigure(i, weight=1, uniform="pane")
            card = tk.Frame(self.body, bg=PANEL, highlightbackground=LINE,
                            highlightthickness=1)
            card.grid(row=0, column=i, sticky="nsew", padx=(0, 8) if i == 0 else (8, 0))
            head = tk.Label(card, text="", bg=PANEL, fg=AMBER, font=font(11, "bold"))
            head.pack(anchor="w", padx=12, pady=(10, 4))
            cv = tk.Canvas(card, bg=BG, highlightthickness=0, bd=0)
            cv.pack(fill="both", expand=True, padx=12)
            name = tk.Label(card, text="", bg=PANEL, fg=TXT, font=font(9), anchor="w")
            name.pack(fill="x", padx=12, pady=(6, 0))
            when = tk.Label(card, text="", bg=PANEL, fg=DIM, font=font(9), anchor="w")
            when.pack(fill="x", padx=12, pady=(0, 10))
            self.panes.append((card, head, cv, name, when))
        self.body.rowconfigure(0, weight=1)

        self.status = tk.Label(w, text="", bg=BG, fg=DIM, font=font(9),
                               anchor="w", justify="left")
        self.status.pack(fill="x", padx=16, pady=(0, 12))

        # The help card sits in the same grid cell as the panes and is raised
        # when there is nothing to show.
        self.help = tk.Frame(self.body, bg=PANEL, highlightbackground=EDGE,
                             highlightthickness=1)
        self.help.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.help_text = tk.Label(self.help, text="", bg=PANEL, fg=TXT,
                                  font=font(10), justify="left", anchor="nw")
        self.help_text.pack(fill="both", expand=True, padx=20, pady=18)

        self.win.bind("<Configure>", self._on_configure)

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        self.files = compare.find_screenshots(self.install_dir)
        p = compare.pair(self.files)
        self._full = [None, None]
        if p is None:
            self.shots = []
            self._show_help()
            return
        self.shots = [p[0], p[1]]
        self.help.lower()
        for i in range(2):
            self._load(i)
        self._fill_text()
        self._fit()

    def _show_help(self) -> None:
        key = compare.screenshot_key(self.install_dir)
        n = len(self.files)
        why = ("no screenshots found yet" if n == 0 else
               f"{n} screenshot{'s' if n != 1 else ''} found, but none two "
               f"taken within {compare.PAIR_WINDOW // 60} minutes of each other")
        self.help_text.config(text=(
            f"{why}\n\n"
            f"how to make a pair:\n"
            f"   1. in the game, press {TOGGLE_KEY} to switch neural rendering off\n"
            f"   2. press {key} - ReShade saves a screenshot\n"
            f"   3. press {TOGGLE_KEY} again to switch it back on\n"
            f"   4. press {key} again, then hit refresh here\n\n"
            f"screenshots land in:\n   {compare.save_path(self.install_dir)}\n\n"
            f"the screenshot key and folder are ReShade's own - change them "
            f"in its overlay (Home) under settings.\n"
            f"set the format to PNG there; this window cannot decode JPG or BMP."))
        self.help.lift()
        self.btn_swap.state(["disabled"])
        self.btn_export.state(["disabled"])
        self.status.config(text="")

    def _load(self, i: int) -> None:
        p = self.shots[i]
        self._full[i] = None
        if not compare.is_png(p):
            return
        try:
            self._full[i] = tk.PhotoImage(master=self.win, file=str(p))
        except tk.TclError:
            self._full[i] = None

    def _fill_text(self) -> None:
        for i, (_, head, _, name, when) in enumerate(self.panes):
            p = self.shots[i]
            head.config(text=self.labels[i])
            name.config(text=p.name)
            extra = ""
            if self._full[i] is not None:
                extra = f"   {self._full[i].width()}x{self._full[i].height()}"
            when.config(text=compare.when(p) + extra)
        gap = abs(compare.taken(self.shots[1]) - compare.taken(self.shots[0]))
        both_png = all(x is not None for x in self._full)
        self.btn_swap.state(["!disabled"])
        self.btn_export.state(["!disabled"] if both_png else ["disabled"])
        self.status.config(text=(
            f"{len(self.files)} screenshots in {compare.save_path(self.install_dir)}"
            f"   -   the two newest, {int(gap)} s apart. "
            f"not sure which is which? swap."))

    # ------------------------------------------------------------- drawing
    def _on_configure(self, _e=None) -> None:
        if not self.shots:
            return
        if self._fit_job:
            self.win.after_cancel(self._fit_job)
        self._fit_job = self.win.after(_FIT_DELAY_MS, self._fit)

    def _fit(self) -> None:
        """Scale each image to its canvas with an integer subsample.

        tk only shrinks by whole factors, so a 3840-wide shot in a 900-wide
        pane becomes 1/5 - slightly small rather than cropped, which is the
        right side to err on for a comparison.
        """
        self._fit_job = None
        for i, (_, _, cv, _, _) in enumerate(self.panes):
            cv.delete("all")
            img = self._full[i]
            cw, ch = max(cv.winfo_width(), 1), max(cv.winfo_height(), 1)
            if img is None:
                if self.shots:
                    cv.create_text(cw // 2, ch // 2, fill=DIM, font=font(10),
                                   justify="center",
                                   text="PNG only - set ReShade to PNG\n"
                                        "(overlay > settings > screenshot format)")
                continue
            f = max(math.ceil(img.width() / cw), math.ceil(img.height() / ch), 1)
            shown = img.subsample(f, f) if f > 1 else img
            self._shown[i] = shown
            cv.create_image(cw // 2, ch // 2, image=shown, anchor="center")

    # ------------------------------------------------------------- actions
    def _swap(self) -> None:
        if len(self.shots) != 2:
            return
        self.shots.reverse()
        self._full.reverse()
        self._fill_text()
        self._fit()

    def _export(self) -> None:
        if len(self.shots) != 2 or not all(x is not None for x in self._full):
            return
        a, b = self.shots
        out = compare.export_name(self.install_dir)
        self.btn_export.state(["disabled"])
        self.status.config(text="writing combined png...")

        # The copy is pure C but can still take a second on 4K shots; a
        # frozen button reads as a crash, so tell the user and work after
        # the redraw. Tk images are not thread-safe, so no thread here.
        def go() -> None:
            try:
                compare.export_side_by_side(a, b, out, master=self.win)
                self.status.config(text=f"saved {out.name} next to the screenshots")
            except (tk.TclError, OSError) as e:
                self.status.config(text=f"export failed: {e}")
            finally:
                self.btn_export.state(["!disabled"])
        self.win.after(50, go)


def show(parent, install_dir: Path, game_name: str) -> CompareWindow:
    return CompareWindow(parent, install_dir, game_name)
