r"""Drive the real 2.0 window the way a person does, and see that it still works.

    python _tools\walkthrough.py                  the walk
    python _tools\walkthrough.py --break NAME     the walk with one thing broken
    python _tools\walkthrough.py --list-breaks    what can be broken on purpose
    python _tools\walkthrough.py --show           the window on screen while it walks
                                                  (off screen by default: the real
                                                  pointer over it moves things)

Four green suites do not prove the application opens, or that a menu closes
when you click beside it. This builds the actual window (core.ui.app.App) on
a temporary library and walks it with real events - clicks, keys, the wheel,
a drag - never by calling a handler: Tk runs its class and tag bindings in
front of the handler, and that is where the 1.8.1 wheel bug lived.

What it walks:
  library   opens on the saved games; search as you type, Esc clears it,
            typing anywhere on the page searches; filter tabs; the view menu
            (sort, hidden games) and its closing; hide and show again by
            right-click; a card click opens the game
  game      settings open and close by click, second click, Esc and 'close';
            every route draws its settings with no two controls on top of
            each other and none past the edge; the proxy control the
            diagnosis names ('loads as' on optiscaler, 'reshade loads as' on
            the ReShade routes, none on remix) is on that route; 'swap the
            Remix runtime' reaches the install options and another route
            drops it; dropdown menus close on an outside click, Esc and a
            second click; the wheel over an open menu scrolls the menu, never
            the page, and never changes a value; Esc goes back
  shell     the log drawer opens, drags taller, remembers the size, closes;
            the help menu; the update and crash banners each have their own
            button (the 1.9 crash banner rebound the update one); 'report a
            bug' with no game picked; toasts close on Esc; the watcher menu
  pages     the video page with and without a player, the remix page, and
            that their buttons are there and do not overlap

Everything it touches is a temporary folder: no download, no browser, no
process started, no setting on this machine changed (see ui_sandbox.py).

Each check prints PASS or FAIL; the exit code is 1 on any FAIL. `--break`
applies one deliberate fault first - every check in this file was made to
fail once that way, and the list says which fault proves which check.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_sandbox import Sandbox, overlaps   # noqa: E402

FAILS: list[str] = []


def ok(what, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + what + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(what)
    return bool(cond)


# ---------------------------------------------------------------- faults on purpose
def _breaks():
    from core.ui import app as A, ctl_game, kit, setpanel, shell
    from core.ui import theme as T

    def menu_wheel():
        kit.Menu._wheel_if_inside = lambda self, e: False

    def second_click():
        real = kit.Kit.on_click

        def on_click(self, tag, cmd, nav=False):
            real(self, tag, cmd, nav)

            def click(_e, self=self, cmd=cmd, tag=tag):
                self._handled = True
                top = self.top()
                if top is not None and self._layer_of(tag) is not top:
                    self.pop()
                    if not nav and top.opener != tag:
                        return
                    if top.opener == tag:
                        pass          # the bug: the opener opens it again
                cmd()
            self.c.tag_bind(tag, "<Button-1>", click)
        kit.Kit.on_click = on_click

    def remix_swap():
        real = ctl_game.GameControl.opts

        def opts(self, route=None):
            o = real(self, route)
            o.remix_swap = False
            return o
        ctl_game.GameControl.opts = opts

    def crash_banner():
        def offer(self):
            if self._crash_shown:
                return
            self._crash_shown = True
            self.shell.banner("update", "something went wrong - the details are in the log file",
                              actions=[("report it", lambda: self.report_bug("crash"), True)], colour=T.WARN)
        A.App.offer_crash_report = offer

    def proxy_hidden():
        real = ctl_game.GameControl.shown_setting
        ctl_game.GameControl.shown_setting = lambda self, key: False if key == "reshade_proxy" else real(self, key)

    def overlap():
        real = setpanel.SettingsSection._control
        setpanel.SettingsSection._control = lambda self, *a: (real(self, *a), T.px(12))[1]

    def report_unguarded():
        real = A.App.report_bug

        def report_bug(self, kind="bug"):
            _ = self.game.install_dir if self.shell.page.name == "game" else self.game.name
            return real(self, kind)
        A.App.report_bug = report_bug

    def escape():
        shell.Shell._escape = lambda self, e: None

    def log_share():
        shell.Shell._set_share = lambda self, v, save: setattr(self, "log_share", v)

    def search():
        from core.ui import libpage
        libpage.LibraryPage._typed = lambda self, text: None

    return {
        "menu-wheel": (menu_wheel, "the wheel over an open menu scrolls the page"),
        "second-click": (second_click, "a second click on a dropdown opens its menu again"),
        "remix-swap": (remix_swap, "the Remix swap toggle never reaches the install options"),
        "crash-banner": (crash_banner, "the crash banner replaces the update banner's button"),
        "proxy-hidden": (proxy_hidden, "'reshade loads as' is not drawn on the ReShade routes"),
        "overlap": (overlap, "settings rows drawn 12 px apart"),
        "report-unguarded": (report_unguarded, "report a bug reads the picked game outside a guard"),
        "escape": (escape, "Esc does nothing"),
        "log-share": (log_share, "the dragged log height is not saved"),
        "search": (search, "typing in the search box filters nothing"),
    }


# ---------------------------------------------------------------- the walk
def main() -> int:
    args = sys.argv[1:]
    sb = Sandbox("walk_")
    breaks = _breaks()
    if "--list-breaks" in args:
        for name, (_fn, what) in breaks.items():
            print(f"  {name:<18} {what}")
        return 0
    if "--break" in args:
        name = args[args.index("--break") + 1]
        breaks[name][0]()
        print(f"!! walking with a deliberate fault: {breaks[name][1]}\n")

    root = None
    try:
        from core import dlss, library, prefs, reportui, update, video
        from core.ui.ctl_library import LibraryControl

        # A library of four, saved the way a scan saves it, so the window opens
        # on it like it does for somebody who has scanned before.
        main_game = sb.game("Walkthrough Game", "DX11", 64, installed=True,
                            extra=("nvngx_dlss.dll", "nvngx_dlssd.dll"))
        gta = sb.game("Grand Theft Auto IV", "DX9", 32, source="Steam")
        other = sb.game("Another Game", "DX12", 64)
        zeta = sb.game("Zeta Racer", "DX11", 64)
        gs = [main_game, gta, other, zeta]
        rows = {(str(g.folder), str(g.exe)): LibraryControl.inspect_row(g, None) for g in gs}
        sm = None
        try:
            from core import gpu
            sm = gpu.detect()[1]
        except Exception:
            pass
        library.save(gs, rows, update.VERSION, sm)

        ask = Mock(return_value={"started": "it closed itself", "happened": "walkthrough"})
        sb._start(patch.object(reportui, "ask", ask))

        app = sb.app(scale=1.0, size=(1400, 900), visible="--show" in args)
        root, shell = app.root, app.shell
        c, k = shell.content, shell.kit
        page = shell.pages["library"]
        sb.pump(root, 0.8)

        def cards():
            return len([t for t in ("card%d" % i for i in range(20)) if c.find_withtag(t)])

        def names_shown():
            return [cd["g"].name for cd in page.cards]

        def settle_cards():
            sb.until(root, lambda: all(app.card(g)["kind"] != "reading" for g in app.all_games if g.exe), 20)
            sb.pump(root, 0.4)

        def menu_open():
            top = k.top()
            return top is not None and top.tag.startswith("menu")

        def pick(i):
            """Click row i of the open menu; nothing when no menu is open."""
            top = k.top()
            if top is None or not top.tag.startswith("menu"):
                return False
            sb.reveal(shell, f"{top.tag}r{i}")
            return sb.click(c, f"{top.tag}r{i}")

        def the_menu():
            from core.ui import kit as _kit
            return next(_menu_objects(_kit, k), None)

        def type_into(widget, text):
            widget.focus_force()
            root.update()
            for ch in text:
                widget.event_generate("<KeyPress>", keysym=ch, when="now")
                widget.event_generate("<KeyRelease>", keysym=ch, when="now")
            sb.pump(root, 0.4)

        def tab(label):
            """A filter tab by its words - on an item tagged as a tab, since a
            card's status can say 'installed' too."""
            for i in c.find_withtag("grid"):
                if c.type(i) == "text" and c.itemcget(i, "text") == label and any(
                        x.startswith("flt") for x in c.gettags(i)):
                    return i
            return None

        def outside_click():
            sb.click_xy(c, 6, c.winfo_height() - 6)

        # ============================================================ library
        print("library")
        ok("the window opens on the library", shell.page is not None and shell.page.name == "library")
        settle_cards()
        ok("...with the saved games on it", cards() == 4, cards())
        ok("...read from the last scan, not scanning again", not app.scanning and "last scan" in app.scan_note,
           app.scan_note)
        ok("...and the rail shows the logo", c.winfo_toplevel() is root and bool(shell.rail_c.find_withtag("logo")))

        type_into(page.field.entry, "zeta")
        ok("typing in search filters the cards as you type", names_shown() == ["Zeta Racer"], names_shown())
        sb.key(page.field.entry, "Escape", settle=0.4)
        ok("...Esc in the box clears it and every game is back", cards() == 4 and app.query == "", (cards(), app.query))
        c.focus_force()
        root.update()
        for ch in "gran":
            # the first key moves the focus into the box; Tk hands the rest to it
            c.event_generate("<KeyPress>", keysym=ch, when="now")
            c.event_generate("<KeyRelease>", keysym=ch, when="now")
            root.update()
        sb.pump(root, 0.4)
        ok("typing anywhere on the page goes into the search box", names_shown() == ["Grand Theft Auto IV"],
           (app.query, names_shown()))
        page.field.reset()
        app.query = ""
        app.refresh("library")
        sb.pump(root, 0.3)

        installed_tab = tab("installed")
        ok("the 'installed' filter tab is drawn", installed_tab is not None)
        if installed_tab is not None:
            sb.click(c, installed_tab)
            ok("...a click shows the installed games only", names_shown() == ["Walkthrough Game"], names_shown())
            sb.click(c, tab("all"))
            ok("...and 'all' brings the rest back", cards() == 4, cards())

        view = k.find("view", "link")
        ok("the view menu is there", view is not None)
        if view:
            sb.click(c, view)
            ok("...a click opens it", menu_open())
            sb.click(c, view)
            ok("...a second click closes it", not menu_open())
            sb.click(c, view)
            sb.key(c, "Escape")
            ok("...Esc closes it", not menu_open())
            sb.click(c, view)
            outside_click()
            ok("...a click beside it closes it", not menu_open())
            # a click on the canvas does not take the focus out of the search box,
            # so this is the Esc somebody presses right after searching
            page.field.entry.focus_force()
            root.update()
            sb.click(c, view)
            page.field.entry.event_generate("<Escape>", when="now")
            sb.pump(root, 0.25)
            ok("...Esc closes it even while the (empty) search box has the focus", not menu_open())
            if menu_open():
                sb.key(c, "Escape")
            sb.click(c, view)
            pick(1)                                           # sort: name
            want = sorted(g.name for g in gs)
            ok("sort by name from the view menu orders the cards", names_shown() == want, names_shown())
            ok("...and is remembered", prefs.get("games_sort") == ["name", False], prefs.get("games_sort"))

        idx = names_shown().index("Zeta Racer")
        sb.click(c, f"card{idx}", button=3)
        ok("right-click on a card opens its menu", menu_open())
        if menu_open():
            pick(1)                                           # hide from the list
            ok("...'hide from the list' takes the card away", "Zeta Racer" not in names_shown() and cards() == 3,
               names_shown())
            ok("...and is remembered", str(zeta.folder) in (prefs.get("hidden_games") or []))
            sb.click(c, k.find("view", "link"))
            top = the_menu()
            hid_row = next((i for i, it in enumerate(top.items) if it and it[0].startswith("show hidden")), None)
            ok("the view menu offers the hidden games", hid_row is not None)
            if hid_row is not None:
                pick(hid_row)
                ok("...and shows them again", "Zeta Racer" in names_shown(), names_shown())
                idx = names_shown().index("Zeta Racer")
                sb.click(c, f"card{idx}", button=3)
                pick(1)                                       # show in the list again
                ok("...'show in the list again' un-hides it", str(zeta.folder) not in (prefs.get("hidden_games") or []))
                app.show_hidden = False
                app.refresh("library")
                sb.pump(root, 0.2)

        # nothing has been opened yet: this is 'report a bug' with no game picked
        sb.click(shell.rail_c, "nav_help")
        ok("help opens a menu", menu_open())
        labels = [it[0] for it in (the_menu().items if menu_open() else []) if it]
        ok("...with 'report a bug' and 'open the log file' in it",
           "report a bug" in labels and "open the log file" in labels, labels)
        sb.click(shell.rail_c, "nav_help")
        ok("...a second click closes it", not menu_open())
        errs = len(sb.errors)
        sb.click(shell.rail_c, "nav_help")
        row = labels.index("report a bug") if "report a bug" in labels else 1
        pick(row)
        ok("'report a bug' with no game picked raises nothing", len(sb.errors) == errs, sb.errors[errs:][:1])
        ok("...asks its questions and opens a new issue",
           ask.call_count == 1 and bool(sb.opened) and "/issues/new" in sb.opened[-1], sb.opened[-1:])

        idx = names_shown().index("Walkthrough Game")
        sb.click(c, f"card{idx}")
        ok("a click on a card opens the game", shell.page.name == "game" and getattr(app.game, "name", "") == main_game.name,
           getattr(shell.page, "name", None))
        sb.until(root, lambda: not app.entering and app.support is not None, 20)
        sb.pump(root, 0.4)

        # ============================================================ game page
        print("game page")
        gp = app.game_page
        settings = k.find("settings", "button")
        ok("the settings button is there once the game is read", settings is not None)
        sb.click(c, settings)
        ok("...a click opens the settings", gp.settings_open and k.find("route", "dropdown") is not None)
        sb.click(c, k.find("settings", "button"))
        ok("...a second click closes them", not gp.settings_open)
        sb.click(c, k.find("settings", "button"))
        sb.key(c, "Escape")
        ok("...Esc closes them", not gp.settings_open and shell.page.name == "game")
        sb.click(c, k.find("settings", "button"))
        sb.reveal(shell, k.find("close", "link"))
        sb.click(c, k.find("close", "link"))
        ok("...and so does their 'close'", not gp.settings_open)
        sb.click(c, k.find("settings", "button"))

        # the route dropdown, with real clicks
        rd = k.find("route", "dropdown")
        sb.reveal(shell, rd)
        sb.click(c, rd)
        ok("the route dropdown opens its menu", menu_open())
        sb.click(c, rd)
        ok("...a second click closes it", not menu_open())
        sb.click(c, rd)
        sb.key(c, "Escape")
        ok("...Esc closes it and leaves the settings open", not menu_open() and gp.settings_open)
        sb.click(c, rd)
        outside_click()
        ok("...a click beside it closes it", not menu_open())
        offered = list(app.support.options if app.support else [])
        was = app.route
        target = next((i for i, o in enumerate(offered) if o != was), None)
        if target is not None:
            sb.click(c, rd)
            pick(target)
            ok("...picking another route in it switches the route", app.route == offered[target], (was, app.route))
            sb.click(c, k.find("route", "dropdown"))
            inst = k.find(app.game.installed and "install again" or "install", "button")
            before = app.busy
            if inst:
                sb.click(c, inst)
            ok("...with the menu open, a click on a button only closes the menu",
               not menu_open() and app.busy == before and app.action == "")

        # every route, drawn
        for route in (dlss.FEEDER, dlss.OPTI, dlss.NATIVE, dlss.BRIDGE, dlss.RENODX,
                      dlss.UPSTREAM, dlss.STANDALONE, dlss.REMIX):
            errs = len(sb.errors)
            try:
                app.set_setting("route", route)
                shell.redraw()
                sb.pump(root, 0.15)
                drawn = gp.settings_open and bool(k.controls("dropdown"))
                ok(f"the {route} route draws its settings", drawn and len(sb.errors) == errs,
                   sb.errors[errs:][:1])
            except Exception as e:
                ok(f"the {route} route draws its settings", False, f"{type(e).__name__}: {e}")
                continue
            items = [x for x in k.controls() if not x[0].startswith("menu")]
            bad = overlaps(c, items)
            ok(f"...no two controls on top of each other on {route}", not bad, bad[:3])
            width = c.winfo_width()
            past = [lab for tag, kind, lab in items if (c.bbox(tag) or (0, 0, 0, 0))[2] > width + 1]
            ok(f"...none past the right edge on {route}", not past, past[:3])
            labels = {lab for _t, kind, lab in k.controls("dropdown")}
            if route == dlss.OPTI:
                ok("...'loads as' is on the optiscaler route, as the diagnosis says",
                   "loads as" in labels and "reshade loads as" not in labels, sorted(labels))
            elif route == dlss.REMIX:
                ok("...remix shows no proxy control, and the diagnosis names none",
                   "loads as" not in labels and "reshade loads as" not in labels, sorted(labels))
            else:
                ok(f"...'reshade loads as' is on {route}, as the diagnosis says",
                   "reshade loads as" in labels and "loads as" not in labels, sorted(labels))

        app.set_setting("route", dlss.REMIX)
        shell.redraw()
        sb.pump(root, 0.15)
        swap = k.find("swap the Remix runtime", "toggle")
        ok("the remix route shows 'swap the Remix runtime'", swap is not None)
        if swap:
            sb.reveal(shell, swap)
            sb.click(c, swap)
            ok("...a click reaches the install options", app.opts().remix_swap is True)
        app.set_setting("route", dlss.FEEDER)
        shell.redraw()
        sb.pump(root, 0.15)
        ok("...another route hides it and drops the choice",
           k.find("swap the Remix runtime", "toggle") is None and app.opts().remix_swap is False)

        # the wheel over an open menu that is longer than it shows
        key_dd = k.find("overlay key", "dropdown")
        ok("the overlay key dropdown is there on feeder", key_dd is not None)
        if key_dd:
            sb.reveal(shell, key_dd)
            value = app.overlay_key()
            sb.click(c, key_dd)
            menu = the_menu()
            long_menu = menu is not None and len(menu.items) > menu.rows
            ok("...its menu is longer than it shows", long_menu)
            if long_menu:
                page_y = c.canvasy(0)
                mx, my = int(menu.x + menu.w / 2 - c.canvasx(0)), int(menu.y + menu.h / 2 - c.canvasy(0))
                off = menu.offset
                sb.wheel(c, mx, my, -120)
                sb.wheel(c, mx, my, -120)
                ok("the wheel over an open menu scrolls the menu", menu.offset > off, (off, menu.offset))
                ok("...not the page", c.canvasy(0) == page_y, (page_y, c.canvasy(0)))
                ok("...and changes no value", app.overlay_key() == value and menu_open(), (value, app.overlay_key()))
                sb.key(c, "Escape")
            shell.scroll_to(0)
            sb.pump(root, 0.1)
            y0 = c.canvasy(0)
            sb.wheel(c, 300, 300, -120)
            ok("with nothing open, the wheel scrolls the page", c.canvasy(0) > y0, (y0, c.canvasy(0)))
            ok("...and still changes no value", app.overlay_key() == value)

        # ---------------------------------------------------- what the buttons do
        print("game page: did it work?")
        d = Path(app.game.install_dir)
        (d / "renodx-dlss5.addon64").write_bytes(b"MZ")
        (d / "nvngx_dlssnr.dll").write_bytes(b"MZ")
        shaders = d / "reshade-shaders" / "Shaders"
        shaders.mkdir(parents=True, exist_ok=True)
        (shaders / "DLSS5_Feed.fx").write_text("// t", encoding="utf8")
        (d / "ReShade.log").write_text(
            'INFO | Initializing crosire\'s ReShade\nRegistered add-on "DLSS 5 Feed" v0.1\n', encoding="utf8")
        (d / "dlss5-feed.log").write_text(
            "[feed] effects: DLSS5_Feed.fx technique found, ColorInput found\n"
            "[feed] NVSDK_NGX_D3D12_Init -> 0x00000001 (Success)\n"
            "[feed] feature ready: 1920x1080 DLAA\n"
            "[feed] frame 1 delivered (1920x1080 at 100%)\n"
            "[feed] 3600 frames: feed CPU 2.10 ms/frame, GPU 4.80 ms/frame, 47.0 fps\n", encoding="utf8")
        app.set_setting("route", dlss.FEEDER)
        app.game_page.settings_open = True
        shell.redraw()
        sb.pump(root, 0.2)
        aim = k.find("aim for", "dropdown")
        ok("'aim for' is in the feeder settings", aim is not None)
        if aim:
            sb.reveal(shell, aim)
            sb.click(c, aim)
            menu = the_menu()
            row = next((i for i, it in enumerate(menu.items if menu else []) if it and it[0] == "60 fps"), None)
            if row is not None:
                pick(row)
            ok("...picking '60 fps' in it sets the target", app.target() == 60, app.target_fps)
        shell.scroll_to(0)
        sb.pump(root, 0.1)
        sb.click(c, k.find("did it work?", "button"))
        sb.until(root, lambda: not app.busy and app.result is not None, 30)
        sb.pump(root, 0.5)
        verdict = (app.result or {}).get("title", "")
        ok("'did it work?' reads the logs and says Working for a healthy session", verdict == "Working.", verdict)
        ok("...the result offers 'share the result'", k.find("share the result", "button") is not None)
        tune = next((lab for _t, _k, lab in k.controls("button") if lab.startswith("set the work area to")), None)
        ok("...and, aiming for 60 fps, a work area to set", tune is not None and app._tune is not None,
           [lab for _t, _k, lab in k.controls("button")])
        if tune:
            want = app._tune.resolution
            tag = k.find(tune, "button")
            sb.reveal(shell, tag)
            sb.click(c, tag)
            cfg = d / "dlss5-feed.cfg"
            text = cfg.read_text(encoding="utf8") if cfg.is_file() else ""
            ok("...a click writes it into dlss5-feed.cfg", f"work_resolution={want}" in text, text[:80])
        errs = len(sb.errors)
        app.q.put(("community", (app.game.install_dir, None, ["12 people have reported this game."],
                                 [("feeder", 9, 12), ("optiscaler", 2, 5)])))
        app.q.put(("wincrash", (app.game.install_dir, None,
                                ("Windows recorded Game.exe faulting in dxgi.dll (0xC0000005).", "detail"))))
        sb.pump(root, 0.5)
        ok("the community and Windows-crash answers are handled", len(sb.errors) == errs, sb.errors[errs:][:1])
        ok("...and what worked for others is drawn on the page",
           sb.text_tag(c, "what worked for others") is not None)

        shell.scroll_to(0)
        sb.pump(root, 0.1)
        app.game_page.settings_open = False
        shell.redraw()
        sb.key(c, "Escape")
        ok("Esc on a game page goes back to the library", shell.page.name == "library")

        # ============================================================ shell
        print("shell")
        bk = shell.bottom_kit
        loglink = bk.find("log", "link")
        ok("the log link is on the bottom line", loglink is not None)
        if loglink:
            sb.click(shell.bottom_c, loglink, settle=0.5)
            ok("...a click opens the log drawer", shell.log_open and shell.drawer.winfo_height() > 50,
               shell.drawer.winfo_height())
            head = shell.log_head
            h0 = shell.drawer.winfo_height()
            ry = head.winfo_rooty() + 5
            head.event_generate("<ButtonPress-1>", x=40, y=5, rootx=head.winfo_rootx() + 40, rooty=ry, when="now")
            head.event_generate("<Motion>", x=40, y=-115, rootx=head.winfo_rootx() + 40, rooty=ry - 120,
                                state=0x100, when="now")
            sb.pump(root, 0.1)          # a hand takes longer than one event batch
            head.event_generate("<ButtonRelease-1>", x=40, y=-115, rootx=head.winfo_rootx() + 40, rooty=ry - 120,
                                when="now")
            sb.pump(root, 0.3)
            h1 = shell.drawer.winfo_height()
            ok("...dragging its top edge makes it taller", h1 > h0 + 60, (h0, h1))
            share = prefs.get("log_share")
            ok("...and the size is remembered", isinstance(share, (int, float)) and 0.1 <= share <= 0.9
               and abs(share - h1 / root.winfo_height()) < 0.05, share)
            sb.key(c, "Escape", settle=0.5)
            ok("...Esc closes it", not shell.log_open and not shell.drawer.winfo_ismapped())
            sb.click(shell.bottom_c, bk.find("log", "link"), settle=0.5)
            sb.click(shell.bottom_c, bk.find("log", "link"), settle=0.5)
            ok("...and the link closes it on a second click", not shell.log_open)

        app.q.put(("update", ("9.9.9", "https://example.invalid/release")))
        sb.pump(root, 0.3)
        app.offer_crash_report()
        sb.pump(root, 0.3)
        bn = shell.banner_kit
        upd, rep = bn.find("update now", "button"), bn.find("report it", "button")
        ok("the update and crash banners are both up, each with its own button",
           upd is not None and rep is not None and upd != rep and len(shell.banners) == 2, bn.controls("button"))
        if upd and rep:
            asked, opened = ask.call_count, len(sb.opened)
            sb.click(shell.banner_c, upd)
            ok("...'update now' opens the release, not the bug report",
               len(sb.opened) == opened + 1 and sb.opened[-1] == "https://example.invalid/release"
               and ask.call_count == asked, sb.opened[-1:])
            sb.click(shell.banner_c, bn.find("report it", "button"))
            ok("...'report it' opens the bug report, not the release",
               ask.call_count == asked + 1 and "/issues/new" in sb.opened[-1], sb.opened[-1:])

        shell.toast("Walkthrough Game closed", "working", actions=[("open", lambda: None, True)])
        sb.pump(root, 0.5)
        ok("a toast comes up", bool(c.find_withtag("toast")))
        sb.key(c, "Escape", settle=0.6)
        ok("...and Esc sends it away", not c.find_withtag("toast"))

        ok("the rail says the installed game is watched", (shell.watching or (0, ""))[1] == "watching 1",
           shell.watching)
        sb.click(shell.rail_c, "nav_watch")
        ok("the watcher opens its menu", menu_open())
        sb.click(shell.rail_c, "nav_watch")
        ok("...a second click on it closes the menu", not menu_open())
        if not menu_open():
            sb.click(shell.rail_c, "nav_watch")
        pick(0)
        ok("...its first row switches watching off", prefs.get("watch_games") is False
           and (shell.watching or (1, ""))[1] == "watch off", shell.watching)

        # ============================================================ video and remix
        print("video and remix")
        sb.click(shell.rail_c, "nav_video")
        ok("the rail opens the video page", shell.page.name == "video")
        ok("...with no player: 'set up the player'", k.find("set up the player", "button") is not None)
        player = sb.game("Video player", "DX11", 64, installed=True)
        player.kind = "video"
        with patch.object(video, "known", return_value=player), \
                patch.object(video, "list_cameras", return_value=["Walk Cam"]), \
                patch.object(video, "list_screens", return_value=["Screen 1"]):
            app.cameras = None
            shell.show("video", remember=False)
            sb.until(root, lambda: app.cameras is not None, 10)
            sb.pump(root, 0.3)
            want = ("open the player", "neural rendering on/off (F6)", "settings", "play", "download, then play",
                    "pick a video and render it", "start", "stop")
            have = {lab for _t, _k, lab in k.controls("button")}
            ok("...with a player: every button is there", all(w in have for w in want),
               sorted(set(want) - have))
            ok("...and the size, style, webcam and screen dropdowns",
               {"size", "style", "webcam", "screen"} <= {lab for _t, _k, lab in k.controls("dropdown")})
            bad = overlaps(c, k.controls())
            ok("...none of them on top of another", not bad, bad[:3])
        sb.click(shell.rail_c, "nav_remix")
        sb.pump(root, 0.5)
        ok("the rail opens the remix page", shell.page.name == "remix")
        ok("...it offers to fetch the mod for the game in the library",
           k.find("download & install", "button") is not None)
        # every other project is a tile that opens its page; a click on one really opens it
        from core import remixlist as _rl
        tiles = [t for t, _k, lab in k.controls("link") if lab in {m.game for m in _rl.MODS + _rl.BUILT_IN}]
        ok("...and every mod has a tile that opens its page", len(tiles) > 3, len(tiles))
        if tiles:
            opened = len(sb.opened)
            sb.reveal(shell, tiles[0])
            sb.click(c, tiles[0])
            ok("...and clicking a tile opens that page", len(sb.opened) == opened + 1, sb.opened[-1:])
        bad = overlaps(c, k.controls())
        ok("...nothing on top of anything", not bad, bad[:3])
        sb.click(shell.rail_c, "nav_library")
        ok("the rail's games goes home", shell.page.name == "library")

        # ============================================================ rescans
        print("rescan and full rescan")
        from core import games as _games
        new = sb.game("A New Game", "DX12", 64, source="Steam")
        full, quick, inspected = [], [], []
        real_inspect = LibraryControl.inspect_row

        def inspect(g, sm_):
            inspected.append(g.name)
            return real_inspect(g, sm_)

        def quick_scan(known, progress=None):
            quick.append(1)
            return list(known) + [new], [new]

        with patch.object(_games, "quick_scan", quick_scan), \
                patch.object(_games, "scan_all", lambda progress=None: full.append(1) or list(gs)), \
                patch.object(LibraryControl, "inspect_row", staticmethod(inspect)):
            scan_btn = k.find("scan", "button")
            sb.click(c, scan_btn)
            menu = the_menu()
            labels = [it[0] for it in (menu.items if menu else []) if it]
            ok("the scan button opens rescan, full rescan and choose a folder",
               any(x.startswith("rescan") for x in labels) and any(x.startswith("full rescan") for x in labels)
               and any(x.startswith("choose a folder") for x in labels), labels)
            pick(next((i for i, x in enumerate(labels) if x.startswith("rescan")), 0))
            sb.until(root, lambda: quick and not app.scanning, 20)
            sb.pump(root, 0.3)
            names = [g.name for g in app.all_games]
            ok("'rescan' adds the new game without the full walk", "A New Game" in names and not full, (names, full))
            ok("...and inspects the new game, not the ones it already knew",
               "A New Game" in inspected and "Another Game" not in inspected, inspected)
            sb.click(c, k.find("scan", "button"))
            pick(next((i for i, x in enumerate(labels) if x.startswith("full rescan")), 1))
            sb.until(root, lambda: full and not app.scanning, 20)
            ok("'full rescan' walks everything", full == [1], full)

        # the video player alone is not a library, and is never saved as one
        player_only = sb.game("Video player", "DX11", 64, installed=True)
        player_only.kind = "video"
        held = app.all_games
        library.FILE.unlink(missing_ok=True)
        app.all_games = [player_only]
        app.remember_library()
        ok("the video player alone is never saved as the library", not library.FILE.is_file())
        app.all_games = held


    except Exception as e:
        # a walk that stops half way is a failure with a name, not a traceback
        import traceback
        where = traceback.extract_tb(e.__traceback__)[-1]
        ok("the walk reached its end", False, f"{type(e).__name__}: {e} (line {where.lineno})")
    if root is not None:
        sb.destroy(root)
    # ============================================================ after an update
    # The saved library is from another version and cannot be used: the window
    # still opens on the library, and reads it again by itself.
    try:
        import json as _json
        from core import games as _games, library as _library
        _library.FILE.write_text(_json.dumps({"schema": _library.SCHEMA, "app_version": "0.0.0", "sm": None,
                                              "games": [], "rows": {}}), encoding="utf8")
        scans = []
        with patch.object(_games, "scan_all", lambda progress=None: scans.append(1) or []), \
                patch.object(_games, "quick_scan", lambda known, progress=None: (scans.append(1) or [], [])):
            app2 = sb.app(scale=1.0, size=(1400, 900), visible="--show" in args)
            sb.until(app2.root, lambda: scans and not app2.scanning, 20)
            ok("after an update it opens on the library", app2.shell.page is not None
               and app2.shell.page.name == "library")
            ok("...and reads the library again by itself", scans == [1] and not app2.scanning, scans)
            sb.destroy(app2.root)
    except Exception as e:
        ok("after an update it opens on the library", False, f"{type(e).__name__}: {e}")

    ok("no Tk callback raised during the walk", not sb.errors, [e.strip().splitlines()[-1] for e in sb.errors][:3])
    sb.close()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print("   -", f)
        return 1
    print("THE WINDOW STILL WORKS")
    return 0


def _menu_objects(_kit, k):
    """The Menu objects behind the kit's open layers (a layer holds the menu's
    bound wheel method, which leads back to it)."""
    for layer in reversed(k.layers):
        wheel = getattr(layer, "wheel", None)
        owner = getattr(wheel, "__self__", None)
        if isinstance(owner, _kit.Menu):
            yield owner


if __name__ == "__main__":
    raise SystemExit(main())
