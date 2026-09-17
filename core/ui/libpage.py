"""The library page: covers, search, filters, one glance at every game.

The header (title, search, scan) is drawn once per width; the grid below it
is redrawn from state whenever something changes, so typing in the search
box never loses focus to a redraw. Hovering a cover lifts it in the game's
own colour and warms the page's glow towards that colour.
"""
from __future__ import annotations

from . import imaging
from . import theme as T
from .ctl_library import ARCH, FILTERS, SORTS
from .motion import mix
from .shell import Page


class LibraryPage(Page):
    name = "library"
    rail = 0

    def __init__(self, shell, app):
        super().__init__(shell)
        self.app = app
        self.field = None
        self.cards: list = []
        self.sel = None
        self.heat: dict[str, float] = {}
        self.glow_now = (T.BG, 0)
        self.width = 0
        self.cols = 1

    # ------------------------------------------------------------ layout
    def metrics(self, width):
        pad = T.px(44)
        cw, ch = T.px(176), T.px(264)
        gap_min = T.px(22)
        room = width - 2 * pad
        cols = max(2, (room + gap_min) // (cw + gap_min))
        gap = (room - cols * cw) / max(1, cols - 1)
        return pad, cw, ch, cols, gap

    def draw(self, width: int) -> int:
        c, k, a = self.c, self.kit, self.app
        self.width = width
        pad = T.px(44)
        tags = ("page",)
        # The glow is a stack of flat bands recoloured in place. It was a
        # gradient image zoomed to the page width, rebuilt on every animation
        # frame - forty full-width images in half a second each time the
        # pointer crossed a cover, which is what made scrolling stutter.
        self.glow = []
        bands, gh = 40, T.px(340)
        for b in range(bands):
            y0 = int(gh * b / bands)
            y1 = int(gh * (b + 1) / bands) + 1
            self.glow.append(c.create_rectangle(0, y0, width, y1, fill=T.BG, outline="",
                                                tags=tags + ("glow",)))
        self._set_glow(*self.glow_now)
        c.create_text(pad, T.px(50), text="games", font=T.mono(22, True), fill=T.TEXT, anchor="w",
                      tags=tags)
        # what is in it, in words, under the title - a bare number beside it
        # read as a badge nobody could explain
        self.count = c.create_text(pad + T.px(2), T.px(84), text="", font=T.mono(10), fill=T.MUTED,
                                   anchor="w", tags=tags)
        # scan, with a menu of the other ways to fill the library
        bw = T.px(132)
        bx = width - pad - bw
        self.scan_btn = k.button(bx, T.px(36), bw, "scan", self.scan_menu, glyph="scan",
                                 tags=tags, h=T.px(44), tip="rescan, full rescan, choose a folder, update all")
        fw = min(T.px(360), bx - T.px(12) - pad - T.px(220))
        prev = self.field.get() if self.field else a.query
        self.field = k.field(bx - T.px(12) - fw, T.px(38), fw, "search   ctrl+f",
                             on_change=self._typed, on_enter=self._first, tags=tags, value=prev)
        self.header_h = T.px(150)
        h = self.refresh(first_draw=True)
        return h

    def refresh(self, first_draw: bool = False) -> int:
        """Filters and grid only - the header and its search box stay."""
        # counts(), visible() and every card ask each game's card once per pass
        with self.app.card_pass():
            return self._refresh()

    def _refresh(self) -> int:
        c, k, a = self.c, self.kit, self.app
        c.delete("grid")
        width = self.width
        pad, cw, ch, cols, gap = self.metrics(width)
        self.cols = cols
        tags = ("page", "grid")
        n = a.counts()
        shown = a.visible()
        total = n["all"]
        said = [f"{len(shown)} of {total} games" if len(shown) != total else f"{total} games"]
        if n["installed"]:
            said.append(f"{n['installed']} with dlss 5")
        # a scan that stopped (GitHub's rate limit, a store that did not
        # answer) says so where the games are, not only in the closed log
        if a.scan_failed and not a.scanning:
            said.append(a.scan_failed)
        c.itemconfigure(self.count, text=T.fit("  \u00b7  ".join(said), T.mono(10), width - 2 * pad))
        # the filter tabs
        y = T.px(124)
        x = pad
        for f in FILTERS:
            if f in ("working", "needs a look", "update") and not n[f] and a.filter != f:
                continue
            on = a.filter == f
            font = T.mono(10, on)
            label = f
            tag = k.uid("flt")
            wid = T.width(label, font)
            c.create_rectangle(x - T.px(6), y - T.px(14), x + wid + T.px(34), y + T.px(16), fill="",
                               outline="", tags=tags + (tag,))
            t = c.create_text(x, y, text=label, font=font, fill=T.AMBER if on else T.MUTED, anchor="w",
                              tags=tags + (tag,))
            if f != "all" and n[f]:
                colour = {"update": T.AMBER, "needs a look": T.WARN, "working": T.OK}.get(f, T.DIM)
                c.create_text(x + wid + T.px(8), y, text=str(n[f]), font=T.mono(10, True),
                              fill=colour, anchor="w", tags=tags + (tag,))
                wid += T.px(8) + T.width(str(n[f]), T.mono(10, True))
            if on:
                c.create_rectangle(x, y + T.px(13), x + T.width(label, font), y + T.px(15), fill=T.AMBER,
                                   outline="", tags=tags)
            else:
                k.hover(tag, lambda t=t: c.itemconfigure(t, fill=T.TEXT),
                        lambda t=t: c.itemconfigure(t, fill=T.MUTED))
            k.on_click(tag, lambda f=f: a.set_filter(f))
            x += wid + T.px(30)
        # view menu: architecture, hidden games, sort, scan at start
        k.link(width - pad, y, "view", self.view_menu, glyph="more", anchor="e", tags=tags,
               tip="sort, 32/64-bit, hidden games")
        right = width - pad - T.px(90)
        if a.arch != "all" or a.show_hidden or a.sort:
            bits = [dict(ARCH)[a.arch]] if a.arch != "all" else []
            if a.show_hidden:
                bits.append("hidden shown")
            if a.sort:
                bits.append("by " + dict(SORTS)[a.sort] + (" (desc)" if a.sort_desc else ""))
            c.create_text(right, y, text="  \u00b7  ".join(bits), font=T.mono(9), fill=T.DIM, anchor="e",
                          tags=tags)
        # scanning line
        if a.scanning:
            c.create_rectangle(pad, y + T.px(26), width - pad, y + T.px(28), fill=T.SURF, outline="",
                               tags=tags)
            self._bar = c.create_rectangle(pad, y + T.px(26), pad + T.px(120), y + T.px(28), fill=T.AMBER,
                                           outline="", tags=tags)
            self._sweep()
        top = self.header_h
        self.cards = []
        if not shown:
            return self._empty(top, n)
        for i, g in enumerate(shown):
            r, col = divmod(i, cols)
            x = int(pad + col * (cw + gap))
            yy = top + r * (ch + T.px(64))
            self._card(i, g, x, yy, cw, ch)
        rows = (len(shown) + cols - 1) // cols
        foot = top + rows * (ch + T.px(64)) + T.px(10)
        # only what the person can act on: games that are hidden (and where to get them back)
        if n["hidden"] and not a.show_hidden:
            c.create_text(pad, foot, text=f"{n['hidden']} hidden  \u00b7  view shows them",
                          font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
        if self.sel is not None and self.sel < len(self.cards):
            self._heat(self.sel, 1.0, ms=1)
        return foot + T.px(40)

    def _empty(self, top, n) -> int:
        c, k, a = self.c, self.kit, self.app
        pad = T.px(44)
        tags = ("page", "grid")
        if not a.have_library():
            if a.scanning:
                c.create_text(pad, top + T.px(40), text="finding your games...", font=T.mono(14, True),
                              fill=T.TEXT, anchor="w", tags=tags)
                c.create_text(pad, top + T.px(74), text="Steam, Epic, GOG, EA, Ubisoft, Xbox and the usual folders",
                              font=T.mono(10), fill=T.MUTED, anchor="w", tags=tags)
                return top + T.px(200)
            c.create_text(pad, top + T.px(40), text="dlss 5 for the games you have", font=T.mono(16, True),
                          fill=T.TEXT, anchor="w", tags=tags)
            c.create_text(pad, top + T.px(76),
                          text="finds your games, reads each one, and picks the route that fits it.",
                          font=T.mono(10), fill=T.MUTED, anchor="w", tags=tags)
            k.button(pad, top + T.px(110), T.px(240), "find my games", lambda: a.scan(full=True),
                     glyph="search", kind="primary", tags=tags)
            k.button(pad + T.px(252), top + T.px(110), T.px(220), "choose a folder", a.pick_folder,
                     glyph="folder", tags=tags)
            y = top + T.px(200)
            for line, colour in (
                    ("works best on 64-bit DirectX 11/12. DirectX 9, OpenGL, Vulkan and 32-bit", T.DIM),
                    ("games go through extra layers and fail far more often.", T.DIM),
                    ("never online: anti-cheat treats ReShade add-ons as tampering.", T.AMBER)):
                c.create_text(pad, y, text=line, font=T.mono(9), fill=colour, anchor="w", tags=tags)
                y += T.px(22)
            return y + T.px(60)
        q = a.query.strip()
        if q:
            msg = f'no game matches "{q}"'
        elif a.filter != "all":
            msg = f"no games are '{a.filter}' right now"
        elif n["hidden"] and not a.show_hidden:
            msg = f"all {n['hidden']} games are hidden"
        else:
            msg = "no games to show with this view"
        c.create_text(pad, top + T.px(40), text=msg, font=T.mono(12), fill=T.MUTED, anchor="w", tags=tags)
        k.link(pad, top + T.px(76), "show all games", self._reset_view, glyph="refresh", colour=T.AMBER,
               hot=T.TEXT, tags=tags)
        return top + T.px(160)

    # ------------------------------------------------------------ a card
    def _card(self, i, g, x, y, cw, ch):
        c, k, a = self.c, self.kit, self.app
        # bound through a tag of its own: "card{i}" is reused by every redraw,
        # and its bindings piled up on the next grid's card of that number.
        # card{i} stays on the items as the name the checks find a card by.
        tag = k.uid("card")
        tags = ("page", "grid", tag, f"card{i}")
        info = a.card(g)
        cov = a.cover(g, cw, ch)
        items = {"ring": c.create_rectangle(x - T.px(4), y - T.px(4), x + cw + T.px(4), y + ch + T.px(4),
                                            outline="", width=T.px(2), tags=tags)}
        levels = []
        if cov and cov["kind"] == "cover":
            levels = [cov["dim"]] if info["dim"] else cov["levels"]
            items["img"] = c.create_image(x, y, image=levels[0], anchor="nw", tags=tags)
        else:
            c.create_rectangle(x, y, x + cw, y + ch, fill=T.SURF, outline="", tags=tags)
            if cov and cov["kind"] == "icon" and cov["levels"]:
                c.create_image(x + cw / 2, y + ch / 2 - T.px(12), image=cov["levels"][0], tags=tags)
            else:
                c.create_text(x + cw / 2, y + ch / 2 - T.px(12), text=T.GLYPH["game"], font=T.icons(30),
                              fill=T.LINE, tags=tags)
        accent = (cov or {}).get("accent") or T.AMBER
        name_col = T.DIM if info["dim"] else T.TEXT
        nx = x
        if info["dot"]:
            c.create_oval(x, y + ch + T.px(15), x + T.px(8), y + ch + T.px(23), fill=info["colour"],
                          outline="", tags=tags)
            nx = x + T.px(16)
        items["name"] = c.create_text(nx, y + ch + T.px(19), text=T.fit(g.name, T.mono(10, True), x + cw - nx),
                                      font=T.mono(10, True), fill=name_col, anchor="w", tags=tags)
        if info["status"]:
            c.create_text(x, y + ch + T.px(41), text=T.fit(info["status"], T.mono(8), cw), font=T.mono(8),
                          fill=info["colour"], anchor="w", tags=tags)
        self.cards.append({"g": g, "tag": tag, "items": items, "levels": levels, "accent": accent,
                           "name_col": name_col})
        key = str(g.folder)
        self.heat[key] = 0.0
        # not while the wheel is moving the page: every cover passing under
        # the pointer would light up and pull the glow along
        k.hover(tag, lambda i=i: None if self.shell.scrolling() else self._select(i))
        k.on_click(tag, lambda g=g: a.open_game(g))
        # through the kit, which forgets it with the card: a bare tag_bind left
        # one Tcl command behind per card per redraw
        k._bind(tag, "<Button-3>", lambda e, g=g: self._card_menu(e, g), role="menu")

    # ------------------------------------------------------------ hover and glow
    def _select(self, i, glow=True):
        if i is None or i >= len(self.cards):
            return
        if self.sel is not None and self.sel != i and self.sel < len(self.cards):
            self._heat(self.sel, 0.0)
        self.sel = i
        self._heat(i, 1.0)
        if glow:
            if getattr(self, "_rest", None):
                self.c.after_cancel(self._rest)
            accent = self.cards[i]["accent"]
            self._rest = self.c.after(90, lambda: self.sel == i and self._glow_to(accent))

    def _heat(self, i, to, ms=None):
        card = self.cards[i]
        key = str(card["g"].folder)
        k0 = self.heat.get(key, 0.0)
        items, levels = card["items"], card["levels"]
        c = self.c

        def step(kk):
            h = k0 + (to - k0) * kk
            self.heat[key] = h
            if levels and "img" in items:
                c.itemconfigure(items["img"], image=levels[round(h * (len(levels) - 1))])
            c.itemconfigure(items["ring"], outline=mix(T.BG, card["accent"], h) if h > 0.02 else "")
            c.itemconfigure(items["name"], fill=mix(card["name_col"], card["accent"], h))
        self.shell.motion.run(card["tag"], ms or (220 if to else 320), step)

    def _set_glow(self, colour, alpha):
        """colour at `alpha` (0-255) over the page at the top, fading to nothing."""
        try:
            n = len(self.glow)
            for b, item in enumerate(self.glow):
                k = alpha / 255.0 * (1.0 - (b + 0.5) / n)
                self.c.itemconfigure(item, fill=mix(T.BG, colour, k))
            self.glow_now = (colour, alpha)
        except Exception:
            pass

    def _glow_to(self, colour, alpha=34):
        c0, a0 = self.glow_now

        def step(k):
            self._set_glow(mix(c0, colour, k), a0 + (alpha - a0) * k)
        self.shell.motion.run("glow", 520, step)

    def _sweep(self):
        """The scanning line: a short bar travelling across, while scanning."""
        pad = T.px(44)
        span = self.width - 2 * pad - T.px(120)
        c = self.c

        def loop(k):
            try:
                x = pad + span * k
                y1 = c.coords(self._bar)[1]
                c.coords(self._bar, x, y1, x + T.px(120), y1 + T.px(2))
            except Exception:
                pass

        def again():
            if self.app.scanning and self.shell.page is self:
                self.shell.motion.run("sweep", 1400, loop, again)
        self.shell.motion.run("sweep", 1400, loop, again)

    # ------------------------------------------------------------ menus
    def scan_menu(self):
        a = self.app
        items = [("rescan  -  look for new games", lambda: a.scan()),
                 ("full rescan  -  read every game again", lambda: a.scan(full=True)),
                 ("choose a folder  -  one game by hand", a.pick_folder)]
        # the number update all acts on, not the update tab's: a 'reinstall -
        # was DX11' card and a game with anti-cheat are on the tab, and update
        # all leaves both alone
        todo, _skipped = a.update_targets()
        if todo:
            items.append((f"update all  -  {len(todo)} with newer parts", a.update_all))
        x1, y1, x2, y2 = self.c.bbox(self.scan_btn.tag)
        self.kit.menu(x2 - T.px(380), y2 + T.px(4), items, opener=self.scan_btn.tag, width=T.px(380))

    def view_menu(self):
        from .. import prefs
        a = self.app
        n = a.counts()
        cur = a.sort
        items = []
        for key, label in SORTS:
            mark = ("  (desc)" if a.sort_desc else "  (asc)") if key and key == cur else ""
            items.append((f"sort: {label}{mark}", lambda key=key: a.set_sort(key)))
        items.append(None)
        for key, label in ARCH:
            items.append((("\u2022 " if a.arch == key else "  ") + label, lambda key=key: a.set_arch(key)))
        items.append(None)
        items.append((("hide" if a.show_hidden else "show") + f" hidden games ({n['hidden']})",
                      self._toggle_hidden))
        at_start = prefs.get("scan_on_start", True)
        items.append((("\u2022 " if at_start else "  ") + "read the library when the tool opens",
                      lambda: prefs.set_("scan_on_start", not at_start)))
        # the one setting that sends something (a game's name to Steam's
        # store search), so it says so where it is switched
        from .. import covers as _covers
        online = _covers.online()
        items.append((("\u2022 " if online else "  ") + "look up covers online (sends game names)",
                      lambda: a.set_online_art(not online)))
        vx2 = self.c.canvasx(self.c.winfo_width())
        self.kit.menu(vx2 - T.px(44) - T.px(400), T.px(144), items, width=T.px(400), max_rows=16)

    def _toggle_hidden(self):
        self.app.show_hidden = not self.app.show_hidden
        self.app.refresh("library")

    def _card_menu(self, e, g):
        a = self.app
        hid = str(g.folder) in a.hidden()
        items = [("open", lambda: a.open_game(g)),
                 (("show in the list again" if hid else "hide from the list"),
                  lambda: a.set_hidden(g, not hid)),
                 ("open folder", lambda: __import__("webbrowser").open(str(g.install_dir))),
                 None] + a.picture_items(g)
        self.kit.menu(self.c.canvasx(e.x), self.c.canvasy(e.y), items, width=T.px(300))

    # ------------------------------------------------------------ keys and search
    def _typed(self, text):
        self.app.query = text
        self.sel = None
        if getattr(self, "_typed_job", None):
            self.c.after_cancel(self._typed_job)
        self._typed_job = self.c.after(120, lambda: self.app.refresh("library", soft=True))

    def _first(self):
        if self.cards:
            self.c.focus_set()
            self._select(0)

    def _reset_view(self):
        a = self.app
        a.filter, a.arch, a.show_hidden = "all", "all", False
        if self.field:
            self.field.reset()
        a.query = ""
        a.refresh("library")

    def key(self, e) -> bool:
        if self.kit.top() is not None:
            return False
        ks = e.keysym
        if ks in ("Left", "Right", "Up", "Down") and self.cards:
            d = {"Left": -1, "Right": 1, "Up": -self.cols, "Down": self.cols}[ks]
            i = 0 if self.sel is None else max(0, min(len(self.cards) - 1, self.sel + d))
            self._select(i)
            self._scroll_to_card(i)
            return True
        if ks == "Return" and self.sel is not None and self.sel < len(self.cards):
            self.app.open_game(self.cards[self.sel]["g"])
            return True
        # the right-click menu from the keyboard: Shift+F10 or the Menu key
        if (ks == "App" or (ks == "F10" and e.state & 0x1)) and self.cards:
            i = self.sel if self.sel is not None and self.sel < len(self.cards) else 0
            self._select(i)
            box = self.c.bbox(self.cards[i]["tag"])
            if box:
                from types import SimpleNamespace
                self._card_menu(SimpleNamespace(x=box[0] - self.c.canvasx(0) + T.px(24),
                                                y=box[1] - self.c.canvasy(0) + T.px(24)), self.cards[i]["g"])
            return True
        if (e.state & 0x4) and ks.lower() == "f":
            self.field.focus()
            return True
        if e.char and e.char.isprintable() and not (e.state & 0x4) and self.field:
            self.field.focus(append=e.char)
            return True
        return False

    def _scroll_to_card(self, i):
        c = self.c
        items = c.bbox(self.cards[i]["tag"])
        if not items:
            return
        top, bottom = c.canvasy(0), c.canvasy(c.winfo_height())
        if items[1] < top + T.px(20) or items[3] > bottom - T.px(20):
            self.shell.scroll_to(max(0, items[1] - T.px(160)))

    def shown(self) -> None:
        self.sel = None
