"""A game's page: its own art, one action in focus, and what happened.

The backdrop is the game's Steam hero, sharp at its own aspect, rendered on
a worker; until it lands the page is plain and fades up when it does.
Settings open in place under the actions (never a panel that has to be
closed first), and the result of the last action - an install, a pass of
autopilot, a diagnosis - is a short block with its own buttons, the
full account being in the log.
"""
from __future__ import annotations

import threading
import tkinter as tk

from .. import community, dlss, installer, sessioncard
from . import art, imaging
from . import theme as T
from .motion import mix
from .setpanel import SettingsSection
from .shell import Page


class GamePage(Page):
    name = "game"
    rail = 0

    def __init__(self, shell, app):
        super().__init__(shell)
        self.app = app
        self.settings_open = False
        self.steps_open = False
        self.backdrops: dict[str, dict] = {}
        self._busy_art: set[str] = set()
        self.section = SettingsSection(self)
        self.width = 0

    # ------------------------------------------------------------ backdrop
    def _backdrop(self, g, width):
        """(image frames, band height, logo drawn) or None while it renders."""
        key = f"{g.folder}|{width}"
        hit = self.backdrops.get(key)
        if hit is not None:
            return hit
        # Only the page on screen keeps its pictures. Every game visited and
        # every width the window was dragged through kept a full-window set
        # of frames: about 64 MB a game at 2560 wide, 660 MB at 8K.
        self._keep_only(key)
        if key in self._busy_art:
            return None
        self._busy_art.add(key)
        h = max(T.px(900), self.c.winfo_height())
        pad = T.px(44)

        def work():
            buf, band, logo = None, 0, False
            try:
                a = art.find(g)
                if a.hero:
                    band_guess = int(width * 620 / 1920)
                    buf, band, logo = imaging.backdrop(a.hero, a.blur, a.logo, width, h, T.BG,
                                                       logo_box=(pad, band_guess - T.px(200), T.px(480),
                                                                 T.px(170)))
                elif a.logo:
                    # a logo chosen (or found) for a game with no background
                    # was never drawn (#18): it goes on the plain page, where
                    # the name would be
                    band = max(T.px(260), int(width * 0.2))
                    buf, _band, logo = imaging.backdrop(None, None, a.logo, width, h, T.BG,
                                                        logo_box=(pad, band - T.px(200), T.px(480), T.px(170)))
            except Exception:
                buf = None
            self.app.q.put(("backdrop", (key, buf, width, h, band, logo)))
        threading.Thread(target=work, daemon=True).start()
        return None

    def _keep_only(self, key: str) -> None:
        for k in [k for k in self.backdrops if k != key]:
            del self.backdrops[k]

    def got_backdrop(self, payload):
        key, buf, w, h, band, logo = payload
        self._busy_art.discard(key)
        g = self.app.game
        current = g is not None and key == f"{g.folder}|{self.width or w}"
        if not current:
            return            # the person moved on (another game, another width) before it landed
        self._keep_only(key)
        entry = {"frames": [], "band": band, "logo": logo, "h": h}
        if buf:
            # three steps of the fade are enough to read as one, at 3/5 of the memory
            entry["frames"] = [imaging.photo(tk, imaging.brightness(buf, k), w, h, self.c) for k in (0.3, 0.7, 1.0)]
        self.backdrops[key] = entry
        if self.shell.page is self:
            self.fade_in = True
            self.shell.redraw()

    # ------------------------------------------------------------ drawing
    def draw(self, width: int) -> int:
        a, c, k = self.app, self.c, self.kit
        g = a.game
        self.width = width
        if g is None:
            return 0
        pad = T.px(44)
        tags = ("page",)
        bd = self._backdrop(g, width)
        band = int(width * 620 / 1920)
        logo = False
        accent = T.AMBER
        # peek, not find: find walks Steam's libraries and reads the cover,
        # and draw runs on the Tk thread on every redraw. The backdrop worker
        # has called find by the time its picture lands and redraws.
        known = art.peek(g)
        if known is not None and known.accent:
            accent = known.accent
        self.accent = accent
        if bd and bd["frames"]:
            band, logo = bd["band"], bd["logo"]
            item = c.create_image(0, 0, image=bd["frames"][-1], anchor="nw", tags=tags + ("backdrop",))
            if getattr(self, "fade_in", False):
                self.fade_in = False
                frames = bd["frames"]
                self.shell.motion.run("backdrop", 420, lambda kk: c.itemconfigure(
                    item, image=frames[min(len(frames) - 1, int(kk * len(frames)))]))
        else:
            band = max(T.px(260), int(width * 0.2))
        # a real button, readable over any art: the small link read as part of the picture
        back = k.button(pad, T.px(22), T.px(128), "games", self.shell.home, glyph="back", h=T.px(38),
                        tags=tags, size=10, tip="back to your games  (esc)")
        k.on_click(back.tag, self.shell.home, nav=True)
        # the pictures are the game's own business: opposite the back button,
        # over the art it changes
        pw = T.width("change picture", T.mono(10)) + T.px(66)
        k.button(width - pad - pw, T.px(22), pw, "change picture",
                 lambda: self._picture_menu(g, width - pad - T.px(300), T.px(66)), glyph="camera",
                 h=T.px(38), tags=tags, size=10, tip="choose your own cover, background or logo")
        # the name, unless the art carries the game's own logo
        y = band - T.px(10)
        if not logo:
            title_font = T.mono(30 if width > T.px(900) else 22, True)
            c.create_text(pad, band - T.px(70), text=T.fit(g.name, title_font, width - 2 * pad), font=title_font,
                          fill=T.TEXT, anchor="w", tags=tags)
        # chips: api, bits, store, route and how far it can be trusted
        x = pad
        y = band + T.px(8)
        chips = [T.api_name(g.api), g.bit_label if g.bitness else "?-bit", str(g.source)]
        if a.support is not None and a.route:
            lvl = a.level()
            chips.append(f"{a.route}  \u00b7  {lvl}" if lvl else a.route)
        for i, label in enumerate(chips):
            f = T.mono(9)
            wid = T.width(label, f) + T.px(22)
            colour = T.MUTED
            if i == 3:
                colour = {installer.STABLE: T.OK, installer.BETA: T.AMBER}.get(a.level(), T.WARN)
            c.create_rectangle(x, y, x + wid, y + T.px(26), fill=T.SURF, outline=T.LINE, tags=tags)
            c.create_text(x + wid / 2, y + T.px(13), text=label, font=f, fill=colour, tags=tags)
            x += wid + T.px(8)
        y += T.px(48)
        self._busy_btn = self._busy_text = None
        y = self._status(g, pad, y, width)
        y = self._actions(g, pad, y, width, accent)
        y = self._features(g, pad, y, width, accent)
        if self.settings_open:
            y = self.section.draw(pad, y + T.px(10), width - 2 * pad, accent)
        y += T.px(16)
        if not self._offer_in_card(g):
            y = self._offer(g, pad, y, width, accent)
        y = self._result(g, pad, y, width, accent)
        y = self._others(pad, y + T.px(10), width, accent)
        return y + T.px(60)

    def refresh(self) -> int:
        self.shell.redraw()
        return self.shell.content_h

    def paint_progress(self) -> bool:
        """The busy button's percentage and the line under it, changed in
        place. False when they are not on screen as drawn (another page,
        another game, no line drawn yet) - the caller redraws then."""
        a = self.app
        held = getattr(self, "_busy_btn", None)
        if self.shell.page is not self or held is None or not a.busy or not a.progress:
            return False
        b, label, g = held
        if g is not a.game or getattr(self, "_busy_text", None) is None:
            return False
        p, m = a.progress
        try:
            if not self.c.type(b.label) or not self.c.type(self._busy_text):
                return False
            b.set(label=f"{label}  ·  {p}%", progress=max(0, min(100, p)) / 100)
            room = max(T.px(100), (self.width or self.c.winfo_width()) - 2 * T.px(44))
            self.c.itemconfigure(self._busy_text, text=T.fit(str(m or ""), T.mono(9), room))
        except tk.TclError:
            return False
        return True

    # ------------------------------------------------------------ parts
    def _status(self, g, pad, y, width):
        a, c = self.app, self.c
        tags = ("page",)
        lines = []
        if a.entering:
            lines.append((T.DIM, "reading the game..."))
        else:
            ok, why = (getattr(a, "entry", {}) or {}).get("ok", (True, ""))
            if not ok:
                lines.append((T.WARN, why))
            v = a.verdict_of(g)
            n_stale = a.stale.get(str(g.install_dir), 0)
            if n_stale:
                # Not "a newer build": a part can need installing again
                # while its version number has not moved at all - the
                # package behind it changed (#196, #364), or the release
                # line it came from was never meant to be installed (#325).
                # components.summary(), printed under 'check versions', says
                # which of the two it is; this line only has a count.
                lines.append((T.AMBER, f"{n_stale} installed part"
                                       f"{'s need' if n_stale != 1 else ' needs'} "
                                       f"installing again - press update"))
            elif v:
                lines.append((T.OK if v.get("ok") else T.WARN,
                              ("last run: " + ("working" if v.get("ok") else v.get("said", "needs a look")))
                              + (f"  \u00b7  {v['fps']} fps" if v.get("ok") and v.get("fps") else "")))
            elif g.installed:
                lines.append((T.MUTED, "installed"))
            elif (getattr(a, "entry", {}) or {}).get("stopped"):
                # the failed first install, taken back out: its reason
                lines.append((T.WARN, "the last install was taken back out: "
                                      + str(a.entry["stopped"])))
        for colour, text in lines:
            dot = colour in (T.OK, T.WARN, T.AMBER)
            x = pad
            if dot:
                c.create_oval(pad, y - T.px(4), pad + T.px(8), y + T.px(4), fill=colour, outline="", tags=tags)
                x = pad + T.px(18)
            # up to two lines, then an ellipsis: the full text is in the log
            room = width - x - pad
            words, first, second = str(text).split(), "", ""
            for wd in words:
                if not second and T.width((first + " " + wd).strip(), T.mono(10)) <= room:
                    first = (first + " " + wd).strip()
                else:
                    second = (second + " " + wd).strip()
            c.create_text(x, y, text=first, font=T.mono(10), fill=colour, anchor="w", tags=tags)
            if second:
                y += T.px(22)
                c.create_text(x, y, text=T.fit(second, T.mono(10), room), font=T.mono(10), fill=colour, anchor="w",
                              tags=tags)
            y += T.px(28)
        if not a.entering:
            for kind, text in a.notes[:3]:
                y = self._callout(kind, text, pad, y, width)
        if len(a.notes) > 3:
            self.kit.link(pad, y, f"{len(a.notes) - 3} more notes in the log", lambda: self.shell.toggle_log(True),
                          colour=T.DIM, tags=tags)
            y += T.px(26)
        return y + T.px(14)

    @staticmethod
    def headline(text: str) -> tuple[str, str]:
        """A note split into a few words to read first and the rest.

        A note is one long sentence written for the log; on the page it read
        as a wall of amber text. Split at the first ':' or ' - ' when what is
        before it is short, otherwise at a word near 56 characters."""
        t = " ".join(str(text).split())
        for sep in (": ", " - "):
            i = t.find(sep)
            if 0 < i <= 56:
                return t[:i], t[i + len(sep):]
        if len(t) <= 64:
            return t, ""
        cut = t.rfind(" ", 0, 56)
        cut = cut if cut > 20 else 56
        return t[:cut] + "\u2026", t[cut:].strip()

    def _callout(self, kind, text, pad, y, width) -> int:
        c, k = self.c, self.kit
        tags = ("page",)
        colour = {"bad": T.WARN, "driver": T.AMBER, "warn": T.AMBER}.get(kind, T.MUTED)
        glyph = {"bad": "warn", "driver": "warn", "warn": "warn"}.get(kind, "info")
        head, rest = self.headline(text)
        w = min(width - 2 * pad, T.px(980))
        h = T.px(40)
        c.create_rectangle(pad, y - T.px(4), pad + w, y + h - T.px(4), fill=T.SURF, outline="", tags=tags)
        c.create_rectangle(pad, y - T.px(4), pad + T.px(3), y + h - T.px(4), fill=colour, outline="", tags=tags)
        cy = y + h / 2 - T.px(4)
        k.glyph(pad + T.px(22), cy, glyph, colour, 11, tags=tags)
        hf = T.mono(10, True)
        x = pad + T.px(42)
        more_w = T.px(70)
        head_fit = T.fit(head, hf, w - (x - pad) - more_w - T.px(20))
        c.create_text(x, cy, text=head_fit, font=hf, fill=T.TEXT, anchor="w", tags=tags)
        x += T.width(head_fit, hf) + T.px(14)
        room = pad + w - more_w - T.px(12) - x
        shown = T.fit(rest, T.mono(9), room) if rest and room > T.px(80) else ""
        if shown:
            c.create_text(x, cy, text=shown, font=T.mono(9), fill=T.MUTED, anchor="w", tags=tags)
        if shown != rest or head_fit != head:
            k.link(pad + w - T.px(14), cy, "more", lambda: self.shell.info(head, " ".join(str(text).split())),
                   colour=T.DIM, anchor="e", tags=tags, size=9)
        return y + h + T.px(8)

    def _actions(self, g, pad, y, width, accent):
        a, k = self.app, self.kit
        tags = ("page",)
        h = T.px(48)
        x = pad
        gap = T.px(12)

        rows_used = [0]

        def btn(label, cmd, glyph=None, kind="secondary", w=None, enabled=True, tip=""):
            nonlocal x, y
            w = w or (T.width(label, T.mono(11, kind == "primary")) + T.px(70 if glyph else 44))
            # no room on this row: the next button goes under it. Without
            # this the last one - uninstall, since 2.0.1 - hung off the right
            # edge at 250% scaling, and there is no sideways scroll
            if x > pad and x + w > width - pad:
                x, y = pad, y + h + T.px(12)
                rows_used[0] += 1
            b = k.button(x, y, w, label, cmd, glyph=glyph, kind=kind, accent=accent, h=h, tags=tags,
                         enabled=enabled, tip=tip)
            x += w + gap
            return b

        busy_actions = {"installing": "installing", "autopilot": "autopilot running",
                        "stopping": "stopping after this install", "diagnosing": "reading the logs",
                        "uninstalling": "uninstalling", "preview": "working out the plan",
                        "checking": "asking for versions", "dlss": "updating dlss files",
                        "remix": "installing a remix mod"}
        job = getattr(a, "job_game", None)
        if a.busy and a.action in busy_actions and job is not None and job is not g:
            # the job belongs to another game (an autopilot pass waiting for
            # it to close, say): this page says so and leads back to it
            label = f"{busy_actions[a.action]}: {job.name}"
            bw = min(T.px(560), T.width(label, T.mono(11)) + T.px(70))
            btn(T.fit(label, T.mono(11), bw - T.px(70)), lambda: a.open_game(job), glyph="back", w=bw,
                tip="open that game's page")
            if a.action == "autopilot":
                btn("stop", a.stop_autopilot, glyph="close")
            return y + h + T.px(24)
        if a.busy and a.action in busy_actions:
            label = busy_actions[a.action]
            if a.progress:
                p, m = a.progress
                label = f"{label}  \u00b7  {p}%"
            b = btn(label, None, glyph="refresh", kind="primary", w=T.px(360), enabled=True)
            if a.progress:
                b.set(progress=a.progress[0] / 100)
            self._busy_btn = (b, busy_actions[a.action], g)
            if a.action == "autopilot":
                btn("stop", a.stop_autopilot, glyph="close")
            if a.progress:
                # drawn even while empty, so the next progress lands in place
                self._busy_text = self.c.create_text(
                    pad, y + h + T.px(18), text=T.fit(str(a.progress[1] or ""), T.mono(9), width - 2 * pad),
                    font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
                return y + h + T.px(40)
            return y + h + T.px(24)
        entering = a.entering or a.support is None
        ok = (getattr(a, "entry", {}) or {}).get("ok", (True, ""))[0]
        n_stale = a.stale.get(str(g.install_dir), 0)
        play_tip = "starts the game; the tool watches what loads"
        work_tip = "reads the game's logs and says what happened"
        if getattr(g, "kind", "") == "video":
            if g.installed:
                btn("open the player", lambda: a.video_launch(), glyph="play", kind="primary")
                btn("install again", a.install, glyph="download", enabled=not entering)
            else:
                btn("install", a.install, glyph="download", kind="primary", enabled=not entering)
        elif g.installed and n_stale:
            # an installed game with newer parts is still installed: the
            # update leads, and play and the check stay beside it
            btn(f"update ({n_stale})", a.install, glyph="download", kind="primary", enabled=not entering and ok,
                tip="installs again on the same route and settings, with the current parts")
            btn("play", a.start_game, glyph="play", enabled=not getattr(a, "launching", False), tip=play_tip)
            btn("did it work?", a.diagnose, glyph="check", tip=work_tip)
        elif g.installed:
            btn("play", a.start_game, glyph="play", kind="primary", enabled=not getattr(a, "launching", False),
                tip=play_tip)
            btn("did it work?", a.diagnose, glyph="check", tip=work_tip)
            btn("install again", a.install, glyph="download", enabled=not entering and ok)
        else:
            btn("install", a.install, glyph="download", kind="primary", enabled=not entering and ok)
            btn("autopilot", a.autopilot, glyph="pilot", enabled=not entering and ok,
                tip="experimental: installs, starts the game, checks it really loaded - "
                    "and if not, tries the next route and tells you")
        s = btn("settings", self.toggle_settings, glyph="gear", enabled=not entering)
        self.settings_tag = s.tag
        # the way out, next to the way in: it was only inside 'settings' in
        # 2.0.0, which read as "2.0 cannot uninstall" (#259)
        if g.installed:
            btn("uninstall", a.uninstall, glyph="trash", enabled=not a.busy,
                tip="takes out what was installed here; the game's own files come back")
        return y + h + T.px(24)

    def _features(self, g, pad, y, width, accent):
        a, k = self.app, self.kit
        tags = ("page",)
        x = pad
        items = []
        rows = getattr(a, "others", None) if getattr(a, "others_for", None) == str(g.install_dir) else None
        if rows and rows[0][1]:
            best = rows[0]
            from ..community import route_name
            items.append(("people", f"{best[1]} of {best[2]} worked with {route_name(best[0])}", accent,
                          lambda: self.shell.scroll_to(self._others_y - T.px(80)) if hasattr(self, "_others_y")
                          else None))
        # what the watcher really does for this game: only installed games are
        # watched, and only while watching is on
        if g.installed:
            # every installed game is on the watcher's list once watching is on
            # (watch_refresh runs after each scan, install and uninstall)
            watching = bool(a.watch_on())
            items.append(("eye", "watching this game" if watching else "watch off",
                          T.OK if watching else T.DIM, None))
        if g.installed:
            items.append(("compare", "before / after", T.MUTED, a.compare))
        items.append(("info", "what will happen?", T.MUTED, a.preview))
        items.append(("folder", "open folder", T.MUTED, a.open_folder))
        # the game ships an older DLSS than NVIDIA's newest (the dlss page's last scan)
        dl = a.dlss_line(g) if hasattr(a, "dlss_line") else ""
        if dl:
            quiet = a.dlss_line_quiet(g)          # an anti-cheat game: shown, not suggested
            items.insert(0, ("download", f"{dl}  update", T.MUTED if quiet else accent,
                             lambda: a.dlss_update_from_game(g)))
        right = width - pad
        for glyph, label, colour, cmd in items:
            wanted = T.px(22) + T.width(label, T.mono(9))
            if x > pad and x + wanted > right:
                x, y = pad, y + T.px(30)      # no room on this row: the rest go under it
            if cmd:
                tag, wid = k.link(x, y, label, cmd, glyph=glyph, colour=colour, hot=T.TEXT, tags=tags)
            else:
                k.glyph(x, y, glyph, colour, 11, anchor="w", tags=tags)
                self.c.create_text(x + T.px(22), y, text=label, font=T.mono(9), fill=colour, anchor="w", tags=tags)
                wid = wanted
            x += wid + T.px(30)
        return y + T.px(34)

    def _wrapped(self, text, x, y, room, colour):
        """One line of an answer, over as many rows as it needs. Cut to the
        width, the sentence that says what happened stopped mid-word and there
        was nowhere to read the rest of it."""
        c = self.c
        item = c.create_text(x, y, text=" ".join(str(text).split()), font=T.mono(9), fill=colour,
                             anchor="nw", width=room, tags=("page",))
        # the glyph beside it sits on the first row's middle, so the text
        # starts half a row higher
        c.move(item, 0, -T.px(8))
        box = c.bbox(item)
        return (box[3] + T.px(14)) if box else y + T.px(24)

    def _card(self, g):
        """The session card's content (sessioncard.build) for this game, or
        None. One reading, used by the layout and by the drawing."""
        a = self.app
        verdicts = a.verdicts if isinstance(getattr(a, "verdicts", None), dict) else {}
        running = g.name if getattr(a, "is_running", lambda _g: False)(g) else ""
        # "did it work?" pressed while the game runs is the answer the person
        # asked for: it outranks the running card (gate 2.1.0)
        r = a.result if isinstance(a.result, dict) else {}
        since = (getattr(a, "running_since", None) or {}).get(
            a._folder_key(g.install_dir) if hasattr(a, "_folder_key") else "",
            float("inf"))     # a start with no time: nothing older outranks it
        if running and r.get("kind") == "diagnosis" and float(r.get("at") or 0) >= since:
            running = ""
        return sessioncard.build(a.result, verdicts.get(str(g.install_dir)),
                                 getattr(a, "_card_measured", None), running)

    def _offer_in_card(self, g) -> bool:
        """Does the session card carry the offered route itself? The same
        conditions _offer has - and then _offer stays out, so the page shows
        one block, not a card and an offer above it."""
        a = self.app
        card = self._card(g)
        return bool(card and card["state"] == "failed" and card["next"] and not a.busy and g.installed)

    def _running(self, card, pad, y, width):
        c, tags = self.c, ("page",)
        w = min(width - 2 * pad, T.px(980))
        top = y
        self.kit.glyph(pad + T.px(20), y + T.px(24), "pilot", T.AMBER, 11, anchor="w", tags=tags)
        c.create_text(pad + T.px(46), y + T.px(24), text=card["headline"], font=T.mono(11, True),
                      fill=T.MUTED, anchor="w", tags=tags + ("result",))
        y = top + T.px(48)
        box = c.create_rectangle(pad, top, pad + w, y, fill=mix(T.BG, T.SURF, 0.9), outline=T.LINE, tags=tags)
        c.create_rectangle(pad, top, pad + T.px(3), y, fill=T.AMBER, outline="", tags=tags)
        c.tag_lower(box, "result")
        for item in c.find_withtag("backdrop"):
            c.tag_lower(item)
        return y + T.px(10)

    def _result(self, g, pad, y, width, accent):
        a, c, k = self.app, self.c, self.kit
        r = a.result
        tags = ("page",)
        card = self._card(g)
        if card is not None and card["state"] == "running":
            return self._running(card, pad, y, width)
        if not r:
            return y
        kind = r.get("kind")
        w = min(width - 2 * pad, T.px(980))
        top = y
        ok = r.get("ok", kind in ("installed", "removed"))
        warnings = [str(x) for x in (r.get("warnings") or []) if str(x).strip()]
        colour = T.OK if ok else (T.WARN if kind in ("diagnosis", "autopilot", "failed") else T.MUTED)
        if card is not None and card["state"] == "unsure":
            colour = T.AMBER          # the logs cannot tell: not a failure
        if ok and warnings:
            colour = T.AMBER          # installed, with something to read first
        title = r.get("title", "")
        if ok and warnings:
            title = f"{title} - {len(warnings)} warning{'s' if len(warnings) != 1 else ''}"
        # The title is the verdict, and a verdict is a sentence with its
        # instruction at the end ("... - move it out of the game folder"):
        # cut to one line, the part to act on was the part that went missing.
        head = c.create_text(pad + T.px(20), y + T.px(14), text=" ".join(str(title).split()),
                             font=T.mono(12, True), fill=colour, anchor="nw", width=w - T.px(40),
                             tags=tags + ("result",))
        box = c.bbox(head)
        y = max(y + T.px(42), (box[3] if box else y) + T.px(8))
        # the install's warnings (the swap ban, a taken proxy name, 'not
        # supported') went only to the log drawer, which is closed
        for text in warnings[:4]:
            head, rest = self.headline(text)
            k.glyph(pad + T.px(20), y, "warn", T.AMBER, 10, anchor="w", tags=tags)
            line = head + (f" - {rest}" if rest else "")
            y = self._wrapped(line, pad + T.px(44), y, w - T.px(64), T.TEXT)
        if len(warnings) > 4:
            c.create_text(pad + T.px(44), y, text=f"{len(warnings) - 4} more in details", font=T.mono(9),
                          fill=T.DIM, anchor="w", tags=tags)
            y += T.px(24)
        if kind == "diagnosis":
            if card is not None and card["stat"]:
                c.create_text(pad + T.px(20), y - T.px(4), text=card["stat"], font=T.mono(10), fill=T.TEXT,
                              anchor="w", tags=tags)
                y += T.px(26)
            for level, text in r.get("findings", []):
                col = T.WARN if level == "bad" else T.AMBER
                k.glyph(pad + T.px(20), y, "cross" if level == "bad" else "warn", col, 10, anchor="w", tags=tags)
                y = self._wrapped(text, pad + T.px(44), y, w - T.px(64), T.MUTED)
            offered = card["next"] if card is not None and not a.busy and g.installed else ""
            if offered and card["why"]:
                y = self._wrapped(card["why"], pad + T.px(20), y + T.px(6), w - T.px(40), T.MUTED)
        elif kind == "autopilot":
            x = pad + T.px(20)
            for i, t in enumerate(r.get("tries", [])):
                col = T.OK if t["ok"] else T.WARN
                k.glyph(x, y, "check" if t["ok"] else "cross", col, 11, anchor="w", tags=tags)
                c.create_text(x + T.px(22), y, text=t["route"], font=T.mono(10, t["ok"]),
                              fill=T.TEXT if t["ok"] else T.MUTED, anchor="w", tags=tags)
                x += T.px(22) + T.width(t["route"], T.mono(10, True)) + T.px(34)
            y += T.px(30)
        elif kind == "failed":
            # a refusal says what to pick instead in its last words
            # ("... Choose wilsjo2's fork"), which one line cut off
            y = self._wrapped(r.get("detail", ""), pad + T.px(20), y, w - T.px(40), T.MUTED)
        if kind in ("installed", "autopilot") and a.steps:
            steps = [s for s in a.steps if s[0] == "step"]
            shown = steps if self.steps_open else steps[:3]
            c.create_text(pad + T.px(20), y + T.px(4), text="in the game", font=T.mono(9), fill=T.DIM, anchor="w",
                          tags=tags)
            y += T.px(28)
            for i, (_k, text) in enumerate(shown, 1):
                c.create_text(pad + T.px(20), y, text=f"{i}", font=T.mono(9, True), fill=accent, anchor="w", tags=tags)
                y = self._wrapped(text, pad + T.px(44), y, w - T.px(64), T.TEXT) - T.px(4)
            warns = [s for s in a.steps if s[0] == "warn"]
            if self.steps_open:
                for _k, text in warns:
                    k.glyph(pad + T.px(20), y, "warn", T.AMBER, 10, anchor="w", tags=tags)
                    y = self._wrapped(text, pad + T.px(44), y, w - T.px(64), T.MUTED) - T.px(4)
            if len(steps) > 3 or warns:
                k.link(pad + T.px(20), y + T.px(4), "fewer" if self.steps_open else "all steps and warnings",
                       self._toggle_steps, glyph="up" if self.steps_open else "down", colour=T.DIM, tags=tags)
                y += T.px(30)
        # the buttons that belong to the result
        by = y + T.px(10)
        x = pad + T.px(20)
        acts = []
        if kind == "diagnosis":
            if offered:
                acts.append((f"try {community.route_name(offered)}", lambda: a.try_next(g, offered), "pilot"))
            acts.append(("share the result", a.share_result, "people"))
            if a._tune is not None:
                acts.append((f"set the work area to {a._tune.resolution}%", a.apply_tune, "gear"))
            if not ok:
                acts.append(("report a bug", lambda: a.report_bug("notwork"), "bug"))
        if a.seen_exe is not None:
            acts.append((f"use {a.seen_exe.name}", a.use_seen_exe, "refresh"))
        if kind == "failed":
            acts.append(("report a bug", lambda: a.report_bug("bug"), "bug"))
        acts.append(("details", lambda: self.shell.toggle_log(True), "down"))
        for label, cmd, glyph in acts:
            bw = T.width(label, T.mono(10)) + T.px(60)
            extra = {"kind": "primary"} if (kind == "diagnosis" and offered and label.startswith("try ")) else {}
            k.button(x, by, bw, label, cmd, glyph=glyph, h=T.px(38), tags=tags, size=10, accent=accent, **extra)
            x += bw + T.px(10)
        y = by + T.px(38) + T.px(16)
        box = c.create_rectangle(pad, top, pad + w, y, fill=mix(T.BG, T.SURF, 0.9), outline=T.LINE, tags=tags)
        c.create_rectangle(pad, top, pad + T.px(3), y, fill=colour, outline="", tags=tags)
        c.tag_lower(box, "result")
        for item in c.find_withtag("backdrop"):
            c.tag_lower(item)
        return y + T.px(10)

    def _offer(self, g, pad, y, width, accent):
        """The next route the watcher offered when this game closed and the
        route it had did not work - kept with the game's verdict, so it is
        still here after the toast is gone or the tool was restarted."""
        a, c, k = self.app, self.c, self.kit
        v = a.verdicts.get(str(g.install_dir)) if isinstance(getattr(a, "verdicts", None), dict) else None
        route = v.get("next") if isinstance(v, dict) else None
        if not isinstance(route, str) or not route or a.busy or not g.installed:
            return y
        tags = ("page",)
        why = str(v.get("why") or "")
        w = min(width - 2 * pad, T.px(980))
        top = y
        label = f"try {route}"
        bw = T.width(label, T.mono(10)) + T.px(60)
        k.button(pad + T.px(20), y + T.px(12), bw, label, lambda: a.try_next(g, route), glyph="pilot", h=T.px(38),
                 tags=tags, size=10, accent=accent, kind="primary",
                 tip="installs that route and checks it loads, the way autopilot does")
        bottom = y + T.px(62)
        if why:
            # beside the button while it fits one row, under it when it does
            # not: the reason is what says why this route and not another
            room = w - bw - T.px(60)
            one = T.width(" ".join(why.split()), T.mono(9)) <= room
            item = c.create_text(pad + bw + T.px(40), y + T.px(31) if one else y + T.px(12),
                                 text=" ".join(why.split()), font=T.mono(9), fill=T.MUTED,
                                 anchor="w" if one else "nw", width=room, tags=tags)
            box = c.bbox(item)
            if box:
                bottom = max(bottom, box[3] + T.px(14))
        y = bottom
        box = c.create_rectangle(pad, top, pad + w, y, fill=mix(T.BG, T.SURF, 0.9), outline=T.LINE, tags=tags)
        c.create_rectangle(pad, top, pad + T.px(3), y, fill=T.AMBER, outline="", tags=tags)
        c.tag_lower(box)
        for item in c.find_withtag("backdrop"):
            c.tag_lower(item)
        return y + T.px(12)

    def _others(self, pad, y, width, accent):
        a, c = self.app, self.c
        g = a.game
        rows = getattr(a, "others", None) if getattr(a, "others_for", None) == str(g.install_dir) else None
        if not rows:
            return y
        tags = ("page",)
        self._others_y = y
        c.create_text(pad, y + T.px(10), text="what worked for others", font=T.mono(9), fill=T.DIM, anchor="w",
                      tags=tags)
        # the same shared results, added up: how many people, how many got it
        # working, and how it went on the driver THIS machine has
        entry = None
        try:
            from .. import community
            entry = community.for_game(getattr(a, "_community", None) or {}, g)
        except Exception:
            entry = None
        # every read from the shared file is defended: it is written by
        # another repository from bodies people type by hand, and one odd
        # value used to stop the page mid-draw, on every redraw
        def _n(v):
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                return 0
        routes_in = (entry or {}).get("routes")
        routes_in = routes_in if isinstance(routes_in, dict) else {}
        totals = [(_n(v.get("worked")), _n(v.get("failed")))
                  for v in routes_in.values() if isinstance(v, dict)]
        n_ok = sum(w for w, _f in totals)
        n_all = sum(w + f for w, f in totals)
        head = ""
        if n_all:
            head = f"{n_all} shared result{'s' if n_all != 1 else ''}  \u00b7  {n_ok} worked"
            from .. import gpu
            drv = str(gpu.driver_version() or "")      # read once and cached in gpu
            drivers_in = (entry or {}).get("drivers")
            mine = drivers_in.get(drv) if isinstance(drivers_in, dict) else None
            if isinstance(mine, dict):
                mw, mf = _n(mine.get("worked")), _n(mine.get("failed"))
                if mw + mf:
                    head += f"  \u00b7  on driver {drv}: {mw} of {mw + mf}"
        if head:
            c.create_text(pad + T.width("what worked for others", T.mono(9)) + T.px(22), y + T.px(10),
                          text=T.fit(head, T.mono(9), width - 2 * pad - T.px(220)), font=T.mono(9),
                          fill=T.MUTED, anchor="w", tags=tags)
        y += T.px(40)
        bw = min(T.px(300), width - 2 * pad - T.px(260))
        measured = (entry or {}).get("measured")
        measured = measured if isinstance(measured, dict) else {}
        for i, (route, ok, n) in enumerate(rows[:5]):
            from ..community import route_name
            c.create_text(pad, y, text=T.fit(route_name(route), T.mono(9), T.px(146)), font=T.mono(9),
                          fill=T.MUTED, anchor="w", tags=tags)
            bx = pad + T.px(150)
            c.create_rectangle(bx, y - T.px(3), bx + bw, y + T.px(3), fill=T.SURF2, outline="", tags=tags)
            c.create_rectangle(bx, y - T.px(3), bx + bw * ok / max(1, n), y + T.px(3),
                               fill=accent if i == 0 else T.MUTED, outline="", tags=tags)
            c.create_text(bx + bw + T.px(16), y, text=f"{ok} of {n}", font=T.mono(9),
                          fill=T.TEXT if i == 0 else T.MUTED, anchor="w", tags=tags)
            m = measured.get(route)
            m = m if isinstance(m, dict) else {}
            try:
                fps = float(m.get("fps") or 0)
            except (TypeError, ValueError):
                fps = 0.0
            if fps > 0:
                res, n_rep = _n(m.get("res")), max(1, _n(m.get("n")) or 1)
                said = f"{fps:.0f} fps" + (f" at {res}%" if res else "")
                said += f"  ({n_rep} report{'s' if n_rep != 1 else ''})"
                c.create_text(bx + bw + T.px(110), y, text=said, font=T.mono(9), fill=T.DIM,
                              anchor="w", tags=tags)
            y += T.px(30)
        if n_all:
            c.create_text(pad, y + T.px(6), text=T.fit(
                "counts, not a promise: a route that worked for somebody else can still fail here, "
                "and one that failed for them can work", T.mono(9), width - 2 * pad),
                font=T.mono(9), fill=T.DIM, anchor="w", tags=tags)
            y += T.px(30)
        return y

    # ------------------------------------------------------------ behaviour
    def toggle_settings(self):
        self.settings_open = not self.settings_open
        self.shell.redraw()
        if self.settings_open:
            try:
                x1, y1, x2, y2 = self.c.bbox(self.settings_tag)
                target = max(0, y1 - T.px(120))
                start = self.c.canvasy(0)
                self.shell.motion.run("scroll", 320, lambda kk: self.shell.scroll_to(start + (target - start) * kk))
            except Exception:
                pass

    def _toggle_steps(self):
        self.steps_open = not self.steps_open
        self.shell.redraw()

    def _picture_menu(self, g, x, y):
        self.kit.menu(max(0, x), y, self.app.picture_items(g), width=T.px(300))

    def back(self) -> bool:
        if self.settings_open:
            self.toggle_settings()
            return True
        return False

    def shown(self) -> None:
        self.settings_open = False
        self.steps_open = False
        self.fade_in = True
