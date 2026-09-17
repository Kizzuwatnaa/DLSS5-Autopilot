"""The RTX Remix page: which of your games have a mod, and the rest of them.

A few Remix builds carry DLSS 5 inside their own renderer; this tool
switches it on. Where a project publishes a complete install and the game is
in the library, the mod is fetched and put in from here.
"""
from __future__ import annotations

import threading
import webbrowser

from .. import log, net, remixdl, remixlist
from . import theme as T
from .shell import Page


class RemixPage(Page):
    name = "remix"
    rail = 3

    def __init__(self, shell, app):
        super().__init__(shell)
        self.app = app
        self.busy_mod = None
        self.state: dict[str, tuple[str, str]] = {}      # mod url -> (label, colour)

    def refresh(self) -> int:
        self.shell.redraw()
        return self.shell.content_h

    def shown(self) -> None:
        """Which owned games already have a mod in - a folder walk, so on a worker."""
        a = self.app
        self._tries = 0
        owned = [(g, m) for g, m in remixlist.for_library(a.all_games)]

        def work():
            from .. import remix
            have = {}
            for g, m in owned:
                try:
                    have[m.url] = bool(remix.is_remix_game(g.install_dir))
                except Exception:
                    have[m.url] = False
            a.q.put(("remixhave", have))
        threading.Thread(target=work, daemon=True).start()

    def got_have(self, have) -> None:
        for url, present in have.items():
            if present and self.state.get(url, ("",))[0] != "working...":
                self.state[url] = ("already in", T.MUTED)
        if self.shell.page is self:
            self.shell.redraw()

    def draw(self, width: int) -> int:
        a, c, k = self.app, self.c, self.kit
        pad = T.px(44)
        tags = ("page",)
        w = width - 2 * pad
        # the same header as the library: a title, one muted line under it
        c.create_text(pad, T.px(50), text="remix", font=T.mono(22, True), fill=T.TEXT, anchor="w", tags=tags)
        c.create_text(pad + T.px(2), T.px(84), text="path-traced classics  \u00b7  dlss 5 runs inside the mod's own "
                                                    "renderer", font=T.mono(10), fill=T.MUTED, anchor="w", tags=tags)
        y = T.px(130)
        y = self._steps(pad, y, w)
        owned = remixlist.for_library(a.all_games)
        seen = set()
        mine = []
        for g, m in owned:
            if id(m) not in seen:
                seen.add(id(m))
                mine.append((g, m))
        self._waiting = False
        if mine:
            y = self._heading("in your library", pad, y, w, len(mine))
            for g, m in mine:
                y = self._owned(g, m, pad, y, w)
        elif not a.have_library():
            c.create_text(pad, y + T.px(6), text="scan your games first to see which of them have a mod.",
                          font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
            y += T.px(40)
        y = self._heading("already remix, nothing to install", pad, y + T.px(12), w)
        y = self._tiles(list(remixlist.BUILT_IN), pad, y, w)
        y = self._heading("community mods", pad, y + T.px(12), w)
        y = self._tiles([m for m in remixlist.MODS if id(m) not in seen], pad, y, w)
        y += T.px(18)
        x = pad
        c.create_text(x, y, text="more projects", font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
        x += T.width("more projects", T.mono(9)) + T.px(22)
        for label, url in remixlist.MORE:
            tag, wid = k.link(x, y, label, lambda u=url: webbrowser.open(u), glyph="link", colour=T.AMBER, tags=tags)
            x += wid + T.px(28)
        if not self._waiting:
            # every cover is in: the next wait (a rescan, new games) gets its
            # own 20 tries - the count never went back and later ones had none
            self._tries = 0
        elif getattr(self, "_tries", 0) < 20:
            # a cover is still being read: draw again once it has landed
            self._tries = getattr(self, "_tries", 0) + 1
            c.after(500, lambda: self.shell.page is self and self.shell.redraw())
        return y + T.px(60)

    def _steps(self, x, y, w):
        """Three steps side by side (stacked on a narrow window)."""
        c = self.c
        tags = ("page",)
        steps = (("get the mod", "download & install below, or from its page"),
                 ("rescan", "the game comes up on the remix route"),
                 ("install", "that switches dlss 5 on inside the mod"))
        cols = 3 if w >= T.px(900) else 1
        gap = T.px(14)
        cw = (w - gap * (cols - 1)) / cols
        h = T.px(70)
        for i, (head, line) in enumerate(steps):
            col, row = i % cols, i // cols
            sx, sy = x + col * (cw + gap), y + row * (h + gap)
            c.create_rectangle(sx, sy, sx + cw, sy + h, fill=T.SURF, outline="", tags=tags)
            c.create_text(sx + T.px(22), sy + h / 2, text=str(i + 1), font=T.mono(18, True), fill=T.AMBER,
                          anchor="w", tags=tags)
            c.create_text(sx + T.px(58), sy + T.px(24), text=head, font=T.mono(10, True), fill=T.TEXT, anchor="w",
                          tags=tags)
            c.create_text(sx + T.px(58), sy + T.px(47), text=T.fit(line, T.mono(9), cw - T.px(72)),
                          font=T.mono(9), fill=T.MUTED, anchor="w", tags=tags)
        rows = (len(steps) + cols - 1) // cols
        return y + rows * (h + gap) + T.px(26)

    def _heading(self, text, x, y, w, count=None):
        c = self.c
        f = T.mono(9, True)
        c.create_text(x, y, text=text, font=f, fill=T.MUTED, anchor="w", tags=("page",))
        tx = x + T.width(text, f)
        if count is not None:
            c.create_text(tx + T.px(10), y, text=str(count), font=f, fill=T.AMBER, anchor="w", tags=("page",))
            tx += T.px(10) + T.width(str(count), f)
        c.create_line(tx + T.px(14), y, x + w, y, fill=T.LINE, tags=("page",))
        return y + T.px(26)

    def _owned(self, g, m, x, y, w):
        """A game of yours that has a mod: its cover, what the mod is, and the one thing to do."""
        a, c, k = self.app, self.c, self.kit
        tags = ("page",)
        cw, ch = T.px(76), T.px(114)
        h = ch + T.px(20)
        c.create_rectangle(x, y, x + w, y + h, fill=T.SURF, outline="", tags=tags)
        c.create_rectangle(x, y, x + T.px(3), y + h, fill=T.OK, outline="", tags=tags)
        cov = a.cover(g, cw, ch)
        if cov and cov.get("kind") == "cover" and cov.get("levels"):
            c.create_image(x + T.px(18), y + T.px(10), image=cov["levels"][-1], anchor="nw", tags=tags)
        else:
            if cov is None:
                self._waiting = True
            c.create_rectangle(x + T.px(18), y + T.px(10), x + T.px(18) + cw, y + T.px(10) + ch, fill=T.SURF2,
                               outline="", tags=tags)
        tx = x + T.px(18) + cw + T.px(22)
        c.create_text(tx, y + T.px(34), text=g.name, font=T.mono(12, True), fill=T.TEXT, anchor="w", tags=tags)
        c.create_text(tx, y + T.px(60), text=m.mod, font=T.mono(9), fill=T.MUTED, anchor="w", tags=tags)
        right = x + w - T.px(18)
        room = right - T.px(280) - tx
        if m.note:
            c.create_text(tx, y + T.px(84), text=T.fit(m.note, T.mono(9), room), font=T.mono(9), fill=T.DIM,
                          anchor="w", tags=tags)
        tag, wid = k.link(right, y + h - T.px(24), "open page", lambda u=m.url: webbrowser.open(u), glyph="link",
                          colour=T.MUTED, anchor="e", tags=tags)
        by = y + T.px(22)
        if m.installable:
            label, colour = self.state.get(m.url, ("download & install", T.OK))
            if label in ("already in", "installed"):
                k.glyph(right - T.px(150), by + T.px(17), "check", T.OK, 11, anchor="w", tags=tags)
                c.create_text(right - T.px(126), by + T.px(17), text="the mod is in", font=T.mono(10), fill=T.OK,
                              anchor="w", tags=tags)
            else:
                k.button(right - T.px(260), by, T.px(260), label, lambda mm=m, gg=g: self.fetch(mm, gg),
                         glyph="download", kind="primary" if label == "download & install" else "secondary",
                         h=T.px(38), tags=tags, size=10, enabled=self.busy_mod is None)
        else:
            c.create_text(right, by + T.px(17), text="install it from its page", font=T.mono(9), fill=T.DIM,
                          anchor="e", tags=tags)
        return y + h + T.px(12)

    def _tiles(self, mods, x, y, w):
        """Every other project as a tile; the whole tile opens its page."""
        c, k = self.c, self.kit
        if not mods:
            return y
        cols = max(1, min(3, int((w + T.px(14)) // T.px(380))))
        gap = T.px(14)
        tw = (w - gap * (cols - 1)) / cols
        th = T.px(84)
        for i, m in enumerate(mods):
            col, row = i % cols, i // cols
            tx, ty = x + col * (tw + gap), y + row * (th + gap)
            tag = k.uid("mod")
            tags = ("page", tag)
            box = c.create_rectangle(tx, ty, tx + tw, ty + th, fill=T.SURF, outline="", tags=tags)
            name = c.create_text(tx + T.px(18), ty + T.px(24), text=T.fit(m.game, T.mono(10, True), tw - T.px(56)),
                                 font=T.mono(10, True), fill=T.TEXT, anchor="w", tags=tags)
            c.create_text(tx + T.px(18), ty + T.px(46), text=T.fit(m.mod, T.mono(9), tw - T.px(36)), font=T.mono(9),
                          fill=T.MUTED, anchor="w", tags=tags)
            if m.note:
                c.create_text(tx + T.px(18), ty + T.px(66), text=T.fit(m.note, T.mono(8), tw - T.px(36)),
                              font=T.mono(8), fill=T.DIM, anchor="w", tags=tags)
            arrow = k.glyph(tx + tw - T.px(22), ty + T.px(24), "link", T.DIM, 10, tags=tags)
            k.registry[tag] = ("link", m.game)
            k.hover(tag, lambda b=box, n=name, ar=arrow: (c.itemconfigure(b, fill=T.SURF2),
                                                          c.itemconfigure(ar, fill=T.AMBER)),
                    lambda b=box, n=name, ar=arrow: (c.itemconfigure(b, fill=T.SURF),
                                                     c.itemconfigure(ar, fill=T.DIM)))
            k.tip(tag, "opens the project's page")
            k.on_click(tag, lambda u=m.url: webbrowser.open(u))
        rows = (len(mods) + cols - 1) // cols
        return y + rows * (th + gap) + T.px(8)

    def fetch(self, mod, game):
        if self.busy_mod is not None:
            return
        a = self.app
        if a.busy:
            # an install, uninstall or DLSS update is writing somewhere; a mod
            # unpacked into the same folder meanwhile races it
            self.shell.info("remix", "Wait until the job that is running now has finished, then press it again.")
            return
        self.busy_mod = mod
        self.state[mod.url] = ("working...", T.AMBER)
        a.busy, a.action = True, "remix"
        a.job_game = game
        self.shell.redraw()
        said = {"pct": -1, "at": 0.0}

        def prog(done, total):
            """Two shapes reach here: (bytes, bytes) from the download, and
            (percent, "unpacking i/n") from remixdl while it unpacks. The second
            raised TypeError on the first file and the install stopped with
            its files in the folder and no record of them.

            One line per 5 percent, not one per 256 KB chunk."""
            import time as _time
            if isinstance(total, str):
                pct, text = int(done or 0), f"{mod.game}: {total}"
            else:
                pct = int(done * 100 / total) if total else 0
                text = f"{mod.game}: {pct}%  {net.human(done)} / {net.human(total)}"
            now = _time.monotonic()
            if pct == said["pct"] or (pct < 100 and pct - said["pct"] < 5 and now - said["at"] < 2.0):
                return
            said["pct"], said["at"] = pct, now
            a.q.put(("log", (text, "")))

        def work():
            try:
                written = remixdl.install(mod.url, game.install_dir,
                                          log=lambda t: a.q.put(("log", (t.strip(), ""))), progress=prog)
                a.q.put(("remixed", (mod, True, f"{mod.game}: installed, {len(written)} files - rescan and the "
                                                f"game comes up on the remix route")))
            except remixdl.NotAModError as e:
                a.q.put(("remixed", (mod, False, f"{mod.game}: {e}")))
            except Exception as e:
                log.exception("fetching a remix mod")
                a.q.put(("remixed", (mod, False, f"{mod.game}: {type(e).__name__}: {e}")))
        threading.Thread(target=work, daemon=True).start()

    def done(self, payload):
        mod, ok, text = payload
        self.busy_mod = None
        a = self.app
        if getattr(a, "action", "") == "remix":
            a.busy, a.action = False, ""
            a.job_game = None
        self.state[mod.url] = ("installed" if ok else "retry", T.OK if ok else T.WARN)
        self.app.write(text, "ok" if ok else "err")
        self.shell.status(text)
        if self.shell.page is self:
            self.shell.redraw()
