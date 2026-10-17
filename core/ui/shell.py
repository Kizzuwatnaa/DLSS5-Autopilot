"""The window around the pages: rail, scrolling content, banner, log drawer,
status line, toasts and dialogs.

Pages are objects with `draw(width)`; the shell calls it on show and again
when the window is resized, so every page is a function of its state and
never a pile of widgets that drift out of step with it.

Order of closing, everywhere: an open menu or panel (the kit's layers), then
the log drawer, then the page's own back. Esc walks that order one step per
press; the rail and the back arrow jump over it.
"""
from __future__ import annotations

import re
import time
import tkinter as tk

from .. import log, prefs, update
from . import theme as T
from . import win
from .kit import Kit, Layer
from .motion import Motion, ink_on, mix

TITLE = "DLSS 5 Autopilot"


class Page:
    """Base for a page: `draw` from state, `back` for Esc/Backspace."""
    name = ""
    rail = 0                       # which rail item lights up

    def __init__(self, shell: "Shell"):
        self.shell = shell
        self.kit = shell.kit
        self.c = shell.content

    def draw(self, width: int) -> int:
        """Draw at this width; return the content height."""
        return 0

    def shown(self) -> None:
        pass

    def hidden(self) -> None:
        pass

    def back(self) -> bool:
        return False

    def key(self, e) -> bool:
        return False


_GEOMETRY = re.compile(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")


def _restore(root: tk.Tk, sw: int, sh: int) -> bool:
    """Put the window back the way it was closed (#641: it opened maximised
    every time, across the whole of a 32:9 display). True when it should be
    maximised - which is also the answer when nothing was remembered.

    LIMITS: the size is kept only while it fits the desktop, and the place
    only while the title bar would still be on it - a display that was
    unplugged since must not leave the window where nobody can reach it."""
    was = prefs.get("window")
    if not isinstance(was, dict):
        return True
    try:
        w, h, x, y = (int(was[k]) for k in ("w", "h", "x", "y"))
        vx, vy = root.winfo_vrootx(), root.winfo_vrooty()
        vw, vh = max(sw, root.winfo_vrootwidth()), max(sh, root.winfo_vrootheight())
    except (KeyError, TypeError, ValueError, OverflowError, tk.TclError):
        return bool(was.get("zoomed", True))
    if 200 <= w <= vw and 200 <= h <= vh:
        reachable = vx <= x + w - 120 and x + 120 <= vx + vw and vy <= y and y + 40 <= vy + vh
        root.geometry(f"{w}x{h}+{x}+{y}" if reachable else f"{w}x{h}")
    return bool(was.get("zoomed", False))


class Shell:
    def remember_window(self) -> None:
        """Called as the window closes. A minimised or hidden window says
        nothing about where it belongs, and a maximised one keeps the size it
        had before, for the day it is restored."""
        try:
            state = self.root.state()
            at = _GEOMETRY.fullmatch(self.root.geometry())
        except tk.TclError:
            return
        if state not in ("normal", "zoomed"):
            return
        was = prefs.get("window")
        now = dict(was) if isinstance(was, dict) else {}
        now["zoomed"] = state == "zoomed"
        if state == "normal" and at:
            now.update(zip(("w", "h", "x", "y"), map(int, at.groups())))
        if now != was:
            prefs.set_("window", now)

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(TITLE)
        root.configure(bg=T.BG)
        win.set_icons(root)
        root.after(0, lambda: win.dark_titlebar(root))
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(T.px(1400), int(sw * 0.95))}x{min(T.px(900), int(sh * 0.9))}")
        root.minsize(min(T.px(900), int(sw * 0.6)), min(T.px(620), int(sh * 0.5)))
        try:
            if _restore(root, sw, sh):
                root.state("zoomed")
        except tk.TclError:
            pass

        self.motion = Motion(root)
        self.rail_c = tk.Canvas(root, width=T.px(84), bg=T.RAIL, highlightthickness=0)
        self.rail_c.pack(side="left", fill="y")
        right = tk.Frame(root, bg=T.BG)
        right.pack(side="left", fill="both", expand=True)
        self.banner_c = tk.Canvas(right, height=0, bg=T.BG, highlightthickness=0)
        self.banner_c.pack(side="top", fill="x")
        self.bottom_c = tk.Canvas(right, height=T.px(34), bg=T.BG, highlightthickness=0)
        self.bottom_c.pack(side="bottom", fill="x")
        self.drawer = tk.Frame(right, bg=T.LOG_BG, height=1)
        self.drawer.pack_propagate(False)          # packed only while open
        self.content = tk.Canvas(right, bg=T.BG, highlightthickness=0, yscrollincrement=1)
        self.content.pack(side="top", fill="both", expand=True)

        self.kit = Kit(self.content, self.motion)
        # a click on empty page closes the log drawer, like every other panel
        self.kit.on_background = lambda: self.log_open and self.toggle_log(False)
        self.rail_kit = Kit(self.rail_c, self.motion)
        self.bottom_kit = Kit(self.bottom_c, self.motion)
        self.banner_kit = Kit(self.banner_c, self.motion)

        self.pages: dict[str, Page] = {}
        self.page: Page | None = None
        self.history: list[str] = []
        self.content_h = 0
        self._resize_job = None
        self._last_w = 0
        self.status_text = "ready"
        self.busy_text = ""
        self.banners: dict[str, dict] = {}
        self.watching = None          # (on, label) from the controller
        self.on_watch = None
        self.on_help = None           # list of (label, cmd) for the help menu
        self.log_open = False
        self.dialog: "Dialog | None" = None      # the question on screen, if any

        self._build_log()
        self.content.bind("<Configure>", self._configured)
        self.rail_c.bind("<Configure>", lambda _e: self.draw_rail())
        self.bottom_c.bind("<Configure>", lambda _e: self.draw_bottom())
        root.bind_all("<MouseWheel>", self._wheel, add="+")
        root.bind("<Escape>", self._escape)
        root.bind("<BackSpace>", self._backspace)
        # a question that is open follows the window and comes back to the
        # front with it. Bound once, here: a dialog that bound its own had to
        # take them off again, and unbinding one handler takes all of them
        # with it on the Python the release is built with.
        root.bind("<Configure>", lambda _e: self.dialog and self.dialog._place(), add="+")
        root.bind("<FocusIn>", lambda _e: self.dialog and self.dialog._front(), add="+")
        # The window closing is an answer to whatever it was asking. Without
        # it the question's wait has no writer left: quitting from the tray
        # with one up left python.exe spinning on a dead interpreter for
        # ever, and every later launch then met the "already open" guard.
        root.bind("<Destroy>", lambda e: self._answer_dialog() if e.widget is root else None, add="+")
        root.bind("<Control-h>", lambda e: None if self._in_text(e) else self.home())
        root.bind("<KeyPress>", self._key, add="+")

    # ============================================================ pages
    def add(self, page: Page) -> None:
        self.pages[page.name] = page

    def show(self, name: str, remember: bool = True) -> None:
        page = self.pages[name]
        self.kit.close_all()
        self.kit._hide_tip()
        if self.page is not None and self.page is not page:
            self.page.hidden()
            if remember:
                self.history.append(self.page.name)
                del self.history[:-10]
        self.page = page
        self.content.yview_moveto(0)
        self.redraw()
        self.draw_rail()
        page.shown()

    def home(self) -> None:
        self.history.clear()
        self.show("library", remember=False)

    def back(self) -> None:
        if self.page is not None and self.page.back():
            return
        hidden = self.rail_hidden()
        while self.history:
            name = self.history.pop()
            if name in self.pages and name not in hidden \
                    and (self.page is None or name != self.page.name):
                self.show(name, remember=False)
                return
        if self.page is None or self.page.name != "library":
            self.home()

    def redraw(self) -> None:
        if self.page is None:
            return
        c = self.content
        w = max(T.px(400), c.winfo_width())
        c.delete("page")
        # Only the page's own animations: stopping all of them left the log
        # drawer half open with log_open False and froze a toast mid-slide.
        self.kit.motion.stop_all(keep=self.SHELL_MOTION)
        h = self.page.draw(w) or 0
        self.set_height(h)

    # The animation keys the shell owns, which a page redraw never stops.
    # The pages' keys (card{i}, glow, sweep, dlss_sweep, backdrop) animate
    # items the redraw deletes; "scroll" is the view itself, from the wheel
    # or a page gliding to a section.
    SHELL_MOTION = ("drawer", "toast", "scroll")

    def set_height(self, h: int) -> None:
        """Every page draw and soft refresh ends here: the scroll region, then
        what sits above the page (a toast, an open menu) back on top of the
        items just drawn, then the kit's record of what is gone."""
        c = self.content
        view = max(1, c.winfo_height())
        self.content_h = max(h, view)
        c.configure(scrollregion=(0, 0, c.winfo_width(), self.content_h))
        self._raise_layers()
        self.kit.prune()
        self._thumb()

    def _raise_layers(self) -> None:
        c = self.content
        c.tag_raise("toast")                 # one sliding out is no layer any more
        for layer in list(self.kit.layers):
            c.tag_raise(layer.tag)
        c.tag_raise("kit_tip")

    def _configured(self, e) -> None:
        if abs(e.width - self._last_w) < 2 and self.page is not None:
            self.set_height(self.content_h)
            return
        self._last_w = e.width
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(70, self.redraw)

    # ============================================================ scrolling
    def _in_text(self, e) -> bool:
        return isinstance(e.widget, (tk.Entry, tk.Text, tk.Spinbox))

    def _wheel(self, e):
        w = e.widget
        if isinstance(w, tk.Text):
            return None                      # the log scrolls itself
        top = self.kit.top()
        if top is not None and hasattr(top, "wheel") and top.wheel(e):
            return "break"
        inside = w is self.content or (isinstance(w, tk.Misc) and str(w).startswith(str(self.content)))
        if not inside:
            return None
        view = self.content.winfo_height()
        if self.content_h <= view:
            return "break"
        step = -int(e.delta / 120 * T.px(90))
        first = self.content.canvasy(0)
        # glide to where the notches add up to, instead of jumping a notch at
        # a time: a fast flick keeps adding to the same target
        base = self._scroll_to if self.motion.busy("scroll") else first
        target = max(0, min(self.content_h - view, base + step))
        self._scroll_to = target
        self._wheel_at = time.monotonic()
        self.kit._hide_tip()
        start = first

        def glide(k):
            now = self.content.canvasy(0)
            want = start + (target - start) * k
            if int(want - now):
                self.content.yview_scroll(int(want - now), "units")
            self._thumb()
        self.motion.run("scroll", 160, glide)
        return "break"

    def scrolling(self) -> bool:
        """The wheel moved the page in the last moment."""
        return self.motion.busy("scroll") or time.monotonic() - getattr(self, "_wheel_at", 0.0) < 0.25

    def scroll_to(self, y: float) -> None:
        view = self.content.winfo_height()
        self.content.yview_moveto(max(0, min(1, y / max(1, self.content_h))))
        self._thumb()

    def _thumb(self) -> None:
        c = self.content
        c.delete("thumb")
        view = c.winfo_height()
        if self.content_h <= view + 1:
            return
        top = c.canvasy(0)
        h = max(T.px(30), view * view / self.content_h)
        y = top + (view - h) * (top / (self.content_h - view))
        x = c.winfo_width() - T.px(5)
        c.create_rectangle(x, y + T.px(2), x + T.px(3), y + h - T.px(2), fill=T.LINE,
                           outline="", tags="thumb")

    # ============================================================ keys
    def _answer_dialog(self) -> bool:
        """Answer the question on screen the way cancelling it answers it."""
        d = self.dialog
        if d is None:
            return False
        d._finish(None if d.entry is not None else False)
        return True

    def _escape(self, e):
        if self._answer_dialog():
            # a question is up: it is what Escape is for, not the page behind it
            return "break"
        top = self.kit.top()
        if top is not None and not str(top.tag).startswith("toast"):
            # a menu closes even with the caret in the search box: a key that
            # does nothing where the person is looking reads as a stuck window.
            # A toast is not a menu - it goes on its own - so Escape with one
            # on screen still belongs to the search box and the page.
            self.kit._hide_tip()
            self.kit.pop()
            return "break"
        if isinstance(e.widget, tk.Entry):
            return None                      # the field clears itself first
        self.kit._hide_tip()
        if self.kit.top() is not None:
            self.kit.pop()
        elif self.log_open:
            self.toggle_log(False)
        else:
            self.back()
        return "break"

    def _backspace(self, e):
        if self._in_text(e):
            return None
        if self.kit.top() is None:
            self.back()
        return "break"

    def _key(self, e):
        if self.page is not None and not self._in_text(e):
            self.page.key(e)

    # ============================================================ rail
    # A page's `rail` is its index here: "dlss" sits after the games it is about.
    RAIL_ITEMS = (("library", "game", "games"), ("dlss", "chip", "dlss"),
                  ("video", "video", "video"), ("remix", "remix", "remix"),
                  ("vr", "headset", "vr"))
    # The pages a person can take off the rail (#293). Games and dlss stay:
    # one is home, the other is where every game's DLSS files are.
    HIDEABLE = ("video", "remix", "vr")

    def rail_hidden(self) -> set:
        """The rail items switched off. settings.json is a file a person can
        edit, so only names that can be hidden count."""
        v = prefs.get("rail_hidden") or []
        return {x for x in (v if isinstance(v, list) else []) if x in self.HIDEABLE}

    def set_rail_hidden(self, name: str, hide: bool) -> None:
        if name not in self.HIDEABLE:
            return
        now = self.rail_hidden()
        now = (now | {name}) if hide else (now - {name})
        prefs.set_("rail_hidden", [x for x in self.HIDEABLE if x in now])
        if hide:
            # Back must not land on a page the rail no longer shows, and the
            # page on screen goes home with it.
            self.history = [h for h in self.history if h != name]
            if self.page is not None and self.page.name == name:
                self.home()
        self.draw_rail()

    def _rail_item_menu(self, page: str) -> None:
        self.kit.close_all()
        self.rail_menu([(f"hide {page} from the sidebar",
                         lambda: self.set_rail_hidden(page, True)),
                        ("games > view > sidebar pages brings it back", lambda: None, False)],
                       f"nav_{page}", T.px(440), max_rows=2)

    # The rail's shapes, roomiest first; draw_rail takes the first one that
    # fits the window's height. One fixed layout needed 580 px at 100%, so
    # at 175% on a 1080p laptop, or in any window dragged shorter, the pages
    # ran down into watch and help and a click on video landed on watch.
    #   pages:  labels, logo, logo y, first y, step, hit above, hit below,
    #           glyph size, glyph dy, label dy
    #   bottom: watch and help (from the bottom), hit above, hit below watch,
    #           hit below help, label dy, glyph dy
    RAIL_SHAPES = (
        ((True, True, 42, 122, 72, 28, 32, 17, -4, 20), (116, 52, 22, 34, 30, 22, 0)),
        ((True, True, 30, 88, 56, 23, 27, 14, -9, 18), (92, 40, 18, 28, 26, 17, -3)),
        ((False, True, 30, 80, 42, 19, 19, 15, 0, 0), (62, 24, 18, 18, 18, 0, 0)),
        ((False, False, 0, 26, 42, 19, 19, 15, 0, 0), (62, 24, 18, 18, 18, 0, 0)),
    )
    RAIL_GAP = 6                     # between the last page and watch

    @classmethod
    def rail_shape(cls, h: int, n: int):
        """(page shape, bottom shape, pages drawn) for a rail `h` px tall with
        `n` pages. Past the last shape the pages that do not fit go behind a
        "more" item, so nothing is ever drawn on top of anything else."""
        for top, bottom in cls.RAIL_SHAPES:
            last = top[3] + (n - 1) * top[4] + top[6]
            if T.px(last + cls.RAIL_GAP) <= h - T.px(bottom[0] + bottom[2]):
                return top, bottom, n
        top, bottom = cls.RAIL_SHAPES[-1]
        room = h - T.px(bottom[0] + bottom[2] + cls.RAIL_GAP + top[3] + top[6])
        fits = max(1, room // max(1, T.px(top[4])) + 1)
        return top, bottom, min(n, fits)

    def draw_rail(self) -> None:
        c = self.rail_c
        k = self.rail_kit
        c.delete("all")
        self.content.delete("rail_hint")
        rw = T.px(84)
        h = c.winfo_height()
        if h <= 1:                   # not laid out yet; <Configure> draws it again
            h = T.px(600)
        c.create_line(rw - 1, 0, rw - 1, h, fill=T.LINE)
        active = self.RAIL_ITEMS[self.page.rail if self.page is not None else 0][0]
        hidden = self.rail_hidden()
        shown = [it for it in self.RAIL_ITEMS if it[0] not in hidden]
        top, bottom, fits = self.rail_shape(h, len(shown))
        labels, logo, logo_y, first, step, up, down, gsize, gdy, ldy = top
        more = []
        if fits < len(shown):
            shown, more = shown[:fits - 1], shown[fits - 1:]
        if logo:
            try:
                img = getattr(self, "_logo", None)
                if img is None:
                    from . import imaging
                    buf = imaging.picture(win.ico_path(), T.px(36), T.px(36), bg=T.RAIL, cover=False)
                    if buf:
                        img = self._logo = imaging.photo(tk, buf, T.px(36), T.px(36), c)
                if img is not None:
                    c.create_image(rw / 2, T.px(logo_y), image=img, tags="logo")
                    k.hover("logo")
                    k.on_click("logo", self._rail_nav("library"))
            except Exception:
                pass
        items = [(p, g, lb, self._rail_nav(p)) for p, g, lb in shown]
        if more:
            items.append(("more", "more", "more", lambda more=more: self._rail_more(more)))
        for i, (page, glyph, label, go) in enumerate(items):
            y = T.px(first + i * step)
            on = page == active or (page == "more" and active in [m[0] for m in more])
            tag = f"nav_{page}"
            col = T.TEXT if on else T.DIM
            c.create_rectangle(T.px(4), y - T.px(up), rw - T.px(4), y + T.px(down), fill=T.RAIL,
                               outline="", tags=tag)
            if on:
                c.create_rectangle(0, y - T.px(up - 2), T.px(3), y + T.px(down - 2),
                                   fill=T.AMBER, outline="")
            parts = [k.glyph(rw / 2, y + T.px(gdy), glyph, col, gsize, tags=tag)]
            if labels:
                parts.append(k.text(rw / 2, y + T.px(ldy), label, col, 8, anchor="center", tags=tag))
            self._rail_hover(tag, parts, None if on else (T.MUTED, T.DIM), None if labels else label)
            k.on_click(tag, go)
            if page in self.HIDEABLE:
                k._bind(tag, "<Button-3>", lambda _e, page=page: self._rail_item_menu(page),
                        role="menu")
        # watcher and help at the bottom
        w_off, h_off, bup, wdown, hdown, bldy, bgdy = bottom
        on, label = self.watching or (False, "watch off")
        y = h - T.px(w_off)
        c.create_rectangle(T.px(4), y - T.px(bup), rw - T.px(4), y + T.px(wdown), fill=T.RAIL,
                           outline="", tags="nav_watch")
        col = T.OK if on else T.DIM
        parts = [k.glyph(rw / 2, y + T.px(bgdy), "eye", col, 14, tags="nav_watch")]
        if labels:
            parts.append(k.text(rw / 2, y + T.px(bldy), label, col, 7, anchor="center",
                                tags="nav_watch"))
        self._rail_hover("nav_watch", parts, None, None if labels else label)
        k.on_click("nav_watch", lambda: self.on_watch and self.on_watch())
        y = h - T.px(h_off)
        c.create_rectangle(T.px(4), y - T.px(bup), rw - T.px(4), y + T.px(hdown), fill=T.RAIL,
                           outline="", tags="nav_help")
        parts = [k.glyph(rw / 2, y + T.px(bgdy), "help", T.DIM, 13, tags="nav_help")]
        if labels:
            parts.append(k.text(rw / 2, y + T.px(bldy), "help", T.DIM, 7, anchor="center",
                                tags="nav_help"))
        self._rail_hover("nav_help", parts, (T.MUTED, T.DIM), None if labels else "help")
        k.on_click("nav_help", self.open_help)
        k.prune()

    def _rail_hover(self, tag: str, parts: list, colours, hint) -> None:
        """Brighten an item under the pointer and, when the rail is too short
        for words, say its name beside it on the page."""
        c = self.rail_c

        def enter():
            if colours:
                for x in parts:
                    c.itemconfigure(x, fill=colours[0])
            if hint:
                self._rail_hint(tag, hint)

        def leave():
            if colours:
                for x in parts:
                    c.itemconfigure(x, fill=colours[1])
            self.content.delete("rail_hint")
        self.rail_kit.hover(tag, enter, leave)

    def _rail_hint(self, tag: str, text: str) -> None:
        c, r = self.content, self.rail_c
        c.delete("rail_hint")
        box = r.bbox(tag)
        if not box:
            return
        y = r.winfo_rooty() + (box[1] + box[3]) / 2 - c.winfo_rooty() + c.canvasy(0)
        pad = T.px(8)
        t = c.create_text(c.canvasx(0) + T.px(8) + pad, y, text=text, font=T.mono(9),
                          fill=T.TEXT, anchor="w", tags="rail_hint")
        x1, y1, x2, y2 = c.bbox(t)
        b = c.create_rectangle(x1 - pad, y1 - T.px(4), x2 + pad, y2 + T.px(4), fill=T.SURF2,
                               outline=T.LINE, tags="rail_hint")
        c.tag_lower(b, t)
        c.tag_raise("rail_hint")

    def _rail_more(self, pages) -> None:
        """The pages a very short rail had no room for, as a menu beside it."""
        self.kit.close_all()
        self.rail_menu([(label, self._rail_nav(p)) for p, _g, label in pages],
                       "nav_more", T.px(260), max_rows=len(pages))

    def _rail_nav(self, page):
        def go():
            self.kit.close_all()
            if page == "library":
                self.home()
            elif page in self.pages:
                self.show(page)
        return go

    def open_help(self) -> None:
        top = self.kit.top()
        if top is not None and top.tag.startswith("menu") and getattr(self, "_help_menu", None) is top:
            self.kit.pop()
            return
        items = list(self.on_help() if callable(self.on_help) else [])
        if not items:
            return
        self._help_menu = self.rail_menu(items, "nav_help", T.px(300))

    def rail_menu(self, items, rail_tag: str, width: int, max_rows: int = 12):
        """A menu that opens beside the rail item that asked for it, its bottom
        level with the item's, never under the window's bottom edge.

        Placing it from the bottom of the page by hand let Menu's own "no room
        below, open upwards" rule fire as well, and the help menu came up half
        a screen away from the click."""
        from .kit import Menu
        c, r = self.content, self.rail_c
        box = r.bbox(rail_tag) or (0, 0, 0, r.winfo_height())
        # the item's bottom, in the content canvas' own coordinates
        item_bottom = r.winfo_rooty() + box[3] - c.winfo_rooty() + c.canvasy(0)
        rows = min(len(items), max_rows)
        h = rows * T.px(34) + T.px(8)
        vy1, vy2 = c.canvasy(0), c.canvasy(c.winfo_height())
        y = max(vy1 + T.px(8), min(item_bottom, vy2 - T.px(8)) - h - 1)
        Menu(self.kit, c.canvasx(0) + T.px(8), y, items, opener=None, width=width,
             current=None, accent=T.AMBER, max_rows=max_rows)
        return self.kit.top()

    # ============================================================ bottom / status
    def status(self, text: str) -> None:
        self.status_text = text
        self.draw_bottom()

    def busy(self, text: str = "") -> None:
        self.busy_text = text
        self.draw_bottom()

    def draw_bottom(self) -> None:
        c = self.bottom_c
        k = self.bottom_kit
        c.delete("all")
        w = c.winfo_width()
        h = T.px(34)
        c.create_line(T.px(24), 0, w - T.px(24), 0, fill=T.LINE)
        text = self.busy_text or self.status_text
        col = T.AMBER if self.busy_text else T.DIM
        k.text(T.px(44), h / 2, T.fit(text, T.mono(9), w - T.px(200)), col, 9)
        label = "log"
        tag, wid = k.link(w - T.px(44), h / 2, label, lambda: self.toggle_log(),
                          glyph="up" if not self.log_open else "down", colour=T.DIM,
                          hot=T.TEXT, anchor="e")
        k.prune()          # redrawn on every status line: its old link's handlers go

    # ============================================================ log drawer
    def _build_log(self) -> None:
        d = self.drawer
        head = tk.Canvas(d, height=T.px(34), bg=T.LOG_BG, highlightthickness=0, cursor="sb_v_double_arrow")
        head.pack(side="top", fill="x")
        self.log_head = head
        self.log_head_kit = Kit(head, self.motion)
        self.log_text = tk.Text(d, bg=T.LOG_BG, fg=T.MUTED, insertbackground=T.MUTED,
                                font=T.mono(9), borderwidth=0, wrap="word", spacing1=2,
                                highlightthickness=0, padx=T.px(40), pady=T.px(4))
        self.log_text.pack(side="top", fill="both", expand=True)
        for tag, colour in (("ok", T.OK), ("err", T.WARN), ("warn", T.AMBER), ("head", T.TEXT)):
            self.log_text.tag_configure(tag, foreground=colour)
        self.log_text.config(state="disabled")
        self.log_popped: tk.Text | None = None
        head.bind("<Configure>", lambda _e: self._draw_log_head())
        head.bind("<ButtonPress-1>", self._drag_start, add="+")
        head.bind("<B1-Motion>", self._drag, add="+")
        head.bind("<ButtonRelease-1>", self._drag_end, add="+")
        head.bind("<Double-1>", lambda _e: self._set_share(0, save=True))
        try:
            v = float(prefs.get("log_share") or 0)
        except (TypeError, ValueError):
            v = 0.0
        self.log_share = v if 0.1 <= v <= 0.9 else 0.0

    def _draw_log_head(self) -> None:
        c, k = self.log_head, self.log_head_kit
        c.delete("all")
        w = c.winfo_width()
        y = T.px(17)
        c.create_line(0, 0, w, 0, fill=T.LINE)
        c.create_rectangle(w / 2 - T.px(20), T.px(5), w / 2 + T.px(20), T.px(7), fill=T.LINE, outline="")
        k.text(T.px(40), y, "what the tool is doing", T.DIM, 9)
        x = w - T.px(40)
        for label, glyph, cmd in (("close", "down", lambda: self.toggle_log(False)),
                                  ("pop out", "link", self.pop_log),
                                  ("open file", "folder", self.open_log_file),
                                  ("copy", "copy", self.copy_log),
                                  ("clear", "trash", self.clear_log)):
            tag, wid = k.link(x, y, label, cmd, glyph=glyph, colour=T.DIM, anchor="e")
            x -= wid + T.px(28)
        k.prune()

    def _drawer_height(self) -> int:
        total = self.root.winfo_height()
        if self.log_share:
            return int(total * self.log_share)
        return max(T.px(160), min(T.px(320), int(total * 0.32)))

    def toggle_log(self, open_: bool | None = None) -> None:
        want = (not self.log_open) if open_ is None else open_
        if want == self.log_open:
            return
        self.log_open = want
        # A frame cannot be 0 px tall - a closed drawer left a 3 px strip -
        # so it is packed only while it is open.
        if want and not self.drawer.winfo_ismapped():
            self.drawer.configure(height=1)
            self.drawer.pack(side="bottom", fill="x", before=self.content)
        start = self.drawer.winfo_height() if self.drawer.winfo_ismapped() else 1
        end = self._drawer_height() if want else 1

        def step(k):
            self.drawer.configure(height=max(1, int(start + (end - start) * k)))

        def done():
            if not self.log_open:
                self.drawer.pack_forget()
        self.motion.run("drawer", 220, step, done)
        if want:
            self.log_text.see("end")
        self.draw_bottom()

    def _drag_start(self, e):
        self._drag_y = e.y_root
        self._drag_h = self.drawer.winfo_height()

    def _drag(self, e):
        if not hasattr(self, "_drag_y"):
            return
        h = self._drag_h + (self._drag_y - e.y_root)
        total = max(1, self.root.winfo_height())
        h = max(T.px(90), min(int(total * 0.8), h))
        self.drawer.configure(height=h)

    def _drag_end(self, _e):
        if hasattr(self, "_drag_y"):
            total = max(1, self.root.winfo_height())
            self._set_share(round(int(self.drawer.cget("height")) / total, 3), save=True)
            del self._drag_y

    def _set_share(self, v: float, save: bool) -> None:
        self.log_share = v
        if self.log_open:
            self.drawer.configure(height=self._drawer_height())
        if save:
            prefs.set_("log_share", v)

    def write(self, text: str, tag: str = "") -> None:
        for t in (self.log_text, self.log_popped):
            if t is None:
                continue
            try:
                t.config(state="normal")
                t.insert("end", text + "\n", tag or ())
                t.see("end")
                t.config(state="disabled")
            except tk.TclError:
                self.log_popped = None

    def clear_log(self) -> None:
        for t in (self.log_text, self.log_popped):
            if t is None:
                continue
            try:
                t.config(state="normal")
                t.delete("1.0", "end")
                t.config(state="disabled")
            except tk.TclError:
                self.log_popped = None

    def copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", "end-1c"))
        self.status("the log is on the clipboard")

    def open_log_file(self) -> None:
        import subprocess
        try:
            p = log.path()
            if not p.is_file():
                log.write("log opened from the window")
            subprocess.Popen(["explorer", "/select,", str(p)])
        except Exception:
            self.info("log file", str(getattr(log, "path", lambda: "")()))

    def pop_log(self) -> None:
        w = getattr(self, "_logwin", None)
        try:
            if w is not None and w.winfo_exists():
                w.deiconify()
                w.lift()
                return
        except tk.TclError:
            pass
        w = tk.Toplevel(self.root)
        w.title(f"{TITLE} - what the tool is doing")
        w.configure(bg=T.LOG_BG)
        w.geometry(f"{T.px(900)}x{T.px(600)}")
        win.dark_titlebar(w)
        t = tk.Text(w, bg=T.LOG_BG, fg=T.MUTED, font=T.mono(9), borderwidth=0, wrap="word",
                    spacing1=2, highlightthickness=0, padx=T.px(16), pady=T.px(10))
        t.pack(fill="both", expand=True)
        for tag, colour in (("ok", T.OK), ("err", T.WARN), ("warn", T.AMBER), ("head", T.TEXT)):
            t.tag_configure(tag, foreground=colour)
        t.insert("1.0", self.log_text.get("1.0", "end-1c"))
        for tag in ("ok", "err", "warn", "head"):
            r = self.log_text.tag_ranges(tag)
            for a, b in zip(r[0::2], r[1::2]):
                t.tag_add(tag, a, b)
        t.see("end")
        t.config(state="disabled")

        def closed():
            self.log_popped = None
            w.destroy()
        w.protocol("WM_DELETE_WINDOW", closed)
        self._logwin, self.log_popped = w, t

    # ============================================================ banner
    def banner(self, key: str, text: str, actions=(), colour=T.AMBER) -> None:
        """A line across the top: an update, a crash. actions: (label, cmd, primary)."""
        self.banners[key] = {"text": text, "actions": list(actions), "colour": colour}
        self._draw_banners()

    def unbanner(self, key: str) -> None:
        if self.banners.pop(key, None) is not None:
            self._draw_banners()

    def _draw_banners(self) -> None:
        c, k = self.banner_c, self.banner_kit
        c.delete("all")
        row = T.px(44)
        c.configure(height=row * len(self.banners))
        w = max(c.winfo_width(), self.root.winfo_width() - T.px(84))
        for i, (key, b) in enumerate(self.banners.items()):
            y = i * row
            bg = mix(T.BG, b["colour"], 0.10)
            c.create_rectangle(0, y, w, y + row, fill=bg, outline="")
            c.create_rectangle(0, y, T.px(3), y + row, fill=b["colour"], outline="")
            k.text(T.px(44), y + row / 2, b["text"], T.TEXT, 9)
            x = w - T.px(44)
            tag, wid = k.link(x, y + row / 2, "", lambda key=key: self.unbanner(key),
                              glyph="close", colour=T.DIM, anchor="e")
            x -= wid + T.px(20)
            for label, cmd, primary in reversed(b["actions"]):
                bw = T.width(label, T.mono(9, True)) + T.px(28)
                k.button(x - bw, y + T.px(8), bw, label, cmd, kind="primary" if primary else "secondary",
                         accent=b["colour"], h=row - T.px(16), size=9)
                x -= bw + T.px(10)
        k.prune()

    # ============================================================ toast
    def toast(self, title: str, text: str = "", actions=(), accent=T.AMBER, timeout=9000,
              glyph="info") -> None:
        """A card in the corner. Its items carry "toast" and their own
        toast<n>, never "page": a page redraw deleted it (the watcher's answer
        is shown and then the game page refreshes) and left its layer
        swallowing clicks; now a redraw raises it back above the page."""
        c, k = self.content, self.kit
        # one toast at a time: the one before goes at once, with its slide
        for layer in [x for x in k.layers if x.tag.startswith("toast")]:
            k.remove(layer, animate=False)
        for key in [x for x in self.motion.jobs if x.startswith("toast")]:
            self.motion.stop(key)
        c.delete("toast")
        self._toast_n = getattr(self, "_toast_n", 0) + 1
        tag = f"toast{self._toast_n}"
        w = T.px(400)
        lines = 1 + (1 if text else 0)
        h = T.px(28) * lines + T.px(34) + (T.px(44) if actions else 0)
        vx2 = c.canvasx(c.winfo_width())
        vy2 = c.canvasy(c.winfo_height())
        x, y = vx2 - w - T.px(28), vy2 - h - T.px(20)
        tags = (tag, "toast")
        c.create_rectangle(x, y, x + w, y + h, fill=T.SURF2, outline=T.LINE, tags=tags)
        c.create_rectangle(x, y, x + T.px(3), y + h, fill=accent, outline="", tags=tags)
        k.glyph(x + T.px(22), y + T.px(26), glyph, accent, 11, anchor="w", tags=tags)
        k.text(x + T.px(46), y + T.px(26), T.fit(title, T.mono(10, True), w - T.px(90)), T.TEXT, 10, True, tags=tags)
        k.link(x + w - T.px(16), y + T.px(24), "", lambda: self._toast_out(tag), glyph="close",
               colour=T.DIM, anchor="e", tags=tags)
        if text:
            k.text(x + T.px(22), y + T.px(54), T.fit(text, T.mono(9), w - T.px(40)), T.MUTED, 9, tags=tags)
        bx = x + T.px(22)
        for label, cmd, primary in actions:
            bw = T.width(label, T.mono(10, True)) + T.px(32)
            k.button(bx, y + h - T.px(44), bw, label,
                     (lambda cmd=cmd: (self._toast_out(tag), cmd())),
                     kind="primary" if primary else "secondary", accent=accent, h=T.px(32),
                     tags=tags, size=10)
            bx += bw + T.px(10)
        k.push(Layer(tag, lambda animate: self._toast_close(tag, animate)))
        c.move(tag, w + T.px(60), 0)
        moved = [0]

        def step(kk):
            want = int((w + T.px(60)) * kk)
            c.move(tag, -(want - moved[0]), 0)
            moved[0] = want
        self.motion.run(tag, 320, step)
        if getattr(self, "_toast_job", None):
            self.root.after_cancel(self._toast_job)
            self._toast_job = None
        if timeout:
            self._toast_job = self.root.after(timeout, lambda: self._toast_out(tag))

    def _toast_out(self, tag: str | None = None) -> None:
        """Send this toast (or any) away - its own layer only: popping "it and
        everything above it" closed a menu opened after the toast came up."""
        for layer in list(self.kit.layers):
            if layer.tag.startswith("toast") and (tag is None or layer.tag == tag):
                self.kit.remove(layer)

    def _toast_close(self, tag, animate) -> None:
        c = self.content
        if not animate:
            self.motion.stop(tag)
            c.delete(tag)
            return
        moved = [0]

        def step(k):
            want = int(T.px(460) * k)
            c.move(tag, want - moved[0], 0)
            moved[0] = want
        self.motion.run(tag, 240, step, done=lambda: c.delete(tag))

    # ============================================================ dialogs
    def ask(self, title: str, text: str, ok: str = "ok", cancel: str | None = "cancel",
            danger: bool = False, accent: str = T.AMBER) -> bool:
        """A modal question inside the window's look. Blocks until answered."""
        return bool(Dialog(self, title, text, ok, cancel, danger, accent).run())

    def info(self, title: str, text: str) -> None:
        Dialog(self, title, text, "ok", None, False, T.AMBER).run()

    def error(self, title: str, text: str) -> None:
        Dialog(self, title, text, "ok", None, False, T.WARN, glyph="warn").run()

    def ask_text(self, title: str, prompt: str, initial: str = "") -> str | None:
        return Dialog(self, title, prompt, "save", "cancel", False, T.AMBER, entry=initial).run()


class Dialog:
    """A dark card over a dimmed window. Enter = ok, Esc or a click outside = cancel."""

    def __init__(self, shell, title, text, ok, cancel, danger, accent, glyph=None, entry=None):
        self.shell, self.root = shell, shell.root
        self.title, self.text, self.ok, self.cancel = title, text, ok, cancel
        self.danger, self.accent, self.glyph, self.entry = danger, accent, glyph, entry
        self.result = None

    def run(self):
        root = self.root
        # A question asked while the window is minimised or in the tray would
        # be placed on the window's own corner, which Windows reports as
        # -32000: an invisible modal, in front of everything, holding the
        # clicks. The window comes back first, and if it will not, the
        # question goes in the middle of the screen.
        if not root.winfo_viewable():
            try:
                root.deiconify()
                root.lift()
            except tk.TclError:
                pass
        root.update_idletasks()
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        rw, rh = root.winfo_width(), root.winfo_height()
        if rx < -1000 or ry < -1000 or rw < 100 or rh < 100:
            rw, rh = min(T.px(900), root.winfo_screenwidth()), min(T.px(600), root.winfo_screenheight())
            rx = (root.winfo_screenwidth() - rw) // 2
            ry = (root.winfo_screenheight() - rh) // 2
        scrim = tk.Toplevel(root)
        scrim.overrideredirect(True)
        scrim.configure(bg="#000000")
        scrim.geometry(f"{rw}x{rh}+{rx}+{ry}")
        # the dim and the card belong to the main window and stay over it: a
        # bare overrideredirect window went behind the moment anything else
        # was clicked, and the grab then swallowed every click (#263)
        try:
            scrim.transient(root)          # owned by the window: always over it,
        except tk.TclError:                # and away with it when it is minimised
            pass
        try:
            scrim.attributes("-alpha", 0.55)
        except tk.TclError:
            pass
        w = min(T.px(560), int(rw * 0.9))
        f = T.mono(10)
        # measure the text to size the card
        probe = tk.Canvas(root)

        def tall(width):
            tid = probe.create_text(0, 0, text=self.text, font=f, width=width - T.px(64), anchor="nw")
            x1, y1, x2, y2 = probe.bbox(tid) or (0, 0, 0, 0)
            probe.delete(tid)
            return T.px(74) + (y2 - y1) + T.px(90) + (T.px(52) if self.entry is not None else 0)
        h = tall(w)
        # #302: the card was cut to 85% of the WINDOW and a long text lost its
        # end in a small one. The card is its own window: it grows wider first,
        # and the screen is the limit, not the window under it.
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        if h > int(rh * 0.85):
            w = max(w, min(T.px(860), int(sw * 0.9)))
            h = tall(w)
        probe.destroy()
        h = min(h, int(sh * 0.9))
        # a child of the dim, not of the main window: a Tk grab covers the
        # window it is set on and everything under it, so with the grab on the
        # dim a click on the dim is delivered (cancel) and the card's own
        # buttons keep working. The grab used to sit on the card, which threw
        # away every click on the dim - "a click outside = cancel" never ran.
        card = tk.Toplevel(scrim)
        card.overrideredirect(True)
        card.configure(bg=T.SURF2)
        try:
            card.transient(root)
        except tk.TclError:
            pass
        cx, cy = self._pos(rx, ry, rw, rh, w, h)
        card.geometry(f"{w}x{h}+{cx}+{cy}")
        c = tk.Canvas(card, width=w, height=h, bg=T.SURF2, highlightthickness=0)
        c.pack(fill="both", expand=True)
        k = Kit(c)
        c.create_rectangle(0, 0, w - 1, h - 1, outline=T.LINE)
        c.create_rectangle(0, 0, T.px(3), h, fill=self.accent, outline="")
        x = T.px(32)
        if self.glyph:
            k.glyph(x, T.px(38), self.glyph, self.accent, 13, anchor="w")
            x += T.px(28)
        k.text(x, T.px(38), self.title, T.TEXT, 12, True)
        c.create_text(T.px(32), T.px(66), text=self.text, font=f, fill=T.MUTED, anchor="nw",
                      width=w - T.px(64))
        field = None
        if self.entry is not None:
            field = k.field(T.px(32), h - T.px(122), w - T.px(64), "", glyph=None, value=self.entry,
                            on_enter=lambda: self._finish(field.get().strip() or None))
            card.after(50, lambda: (field.entry.focus_set(), field.entry.select_range(0, "end")))
        bx = w - T.px(32)
        by = h - T.px(62)
        ok_w = T.width(self.ok, T.mono(11, True)) + T.px(44)
        bx -= ok_w
        k.button(bx, by, ok_w, self.ok, lambda: self._finish(
            (field.get().strip() or None) if field is not None else True),
                 kind="danger" if self.danger else "primary", accent=self.accent, h=T.px(40))
        if self.cancel:
            cw = T.width(self.cancel, T.mono(11)) + T.px(44)
            k.button(bx - cw - T.px(12), by, cw, self.cancel, lambda: self._finish(
                None if field is not None else False), h=T.px(40))
        self.scrim, self.card, self.field = scrim, card, field
        # mastered on the card, not on whatever tkinter thinks the default
        # root is: with a second Tk interpreter in the process the variable
        # was created in one and waited on in the other, so the answer
        # never arrived and the question hung for ever
        self.done = tk.BooleanVar(master=card, value=False)
        shut = lambda _e=None: self._finish(None if field is not None else False)
        scrim.bind("<Button-1>", shut)
        card.bind("<Escape>", shut)
        scrim.bind("<Escape>", shut)
        if field is None:
            card.bind("<Return>", lambda _e: self._finish(True))
        # Nothing is bound on the main window. Following it and coming back to
        # the front are the shell's own handlers (Shell.__init__), which ask
        # the dialog that is open. A dialog that bound its own had to unbind
        # them again, and on the Python the release is built with (3.12)
        # unbind(sequence, funcid) throws away EVERY binding for that
        # sequence - one answered question and the window's own Escape key
        # was gone for the rest of the session.
        card.lift()
        card.focus_force()
        # Drawn NOW. Almost every question is asked from inside a button press,
        # and a window made during one was mapped but never painted: people saw
        # the screen go dim with no card on it, nothing answered (the grab), and
        # only dragging the window brought it up - the move repainted it.
        for wdg in (scrim, card):
            try:
                wdg.deiconify()
                wdg.update_idletasks()
            except tk.TclError:
                pass
        try:
            scrim.grab_set()
        except tk.TclError:
            pass
        # and once the press that opened it has finished, in front and holding
        # the keyboard, whatever the press did to the focus
        self._first = root.after(1, self._front)
        # a question asked while another is open (a worker's failure landing
        # in the pump) goes on top of it, and the one underneath comes back
        under, self.shell.dialog = self.shell.dialog, self    # what a test answers, and what is open
        try:
            card.wait_variable(self.done)
        finally:
            self.shell.dialog = under
            if under is not None and not under.done.get():
                under._front(grab=True)
        return self.result

    def _front(self, grab: bool = False):
        """Back in front of the window, with the keyboard."""
        if self.done.get():
            return
        try:
            self.scrim.lift()
            self.card.lift()
            if grab:
                self.scrim.grab_set()
            # the keyboard goes to the field when there is one, and a caret
            # already inside the card is left where the person put it
            here = self.card.focus_get()
            if self.field is not None and here is not self.field.entry:
                self.field.entry.focus_force()
            elif here is None:
                self.card.focus_force()
        except tk.TclError:
            pass

    def _place(self):
        if self.done.get():
            return
        try:
            r = self.root
            rx, ry, rw, rh = r.winfo_rootx(), r.winfo_rooty(), r.winfo_width(), r.winfo_height()
            if rx < -1000 or ry < -1000:
                return                        # the window is away; leave it where it is
            self.scrim.geometry(f"{rw}x{rh}+{rx}+{ry}")
            cw, ch = self.card.winfo_width(), self.card.winfo_height()
            cx, cy = self._pos(rx, ry, rw, rh, cw, ch)
            self.card.geometry(f"+{cx}+{cy}")
        except tk.TclError:
            pass

    def _pos(self, rx, ry, rw, rh, w, h):
        """The card centred on the window and kept on the desktop - the whole
        of it, the window may be on a second monitor left of or above the
        first. One place: the first placement clamped a card taller than its
        window (#302) and the next <Configure> centred it back off the top."""
        r = self.root
        vx, vy = r.winfo_vrootx(), r.winfo_vrooty()
        vw = max(r.winfo_screenwidth(), r.winfo_vrootwidth())
        vh = max(r.winfo_screenheight(), r.winfo_vrootheight())
        return (max(vx, min(rx + (rw - w) // 2, vx + vw - w)),
                max(vy, min(ry + (rh - h) // 2, vy + vh - h)))

    def _finish(self, value):
        if self.done.get():
            return
        self.result = value
        # the timer that puts it in front does not outlive it
        first = getattr(self, "_first", None)
        if first:
            try:
                self.root.after_cancel(first)
            except (tk.TclError, ValueError):
                pass
            self._first = None
        for wdg in (self.card, self.scrim):
            try:
                wdg.grab_release()
                wdg.destroy()
            except tk.TclError:
                pass
        self.done.set(True)
