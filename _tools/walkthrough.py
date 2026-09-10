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

FAILS = []


def ok(what, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + what + (f"   {extra}" if extra else ""))
    if not cond:
        FAILS.append(what)


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

app._enter_step2() if hasattr(app, "_enter_step2") else None
root.update()
root.destroy()

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    raise SystemExit(1)
print("THE APPLICATION STILL WORKS")
