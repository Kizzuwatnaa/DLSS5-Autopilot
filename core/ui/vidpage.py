"""The video page: DLSS 5 on a file, a link, a webcam or a screen."""
from __future__ import annotations

from .. import video
from . import theme as T
from .shell import Page


class VideoPage(Page):
    name = "video"
    rail = 2

    def __init__(self, shell, app):
        super().__init__(shell)
        self.app = app
        self.url_field = None

    def shown(self):
        if video.known() is not None and self.app.cameras is None:
            self.app.list_sources()

    def refresh(self) -> int:
        self.shell.redraw()
        return self.shell.content_h

    def paint_progress(self) -> bool:
        """The busy button and its line changed in place; False when the page
        drawn is not the busy one (the caller redraws then)."""
        held = getattr(self, "_busy", None)
        a = self.app
        if held is None or self.shell.page is not self or not a.video_busy:
            return False
        b, busy, line, w = held
        if busy != a.video_busy:
            return False
        p, m = a.video_progress or (0, "")
        try:
            if not self.c.type(line):
                return False
            b.set(label=f"{busy}  ·  {p}%", progress=max(0, min(100, p)) / 100)
            self.c.itemconfigure(line, text=T.fit(str(m), T.mono(9), w))
        except Exception:
            return False
        return True

    def draw(self, width: int) -> int:
        a, c, k = self.app, self.c, self.kit
        pad = T.px(44)
        tags = ("page",)
        w = min(width - 2 * pad, T.px(1100))
        # the same header as the library and remix
        c.create_text(pad, T.px(50), text="video", font=T.mono(22, True), fill=T.TEXT, anchor="w", tags=tags)
        c.create_text(pad + T.px(2), T.px(84), text="dlss 5 on a video file, a YouTube link, a webcam or your screen",
                      font=T.mono(10), fill=T.MUTED, anchor="w", tags=tags)
        y = T.px(130)
        g = video.known()
        busy = a.video_busy
        self._busy = None
        if busy:
            p, m = a.video_progress or (0, "")
            b = k.button(pad, y, T.px(420), f"{busy}  \u00b7  {p}%", None, glyph="refresh", kind="primary", tags=tags)
            b.set(progress=p / 100)
            line = c.create_text(pad, y + T.px(66), text=T.fit(str(m), T.mono(9), w), font=T.mono(9), fill=T.DIM,
                                 anchor="w", tags=tags)
            self._busy = (b, busy, line, w)
            y += T.px(100)
        if g is None:
            # what it can do, as four tiles, then the one thing to press
            tiles = (("file", "a video file", "anything MPC-HC plays"),
                     ("link", "a YouTube link", "live, or saved first for 4K"),
                     ("camera", "a webcam", "live, frame by frame"),
                     ("screen", "your screen", "a window or the desktop"))
            cols = 4 if w >= T.px(1000) else 2
            gap = T.px(14)
            tw = (w - gap * (cols - 1)) / cols
            th = T.px(92)
            for i, (glyph, head, line) in enumerate(tiles):
                tx, ty = pad + (i % cols) * (tw + gap), y + (i // cols) * (th + gap)
                c.create_rectangle(tx, ty, tx + tw, ty + th, fill=T.SURF, outline="", tags=tags)
                k.glyph(tx + T.px(30), ty + th / 2, glyph, T.AMBER, 16, tags=tags)
                c.create_text(tx + T.px(60), ty + T.px(36), text=head, font=T.mono(10, True), fill=T.TEXT,
                              anchor="w", tags=tags)
                c.create_text(tx + T.px(60), ty + T.px(60), text=T.fit(line, T.mono(9), tw - T.px(72)),
                              font=T.mono(9), fill=T.MUTED, anchor="w", tags=tags)
            y += ((len(tiles) + cols - 1) // cols) * (th + gap) + T.px(18)
            if not busy:
                k.button(pad, y, T.px(280), "set up the player", a.video_setup, glyph="download", kind="primary",
                         tags=tags, tip=f"a portable {video.PLAYER} in a folder you choose, nothing else is written")
                hint = f"a portable {video.PLAYER}, in a folder you choose  \u00b7  F6 turns dlss 5 on and off while it plays"
                c.create_text(pad + T.px(300), y + T.px(23), text=T.fit(hint, T.mono(9), width - 2 * pad - T.px(300)),
                              font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
                y += T.px(80)
            return y + T.px(40)
        # the player itself
        x = pad
        if not busy:
            if g.installed:
                bb = k.button(x, y, T.px(250), "open the player", a.video_open_player, glyph="play", kind="primary",
                              tags=tags)
                x += T.px(262)
                k.button(x, y, T.px(300), "neural rendering on/off (F6)", a.video_toggle, glyph="refresh", tags=tags)
                x += T.px(312)
                k.button(x, y, T.px(180), "settings", lambda: a.open_game(g), glyph="gear", tags=tags)
            else:
                k.button(x, y, T.px(250), "install", lambda: a.open_game(g), glyph="download", kind="primary",
                         tags=tags, tip="installs into the player like into a game")
        y += T.px(80)
        # a link
        y = self._section("a link", pad, y, w)
        prev = self.url_field.get() if self.url_field else a.video_url
        fw = w - T.px(420)
        self.url_field = k.field(pad, y, fw, "https://...   (the clipboard is used when empty)", glyph="link",
                                 on_enter=lambda: a.video_play_url(self.url_field.get()), tags=tags, value=prev)
        k.button(pad + fw + T.px(12), y, T.px(120), "play", lambda: a.video_play_url(self.url_field.get()),
                 glyph="play", h=T.px(40), tags=tags, enabled=not busy)
        k.button(pad + fw + T.px(144), y, T.px(260), "download, then play",
                 lambda: a.video_download(self.url_field.get()), glyph="download", h=T.px(40), tags=tags,
                 enabled=not busy)
        y += T.px(58)
        k.toggle(pad, y + T.px(8), "4K when downloading", a.video_full, lambda v: setattr(a, "video_full", v),
                 tags=tags)
        k.link(pad + T.px(300), y + T.px(8), "downloads folder", lambda: a.video_folder("downloads"),
               glyph="folder", tags=tags)
        y += T.px(48)
        # a file
        y = self._section("a file", pad, y, w)
        tag, wid = k.link(pad, y + T.px(10), "open a video file...", a.video_open_file, glyph="file", colour=T.TEXT,
                          tags=tags)
        y += T.px(46)
        # render
        y = self._section("render a file through dlss 5", pad, y, w)
        dw = T.px(220)
        k.dropdown(pad, y, dw, a.video_scale, [(s, s) for s in video.SCALES], lambda v: setattr(a, "video_scale", v),
                   label="size", tags=tags, enabled=not busy)
        k.dropdown(pad + dw + T.px(16), y, dw, a.video_style, list(video.STYLES.items()),
                   lambda v: setattr(a, "video_style", v), label="style", tags=tags, enabled=not busy)
        k.button(pad + 2 * dw + T.px(32), y + T.px(16), T.px(290), "pick a video and render it", a.video_render,
                 glyph="play", h=T.px(38), tags=tags, enabled=not busy and g.installed,
                 tip="runs the model on every frame and writes a new file")
        k.link(pad + 2 * dw + T.px(32), y + T.px(74), "rendered folder", lambda: a.video_folder("processed"),
               glyph="folder", tags=tags)
        y += T.px(110)
        # capture
        y = self._section("webcam and screen", pad, y, w)
        for kind, label, items, cur in (("webcam", "webcam", a.cameras, a.camera),
                                        ("screen", "screen", a.screens, a.screen)):
            if items is None:
                opts, value = [("", "looking...")], ""
            elif not items:
                opts, value = [("", "none found")], ""
            else:
                opts, value = [(i, i) for i in items], cur
            k.dropdown(pad, y, T.px(420), value, opts,
                       lambda v, kind=kind: setattr(a, "camera" if kind == "webcam" else "screen", v),
                       label=label, tags=tags, enabled=bool(items))
            k.button(pad + T.px(436), y + T.px(16), T.px(120), "start", lambda kind=kind: a.capture_start(kind),
                     glyph="play", h=T.px(38), tags=tags, enabled=bool(items) and g.installed)
            k.button(pad + T.px(568), y + T.px(16), T.px(110), "stop", a.capture_stop, glyph="close", h=T.px(38),
                     tags=tags)
            y += T.px(76)
        k.link(pad, y + T.px(6), "look again", a.list_sources, glyph="refresh", colour=T.DIM, tags=tags)
        return y + T.px(60)

    def _section(self, title, x, y, w):
        c = self.c
        c.create_text(x, y, text=title, font=T.mono(9), fill=T.DIM, anchor="w", tags=("page",))
        c.create_line(x + T.width(title, T.mono(9)) + T.px(12), y, x + w, y, fill=T.LINE, tags=("page",))
        return y + T.px(26)
