"""The window's building blocks, drawn on a Canvas, and the rule that keeps
them easy: whatever is open closes first.

Every control here is canvas items under a tag, with its own hover state and
hand cursor. Nothing looks clickable that is not, and nothing clickable looks
like plain text.

Layers - a menu, a panel, a drawer, a toast - sit on a stack. A click
outside the top one closes it and goes no further; a click on navigation
(`nav=True`) closes it and still goes where it says; Esc closes the top one;
the control that opened a layer closes it again on a second click. The
owner found the first version of this without that rule within a minute.
"""
from __future__ import annotations

import tkinter as tk

from . import theme as T
from .motion import Motion, ink_on, mix


class Layer:
    def __init__(self, tag: str, close, opener: str | None = None, modal: bool = False):
        self.tag, self.close, self.opener, self.modal = tag, close, opener, modal


class Kit:
    def __init__(self, canvas: tk.Canvas, motion: Motion | None = None):
        self.c = canvas
        self.motion = motion or Motion(canvas)
        self.layers: list[Layer] = []
        self._handled = False
        self._tip_job = None
        self._uid = 0
        # Every control drawn, by tag: (kind, label). Lets a test find "install"
        # by its words and press it with real events, and lets the check that
        # every "tick 'X'" in the tool's texts names a real toggle read the
        # window instead of the source.
        self.registry: dict[str, tuple[str, str]] = {}
        # The bindings this kit made, by tag: the items the tag had when it
        # was bound and (sequence, funcid, role) for each. A tag drawn again
        # (the rail's nav_*, the library's card{i}) was bound again with
        # add="+", so every redraw stacked one more hover handler and leaked
        # one more Tcl command; now a bind to a tag whose earlier items are
        # all gone drops the earlier bindings first.
        self._binds: dict[str, dict] = {}
        # the embedded widgets (a Field's Entry) by tag: deleting the canvas
        # item does not destroy the widget, so prune() does
        self._widgets: dict[str, "Field"] = {}
        # what a click on empty canvas does when nothing is open (the shell
        # closes the log drawer with it)
        self.on_background = None
        canvas.bind("<Button-1>", self._background_click, add="+")

    # ------------------------------------------------------------ plumbing
    def uid(self, prefix: str = "k") -> str:
        self._uid += 1
        return f"{prefix}{self._uid}"

    def controls(self, kind: str | None = None) -> list[tuple[str, str, str]]:
        """(tag, kind, label) for every control still on the canvas."""
        out = []
        for tag, (k, label) in list(self.registry.items()):
            if not self.c.find_withtag(tag):
                self.registry.pop(tag, None)
                continue
            if kind is None or k == kind:
                out.append((tag, k, label))
        return out

    def find(self, label: str, kind: str | None = None) -> str | None:
        """The tag of the control whose label is `label` (exact, then prefix)."""
        items = self.controls(kind)
        for tag, _k, lab in items:
            if lab == label:
                return tag
        for tag, _k, lab in items:
            if lab.startswith(label):
                return tag
        return None

    # ------------------------------------------------------------ bindings
    def _bind(self, tag: str, sequence: str, fn, role: str = "") -> None:
        """tag_bind that is remembered. A `role` replaces this kit's earlier
        binding of the same role and sequence on the tag (a second on_click
        on a button means "this instead"), and leaves the other handlers -
        a tip's, a hover's - where they are."""
        c = self.c
        now = set(c.find_withtag(tag))
        rec = self._binds.get(tag)
        if rec is not None and rec["items"] and not (rec["items"] & now):
            self._forget(tag)            # drawn again: the old handlers go
            rec = None
        if rec is None:
            rec = self._binds[tag] = {"items": set(), "ids": []}
        rec["items"] |= now
        if role:
            keep = []
            for seq, fid, r in rec["ids"]:
                if r == role and seq == sequence:
                    self._unbind(tag, seq, fid)
                else:
                    keep.append((seq, fid, r))
            rec["ids"] = keep
        fid = c.tag_bind(tag, sequence, fn, add="+")
        rec["ids"].append((sequence, fid, role))

    def _unbind(self, tag, sequence, fid) -> None:
        try:
            self.c.tag_unbind(tag, sequence, fid)
        except (tk.TclError, ValueError):
            pass

    def _forget(self, tag: str) -> None:
        rec = self._binds.pop(tag, None)
        for seq, fid, _r in (rec or {}).get("ids", ()):
            self._unbind(tag, seq, fid)

    def prune(self) -> None:
        """After a draw: forget the bindings, controls and embedded widgets of
        tags that have no items left. The shell calls it after every page
        draw, so none of them grows with the number of redraws."""
        c = self.c
        for tag in [t for t in self._binds if not c.find_withtag(t)]:
            self._forget(tag)
        for tag in [t for t in self.registry if not c.find_withtag(t)]:
            self.registry.pop(tag, None)
        dead = [t for t in self._widgets if not c.find_withtag(t)]
        if not dead:
            return
        try:
            focus = c.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        for tag in dead:
            old = self._widgets.pop(tag)
            if focus is not None and focus is old.entry:
                # typing in the box when the page redrew: the box drawn in
                # its place takes the focus, not a hidden copy of it
                new = next((f for f in self._widgets.values() if f.placeholder == old.placeholder), None)
                try:
                    if new is not None:
                        new.entry.focus_set()
                        new.entry.icursor("end")
                    else:
                        c.focus_set()
                except tk.TclError:
                    pass
            try:
                old.entry.destroy()
            except tk.TclError:
                pass

    # ------------------------------------------------------------ layers
    def _drop_dead(self) -> None:
        """A layer whose items are gone (a redraw deleted them) is no longer
        open: left on the stack it swallowed the next click anywhere and gave
        Esc an invisible thing to close."""
        if any(not self.c.find_withtag(layer.tag) for layer in self.layers):
            self.layers[:] = [layer for layer in self.layers if self.c.find_withtag(layer.tag)]

    def top(self) -> Layer | None:
        self._drop_dead()
        return self.layers[-1] if self.layers else None

    def push(self, layer: Layer) -> None:
        self.layers.append(layer)

    def remove(self, layer: Layer, animate: bool = True) -> bool:
        """Close this one layer and nothing above it: a toast timing out must
        not close the menu somebody opened after it appeared."""
        if layer not in self.layers:
            return False
        self.layers.remove(layer)
        try:
            layer.close(animate)
        except Exception:
            self.c.delete(layer.tag)
        return True

    def pop(self, layer: Layer | None = None, animate: bool = True) -> bool:
        """Close the top layer (or the given one and everything above it)."""
        self._drop_dead()
        if not self.layers:
            return False
        target = layer or self.layers[-1]
        if target not in self.layers:
            return False
        while self.layers:
            top = self.layers.pop()
            try:
                top.close(animate)
            except Exception:
                self.c.delete(top.tag)
            if top is target:
                break
        return True

    def close_all(self, animate: bool = False) -> None:
        while self.layers:
            self.pop(animate=animate)

    def _layer_of(self, tag: str) -> Layer | None:
        items = self.c.find_withtag(tag)
        if not items:
            return None
        tags = set(self.c.gettags(items[0]))
        for layer in reversed(self.layers):
            if layer.tag in tags:
                return layer
        return None

    def _background_click(self, e) -> None:
        if self._handled:
            self._handled = False
            return
        top = self.top()
        if top is None:
            if self.on_background:
                self.on_background()
            return
        hit = self.c.find_overlapping(self.c.canvasx(e.x), self.c.canvasy(e.y),
                                      self.c.canvasx(e.x), self.c.canvasy(e.y))
        if any(top.tag in self.c.gettags(i) for i in hit):
            return
        self.pop()

    def on_click(self, tag: str, cmd, nav: bool = False) -> None:
        """A click on `tag` runs cmd - after closing whatever is open above it."""
        def click(_e):
            self._handled = True
            top = self.top()
            if top is not None:
                mine = self._layer_of(tag)
                if top.opener == tag:          # second click on the opener
                    self.pop()
                    return
                if mine is not top:
                    self.pop()
                    if not nav:
                        return
            # After the press has been handled, not inside it: a command that
            # opens a modal dialog ran its wait loop inside the press, and on
            # Windows the dialog did not show until the next click ("set up
            # the player" did nothing, then a click on empty space opened it).
            self.c.after(1, cmd)
        self._bind(tag, "<Button-1>", click, role="click")

    def hover(self, tag: str, enter=None, leave=None, cursor: str = "hand2") -> None:
        def on_enter(_e):
            self.c.configure(cursor=cursor)
            if enter:
                enter()

        def on_leave(_e):
            self.c.configure(cursor="")
            if leave:
                leave()
        self._bind(tag, "<Enter>", on_enter)
        self._bind(tag, "<Leave>", on_leave)

    def tip(self, tag: str, text: str) -> None:
        """A short explanation after a moment's rest over `tag`."""
        if not text:
            return

        def show(e):
            self._hide_tip()
            x, y = self.c.canvasx(e.x), self.c.canvasy(e.y)
            self._tip_job = self.c.after(450, lambda: self._draw_tip(x, y, text))

        self._bind(tag, "<Enter>", show)
        self._bind(tag, "<Leave>", lambda _e: self._hide_tip())
        self._bind(tag, "<Button-1>", lambda _e: self._hide_tip())

    def _draw_tip(self, x, y, text):
        f = T.mono(9)
        room = T.px(360)
        pad = T.px(10)
        t = self.c.create_text(0, 0, text=text, font=f, fill=T.TEXT, anchor="nw",
                               width=room, tags="kit_tip")
        x1, y1, x2, y2 = self.c.bbox(t)
        w, h = x2 - x1 + 2 * pad, y2 - y1 + 2 * pad
        vx2 = self.c.canvasx(self.c.winfo_width())
        vy2 = self.c.canvasy(self.c.winfo_height())
        tx = min(x + T.px(14), vx2 - w - T.px(8))
        ty = y + T.px(22) if y + T.px(22) + h < vy2 else y - h - T.px(10)
        self.c.coords(t, tx + pad, ty + pad)
        r = self.c.create_rectangle(tx, ty, tx + w, ty + h, fill=T.SURF2, outline=T.LINE,
                                    tags="kit_tip")
        self.c.tag_lower(r, t)
        self.c.tag_raise("kit_tip")

    def _hide_tip(self):
        if self._tip_job:
            self.c.after_cancel(self._tip_job)
            self._tip_job = None
        self.c.delete("kit_tip")

    # ------------------------------------------------------------ text
    def text(self, x, y, s, colour=T.TEXT, size=10, bold=False, anchor="w",
             tags=(), width=None):
        kw = {"width": width} if width else {}
        return self.c.create_text(x, y, text=s, fill=colour, font=T.mono(size, bold),
                                  anchor=anchor, tags=tags, **kw)

    def glyph(self, x, y, name, colour=T.TEXT, size=12, anchor="center", tags=()):
        return self.c.create_text(x, y, text=T.GLYPH.get(name, name), fill=colour,
                                  font=T.icons(size), anchor=anchor, tags=tags)

    def link(self, x, y, label, cmd, glyph=None, colour=T.MUTED, hot=T.TEXT, size=9,
             tags=(), anchor="w", nav=False, tip=""):
        """Words that do something: an icon, a label, hover brightens them."""
        tag = self.uid("link")
        self.registry[tag] = ("link", label)
        all_tags = (tag,) + tuple(tags)
        f = T.mono(size)
        x0 = x
        wid = T.width(label, f) + (T.px(22) if glyph else 0)
        if anchor == "e":
            x0 = x - wid
        items = []
        if glyph:
            items.append(self.glyph(x0, y, glyph, colour, size + 2, anchor="w", tags=all_tags))
            x0 += T.px(22)
        items.append(self.c.create_text(x0, y, text=label, fill=colour, font=f, anchor="w",
                                        tags=all_tags))
        # a hit area that covers the gap between icon and word
        hit = self.c.create_rectangle(x0 - (T.px(22) if glyph else 0) - T.px(6), y - T.px(13),
                                      x0 + T.width(label, f) + T.px(6), y + T.px(13),
                                      fill="", outline="", tags=all_tags)
        self.c.tag_lower(hit, items[0])
        self.hover(tag, lambda: [self.c.itemconfigure(i, fill=hot) for i in items],
                   lambda: [self.c.itemconfigure(i, fill=colour) for i in items])
        self.on_click(tag, cmd, nav=nav)
        self.tip(tag, tip)
        return tag, wid

    # ------------------------------------------------------------ buttons
    def button(self, x, y, w, label, cmd=None, glyph=None, kind="secondary",
               accent=T.AMBER, h=None, tags=(), enabled=True, tip="", size=11):
        """kind: primary (filled accent), secondary (raised), ghost, danger."""
        return Button(self, x, y, w, label, cmd, glyph, kind, accent, h or T.px(46),
                      tags, enabled, tip, size)

    # ------------------------------------------------------------ inputs
    def dropdown(self, x, y, w, value, options, on_pick, label=None, tags=(),
                 enabled=True, changed=False, accent=T.AMBER, tip="", auto_hint=""):
        return Dropdown(self, x, y, w, value, options, on_pick, label, tags, enabled,
                        changed, accent, tip, auto_hint)

    def toggle(self, x, y, label, value, on_change, tags=(), enabled=True,
               accent=T.AMBER, tip="", width=None):
        return Toggle(self, x, y, label, value, on_change, tags, enabled, accent, tip, width)

    def slider(self, x, y, w, value, lo, hi, step, on_change, tags=(), enabled=True,
               accent=T.AMBER, fmt="{}%"):
        return Slider(self, x, y, w, value, lo, hi, step, on_change, tags, enabled, accent, fmt)

    def field(self, x, y, w, placeholder, on_change=None, on_enter=None, glyph="search",
              tags=(), value="", down=None):
        """down: the Down arrow runs on_enter too. None means only in a search
        box, where Down goes from the box into the results; in the
        save-profile dialog it saved the profile."""
        if down is None:
            down = glyph == "search"
        return Field(self, x, y, w, placeholder, on_change, on_enter, glyph, tags, value, down)

    # ------------------------------------------------------------ menus
    def menu(self, x, y, items, opener=None, width=None, current=None, accent=T.AMBER,
             max_rows=10):
        """A popup list. items: (label, cmd) or (label, cmd, enabled) or None (rule)."""
        return Menu(self, x, y, items, opener, width, current, accent, max_rows)


class Button:
    def __init__(self, kit, x, y, w, label, cmd, glyph, kind, accent, h, tags, enabled,
                 tip, size):
        self.kit, self.c = kit, kit.c
        self.tag = kit.uid("btn")
        kit.registry[self.tag] = ("button", label or glyph or "")
        self.x, self.y, self.w, self.h = x, y, w, h
        self.kind, self.accent, self.cmd = kind, accent, cmd
        self.enabled = enabled
        self.progress = None
        tags = (self.tag,) + tuple(tags)
        self.box = self.c.create_rectangle(x, y, x + w, y + h, outline="", tags=tags)
        self.bar = self.c.create_rectangle(x, y + h - T.px(3), x, y + h, outline="",
                                           fill="", tags=tags)
        f = T.mono(size, kind in ("primary", "danger"))
        self.icon = None
        if glyph and label:
            tw = T.width(label, f) + T.px(26)
            left = x + max(T.px(18), (w - tw) / 2)
            self.icon = self.c.create_text(left, y + h / 2, text=T.GLYPH.get(glyph, glyph),
                                           font=T.icons(size + 1), anchor="w", tags=tags)
            self.label = self.c.create_text(left + T.px(26), y + h / 2, text=label, font=f,
                                            anchor="w", tags=tags)
        elif glyph:
            self.label = self.c.create_text(x + w / 2, y + h / 2, text=T.GLYPH.get(glyph, glyph),
                                            font=T.icons(size + 2), tags=tags)
        else:
            self.label = self.c.create_text(x + w / 2, y + h / 2, text=label, font=f, tags=tags)
        self._paint(False)
        kit.hover(self.tag, lambda: self._paint(True), lambda: self._paint(False))
        kit.on_click(self.tag, self._click)
        kit.tip(self.tag, tip)

    def _colours(self, hot):
        if self.kind == "primary":
            fill = self.accent
            ink = ink_on(fill)
        elif self.kind == "danger":
            fill = T.WARN
            ink = ink_on(fill)
        elif self.kind == "ghost":
            fill = T.SURF if hot else ""
            ink = T.TEXT
        else:
            fill = T.LINE if hot else T.SURF2
            ink = T.TEXT
        if hot and self.kind in ("primary", "danger"):
            fill = mix(fill, "#ffffff", 0.14)
        if not self.enabled:
            fill = T.SURF if self.kind != "ghost" else ""
            ink = T.DIM
        return fill, ink

    def _paint(self, hot):
        fill, ink = self._colours(hot and self.enabled)
        try:
            self.c.itemconfigure(self.box, fill=fill)
            self.c.itemconfigure(self.label, fill=ink)
            if self.icon:
                self.c.itemconfigure(self.icon, fill=ink)
        except tk.TclError:
            pass

    def _click(self):
        if self.enabled and self.cmd:
            self.cmd()

    def set(self, label=None, enabled=None, progress=-1):
        if label is not None:
            self.c.itemconfigure(self.label, text=label)
        if enabled is not None:
            self.enabled = enabled
            self._paint(False)
        if progress != -1:
            self.progress = progress
            if progress is None:
                self.c.coords(self.bar, self.x, self.y + self.h - T.px(3), self.x, self.y + self.h)
                self.c.itemconfigure(self.bar, fill="")
            else:
                p = max(0.0, min(1.0, progress))
                self.c.coords(self.bar, self.x, self.y + self.h - T.px(3),
                              self.x + self.w * p, self.y + self.h)
                ink = ink_on(self.accent) if self.kind == "primary" else self.accent
                self.c.itemconfigure(self.bar, fill=mix(self.accent, ink, 0.5))


class Dropdown:
    """A labelled field that opens a Menu of choices.

    options: list of (value, label). "auto" as a value is shown dim with what
    auto resolves to (auto_hint), so a person can see what the tool will do
    without choosing anything.
    """

    def __init__(self, kit, x, y, w, value, options, on_pick, label, tags, enabled,
                 changed, accent, tip, auto_hint):
        self.kit, self.c = kit, kit.c
        self.tag = kit.uid("dd")
        kit.registry[self.tag] = ("dropdown", label or "")
        self.x, self.y, self.w = x, y, w
        self.options, self.on_pick, self.accent = options, on_pick, accent
        self.value, self.enabled, self.changed = value, enabled, changed
        self.auto_hint = auto_hint
        tags = (self.tag,) + tuple(tags)
        top = y
        if label:
            kit.text(x, y, label, T.DIM, 9, tags=tuple(tags[1:]))
            top = y + T.px(16)
        self.h = T.px(38)
        self.top = top
        self.box = self.c.create_rectangle(x, top, x + w, top + self.h, fill=T.SURF,
                                           outline="", tags=tags)
        self.edge = self.c.create_rectangle(x, top, x + T.px(3), top + self.h, outline="",
                                            fill="", tags=tags)
        self.lbl = self.c.create_text(x + T.px(14), top + self.h / 2, anchor="w",
                                      font=T.mono(10), tags=tags)
        self.hint = self.c.create_text(x + w - T.px(30), top + self.h / 2, anchor="e",
                                       font=T.mono(8), fill=T.DIM, tags=tags)
        self.arrow = self.c.create_text(x + w - T.px(12), top + self.h / 2, anchor="e",
                                        text=T.GLYPH["down"], font=T.icons(8), fill=T.DIM, tags=tags)
        self._render()
        kit.hover(self.tag, lambda: self._hot(True), lambda: self._hot(False),
                  cursor="hand2" if enabled else "")
        kit.on_click(self.tag, self.open)
        kit.tip(self.tag, tip)
        self.bottom = top + self.h

    def _label_of(self, value):
        for v, lab in self.options:
            if v == value:
                return lab
        return str(value)

    def _render(self):
        # "auto" is the value "auto", or an empty value on a control that
        # says what auto resolves to - "" means a real choice elsewhere.
        auto = self.value == "auto" or (self.value in ("", None) and bool(self.auto_hint))
        text = self.auto_hint if auto and self.auto_hint else self._label_of(self.value)
        room = self.w - T.px(14) - T.px(30)
        f = T.mono(10, self.changed)
        col = T.DIM if not self.enabled else (self.accent if self.changed else T.TEXT)
        self.c.itemconfigure(self.lbl, text=T.fit(text, f, room), font=f, fill=col)
        self.c.itemconfigure(self.hint, text="")
        self.c.itemconfigure(self.edge, fill=self.accent if self.changed and self.enabled else "")
        self.c.itemconfigure(self.arrow, fill=T.DIM if self.enabled else T.SURF2)

    def _hot(self, on):
        if self.enabled:
            self.c.itemconfigure(self.box, fill=T.SURF2 if on else T.SURF)

    def open(self):
        if not self.enabled:
            return
        items = [(lab, (lambda v=v: self.pick(v))) for v, lab in self.options]
        cur = next((i for i, (v, _l) in enumerate(self.options) if v == self.value), None)
        self.kit.menu(self.x, self.bottom + T.px(2), items, opener=self.tag, width=self.w,
                      current=cur, accent=self.accent)

    def pick(self, value):
        self.value = value
        self._render()
        if self.on_pick:
            self.on_pick(value)

    def set(self, value=None, options=None, enabled=None, changed=None, auto_hint=None):
        if options is not None:
            self.options = options
        if value is not None:
            self.value = value
        if enabled is not None:
            self.enabled = enabled
        if changed is not None:
            self.changed = changed
        if auto_hint is not None:
            self.auto_hint = auto_hint
        self._render()


class Toggle:
    def __init__(self, kit, x, y, label, value, on_change, tags, enabled, accent, tip, width):
        self.kit, self.c = kit, kit.c
        self.tag = kit.uid("tg")
        kit.registry[self.tag] = ("toggle", label)
        self.value, self.enabled, self.accent, self.on_change = bool(value), enabled, accent, on_change
        tags = (self.tag,) + tuple(tags)
        s = T.px(18)
        self.box = self.c.create_rectangle(x, y - s / 2, x + s, y + s / 2, outline=T.LINE,
                                           width=max(1, T.px(1)), tags=tags)
        self.mark = self.c.create_text(x + s / 2, y, text=T.GLYPH["check"], font=T.icons(9),
                                       tags=tags)
        f = T.mono(10)
        wrap = (width - s - T.px(12)) if width else None
        kw = {"width": wrap} if wrap else {}
        self.lbl = self.c.create_text(x + s + T.px(12), y, text=label, anchor="w", font=f,
                                      tags=tags, **kw)
        x1, y1, x2, y2 = self.c.bbox(self.lbl)
        self.hit = self.c.create_rectangle(x - T.px(4), min(y1, y - s / 2) - T.px(4), x2 + T.px(6),
                                           max(y2, y + s / 2) + T.px(4), fill="", outline="", tags=tags)
        self.bottom = max(y2, y + s / 2)
        self._render()
        kit.hover(self.tag, lambda: self._hot(True), lambda: self._hot(False),
                  cursor="hand2" if enabled else "")
        kit.on_click(self.tag, self.flip)
        kit.tip(self.tag, tip)

    def _render(self):
        on, en = self.value, self.enabled
        self.c.itemconfigure(self.box, fill=(self.accent if on else T.SURF) if en else T.SURF,
                             outline=self.accent if on and en else T.LINE)
        self.c.itemconfigure(self.mark, fill=ink_on(self.accent) if on and en else
                             (T.DIM if on else ""))
        self.c.itemconfigure(self.lbl, fill=(T.TEXT if on else T.MUTED) if en else T.DIM)

    def _hot(self, hot):
        if self.enabled and not self.value:
            self.c.itemconfigure(self.box, outline=T.MUTED if hot else T.LINE)

    def flip(self):
        if not self.enabled:
            return
        self.value = not self.value
        self._render()
        if self.on_change:
            self.on_change(self.value)

    def set(self, value=None, enabled=None):
        if value is not None:
            self.value = bool(value)
        if enabled is not None:
            self.enabled = enabled
        self._render()


class Slider:
    def __init__(self, kit, x, y, w, value, lo, hi, step, on_change, tags, enabled, accent, fmt):
        self.kit, self.c = kit, kit.c
        self.tag = kit.uid("sl")
        kit.registry[self.tag] = ("slider", "")
        self.x, self.y, self.w = x, y, w
        self.lo, self.hi, self.step = lo, hi, step
        self.value, self.enabled, self.accent, self.on_change, self.fmt = value, enabled, accent, on_change, fmt
        tags = (self.tag,) + tuple(tags)
        t = T.px(4)
        self.track = self.c.create_rectangle(x, y - t / 2, x + w, y + t / 2, fill=T.SURF2,
                                             outline="", tags=tags)
        self.fill = self.c.create_rectangle(x, y - t / 2, x, y + t / 2, outline="", tags=tags)
        r = T.px(9)
        self.r = r
        self.knob = self.c.create_oval(x - r, y - r, x + r, y + r, outline="", tags=tags)
        self.hit = self.c.create_rectangle(x - r, y - T.px(16), x + w + r, y + T.px(16),
                                           fill="", outline="", tags=tags)
        self.c.tag_lower(self.hit, self.track)
        self._render()
        kit.hover(self.tag, cursor="hand2" if enabled else "")
        kit._bind(self.tag, "<Button-1>", self._press, role="click")
        kit._bind(self.tag, "<B1-Motion>", self._drag, role="drag")
        kit._bind(self.tag, "<ButtonRelease-1>", self._release, role="release")

    def _render(self):
        k = (self.value - self.lo) / max(1, self.hi - self.lo)
        px_ = self.x + self.w * k
        t = T.px(4)
        col = self.accent if self.enabled else T.DIM
        self.c.coords(self.fill, self.x, self.y - t / 2, px_, self.y + t / 2)
        self.c.itemconfigure(self.fill, fill=col)
        self.c.coords(self.knob, px_ - self.r, self.y - self.r, px_ + self.r, self.y + self.r)
        self.c.itemconfigure(self.knob, fill=col)

    def _value_at(self, e):
        cx = self.c.canvasx(e.x)
        k = max(0.0, min(1.0, (cx - self.x) / self.w))
        v = self.lo + k * (self.hi - self.lo)
        return int(round(v / self.step) * self.step)

    def _press(self, e):
        self.kit._handled = True
        top = self.kit.top()
        if top is not None and self.kit._layer_of(self.tag) is not top:
            self.kit.pop()
            return
        if self.enabled:
            self._drag(e)

    def _drag(self, e):
        if not self.enabled:
            return
        v = max(self.lo, min(self.hi, self._value_at(e)))
        if v != self.value:
            self.value = v
            self._render()
            if self.on_change:
                self.on_change(v, False)

    def _release(self, _e):
        if self.enabled and self.on_change:
            self.on_change(self.value, True)

    def set(self, value=None, enabled=None):
        if value is not None:
            self.value = value
        if enabled is not None:
            self.enabled = enabled
        self._render()


class Field:
    """A text box: a real Entry on the canvas, a placeholder, a clear button.

    The placeholder lives inside the Entry itself (dim text while empty and
    not focused): a canvas item under an embedded window is never visible.
    """

    def __init__(self, kit, x, y, w, placeholder, on_change, on_enter, glyph, tags, value, down=False):
        self.kit, self.c = kit, kit.c
        self.tag = kit.uid("fd")
        kit.registry[self.tag] = ("field", placeholder)
        self.placeholder = placeholder
        self._ph_on = False
        h = T.px(40)
        tags = (self.tag,) + tuple(tags)
        self.box = self.c.create_rectangle(x, y, x + w, y + h, fill=T.SURF, outline="", tags=tags)
        left = x + T.px(12)
        if glyph:
            kit.glyph(x + T.px(20), y + h / 2, glyph, T.DIM, 11, tags=tags)
            left = x + T.px(38)
        self.entry = tk.Entry(self.c, bg=T.SURF, fg=T.TEXT, insertbackground=T.AMBER,
                              relief="flat", font=T.mono(10), highlightthickness=0, borderwidth=0,
                              selectbackground=T.AMBER, selectforeground=T.BG, disabledbackground=T.SURF)
        self.win = self.c.create_window(left, y + h / 2, window=self.entry, anchor="w",
                                        width=w - (left - x) - T.px(34), height=T.px(26), tags=tags)
        kit._widgets[self.tag] = self
        self.clear = self.c.create_text(x + w - T.px(16), y + h / 2, text=T.GLYPH["close"],
                                        font=T.icons(9), fill=T.DIM, tags=tags + (self.tag + "x",))
        kit.hover(self.tag + "x", lambda: self.c.itemconfigure(self.clear, fill=T.TEXT),
                  lambda: self.c.itemconfigure(self.clear, fill=T.DIM))
        kit.on_click(self.tag + "x", self.reset)
        kit.hover(self.tag, cursor="xterm")
        kit._bind(self.tag, "<Button-1>", lambda _e: self.entry.focus_set())
        self.on_change, self.on_enter = on_change, on_enter
        if value:
            self.entry.insert(0, value)
        self._last = value or ""
        self.entry.bind("<KeyRelease>", lambda _e: self._changed())
        self.entry.bind("<<Paste>>", lambda _e: self.c.after(1, self._changed), add="+")
        self.entry.bind("<FocusIn>", lambda _e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._focus(False))
        self.entry.bind("<Escape>", self._escape)
        if on_enter:
            self.entry.bind("<Return>", lambda _e: on_enter())
            if down:
                self.entry.bind("<Down>", lambda _e: on_enter())
        self.bottom = y + h
        self._focus(False)

    def _show_placeholder(self, on: bool):
        if on and not self._ph_on and not self.entry.get():
            self._ph_on = True
            self.entry.insert(0, self.placeholder)
            self.entry.configure(fg=T.DIM)
        elif not on and self._ph_on:
            self._ph_on = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=T.TEXT)

    def _focus(self, focused: bool):
        self._show_placeholder(not focused)
        self.c.itemconfigure(self.box, outline=T.LINE if focused else "")
        self._paint_clear()

    def _paint_clear(self):
        self.c.itemconfigure(self.clear, state="normal" if self.get() else "hidden")

    def _changed(self):
        if self._ph_on:
            return
        now = self.entry.get()
        if now == self._last:
            return
        self._last = now
        self._paint_clear()
        if self.on_change:
            self.on_change(now)

    def _escape(self, _e):
        if self.get():
            self.reset()
        else:
            # empty: leave the box, and the same key does what it does anywhere
            # else in the window - close what is open, then go back
            self.c.focus_set()
            self.c.event_generate("<Escape>")
        return "break"

    def reset(self):
        focused = self.c.focus_get() is self.entry
        self._show_placeholder(False)
        self.entry.delete(0, "end")
        self._changed()
        if not focused:
            self._show_placeholder(True)

    def get(self) -> str:
        return "" if self._ph_on else self.entry.get()

    def focus(self, append: str = ""):
        self.entry.focus_set()
        self._show_placeholder(False)
        if append:
            self.entry.insert("end", append)
            self._changed()


class Menu:
    """A popup list under a control. Scrolls when it is long."""

    def __init__(self, kit, x, y, items, opener, width, current, accent, max_rows):
        self.kit, self.c = kit, kit.c
        # a menu replaces any other open menu
        for layer in list(kit.layers):
            if layer.tag.startswith("menu"):
                kit.pop(layer, animate=False)
        self.tag = kit.uid("menu")
        self.items = items
        self.current, self.accent = current, accent
        self.row = T.px(34)
        f = T.mono(10)
        self.w = width or max(T.px(180), max((T.width(i[0], f) for i in items if i), default=0) + T.px(60))
        self.rows = min(len(items), max_rows)
        self.offset = 0
        h = self.rows * self.row + T.px(8)
        vy1 = self.c.canvasy(0)
        vy2 = self.c.canvasy(self.c.winfo_height())
        vx2 = self.c.canvasx(self.c.winfo_width())
        if y + h > vy2 - T.px(8) and y - h - T.px(50) > vy1:
            y = y - h - T.px(46)          # open upwards when there is no room below
        x = min(x, vx2 - self.w - T.px(8))
        self.x, self.y, self.h = x, y, h
        if current is not None and current >= self.rows:
            self.offset = min(current - self.rows // 2, len(items) - self.rows)
        layer = Layer(self.tag, self._close, opener=opener)
        layer.wheel = self._wheel_if_inside
        kit.push(layer)
        # A real Entry on the canvas always paints above canvas items, so a
        # menu over a search box had the box cut through it: hide what the
        # menu covers while it is open.
        self.covered = [i for i in self.c.find_overlapping(x, y, x + self.w, y + h)
                        if self.c.type(i) == "window" and self.c.itemcget(i, "state") != "hidden"]
        for i in self.covered:
            self.c.itemconfigure(i, state="hidden")
        self._draw()

    def _draw(self):
        c, tag = self.c, self.tag
        c.delete(tag)
        x, y, w = self.x, self.y, self.w
        c.create_rectangle(x, y, x + w, y + self.h, fill=T.SURF2, outline=T.LINE, tags=tag)
        visible = self.items[self.offset:self.offset + self.rows]
        for i, item in enumerate(visible):
            idx = self.offset + i
            ry = y + T.px(4) + i * self.row
            if item is None:
                c.create_line(x + T.px(10), ry + self.row / 2, x + w - T.px(10), ry + self.row / 2,
                              fill=T.LINE, tags=tag)
                continue
            label, cmd = item[0], item[1]
            enabled = item[2] if len(item) > 2 else True
            rt = f"{tag}r{idx}"
            r = c.create_rectangle(x + 1, ry, x + w - 1, ry + self.row, fill=T.SURF2, outline="",
                                   tags=(tag, rt))
            cur = idx == self.current
            col = self.accent if cur else (T.TEXT if enabled else T.DIM)
            c.create_text(x + T.px(14), ry + self.row / 2, text=T.fit(label, T.mono(10, cur), w - T.px(48)),
                          fill=col, font=T.mono(10, cur), anchor="w", tags=(tag, rt))
            if cur:
                c.create_text(x + w - T.px(14), ry + self.row / 2, text=T.GLYPH["check"],
                              font=T.icons(9), fill=self.accent, anchor="e", tags=(tag, rt))
            if enabled:
                self.kit.hover(rt, lambda r=r: c.itemconfigure(r, fill=T.LINE),
                               lambda r=r: c.itemconfigure(r, fill=T.SURF2))
                self.kit._bind(rt, "<Button-1>", lambda _e, cmd=cmd: self._pick(cmd), role="click")
        if len(self.items) > self.rows:
            k = self.offset / (len(self.items) - self.rows)
            th = self.h * self.rows / len(self.items)
            ty = y + (self.h - th) * k
            c.create_rectangle(x + w - T.px(4), ty, x + w - T.px(1), ty + th, fill=T.MUTED,
                               outline="", tags=tag)
        c.tag_raise(tag)

    def _wheel_if_inside(self, e) -> bool:
        """The wheel over an open menu scrolls the menu, never the page."""
        try:
            x = self.c.canvasx(e.x_root - self.c.winfo_rootx())
            y = self.c.canvasy(e.y_root - self.c.winfo_rooty())
        except Exception:
            return False
        if not (self.x <= x <= self.x + self.w and self.y <= y <= self.y + self.h):
            return False
        if len(self.items) > self.rows:
            step = -1 if e.delta > 0 else 1
            self.offset = max(0, min(len(self.items) - self.rows, self.offset + step))
            self._draw()
        return True

    def _pick(self, cmd):
        self.kit._handled = True
        self.kit.pop(animate=False)
        cmd()

    def _close(self, animate):
        self.c.delete(self.tag)
        for i in self.covered:
            try:
                self.c.itemconfigure(i, state="normal")
            except tk.TclError:
                pass
