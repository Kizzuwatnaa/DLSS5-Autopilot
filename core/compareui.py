"""The before/after window: two ReShade screenshots side by side.

Opened from the main window for the selected game. It only reads files, so
it is safe to leave open while the game runs; "refresh" picks up new shots.
Colours, type and controls come from core/ui so it looks like part of the
same tool.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import tkinter as tk

from . import compare
from .ui import theme as T
from .ui import win as W
from .ui.kit import Kit

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
        self.win.configure(bg=T.BG)
        # Not more than the screen has: at 300% px(420) is 1260 on a
        # 1080-tall panel and the window could not be shrunk to fit.
        self.win.minsize(
            min(T.px(720), int(self.win.winfo_screenwidth() * 0.6)),
            min(T.px(420), int(self.win.winfo_screenheight() * 0.5)))
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{int(sw * 0.8)}x{int(sh * 0.7)}")
        try:
            self.win.after(0, lambda: W.dark_titlebar(self.win))
        except Exception:
            pass

        self.files: list[Path] = []
        self.shots: list[Path] = []          # [left, right] when paired
        self.labels = ["DLSS 5 off", "DLSS 5 on"]
        self._full: list = [None, None]      # decoded originals
        self._shown: list = [None, None]     # subsampled copies on canvas
        self._text: list = [("", "", ""), ("", "", "")]   # head, file, when
        self._fit_job: str | None = None
        self._can = {"swap": False, "export": False}
        self.btn_swap = self.btn_export = None

        self._build()
        self.refresh()

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        w = self.win
        pad = T.px(20)
        self.top = tk.Canvas(w, height=T.px(68), bg=T.BG, highlightthickness=0, bd=0)
        self.top.pack(fill="x")
        self.kit = Kit(self.top)
        self.top.bind("<Configure>", lambda _e: self._draw_top())

        self.body = tk.Frame(w, bg=T.BG)
        self.body.pack(fill="both", expand=True, padx=pad, pady=(0, T.px(8)))

        # Two equal columns, each one canvas: label, picture, file and time.
        self.panes: list[tk.Canvas] = []
        for i in range(2):
            self.body.columnconfigure(i, weight=1, uniform="pane")
            cv = tk.Canvas(self.body, bg=T.SURF, highlightthickness=0, bd=0)
            cv.grid(row=0, column=i, sticky="nsew",
                    padx=(0, T.px(6)) if i == 0 else (T.px(6), 0))
            self.panes.append(cv)
        self.body.rowconfigure(0, weight=1)

        self.status = tk.Label(w, text="", bg=T.BG, fg=T.MUTED, font=T.mono(9),
                               anchor="w", justify="left")
        self.status.pack(fill="x", padx=pad, pady=(0, T.px(14)))
        self.status.bind("<Configure>",
                         lambda e: self.status.config(wraplength=max(e.width, 1)))

        # The help card takes the panes' grid cell when there is nothing to
        # show; it is removed rather than lowered, or it shows through the gap.
        self.help = tk.Frame(self.body, bg=T.SURF)
        self.help.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.help_text = tk.Label(self.help, text="", bg=T.SURF, fg=T.TEXT,
                                  font=T.mono(10), justify="left", anchor="nw")
        self.help_text.pack(fill="both", expand=True, padx=T.px(28), pady=T.px(24))

        self.win.bind("<Configure>", self._on_configure)
        self.win.bind("<Escape>", lambda _e: self.close())
        self.win.protocol("WM_DELETE_WINDOW", self.close)

    def close(self) -> None:
        if self._fit_job:
            try:
                self.win.after_cancel(self._fit_job)
            except tk.TclError:
                pass
            self._fit_job = None
        self.win.destroy()

    def _draw_top(self) -> None:
        c, k = self.top, self.kit
        c.delete("all")
        width = max(c.winfo_width(), 1)
        pad, mid = T.px(20), T.px(34)
        bh, gap = T.px(36), T.px(8)
        x = width - pad
        buttons = (
            ("refresh", "refresh", self.refresh, True),
            ("open folder", "folder",
             lambda: _open_folder(compare.save_path(self.install_dir)), True),
            ("export png", "save", self._export, self._can["export"]),
            ("swap", "compare", self._swap, self._can["swap"]),
        )
        made = {}
        for label, glyph, cmd, on in buttons:
            bw = T.width(label, T.mono(10)) + T.px(58)
            x -= bw
            made[label] = k.button(x, mid - bh / 2, bw, label, cmd, glyph=glyph,
                                   h=bh, enabled=on, size=10)
            x -= gap
        self.btn_swap, self.btn_export = made["swap"], made["export png"]
        title_f = T.mono(14, True)
        k.text(pad, mid, "before / after", T.TEXT, 14, True)
        nx = pad + T.width("before / after", title_f) + T.px(16)
        room = x - nx - T.px(12)
        if room > T.px(30):
            k.text(nx, mid + T.px(2), T.fit(self.game_name, T.mono(10), room), T.MUTED, 10)

    def _enable(self, name: str, on: bool) -> None:
        self._can[name] = on
        btn = self.btn_swap if name == "swap" else self.btn_export
        if btn is not None:
            try:
                btn.set(enabled=on)
            except tk.TclError:
                pass

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
        self.help.grid_remove()
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
            f"   1. in the game, switch neural rendering off (F6 on the feeder, F5 on native and bridge)\n"
            f"   2. press {key} - ReShade saves a screenshot\n"
            f"   3. switch it back on\n"
            f"   4. press {key} again, then hit refresh here\n\n"
            f"screenshots land in:\n   {compare.save_path(self.install_dir)}\n\n"
            f"the screenshot key and folder are ReShade's own - change them "
            f"in ReShade's overlay under settings.\n"
            f"set the format to PNG there; this window cannot decode JPG or BMP."))
        self.help.grid()
        self._enable("swap", False)
        self._enable("export", False)
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
        for i in range(2):
            p = self.shots[i]
            extra = ""
            if self._full[i] is not None:
                extra = f"   {self._full[i].width()}x{self._full[i].height()}"
            self._text[i] = (self.labels[i], p.name, compare.when(p) + extra)
        gap = abs(compare.taken(self.shots[1]) - compare.taken(self.shots[0]))
        both_png = all(x is not None for x in self._full)
        self._enable("swap", True)
        self._enable("export", both_png)
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
        pad = T.px(14)
        for i, cv in enumerate(self.panes):
            cv.delete("all")
            pw, ph = max(cv.winfo_width(), 1), max(cv.winfo_height(), 1)
            head, name, when = self._text[i] if self.shots else ("", "", "")
            room = max(pw - 2 * pad, 1)
            cv.create_text(pad, T.px(24), text=T.fit(head, T.mono(11, True), room),
                           fill=T.AMBER, font=T.mono(11, True), anchor="w")
            cv.create_text(pad, ph - T.px(40), text=T.fit(name, T.mono(9), room),
                           fill=T.TEXT, font=T.mono(9), anchor="w")
            cv.create_text(pad, ph - T.px(20), text=T.fit(when, T.mono(9), room),
                           fill=T.DIM, font=T.mono(9), anchor="w")
            x1, y1, x2, y2 = pad, T.px(44), pw - pad, ph - T.px(56)
            cw, ch = max(x2 - x1, 1), max(y2 - y1, 1)
            cv.create_rectangle(x1, y1, x2, y2, fill=T.BG, outline="")
            cx, cy = x1 + cw // 2, y1 + ch // 2
            img = self._full[i]
            if img is None:
                if self.shots:
                    cv.create_text(cx, cy, fill=T.DIM, font=T.mono(10),
                                   justify="center",
                                   text="png only - set ReShade to PNG\n"
                                        "(overlay > settings > screenshot format)")
                continue
            f = max(math.ceil(img.width() / cw), math.ceil(img.height() / ch), 1)
            shown = img.subsample(f, f) if f > 1 else img
            self._shown[i] = shown
            cv.create_image(cx, cy, image=shown, anchor="center")

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
        self._enable("export", False)
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
                try:
                    self._enable("export", True)
                except tk.TclError:
                    pass
        self.win.after(50, go)


def show(parent, install_dir: Path, game_name: str) -> CompareWindow:
    return CompareWindow(parent, install_dir, game_name)
