r"""Does the 2.0 window still work on somebody else's display?

The owner's monitor is 1920x1080 at 100%, so every display bug in this tool
has arrived as a bug report with a photograph attached (issue #40, twice).
This builds the REAL window (core.ui) at ten display cases from 100% to 350%
and measures what a person needs, not proxies for it.

    python _tools\gui_scale_check.py                  the ten cases
    python _tools\gui_scale_check.py 2.5              one scaling, in this screen
    python _tools\gui_scale_check.py --out shots      and a PrintWindow capture of
                                                      each case's library and game
                                                      page (our window only)

Per case (theme.set_scale(96 * k) before the window is built):
  screen     the window never asks the screen for more than it has - its
             size and its minimum size, against the emulated screen
  covers     the library shows at least one whole row of covers (cover,
             name and status) without scrolling, and no cover runs off the
             right edge
  search     the search box is at least px(150) wide
  buttons    every button's label fits inside the button, on the library,
             the game page with its settings open, the video and remix pages,
             and the dlss page (some behind, all current, nothing found)
  action     the game page's primary action (install) is whole and inside
             the view before any scrolling - required when the window has at
             least the room of a 1080p screen at 150% (1280x688 logical);
             measured and printed in every case
  scroll     a page taller than the view scrolls with the wheel and shows a
             thumb; a page that fits does neither
  log        the log drawer, opened, shows at least three lines

READ THIS BEFORE TRUSTING A NUMBER: a window cannot be larger than the
screen this runs on, so a 2560-wide display is drawn in this machine's width
and the output says so. What is really varied is the ratio - how big
everything is drawn against the room there is. A 4K screen at 300% has the
room of 1280x720 at 100%: it is SCALE 1.5 in a 1920 window, not SCALE 3.0 in
a 1280 one.

Nothing is written outside a temporary folder (see ui_sandbox.py).
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_sandbox import Sandbox   # noqa: E402

# (scaling, display width, display height, what machine this is). The window
# is the display minus a 48 px taskbar.
CASES = (
    (1.0, 1920, 1080, "1920x1080 at 100%"),
    (1.25, 2560, 1440, "2560x1440 at 125%"),
    (1.5, 1920, 1080, "1920x1080 at 150%"),
    (1.5, 3840, 2160, "3840x2160 at 150%"),
    (2.0, 3840, 2160, "3840x2160 at 200%"),
    (2.0, 2560, 1600, "2560x1600 laptop at 200%"),
    (2.5, 5120, 2880, "5120x2880 at 250%"),
    (3.0, 3840, 2160, "3840x2160 at 300%"),
    (3.0, 7680, 4320, "7680x4320 at 300%"),
    (3.5, 7680, 4320, "7680x4320 at 350%"),
)
ROOM_FOR_ACTION = (1280, 688)        # logical px: 1080p at 150%
TASKBAR = 48


def _label_fits(c, kit) -> list[str]:
    bad = []
    for tag, kind, label in kit.controls("button"):
        items = c.find_withtag(tag)
        rects = [i for i in items if c.type(i) == "rectangle"]
        if not rects:
            continue
        x1, _y1, x2, _y2 = c.coords(rects[0])
        for i in items:
            if c.type(i) == "text" and c.itemcget(i, "text").strip():
                b = c.bbox(i)
                if b and (b[0] < x1 - 1 or b[2] > x2 + 1):
                    bad.append(f"'{label}' ({int(x2 - x1)} px, text {b[2] - b[0]} px)")
                    break
    return bad


def check(sb: Sandbox, scale: float, dw: int, dh: int, what: str, out: Path | None,
          games: list, player) -> list[str]:
    from unittest.mock import patch
    from core import video
    from core.ui import theme as T
    bad: list[str] = []
    probe = tk.Tk()
    real = (probe.winfo_screenwidth(), probe.winfo_screenheight())
    probe.destroy()
    pct = f"{scale:.0%} {what}"

    # 1. the window's own request, at SCALE k exactly, on the emulated screen
    T.set_scale(96 * scale)
    r0 = tk.Tk()
    r0.withdraw()
    r0.winfo_screenwidth, r0.winfo_screenheight = (lambda: dw), (lambda: dh)
    asked = {"geometry": [], "minsize": []}
    geo, mins = r0.geometry, r0.minsize
    r0.geometry = lambda spec=None: (asked["geometry"].append(spec) if spec else None, geo(spec))[1]
    r0.minsize = lambda w=None, h=None: (asked["minsize"].append((w, h)) if w is not None else None, mins(w, h))[1]
    try:
        from core.ui.shell import Shell
        Shell(r0)
    finally:
        sb.destroy(r0)

    # 2. the same room in this screen: a display bigger than this one is drawn
    # smaller by the same factor as its window, so what fits is the same
    ww, wh = dw, dh - TASKBAR
    f = min(1.0, real[0] / ww, (real[1] - TASKBAR) / wh)
    ww, wh = int(ww * f), int(wh * f)
    k_eff = max(1.0, scale * f)
    app = sb.app(scale=k_eff, size=(ww, wh), visible=out is not None)
    root, shell = app.root, app.shell
    root.minsize(1, 1)
    root.geometry(f"{ww}x{wh}+0+0")
    sb.pump(root, 0.4)
    c, k = shell.content, shell.kit
    print(f"\n--- {what}: SCALE {scale:.2f}; measured as SCALE {k_eff:.2f} in a "
          f"{root.winfo_width()}x{root.winfo_height()} window"
          + (f" (the same room, {f:.0%} of the display's window)" if f < 1 else "") + " ---")

    def say(name, value, good):
        print(f"  {name:<9} {value}" + ("" if good else "   !!"))
        if not good:
            bad.append(f"{pct}: {name}: {value}")

    def shot(name):
        if out is None:
            return
        try:
            import shot as _shot
            root.update()
            p = out / f"{int(scale * 100)}-{dw}x{dh}-{name}.png"
            _shot.capture(int(root.wm_frame(), 16), p)
        except Exception as e:
            print(f"  (no capture: {e})")

    def scrolls(name):
        view = c.winfo_height()
        taller = shell.content_h > view + 1
        shell.scroll_to(0)
        sb.pump(root, 0.05)
        y0 = c.canvasy(0)
        sb.wheel(c, max(1, c.winfo_width() // 2), max(1, view // 2), -120)
        moved = c.canvasy(0) != y0
        thumb = bool(c.find_withtag("thumb"))
        shell.scroll_to(0)
        sb.pump(root, 0.05)
        say(f"scroll", f"{name}: {shell.content_h} px of content in {view}, wheel "
            f"{'moved' if moved else 'did not move'}, thumb {'yes' if thumb else 'no'}",
            moved == taller and thumb == taller)

    try:
        # screen: what the window asked for, against the emulated screen
        sizes = [tuple(int(v) for v in s.split("+")[0].split("x")) for s in asked["geometry"]
                 if s and "x" in s]
        gw, gh = (max(s[0] for s in sizes), max(s[1] for s in sizes)) if sizes else (0, 0)
        mw, mh = (max(m[0] for m in asked["minsize"]), max(m[1] for m in asked["minsize"])) \
            if asked["minsize"] else (0, 0)
        say("screen", f"asked {gw}x{gh}, minimum {mw}x{mh}, screen {dw}x{dh}",
            bool(sizes) and gw <= dw and gh <= dh and mw <= dw and mh <= dh)

        # the library
        app.all_games = list(games)
        shell.show("library", remember=False)
        sb.until(root, lambda: all(app.card(g)["kind"] != "reading" for g in app.all_games), 20)
        shell.redraw()
        sb.pump(root, 0.3)
        page = shell.pages["library"]
        width, view = c.winfo_width(), c.winfo_height()
        row = [c.bbox(f"card{i}") for i in range(page.cols)]
        whole = [b for b in row if b and b[2] <= width and b[3] <= view and b[0] >= 0]
        say("covers", f"{len(whole)} of {page.cols} covers of the first row whole on screen ({width}x{view})",
            len(whole) >= 1 and len(whole) == len([b for b in row if b]))
        fb = c.coords(page.field.box) if page.field else [0, 0, 0, 0]
        fw = int(fb[2] - fb[0])
        say("search", f"{fw} px (needs {T.px(150)})", fw >= T.px(150))
        cut = _label_fits(c, k)
        say("buttons", "library: " + (", ".join(cut) if cut else "all labels fit"), not cut)
        scrolls("library")
        shot("library")

        # a game page, settings open
        g = games[1]
        app.open_game(g)
        sb.until(root, lambda: not app.entering and app.support is not None, 20)
        sb.pump(root, 0.2)
        shell.scroll_to(0)
        sb.pump(root, 0.1)
        tag = k.find("install", "button")
        b = c.bbox(tag) if tag else None
        room = (root.winfo_width() / k_eff, root.winfo_height() / k_eff)
        needed = room[0] >= ROOM_FOR_ACTION[0] and room[1] >= ROOM_FOR_ACTION[1]
        inside = bool(b) and b[0] >= 0 and b[2] <= c.winfo_width() and b[3] <= c.winfo_height()
        say("action", f"install {'whole in the view' if inside else 'not whole in the view'}"
            f" ({b[3] if b else '-'} of {c.winfo_height()} px; room {room[0]:.0f}x{room[1]:.0f}"
            f"{'' if needed else ', not required here'})", inside or not needed)
        app.game_page.settings_open = True
        shell.redraw()
        sb.pump(root, 0.2)
        cut = _label_fits(c, k)
        say("buttons", "game page: " + (", ".join(cut) if cut else "all labels fit"), not cut)
        scrolls("game page with settings")
        shot("game")

        shell.toggle_log(True)
        sb.pump(root, 0.5)
        line = shell.log_text.tk.call("font", "metrics", shell.log_text.cget("font"), "-linespace")
        lines = shell.log_text.winfo_height() / max(1, int(line) + 2)
        say("log", f"{lines:.1f} lines", lines >= 3)
        shell.toggle_log(False)
        sb.pump(root, 0.4)

        with patch.object(video, "known", return_value=player), \
                patch.object(video, "list_cameras", return_value=["Camera"]), \
                patch.object(video, "list_screens", return_value=["Screen 1"]):
            app.cameras = None
            shell.show("video", remember=False)
            sb.until(root, lambda: app.cameras is not None, 10)
            sb.pump(root, 0.2)
            cut = _label_fits(c, k)
            say("buttons", "video: " + (", ".join(cut) if cut else "all labels fit"), not cut)
        shell.show("remix", remember=False)
        sb.pump(root, 0.4)
        cut = _label_fits(c, k)
        say("buttons", "remix: " + (", ".join(cut) if cut else "all labels fit"), not cut)
        # the dlss page in its three states, from the page's own cache (no scan)
        import gui_lint
        for state, data in gui_lint.dlss_states(games):
            gui_lint.show_dlss(app, sb, data)
            cut = _label_fits(c, k)
            say("buttons", f"dlss/{state}: " + (", ".join(cut) if cut else "all labels fit"), not cut)
            scrolls(f"dlss/{state}")
            shot(f"dlss-{state.replace(' ', '-')}")
        for e in sb.errors:
            bad.append(f"{pct}: a Tk callback raised: {e.strip().splitlines()[-1]}")
        sb.errors.clear()
    finally:
        sb.destroy(root)
    return bad


def main() -> int:
    args = [a for a in sys.argv[1:]]
    out = None
    if "--out" in args:
        i = args.index("--out")
        out = Path(args[i + 1])
        out.mkdir(parents=True, exist_ok=True)
        del args[i:i + 2]
    cases = [(float(args[0]), 1920, 1080, "this screen, as asked")] if args else list(CASES)
    sb = Sandbox("scale_")
    games = [sb.game("Walkthrough Game", "DX11", 64, installed=True)] + [
        sb.game(f"A Game With A Long Name {i}", "DX12", 64) for i in range(1, 12)]
    player = sb.game("Video player", "DX11", 64, installed=True)
    player.kind = "video"
    bad: list[str] = []
    for scale, dw, dh, what in cases:
        bad += check(sb, scale, dw, dh, what, out, games, player)
    sb.close()
    print()
    print("=" * 70)
    if bad:
        print(f"{len(bad)} PROBLEM(S):")
        for b in bad:
            print("  -", b)
        return 1
    print("every case: on the screen, a row of covers, the search box, whole buttons, the install "
          "button in view, scrolling and the log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
