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
import tkinter.font as tkfont

from .ui import theme as T
from .ui import win
from .ui.kit import Button, Kit

# (value written into the report, what the person reads)
STARTED = (
    ("it started and ran", "it started and ran"),
    ("it started, then closed itself", "it started, then closed itself"),
    ("it never started", "it never started"),
)
MIN_WORDS = 3


class _Go(Button):
    """The canvas button, answering `go["state"]` like the Tk button it
    replaced (the walkthrough reads it)."""

    def __getitem__(self, key):
        if key == "state":
            return "normal" if self.enabled else "disabled"
        raise KeyError(key)


class ReportDialog:
    """Modal. `.answers` is None when the person closed it instead."""

    def __init__(self, parent, game_name: str = "") -> None:
        self.answers: dict | None = None
        self.win = tk.Toplevel(parent)
        self.win.title("report a bug")
        self.win.configure(bg=T.BG)
        self.win.transient(parent)
        self.win.resizable(False, False)

        W, pad = T.px(520), T.px(28)
        inner = W - 2 * pad
        c = self.c = tk.Canvas(self.win, width=W, height=T.px(100), bg=T.BG,
                               highlightthickness=0, bd=0)
        c.pack(fill="both", expand=True)
        k = self.kit = Kit(c)

        def bottom(item) -> int:
            return (c.bbox(item) or (0, 0, 0, 0))[3]

        y = pad
        t = k.text(pad, y, "two questions, then the report opens", T.TEXT, 13, True,
                   anchor="nw")
        t = k.text(pad, bottom(t) + T.px(6),
                   "the card, driver, route, files and logs are filled in for you.",
                   T.MUTED, 9, anchor="nw", width=inner)
        y = bottom(t) + T.px(22)

        t = k.text(pad, y, f"did {game_name or 'the game'} start?", T.TEXT, 10,
                   anchor="nw", width=inner)
        y = bottom(t) + T.px(10)
        self.started = tk.StringVar(master=self.win, value="")
        self._rows: list[tuple[str, int, int, int, int]] = []
        rh = T.px(36)
        for n, (value, label) in enumerate(STARTED, 1):
            tag = k.uid("pick")
            box = c.create_rectangle(pad, y, pad + inner, y + rh, fill=T.SURF,
                                     outline="", tags=tag)
            edge = c.create_rectangle(pad, y, pad + T.px(3), y + rh, fill="",
                                      outline="", tags=tag)
            r = T.px(6)
            cx, cy = pad + T.px(22), y + rh / 2
            dot = c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=T.LINE,
                                width=max(1, T.px(2)), fill="", tags=tag)
            lbl = k.text(pad + T.px(40), cy, label, T.MUTED, 10, tags=tag)
            # The text box has the focus, so the rows answer to ctrl+1..3.
            k.text(pad + inner - T.px(14), cy, f"ctrl+{n}", T.DIM, 8, anchor="e", tags=tag)
            self._rows.append((value, box, edge, dot, lbl))
            k.hover(tag, lambda b=box, v=value: self._hot(b, v, True),
                    lambda b=box, v=value: self._hot(b, v, False))
            k.on_click(tag, lambda v=value: self.started.set(v))
            y += rh + T.px(4)
        self.started.trace_add("write", lambda *_a: (self._paint_rows(), self._check()))

        t = k.text(pad, y + T.px(14), "what happened?", T.TEXT, 10, anchor="nw")
        t = k.text(pad, bottom(t) + T.px(4),
                   "a sentence is enough: what you saw, and what you expected instead.",
                   T.DIM, 9, anchor="nw", width=inner)
        y = bottom(t) + T.px(8)
        line = tkfont.Font(font=T.mono(10)).metrics("linespace")
        th = line * 5 + T.px(20)
        self.text_box = c.create_rectangle(pad, y, pad + inner, y + th, fill=T.SURF,
                                           outline="")
        self.text = tk.Text(c, height=5, width=52, bg=T.SURF, fg=T.TEXT,
                            insertbackground=T.AMBER, selectbackground=T.AMBER,
                            selectforeground=T.BG, font=T.mono(10),
                            highlightthickness=0, borderwidth=0, relief="flat",
                            wrap="word")
        c.create_window(pad + T.px(12), y + T.px(10), window=self.text, anchor="nw",
                        width=inner - T.px(24), height=th - T.px(20))
        self.text.bind("<KeyRelease>", lambda _e: self._check())
        self.text.bind("<FocusIn>", lambda _e: c.itemconfigure(self.text_box, outline=T.LINE))
        self.text.bind("<FocusOut>", lambda _e: c.itemconfigure(self.text_box, outline=""))
        self.hint = k.text(pad, y + th + T.px(14), "", T.DIM, 9)
        y += th + T.px(34)

        bh = T.px(40)
        go_label, cancel_label = "open the report", "cancel"
        go_w = T.width(go_label, T.mono(10, True)) + T.px(44)
        cancel_w = T.width(cancel_label, T.mono(10)) + T.px(44)
        gx = pad + inner - go_w
        self.go = _Go(k, gx, y, go_w, go_label, self._ok, None, "primary", T.AMBER,
                      bh, (), False, "", 10)
        k.button(gx - cancel_w - T.px(10), y, cancel_w, cancel_label, self._cancel,
                 h=bh, size=10)
        c.configure(height=y + bh + pad)

        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.bind("<Escape>", lambda _e: self._cancel())
        for n, (value, _label) in enumerate(STARTED, 1):
            self.win.bind(f"<Control-Key-{n}>", lambda _e, v=value: (self.started.set(v), "break")[1])
        self.text.bind("<Control-Return>", lambda _e: (self._submit(), "break")[1])
        self.win.bind("<Control-Return>", lambda _e: self._submit())
        self._paint_rows()
        self._check()
        self._centre(parent)
        try:
            self.win.after(0, lambda: win.dark_titlebar(self.win))
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

    def _hot(self, box, value, on: bool) -> None:
        if self.started.get() != value:
            self.c.itemconfigure(box, fill=T.SURF2 if on else T.SURF)

    def _paint_rows(self) -> None:
        now = self.started.get()
        for value, box, edge, dot, lbl in self._rows:
            on = value == now
            self.c.itemconfigure(box, fill=T.SURF2 if on else T.SURF)
            self.c.itemconfigure(edge, fill=T.AMBER if on else "")
            self.c.itemconfigure(dot, outline=T.AMBER if on else T.LINE,
                                 fill=T.AMBER if on else "")
            self.c.itemconfigure(lbl, fill=T.TEXT if on else T.MUTED)

    def _typed(self) -> str:
        return self.text.get("1.0", "end").strip()

    def _check(self) -> None:
        said = self._typed()
        ready = bool(self.started.get()) and len(said.split()) >= MIN_WORDS
        self.go.set(enabled=ready)
        if not self.started.get():
            hint = "pick one above"
        elif not ready:
            hint = "a few words about what happened"
        else:
            hint = "ctrl+enter opens it"
        self.c.itemconfigure(self.hint, text=hint)

    def _submit(self) -> None:
        if self.go.enabled:
            self._ok()

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
