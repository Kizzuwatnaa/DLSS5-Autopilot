r"""Look at everything drawn on every page of the 2.0 window, and complain.

    python _tools\gui_lint.py

gui_scale_check measures whether what a person needs is on the screen. This
looks at what IS drawn, item by item, on the canvases of the window
(core.ui) at 100% in 1280x800 and 1920x1080, at 150% and at 200%: the
library, a game page with its settings open on every route, the video page
with and without a player, the remix page, and the dlss page (some behind, all
current, nothing found). It reports:

  edge      a text item that runs past the canvas' right (or left) edge
  covers    a text item lying on a control it does not belong to - the
            canvas form of two widgets in one grid cell
  label     a button whose label (or icon) is wider than the button
  colour    a fill or outline that is not in core/ui/theme.py's palette, or
            one of the few colours the window derives from it with
            motion.mix / ink_on (listed in DERIVED below)
  font      a text item in a family that is not theme.MONO_FAMILIES or
            theme.ICON_FAMILIES
  widget    a real Tk widget on a canvas (the search box) painted outside
            the palette

"It found nothing" is not the goal; nothing found AND the screenshots look
right is. Run it after any change under core/ui, with gui_scale_check.
Nothing is written outside a temporary folder (see ui_sandbox.py).
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_sandbox import Sandbox   # noqa: E402

ISSUES: list[str] = []
LAYER_PREFIXES = ("menu", "kit_tip", "toast")


def palette():
    from core.ui import theme as T
    from core.ui.motion import ink_on, mix
    base = [T.BG, T.RAIL, T.SURF, T.SURF2, T.LINE, T.TEXT, T.MUTED, T.DIM, T.AMBER, T.OK, T.WARN, T.LOG_BG]
    inks = sorted({ink_on(c) for c in base} | {"#111111", "#ffffff"})
    allowed = {c.lower() for c in base + inks}
    derived = set()
    for a in base:
        derived.add(mix(a, "#ffffff", 0.14))             # a primary or danger button under the pointer
        derived.add(mix(T.BG, a, 0.10))                  # a banner's ground in its own colour
        for ink in inks:
            derived.add(mix(a, ink, 0.5))                # the progress bar inside a button
    derived.add(mix(T.BG, T.SURF, 0.9))                  # the result box on a game page
    return allowed, {c.lower() for c in derived}


def _norm(w, colour: str) -> str:
    c = str(colour).strip().lower()
    if not c or c.startswith("#") and len(c) == 7:
        return c
    try:
        r, g, b = (v // 257 for v in w.winfo_rgb(colour))
        return f"#{r:02x}{g:02x}{b:02x}"
    except tk.TclError:
        return c


def _in_layer(c, item) -> bool:
    return any(t.startswith(LAYER_PREFIXES) for t in c.gettags(item))


def lint_canvas(c, kit, where: str, allowed, derived, families) -> None:
    root = c.winfo_toplevel()
    width = c.winfo_width()
    controls = kit.controls() if kit is not None else []
    boxes = {tag: c.bbox(tag) for tag, _k, _l in controls}
    for item in c.find_all():
        kind = c.type(item)
        if c.itemcget(item, "state") == "hidden" or _in_layer(c, item):
            continue
        tags = set(c.gettags(item))
        # colours
        opts = {"text": ("fill",), "line": ("fill",), "rectangle": ("fill", "outline"),
                "oval": ("fill", "outline"), "polygon": ("fill", "outline")}.get(kind, ())
        for opt in opts:
            v = _norm(c, c.itemcget(item, opt))
            if v and v not in allowed and v not in derived:
                ISSUES.append(f"{where}: colour  {kind} {opt}={v} tags={sorted(tags)[:3]}")
        if kind == "window":
            try:
                w = c.nametowidget(c.itemcget(item, "window"))
                opts_w = ["bg", "fg", "insertbackground", "selectbackground", "selectforeground",
                          "disabledbackground"]
                try:
                    if int(str(w.cget("highlightthickness") or 0)) > 0:
                        opts_w.append("highlightbackground")   # only a ring that is drawn is seen
                except (tk.TclError, ValueError):
                    pass
                for opt in opts_w:
                    try:
                        v = _norm(w, w.cget(opt))
                    except tk.TclError:
                        continue
                    if v and v not in allowed and v not in derived:
                        ISSUES.append(f"{where}: widget  {w.winfo_class()} {opt}={v}")
            except (KeyError, tk.TclError):
                pass
        if kind != "text":
            continue
        text = c.itemcget(item, "text")
        if not text.strip():
            continue
        # fonts
        try:
            fam = root.tk.splitlist(c.itemcget(item, "font"))[0]
        except (tk.TclError, IndexError):
            fam = ""
        if fam not in families:
            ISSUES.append(f"{where}: font    {text[:30]!r} is {fam!r}")
        box = c.bbox(item)
        if not box:
            continue
        # past the edge
        if box[2] > width + 1 or box[0] < -1:
            ISSUES.append(f"{where}: edge    {text[:40]!r} spans {box[0]}..{box[2]} of {width}")
        # lying on a control it is not part of
        for tag, ckind, label in controls:
            if tag in tags:
                continue
            b = boxes.get(tag)
            if not b:
                continue
            if box[0] < b[2] - 1 and b[0] < box[2] - 1 and box[1] < b[3] - 1 and b[1] < box[3] - 1:
                ISSUES.append(f"{where}: covers  {text[:30]!r} lies on {ckind} '{label}'")
    # button labels wider than the button
    for tag, ckind, label in controls:
        if ckind != "button":
            continue
        items = c.find_withtag(tag)
        rects = [i for i in items if c.type(i) == "rectangle"]
        if not rects:
            continue
        bx1, _by1, bx2, _by2 = c.coords(rects[0])
        for i in items:
            if c.type(i) != "text" or not c.itemcget(i, "text").strip():
                continue
            tb = c.bbox(i)
            if tb and (tb[0] < bx1 - 1 or tb[2] > bx2 + 1):
                ISSUES.append(f"{where}: label   button '{label}' is {int(bx2 - bx1)} px, its text needs "
                              f"{tb[2] - tb[0]} px ({tb[0] - bx1:+.0f}..{tb[2] - bx2:+.0f})")


def lint_window(app, where, allowed, derived, families) -> None:
    s = app.shell
    app.root.update()
    for canvas, kit, part in ((s.content, s.kit, "page"), (s.rail_c, s.rail_kit, "rail"),
                              (s.bottom_c, s.bottom_kit, "bottom"), (s.banner_c, s.banner_kit, "banner")):
        lint_canvas(canvas, kit, f"{where} {part}", allowed, derived, families)


def dlss_states(games: list) -> list[tuple[str, dict]]:
    """The dlss page's three states as the page's own cache, no scan and no
    network: rows with some behind (an anti-cheat one among them), every
    game current, and nothing found. Shared with gui_scale_check."""
    import time
    new = "310.9.1 (NVIDIA SDK)"
    newest = {f: {"label": new, "version": "310.9.1"} for f in ("dlss", "dlssg", "dlssd")}

    def e(fam, ver, state="original", original="", label="", backup=False):
        return {"rel": {"dlss": "nvngx_dlss.dll", "dlssg": "nvngx_dlssg.dll",
                        "dlssd": "nvngx_dlssd.dll"}[fam],
                "family": fam, "version": ver, "state": state, "original": original,
                "label": label, "backup": backup}

    x64 = [g for g in games if g.bitness == 64]
    seen = [str(g.folder) for g in games]
    x86 = len(games) - len(x64)
    behind = {str(x64[0].folder): {"name": x64[0].name, "entries": [
        e("dlss", "3.7.20"), e("dlssg", "310.9.1", "updated", "3.5.0", new, True),
        e("dlssd", "310.8.0", "install")], "anticheat": "", "running": ""}}
    if len(x64) > 1:
        behind[str(x64[1].folder)] = {"name": x64[1].name, "entries": [e("dlss", "2.5.1")],
                                      "anticheat": "Easy Anti-Cheat", "anticheat_found":
                                      "found: EasyAntiCheat", "running": ""}
    current = {str(g.folder): {"name": g.name, "entries": [e("dlss", "310.9.1")], "anticheat": "",
                               "running": ""} for g in x64}
    now = time.time()
    return [("some behind", {"at": now, "newest": newest, "games": behind, "seen": seen, "x86": x86}),
            ("all current", {"at": now, "newest": newest, "games": current, "seen": seen, "x86": x86}),
            ("nothing found", {"at": now, "newest": newest, "games": {str(g.folder): {
                "name": g.name, "entries": []} for g in x64}, "seen": seen, "x86": x86})]


def show_dlss(app, sb, data: dict) -> None:
    """The dlss page drawn from `data`, with no read of the games started."""
    from core.ui import ctl_dlss
    app.dlss_data = data
    with patch.object(ctl_dlss.DlssControl, "dlss_due", lambda self: False), \
            patch.object(ctl_dlss.DlssControl, "dlss_recheck_running", lambda self: None):
        app.shell.show("dlss", remember=False)
        app.shell.redraw()
        sb.pump(app.root, 0.3)


def run(sb: Sandbox, scale: float, size: tuple[int, int], games: list, player) -> None:
    from core import dlss, video
    from core.ui import theme as T
    probe = tk.Tk()
    screen = (probe.winfo_screenwidth(), probe.winfo_screenheight())
    probe.destroy()
    w, h = min(size[0], screen[0]), min(size[1], screen[1] - 40)
    app = sb.app(scale=scale, size=(w, h), visible=False)
    root, shell = app.root, app.shell
    allowed, derived = palette()
    families = set(T.MONO_FAMILIES) | set(T.ICON_FAMILIES)
    tag = f"{int(scale * 100)}% {w}x{h}"
    try:
        app.all_games = list(games)
        shell.show("library", remember=False)
        sb.until(root, lambda: all(app.card(g)["kind"] != "reading" for g in app.all_games), 20)
        sb.pump(root, 0.5)
        shell.redraw()
        lint_window(app, f"{tag} library", allowed, derived, families)
        # the update and crash banners, drawn once so their colours are looked at too
        app.q.put(("update", ("9.9.9", "https://example.invalid")))
        sb.pump(root, 0.2)
        app.offer_crash_report()
        sb.pump(root, 0.2)
        lint_window(app, f"{tag} banners", allowed, derived, families)
        shell.unbanner("update")
        shell.unbanner("crash")

        g = games[0]
        app.open_game(g)
        sb.until(root, lambda: not app.entering and app.support is not None, 20)
        app.game_page.settings_open = True
        for route in (dlss.FEEDER, dlss.OPTI, dlss.NATIVE, dlss.BRIDGE, dlss.RENODX,
                      dlss.UPSTREAM, dlss.STANDALONE, dlss.REMIX):
            try:
                app.set_setting("route", route)
                shell.redraw()
                sb.pump(root, 0.1)
            except Exception as e:
                ISSUES.append(f"{tag} game/{route}: drawing raised {type(e).__name__}: {e}")
                continue
            lint_window(app, f"{tag} game/{route}", allowed, derived, families)

        shell.show("video", remember=False)
        sb.pump(root, 0.2)
        lint_window(app, f"{tag} video (no player)", allowed, derived, families)
        with patch.object(video, "known", return_value=player), \
                patch.object(video, "list_cameras", return_value=["A camera with a long name (USB)"]), \
                patch.object(video, "list_screens", return_value=["Screen 1"]):
            app.cameras = None
            shell.show("video", remember=False)
            sb.until(root, lambda: app.cameras is not None, 10)
            sb.pump(root, 0.2)
            lint_window(app, f"{tag} video", allowed, derived, families)
        shell.show("remix", remember=False)
        sb.pump(root, 0.5)
        lint_window(app, f"{tag} remix", allowed, derived, families)
        for state, data in dlss_states(games):
            try:
                show_dlss(app, sb, data)
            except Exception as e:
                ISSUES.append(f"{tag} dlss/{state}: drawing raised {type(e).__name__}: {e}")
                continue
            lint_window(app, f"{tag} dlss/{state}", allowed, derived, families)
        for e in sb.errors:
            ISSUES.append(f"{tag}: a Tk callback raised: {e.strip().splitlines()[-1]}")
        sb.errors.clear()
    finally:
        sb.destroy(root)


def main() -> int:
    sb = Sandbox("lint_")
    games = [sb.game("A Game With A Fairly Long Name: Definitive Edition", "DX11", 64, installed=True,
                     extra=("nvngx_dlss.dll", "nvngx_dlssd.dll")),
             sb.game("Grand Theft Auto IV", "DX9", 32, source="Steam"),
             sb.game("Another Game", "DX12", 64)]
    player = sb.game("Video player", "DX11", 64, installed=True)
    player.kind = "video"
    for scale, size in ((1.0, (1280, 800)), (1.0, (1920, 1080)), (1.5, (1920, 1080)), (2.0, (2560, 1440))):
        run(sb, scale, size, games, player)
    sb.close()
    seen, unique = set(), []
    for line in ISSUES:
        key = line.split(": ", 1)[1] if ": " in line else line
        if key not in seen:
            seen.add(key)
            unique.append(line)
    if not unique:
        print("nothing past an edge, lying on a control, wider than its button, off the palette or in "
              "another font - on any page, route or scale checked")
        return 0
    print(f"{len(unique)} distinct finding(s) ({len(ISSUES)} in all):\n")
    for line in unique:
        print("  " + line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
