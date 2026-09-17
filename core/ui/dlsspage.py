"""The dlss page: which DLSS each game ships, how far behind NVIDIA's newest it
is, and one button to move it forward or back.

One row per game that ships NVIDIA's runtimes. A row that is behind carries
the accent on its left edge and a primary button; an anti-cheat game is
dimmed and names its anti-cheat; a running game says so instead of offering
a swap that Windows would refuse. The page opens on the last scan and scans
again on its own when that is old or the library has new games.
"""
from __future__ import annotations

import time

from .. import dlssupdate as du
from . import theme as T
from .shell import Page

SHORT = {"dlss": "super resolution", "dlssg": "frame generation", "dlssd": "ray reconstruction"}


def _ago(t: float) -> str:
    s = max(0, time.time() - float(t or 0))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{int(s // 60)} min ago"
    if s < 36 * 3600:
        return f"{int(s // 3600)} h ago"
    d = int(s // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


class DlssPage(Page):
    name = "dlss"
    rail = 1

    def __init__(self, shell, app):
        super().__init__(shell)
        self.app = app
        self.width = 0
        self._progress_item = None
        self._job_btn = None
        self._cover_wait = None

    def shown(self) -> None:
        if self.app.dlss_due():
            self.app.dlss_scan()
        else:
            # a game closed since the scan must not stay "running"
            self.app.dlss_recheck_running()

    def refresh(self) -> int:
        self.shell.redraw()
        return self.shell.content_h

    # ------------------------------------------------------------ header
    def draw(self, width: int) -> int:
        a, c, k = self.app, self.c, self.kit
        self.width = width
        pad = T.px(44)
        tags = ("page",)
        data = a.dlss_state()
        rows = a.dlss_rows()
        todo = a.dlss_todo()
        busy = bool(a.dlss_job or a.dlss_scanning)
        c.create_text(pad, T.px(58), text="dlss", font=T.mono(24, True), fill=T.TEXT, anchor="w", tags=tags)
        right = width - pad
        if todo:
            label = f"update all ({len(todo)})"
            bw = T.width(label, T.mono(11, True)) + T.px(70)
            k.button(right - bw, T.px(36), bw, label, a.dlss_update_all, glyph="download", kind="primary",
                     h=T.px(44), tags=tags, enabled=not busy and not a.busy)
            right -= bw + T.px(24)
        if a.have_library() and data.get("at"):
            tag, wid = k.link(right, T.px(58), "check again", lambda: None if busy else a.dlss_scan(fresh=True),
                              glyph="refresh", anchor="e", tags=tags, colour=T.DIM if busy else T.MUTED,
                              tip="wait for the current job" if busy else "reads every game's dlss files again")
        # what this does, and the one caution, in a line
        say = "NVIDIA's newest dlss files for your games; the game's own stay beside them."
        warn = "online games: keep their own."
        f = T.mono(10)
        room = width - 2 * pad
        c.create_text(pad, T.px(96), text=T.fit(say, f, room), font=f, fill=T.MUTED, anchor="w", tags=tags)
        sw = T.width(say, f) + T.width("  ", f)
        if sw + T.width(warn, f) <= room:
            c.create_text(pad + sw, T.px(96), text=warn, font=f, fill=T.AMBER, anchor="w", tags=tags)
            y = T.px(138)
        else:
            c.create_text(pad, T.px(118), text=warn, font=f, fill=T.AMBER, anchor="w", tags=tags)
            y = T.px(158)
        y = self._band(pad, y, width, data)
        if not a.have_library():
            return self._empty(pad, y, "no games yet",
                               "find your games first; this page then reads which dlss each one ships.",
                               ("go to games", self.shell.home, "game"))
        if not rows:
            if a.dlss_scanning:
                return self._empty(pad, y, "reading your games...", "", None)
            if not data.get("at"):
                return self._empty(pad, y, "not checked yet", "reads which dlss each of your games ships.",
                                   ("check my games", lambda: a.dlss_scan(), "search"))
            # 32-bit games are skipped, not read: they are not "checked"
            n = len(data.get("seen") or []) - int(data.get("x86") or 0)
            if n <= 0:
                return self._empty(pad, y, "no 64-bit games in the library", "", None)
            return self._empty(pad, y, f"none of the {n} game{'s' if n != 1 else ''} checked ship NVIDIA DLSS "
                                       f"files", "", None)
        news = a.dlss_newest()
        behind = [r for r in rows if r["behind"]]
        if not news:
            head, col = "the newest builds could not be read - versions are shown as found", T.DIM
        elif behind:
            n = len(behind)
            head, col = f"{n} game{'s' if n != 1 else ''} behind NVIDIA's newest", T.AMBER
            if len(todo) != n:
                held = n - len(todo)
                head += (f"  \u00b7  {len(todo)} to update now, {held} need a look first" if todo
                         else f"  \u00b7  {held} need a look first")
        else:
            head, col = "every game is on NVIDIA's newest", T.OK
        c.create_text(pad, y, text=head, font=T.mono(10, True), fill=col, anchor="w", tags=tags)
        y += T.px(30)
        waiting = False
        for r in rows:
            y, w8 = self._row(r, pad, y, width)
            waiting = waiting or w8
        if waiting and self._cover_wait is None:
            self._cover_wait = self.c.after(500, self._covers_landed)
        return y + T.px(60)

    def _band(self, pad, y, width, data) -> int:
        """The scan progress while it runs, else what the numbers rest on."""
        a, c = self.app, self.c
        tags = ("page",)
        if a.dlss_scanning:
            self._progress_item = c.create_text(pad, y, text=self._progress_text(), font=T.mono(9),
                                                fill=T.AMBER, anchor="w", tags=tags)
            c.create_rectangle(pad, y + T.px(14), width - pad, y + T.px(16), fill=T.SURF, outline="", tags=tags)
            self._bar = c.create_rectangle(pad, y + T.px(14), pad + T.px(120), y + T.px(16), fill=T.AMBER,
                                           outline="", tags=tags)
            self._sweep()
            return y + T.px(46)
        self._progress_item = None
        if not data.get("at"):
            return y + T.px(10)
        bits = []
        news = a.dlss_newest()
        vers = sorted({v.get("version", "") for v in news.values() if isinstance(v, dict)} - {""})
        if vers:
            bits.append("newest from nvidia: " + " / ".join(vers))
        n = len(data.get("seen") or [])
        bits.append(f"{n} game{'s' if n != 1 else ''} checked {_ago(data.get('at'))}")
        if data.get("x86"):
            bits.append(f"{data['x86']} 32-bit game{'s' if data['x86'] != 1 else ''} skipped")
        if data.get("offline"):
            bits.append("offline: newest from an earlier check")
        c.create_text(pad, y, text=T.fit("  \u00b7  ".join(bits), T.mono(9), width - 2 * pad), font=T.mono(9),
                      fill=T.DIM, anchor="w", tags=tags)
        return y + T.px(40)

    def _progress_text(self) -> str:
        i, n, name = self.app.dlss_seen
        return f"reading {min(i + 1, n)} of {n}" + (f"  \u00b7  {name}" if name else "")

    def progress(self) -> None:
        try:
            if self._progress_item is not None:
                self.c.itemconfigure(self._progress_item, text=T.fit(self._progress_text(), T.mono(9),
                                                                      self.width - T.px(88)))
        except Exception:
            pass

    def _sweep(self):
        pad = T.px(44)
        c = self.c

        def loop(kk):
            try:
                span = self.width - 2 * pad - T.px(120)
                x = pad + span * kk
                y1 = c.coords(self._bar)[1]
                c.coords(self._bar, x, y1, x + T.px(120), y1 + T.px(2))
            except Exception:
                pass

        def again():
            if self.app.dlss_scanning and self.shell.page is self:
                self.shell.motion.run("dlss_sweep", 1400, loop, again)
        self.shell.motion.run("dlss_sweep", 1400, loop, again)

    def _empty(self, pad, y, title, line, action) -> int:
        c, k = self.c, self.kit
        tags = ("page",)
        c.create_text(pad, y + T.px(10), text=title, font=T.mono(14, True), fill=T.TEXT, anchor="w", tags=tags)
        if line:
            c.create_text(pad, y + T.px(44), text=line, font=T.mono(10), fill=T.MUTED, anchor="w", tags=tags)
        if action:
            label, cmd, glyph = action
            bw = T.width(label, T.mono(11, True)) + T.px(70)
            k.button(pad, y + T.px(78), bw, label, cmd, glyph=glyph, kind="primary", tags=tags)
            return y + T.px(180)
        return y + T.px(120)

    # ------------------------------------------------------------ a row
    def _row(self, r, pad, y, width):
        a, c, k = self.app, self.c, self.kit
        tags = ("page",)
        g = r["g"]
        folder = str(g.folder)
        entries = r["entries"]
        news = r["news"]
        dim = bool(r["anticheat"])
        top = y
        # One line per build: RE Engine keeps a second copy of each runtime
        # under _storage_, and six lines for three runtimes read as six.
        lines: dict[tuple, list] = {}
        for e in entries:
            lines.setdefault((e.family, e.version, e.state, e.original), [e, 0])[1] += 1
        h = max(T.px(96), T.px(52) + len(lines) * T.px(24) + T.px(10))
        result = a.dlss_results.get(folder)
        if result:
            h += T.px(24) if result[0] else T.px(44)
        job = a.dlss_job == folder
        free = not (a.dlss_job or a.dlss_scanning or a.busy)
        # left edge: the accent where there is something to do
        if r["behind"] and not dim and not r["running"]:
            c.create_rectangle(pad, top + T.px(8), pad + T.px(3), top + h - T.px(8), fill=T.AMBER, outline="",
                               tags=tags)
        # a small cover, when one is already cached (it is read on a worker)
        tw, th = T.px(48), T.px(72)
        tx = pad + T.px(16)
        waiting = False
        cov = a.cover(g, tw, th)
        if cov is None:
            waiting = True
        if cov and cov.get("levels"):
            img = cov.get("dim") if dim and cov.get("dim") else cov["levels"][min(2, len(cov["levels"]) - 1)]
            if cov.get("kind") == "icon":
                c.create_rectangle(tx, top + T.px(12), tx + tw, top + T.px(12) + th, fill=T.SURF, outline="",
                                   tags=tags)
                c.create_image(tx + tw / 2, top + T.px(12) + th / 2, image=img, tags=tags)
            else:
                c.create_image(tx, top + T.px(12), image=img, anchor="nw", tags=tags)
        else:
            c.create_rectangle(tx, top + T.px(12), tx + tw, top + T.px(12) + th, fill=T.SURF, outline="", tags=tags)
            c.create_text(tx + tw / 2, top + T.px(12) + th / 2, text=T.GLYPH["game"], font=T.icons(14),
                          fill=T.LINE, tags=tags)
        x = tx + tw + T.px(22)
        bw = T.px(170)
        room = width - pad - bw - T.px(24) - x
        # the name opens the game's own page
        name = T.fit(g.name, T.mono(11, True), room)
        ntag = k.uid("dlssname")
        colour = T.DIM if dim else T.TEXT
        nid = c.create_text(x, top + T.px(26), text=name, font=T.mono(11, True), fill=colour, anchor="w",
                            tags=tags + (ntag,))
        k.hover(ntag, lambda: c.itemconfigure(nid, fill=T.AMBER), lambda: c.itemconfigure(nid, fill=colour))
        k.on_click(ntag, lambda: a.open_game(g))
        mx = x + T.width(name, T.mono(11, True)) + T.px(18)
        tag_room = max(0, x + room - mx)
        if r["anticheat"] and tag_room > T.px(30):
            c.create_text(mx, top + T.px(27), text=T.fit(r["anticheat"].lower(), T.mono(8), tag_room),
                          font=T.mono(8), fill=T.AMBER, anchor="w", tags=tags)
        elif r["running"] and tag_room > T.px(30):
            c.create_text(mx, top + T.px(27), text=T.fit(f"running: {r['running']}", T.mono(8), tag_room),
                          font=T.mono(8), fill=T.WARN, anchor="w", tags=tags)
        ly = top + T.px(54)
        for e, copies in lines.values():
            self._entry_line(e, news.get(e.family) or {}, x, ly, room, dim, copies)
            ly += T.px(24)
        if result:
            ok, text = result
            if ok:
                c.create_text(x, ly + T.px(2), text=T.fit(text, T.mono(9), room), font=T.mono(9),
                              fill=T.OK, anchor="w", tags=tags)
            else:
                # the reason and what to do: two lines, the rest is in the log
                first, second = "", ""
                for wd in str(text).split():
                    if not second and T.width((first + " " + wd).strip(), T.mono(9)) <= room:
                        first = (first + " " + wd).strip()
                    else:
                        second = (second + " " + wd).strip()
                c.create_text(x, ly + T.px(2), text=first, font=T.mono(9), fill=T.WARN, anchor="w", tags=tags)
                if second:
                    c.create_text(x, ly + T.px(22), text=T.fit(second, T.mono(9), room), font=T.mono(9),
                                  fill=T.WARN, anchor="w", tags=tags)
        # the action column
        bx = width - pad - bw
        by = top + T.px(12)
        if job:
            pct = a.dlss_pct
            b = k.button(bx, by, bw, "working" if pct < 0 else f"{pct}%", None, glyph="refresh", kind="primary",
                         h=T.px(38), tags=tags, size=10)
            if pct >= 0:
                b.set(progress=pct / 100)
            self._job_btn = b
        elif r["behind"]:
            k.button(bx, by, bw, "update", lambda: a.dlss_update(g), glyph="warn" if dim else "download",
                     kind="secondary" if dim or r["running"] else "primary", h=T.px(38), tags=tags, size=10,
                     enabled=free,
                     tip=f"{r['anticheat']}: asks first" if dim else ("close the game first" if r["running"]
                                                                       else ""))
        elif news and any(e.state != du.INSTALL for e in entries) \
                and all(e.version for e in entries if e.state != du.INSTALL):
            k.glyph(bx + T.px(4), by + T.px(19), "check", T.OK, 11, anchor="w", tags=tags)
            c.create_text(bx + T.px(28), by + T.px(19), text="newest", font=T.mono(9), fill=T.OK, anchor="w",
                          tags=tags)
        elif entries and all(e.state == du.INSTALL for e in entries):
            # every runtime here is the DLSS 5 install's swap: its build is
            # chosen in the game's settings, not on this page
            k.link(bx + T.px(4), by + T.px(19), "game settings", lambda: a.open_game(g), glyph="gear",
                   colour=T.MUTED, tags=tags, tip="the dlss 5 install picked this build")
        if r["restorable"] and not job:
            k.link(bx + T.px(4), by + T.px(62), "restore original", lambda: a.dlss_restore(g) if free else None,
                   glyph="back", colour=T.MUTED if free else T.DIM, tags=tags,
                   tip="puts the game's own files back")
        c.create_line(pad + T.px(16), top + h, width - pad, top + h, fill=T.LINE, tags=tags)
        return top + h + T.px(10), waiting

    def _entry_line(self, e, new, x, y, room, dim, copies=1):
        c = self.c
        tags = ("page",)
        f = T.mono(9)
        c.create_text(x, y, text=SHORT[e.family], font=f, fill=T.DIM if dim else T.MUTED, anchor="w", tags=tags)
        vx = x + T.width("ray reconstruction", f) + T.px(22)
        if e.state == du.MISSING:
            # gone from where the game loads it; the game's own is beside that place
            parts = [("missing", T.WARN), ("   restore original puts the game's own back", T.DIM)]
            e = None
        else:
            ver = e.version or "no version stamp"
            parts = [(ver, T.DIM if dim or not e.version else T.TEXT)]
        if e is None:
            new = {}
            copies = 1
            for text, colour in parts:
                left = x + room - vx
                if left <= T.px(20):
                    break
                text = T.fit(text, f, left)
                c.create_text(vx, y, text=text, font=f, fill=colour, anchor="w", tags=tags)
                vx += T.width(text, f)
            return
        if du.outdated(e, new):
            parts.append(("  ->  " + new.get("version", ""), T.DIM if dim else T.AMBER))
        if e.state == du.UPDATED:
            if e.backup is not None:
                parts.append((f"   game's own {e.original} kept" if e.original else "   game's own kept", T.DIM))
            else:
                parts.append(("   the game's own backup is gone", T.WARN))
        elif e.state == du.INSTALL:
            parts.append(("   set by the dlss 5 install", T.DIM))
        if copies > 1:
            parts.append((f"   {copies} copies", T.DIM))
        for text, colour in parts:
            left = x + room - vx
            if left <= T.px(20):
                break
            text = T.fit(text, f, left)
            c.create_text(vx, y, text=text, font=f, fill=colour, anchor="w", tags=tags)
            vx += T.width(text, f)

    def job_progress(self) -> None:
        b = self._job_btn
        pct = self.app.dlss_pct
        try:
            if b is not None and self.c.find_withtag(b.tag) and pct >= 0:
                b.set(label=f"{pct}%", progress=pct / 100)
        except Exception:
            pass

    def _covers_landed(self) -> None:
        self._cover_wait = None
        if self.shell.page is self and not self.kit.top():
            self.shell.redraw()

    def key(self, e) -> bool:
        if self.kit.top() is not None:
            return False
        if e.keysym == "F5":
            self.app.dlss_scan(fresh=True)
            return True
        return False
