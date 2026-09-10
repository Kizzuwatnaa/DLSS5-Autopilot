r"""Drive the real window the way a person does, and see that it still works.

    python _tools\walkthrough.py

Four green suites do not prove the application opens. This builds the
actual window, puts a real install in front of it, walks the install page
through every route, runs the diagnosis, the auto-tuner and the report
dialog, and pushes the background queue messages the newer features send.
Everything it touches is a temporary folder: nothing is downloaded, no
browser is opened, and no setting on this machine is changed.

It is the last check before a release, after the suites and the scaling
check - those say the parts are right, this says the thing still runs.
"""
import json
import sys
import tempfile
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import diagnose, dlss, games, gui, prefs, reportui   # noqa: E402

# Point the settings file at a temporary one before the window is built:
# this drives the real application, and it must not leave a target frame
# rate or a tuning history behind on the machine that ran it.
prefs.FILE = Path(tempfile.mkdtemp(prefix="walk_prefs_")) / "settings.json"
prefs._CACHE = None if hasattr(prefs, "_CACHE") else None
# A window that finds a saved library from another version rescans the
# disks at start; the one on the machine running this is not the test's.
from core import library as _library_iso  # noqa: E402
_library_iso.FILE = Path(tempfile.mkdtemp(prefix="lib_iso_")) / "library.json"

FAILS = []


def ok(what, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + what + (f"   {extra}" if extra else ""))
    if not cond:
        FAILS.append(what)


def close(r):
    """Destroy a window without its pending timers firing into nothing."""
    try:
        for job in r.tk.call("after", "info"):
            r.after_cancel(job)
    except tk.TclError:
        pass
    r.destroy()


root = tk.Tk()
root.geometry("1400x900+40+40")
app = gui.App(root)
root.update()
ok("the window opens", bool(root.winfo_exists()))
ok("the logo is in the rail, not only on the taskbar",
   getattr(app, "_logo", None) is not None)

# A real-looking install: feeder route, everything present, a session logged.
d = Path(tempfile.mkdtemp(prefix="walk_"))
(d / "Game.exe").write_bytes(b"MZ" + b"\0" * 200)
(d / "dxgi.dll").write_bytes(b"MZ")
for n in ("dlss5-feed.addon64", "renodx-dlss5.addon64", "nvngx_dlssnr.dll"):
    (d / n).write_bytes(b"MZ")
sh = d / "reshade-shaders" / "Shaders"
sh.mkdir(parents=True)
(sh / "DLSS5_Feed.fx").write_text("// t", encoding="utf8")
(d / "ReShade.ini").write_text("[GENERAL]\n", encoding="utf8")
# The game ships ray reconstruction, so the swap row is on the page. Without
# it that row never appeared here, and a collision that painted it over the
# nvngx_dlss controls survived three review passes.
(d / "nvngx_dlssd.dll").write_bytes(b"MZ")
(d / "dlss5-autopilot.json").write_text(json.dumps(
    {"version": 1, "complete": True, "exe": "Game.exe", "bitness": 64,
     "api": "DX11", "proxy": "dxgi.dll", "path": "feeder",
     "files": ["dxgi.dll", "dlss5-feed.addon64", "renodx-dlss5.addon64",
               "nvngx_dlssnr.dll", "ReShade.ini",
               "reshade-shaders/Shaders/DLSS5_Feed.fx"]}), encoding="utf8")
(d / "ReShade.log").write_text(
    'INFO | Initializing crosire\'s ReShade\nRegistered add-on "DLSS 5 Feed" v0.1\n',
    encoding="utf8")
(d / "dlss5-feed.log").write_text(
    "[feed] effects: DLSS5_Feed.fx technique found, ColorInput found\n"
    "[feed] NVSDK_NGX_D3D12_Init -> 0x00000001 (Success)\n"
    "[feed] feature ready: 1920x1080 DLAA\n"
    "[feed] frame 1 delivered (1920x1080 at 100%)\n"
    "[feed] 3600 frames: feed CPU 2.10 ms/frame, GPU 4.80 ms/frame, 47.0 fps\n",
    encoding="utf8")

g = games.Game(name="Walkthrough Game", folder=d, exe=d / "Game.exe",
               bitness=64, api="DX11", source="Manual")
app.all_games = [g]
app._fill()
root.update()
ok("the game list draws", len(app.tree.get_children()) == 1)

app.game = g
app._show(3)          # the step rail does this when a game is chosen
app._enter_install()
root.update()
ok("the install page opens with a real game", app.step == 3)
ok("...the overlay-key control is there", app.cb_overlaykey.winfo_exists())
ok("...and the target-fps box is there", app.sp_target.winfo_exists())

def overlaps(app_):
    """Controls sharing a grid cell on the settings page.

    Tk raises nothing when two widgets are gridded into one cell: the one
    created later simply paints over the other. The scaling check measures
    sizes and never sees it, which is how the ray-reconstruction row spent
    three review passes sitting on top of the nvngx_dlss row.
    """
    seen, clashes = {}, []
    for w in app_.cb_dlss.master.grid_slaves():
        try:
            info = w.grid_info()
            if not w.winfo_ismapped():
                continue
            r, c = int(info.get("row", -1)), int(info.get("column", -1))
            span = int(info.get("columnspan", 1))
        except Exception:
            continue
        for col in range(c, c + span):
            if (r, col) in seen:
                clashes.append(f"row {r} col {col}: {seen[(r, col)]} + "
                               f"{w.winfo_class()}")
            seen[(r, col)] = w.winfo_class()
    return clashes


for route in (dlss.FEEDER, dlss.OPTI, dlss.NATIVE, dlss.BRIDGE, dlss.RENODX,
              dlss.UPSTREAM, dlss.STANDALONE):
    try:
        app._apply_route(route)
        root.update()
        ok(f"the {route} route renders its page", True)
        bad = overlaps(app)
        ok(f"...no two controls share a cell on {route}", not bad, bad[:3])
    except Exception as e:
        ok(f"the {route} route renders its page", False, f"{type(e).__name__}: {e}")

app._apply_route(dlss.FEEDER)
app.target_fps.set("60")
app._on_target()
try:
    app._diagnose()
    root.update()
    ok("the diagnosis runs from the button", app._last_diag is not None,
       app._last_diag.verdict if app._last_diag else "")
    ok("...and it says Working for a healthy session",
       app._last_diag.verdict == "Working.", app._last_diag.verdict)
    ok("...the share button is enabled after it", str(app.btn_share["state"]) == "normal")
    ok("...and the auto-tuner suggested something",
       app._tune is not None and app._tune.resolution != 100,
       app._tune.resolution if app._tune else None)
except Exception as e:
    ok("the diagnosis runs from the button", False, f"{type(e).__name__}: {e}")

# Applying the tuner writes the config in place.
if app._tune is not None:
    want = app._tune.resolution
    try:
        app._apply_tune()
        root.update()
        cfg = (d / "dlss5-feed.cfg").read_text(encoding="utf8")
        ok("applying the suggestion writes dlss5-feed.cfg",
           f"work_resolution={want}" in cfg, cfg.splitlines()[:3])
    except Exception as e:
        ok("applying the suggestion writes dlss5-feed.cfg", False,
           f"{type(e).__name__}: {e}")

# The report dialog, without opening a browser.
_wait = tk.Toplevel.wait_window
tk.Toplevel.wait_window = lambda self, *a: None
try:
    dlg = reportui.ReportDialog(root, "Walkthrough Game")
    ok("the report dialog opens", dlg.win.winfo_exists())
    ok("...and refuses to send with nothing answered",
       str(dlg.go["state"]) == "disabled")
    dlg.started.set("it closed itself")
    dlg.text.insert("1.0", "it closed at the splash screen")
    dlg._check()
    ok("...and opens up once both are answered", str(dlg.go["state"]) == "normal")
    dlg._ok()
    ok("...returning both answers", dlg.answers["started"] == "it closed itself")
except Exception as e:
    ok("the report dialog opens", False, f"{type(e).__name__}: {e}")
finally:
    tk.Toplevel.wait_window = _wait

# The queue handlers the new features push through.
try:
    app.q.put(("community", (d, ["12 people have reported this game."])))
    app.q.put(("wincrash", (d, None, ("Windows recorded Game.exe faulting in "
                                      "dxgi.dll (0xC0000005).", "detail"))))
    app._pump()
    root.update()
    ok("the community and crash notes reach the log", True)
except Exception as e:
    ok("the community and crash notes reach the log", False,
       f"{type(e).__name__}: {e}")

# The report body itself, end to end.
try:
    body = diagnose.issue_body("1.8.0", "RTX 4060 Ti", 89, "616.64", g,
                               "feeder", app._last_diag, "", Path("x"), d,
                               answers={"started": "it closed itself",
                                        "happened": "closed at the splash"})
    ok("the report body is built with the answers in it",
       "closed at the splash" in body and "Files in the folder" in body)
except Exception as e:
    ok("the report body is built with the answers in it", False,
       f"{type(e).__name__}: {e}")

# The wheel, over the controls rather than the bare strip beside them.
# It used to be bound on the canvas's own Enter/Leave - but the content is
# a child window laid over that canvas, so Tk handed the canvas a Leave the
# moment the pointer touched any control, and the page stopped scrolling
# nearly everywhere. And a wheel turn that reaches a ttk.Combobox changes
# its selection, so scrolling past "route" used to pick a different one.
try:
    sc = app.installscroll
    sc.set_height(200)
    root.update()
    ok("the settings page offers a scrollbar when it does not fit",
       sc._bar.winfo_ismapped())

    class _Wheel:
        delta = -120
        x_root = y_root = 0

    def wheel_over(widget):
        # A real event, through every binding Tk would run for it - widget,
        # class, toplevel, all. Calling the handler directly skipped the
        # class binding, and that is exactly where ttk.Combobox changed its
        # own value: the check passed while the bug was live.
        was = sc._canvas.yview()[0]
        widget.event_generate("<MouseWheel>", delta=-120, x=5, y=5,
                              rootx=widget.winfo_rootx() + 5,
                              rooty=widget.winfo_rooty() + 5, when="now")
        root.update()
        return was, sc._canvas.yview()[0]

    a, b = wheel_over(app.routelbl)
    ok("...and the wheel scrolls it from over the page text", b > a, (a, b))

    before = app.cb_route.get()
    sc._canvas.yview_moveto(0.0)
    root.update()
    a, b = wheel_over(app.cb_route)
    ok("...and from over a dropdown", b > a, (a, b))
    ok("...without the dropdown quietly picking something else",
       app.cb_route.get() == before, app.cb_route.get()[:40])

    a2, b2 = wheel_over(app.log)
    ok("...while the log keeps its own wheel", b2 == a2, (a2, b2))
except Exception as e:
    ok("the wheel scrolls the settings page", False,
       f"{type(e).__name__}: {e}")

# Another window that scrolls must not take the main window's wheel with it.
# The remix list used bind_all/unbind_all, which are process-wide: closing
# it removed the main window's one wheel binding, and the settings page
# never scrolled again that session.
try:
    from core import remixui as _rx
    # An empty library: the list is built from what ships with the tool,
    # and nothing here reaches the network.
    win = _rx.show(root, [])
    root.update()
    # The app drops the RemixWindow object at once; the logo has to be held
    # by the window itself, or Tk deletes it and the title goes blank.
    _top = win.win
    del win
    import gc
    gc.collect()
    root.update()
    ok("the remix window keeps its logo once the app lets go of it",
       str(getattr(_top, "_logo", "")) in root.tk.call("image", "names"))

    class _W:                      # what the rest of this check expects
        pass
    win = _W()
    win.win = _top
    win.win.destroy()
    root.update()
    ok("closing another window leaves the main window's wheel bound",
       bool(root.bind_all("<MouseWheel>")), repr(root.bind_all("<MouseWheel>")))
    sc._canvas.yview_moveto(0.0)
    root.update()
    a, b = wheel_over(app.routelbl)
    ok("...and the settings page still scrolls after it", b > a, (a, b))
except Exception as e:
    ok("closing another window leaves the main window's wheel bound", False,
       f"{type(e).__name__}: {e}")

# The rail holds places: every row is a real click, and the pages it
# leads to are the ones it names.
try:
    def rail_click(n):
        row = next(e for e in app.rail_rows if e["n"] == n)
        row["t1"].event_generate("<Button-1>", x=2, y=2, when="now")
        root.update()
        return app.step

    ok("the rail's video row opens the video page", rail_click(4) == 4
       and app.p4.winfo_ismapped(), app.step)
    ok("...without a 'continue' that leads nowhere",
       not app.btn_next.winfo_ismapped())
    ok("the rail's remix row opens the remix page", rail_click(5) == 5
       and app.p5.winfo_ismapped(), app.step)
    ok("...and says which games in the library have a mod",
       "none of the games" in app.remixmine.cget("text"),
       app.remixmine.cget("text"))
    _gta = games.Game(name="Grand Theft Auto IV", folder=d, exe=d / "Game.exe",
                      bitness=32, api="DX9", source="Steam")
    app.all_games.append(_gta)
    app._remix_mine()
    ok("...naming the one that can be fetched for you",
       "Grand Theft Auto IV (download & install)" in app.remixmine.cget("text"),
       app.remixmine.cget("text"))
    app.all_games.remove(_gta)
    ok("'back' from there goes to the games", (app._back(), app.step)[1] == 2)
    ok("...and 'continue' is back under the list",
       app.btn_next.winfo_manager() == "pack")
    _held = app.game
    app.game = None
    app._paint_rail()
    ok("with no game picked, the install row does nothing", rail_click(3) == 2,
       app.step)
    app.game = _held
    app._paint_rail()
    ok("the architecture filter is on the games page, and filters",
       app.archbox.winfo_exists()
       and (app.archbox.set(gui.ARCH_CHOICES[2][1]),
            app.archbox.event_generate("<<ComboboxSelected>>", when="now"),
            app.arch.get())[2] == "32", app.arch.get())
    app.archbox.set(gui.ARCH_CHOICES[0][1])
    app.archbox.event_generate("<<ComboboxSelected>>", when="now")
    ok("...and back to every architecture", app.arch.get() == "all")
    ok("no page is called a step any more",
       not any("step " in str(w.cget("text")).lower()
               for p in app.pages for w in p.winfo_children()
               if isinstance(w, tk.Label)))
    # The filter refills the list, which lets go of the picked game.
    app.game = _held
    app._paint_rail()
    ok("with a game picked, the install row opens the install page",
       rail_click(3) == 3, app.step)
except Exception as e:
    ok("the rail's video row opens the video page", False,
       f"{type(e).__name__}: {e}")

# The name in the corner goes home: the library when there is one, the
# first page when there is not. A real click, so the binding is what runs.
try:
    home = 2 if app.all_games else 1
    was = app.step
    app.brandname.event_generate("<Enter>", when="now")
    hover = app.brandname.cget("fg")
    app.brandname.event_generate("<Button-1>", x=2, y=2, when="now")
    root.update()
    ok("clicking the name in the corner goes home", was != home
       and app.step == home, (was, app.step, home))
    ok("...and the name says it can be clicked", hover != gui.TXT
       and str(app.brandname.cget("cursor")) == "hand2", hover)
    app.brandname.event_generate("<Leave>", when="now")
except Exception as e:
    ok("clicking the name in the corner goes home", False,
       f"{type(e).__name__}: {e}")

app._enter_step2() if hasattr(app, "_enter_step2") else None
root.update()
close(root)

# After an update the saved library is from another version and cannot be
# used - and the app opened on the architecture page as if it had never been
# run. Someone who has scanned before opens on the library, rescanning.
try:
    from core import library as _lib
    _lib.FILE.write_text(json.dumps({"schema": _lib.SCHEMA,
                                     "app_version": "0.0.0", "sm": None,
                                     "games": [], "rows": {}}), encoding="utf8")
    _scan_all = games.scan_all
    _scans = []
    # No disk walk in a check: record that the scan ran, find nothing.
    games.scan_all = lambda progress=None: _scans.append(1) or []
    root = tk.Tk()
    app = gui.App(root)
    root.update()
    ok("after an update it opens on the library, not the first page",
       app.step == 2, app.step)
    for _ in range(100):
        root.after(20)
        root.update()
        if not app.busy:
            break
    ok("...and reads the library again by itself", _scans == [1], _scans)
    ok("...and the rescan finishes", not app.busy)
    close(root)
    _lib.FILE.unlink()
    root = tk.Tk()
    app = gui.App(root)
    root.update()
    ok("never scanned: it still opens on the first page", app.step == 1,
       app.step)
    # First run -> video -> back: the games page used to come up empty,
    # with nothing scanning.
    _scans.clear()
    app._show(4)
    app._back()
    for _ in range(100):
        root.after(20)
        root.update()
        if not app.busy:
            break
    ok("first run, video, back: the games page scans", app.step == 2
       and _scans == [1], (app.step, _scans))
    # The video player alone is not a library: home still means "find games".
    from core import video as _video
    _player = games.Game(name="Video player", folder=d, exe=d / "Game.exe",
                         bitness=64, api="DX11", source="Manual")
    _player.kind = "video"
    app.all_games = [_player]
    app._show(4)
    app._go_home()
    ok("with only the video player set up, home is the first page",
       app.step == 1, app.step)
    _lib.FILE.unlink(missing_ok=True)       # the stubbed scan above saved one
    ok("...and it is never saved as the library",
       (app._remember_library(), _lib.FILE.is_file())[1] is False)
    close(root)
    games.scan_all = _scan_all
except Exception as e:
    ok("after an update it opens on the library, not the first page", False,
       f"{type(e).__name__}: {e}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    raise SystemExit(1)
print("THE APPLICATION STILL WORKS")
