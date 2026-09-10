"""The two questions a bug report cannot be read without.

The report button already fills in the machine, the route, the folder and
every log. What it cannot fill in is what the person saw - and that is the
half that decides which of the buckets a report lands in. Four of the nine
reports that arrived the day 1.7.3 shipped came in with the template's own
"yes / no / it closed itself" still on the line and "What happened" empty,
which costs a round trip each: someone has to ask, and wait.

So they are asked here, in the tool, before the browser opens. Two
questions, both required, and the button stays dim until they are answered.
Nothing else was added: a form people abandon is worse than a blank one.
"""
from __future__ import annotations

import tkinter as tk

from .gui import px

# (value written into the report, what the person reads)
STARTED = (
    ("it started and ran", "it started and ran"),
    ("it started, then closed itself", "it started, then closed itself"),
    ("it never started", "it never started"),
)
MIN_WORDS = 3


class ReportDialog:
    """Modal. `.answers` is None when the person closed it instead."""

    def __init__(self, parent, game_name: str = "") -> None:
        from .gui import AMBER, BG, DIM, EDGE, FAINT, LINE, PANEL, TXT, font
        self.answers: dict | None = None
        self.win = tk.Toplevel(parent)
        self.win.title("report a bug")
        self.win.configure(bg=BG)
        self.win.transient(parent)
        self.win.resizable(False, False)

        wrap = tk.Frame(self.win, bg=BG)
        wrap.pack(fill="both", expand=True, padx=px(22), pady=px(18))

        tk.Label(wrap, text="two questions, then the report opens",
                 bg=BG, fg=TXT, font=font(13)).pack(anchor="w")
        tk.Label(wrap, bg=BG, fg=DIM, font=font(9), justify="left",
                 anchor="w", wraplength=px(430),
                 text="everything else - your card, the driver, the route, "
                      "which files are in the folder and the tail of every "
                      "log - is filled in for you. these two are the ones "
                      "only you can answer."
                 ).pack(anchor="w", pady=(px(4), px(14)))

        tk.Label(wrap, text=f"did {game_name or 'the game'} start?",
                 bg=BG, fg=TXT, font=font(10)).pack(anchor="w")
        self.started = tk.StringVar(value="")
        for value, label in STARTED:
            tk.Radiobutton(wrap, text=label, value=value,
                           variable=self.started, command=self._check,
                           bg=BG, fg=TXT, selectcolor=PANEL,
                           activebackground=BG, activeforeground=AMBER,
                           highlightthickness=0, borderwidth=0,
                           font=font(9), anchor="w").pack(anchor="w",
                                                          pady=(px(2), 0))

        tk.Label(wrap, text="what happened?", bg=BG, fg=TXT,
                 font=font(10)).pack(anchor="w", pady=(px(14), 0))
        tk.Label(wrap, bg=BG, fg=FAINT, font=font(8), justify="left",
                 anchor="w", wraplength=px(430),
                 text="a sentence is enough: what you saw, and what you "
                      "expected instead."
                 ).pack(anchor="w", pady=(0, px(4)))
        self.text = tk.Text(wrap, height=5, width=52, bg=PANEL, fg=TXT,
                            insertbackground=TXT, font=font(9),
                            highlightbackground=LINE, highlightthickness=1,
                            borderwidth=0, wrap="word")
        self.text.pack(fill="x")
        self.text.bind("<KeyRelease>", lambda _e: self._check())

        row = tk.Frame(wrap, bg=BG)
        row.pack(fill="x", pady=(px(14), 0))
        self.hint = tk.Label(row, text="", bg=BG, fg=FAINT, font=font(8))
        self.hint.pack(side="left")
        tk.Button(row, text="cancel", command=self._cancel, bg=BG, fg=DIM,
                  activebackground=BG, activeforeground=TXT, borderwidth=0,
                  highlightthickness=0, font=font(9), cursor="hand2"
                  ).pack(side="right", padx=(px(8), 0))
        self.go = tk.Button(row, text="open the report", command=self._ok,
                            bg=PANEL, fg=FAINT, activebackground=PANEL,
                            activeforeground=AMBER, borderwidth=0,
                            highlightbackground=EDGE, highlightthickness=1,
                            font=font(9), state="disabled", cursor="hand2")
        self.go.pack(side="right")

        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.bind("<Escape>", lambda _e: self._cancel())
        self._check()
        self._centre(parent)
        try:
            from .gui import _dark_titlebar
            self.win.after(0, lambda: _dark_titlebar(self.win))
        except Exception:
            pass
        self.text.focus_set()
        self.win.grab_set()
        self.win.wait_window()

    def _centre(self, parent) -> None:
        """Over the parent, but never off the screen it is on."""
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        except Exception:
            x = y = 80
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        self.win.geometry(f"+{x}+{y}")

    def _typed(self) -> str:
        return self.text.get("1.0", "end").strip()

    def _check(self) -> None:
        from .gui import AMBER, FAINT
        said = self._typed()
        ready = bool(self.started.get()) and len(said.split()) >= MIN_WORDS
        self.go.configure(state="normal" if ready else "disabled",
                          fg=AMBER if ready else FAINT)
        if not self.started.get():
            self.hint.configure(text="pick one above")
        elif not ready:
            self.hint.configure(text="a few words about what happened")
        else:
            self.hint.configure(text="")

    def _ok(self) -> None:
        self.answers = {"started": self.started.get(),
                        "happened": self._typed()}
        self.win.destroy()

    def _cancel(self) -> None:
        self.answers = None
        self.win.destroy()


def ask(parent, game_name: str = "") -> dict | None:
    """Show the dialog and return the answers, or None if it was cancelled."""
    return ReportDialog(parent, game_name).answers
