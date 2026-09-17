r"""The dlss page in the real window, every state, driven by real events.

    python _tools\dlss_page_check.py                 checks only
    python _tools\dlss_page_check.py --shots DIR     and a PrintWindow capture per state

Games are temporary folders with DLLs Windows reads a version from
(fake_pe.py); NVIDIA's build list, the download and the process list are
stubbed, and the sandbox keeps the owner's settings, library and cache out of
reach. Prints PASS/FAIL lines and exits 1 on any failure, so test_all.py runs
it as one of its sections.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_sandbox import Sandbox  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("   PASS  " if cond else "   FAIL  ") + name + (f"   {str(detail)[:160]}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="")
    ap.add_argument("--scale", type=float, default=1.0)
    args = ap.parse_args()
    sb = Sandbox(prefix="dlss_page_")
    import fake_pe
    from core import dlss, dlssupdate as du, games, installer, sources, watch
    from core.ui import ctl_dlss

    def cat(v):
        return {f: [{"tag": f"v{v}", "label": f"{v} (NVIDIA SDK)", "raw": n, "size": 0,
                     "url": f"https://raw.githubusercontent.com/NVIDIA/DLSS/v{v}/{n}"}]
                for f, n, _l in du.FAMILIES}

    CAT = cat("310.9.1")
    gate = threading.Event()
    gate.set()
    hold = {"scan": False, "download": False}
    blobs = sb.dir / "blobs"
    blobs.mkdir()

    def download(url, name, progress=None, **_k):
        if progress:
            progress(50, 100)
        if hold["download"]:
            gate.wait(20)
        v = name.split("-", 1)[1].split(" ")[0]
        p = blobs / name.replace(" ", "_")
        p.write_bytes(fake_pe.dll(v))
        if progress:
            progress(100, 100)
        return p

    real_scan = du.scan

    def scan(g, fresh=False):
        if hold["scan"]:
            gate.wait(20)
        return real_scan(g, fresh)

    running_in: set[str] = set()

    def running(g, all_procs=None):
        return "Delta.exe" if str(g.folder) in running_in else ""

    def game(name, files, bits=64, extra=()):
        d = sb.dir / "games" / name
        d.mkdir(parents=True)
        (d / "Game.exe").write_bytes(fake_pe.dll("1.0.0", size=5000))
        for rel, ver in files:
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_bytes(fake_pe.dll(ver) + name.encode())
        for x in extra:
            (d / x).mkdir(parents=True, exist_ok=True)
        return games.Game(name=name, folder=d, exe=d / "Game.exe", bitness=bits, api="DX12", source="Steam")

    nest = "Engine/Plugins/Runtime/Nvidia/DLSS/Binaries/ThirdParty/Win64/"
    A = game("Alpha Unreal", [(nest + "nvngx_dlss.dll", "3.7.20"), ("nvngx_dlssg.dll", "3.5.0")])
    B = game("Bravo Current", [("nvngx_dlss.dll", "310.9.1")])
    C = game("Charlie Online", [("bin/nvngx_dlss.dll", "3.1.0")], extra=("EasyAntiCheat",))
    D = game("Delta Running", [("nvngx_dlss.dll", "2.5.1")])
    E = game("Echo 32-bit", [("nvngx_dlss.dll", "2.4.0")], bits=32)
    F = game("Foxtrot No DLSS", [])
    running_in.add(str(D.folder))

    patches = [patch.object(sources, "nvidia_dlss", lambda: CAT), patch.object(du.net, "download", download),
               patch.object(du, "scan", scan), patch.object(du, "running", running),
               patch.object(watch, "procs", lambda: [])]
    for p in patches:
        p.start()
    du._NEWEST = {}
    dlss.forget_walk()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    import tkinter as tk
    probe = tk.Tk()
    screen = (probe.winfo_screenwidth(), probe.winfo_screenheight() - 60)
    probe.destroy()
    # never larger than the screen: a capture of a window that runs off it is half blank
    app = sb.app(scale=args.scale, size=(min(int(1400 * args.scale), screen[0]),
                                         min(int(900 * args.scale), screen[1])))
    root, shell = app.root, app.shell
    c, kit = shell.content, shell.kit

    def shot(name):
        if shots is None:
            return
        sb.pump(root, 0.35)
        try:
            from shot import capture
            capture(int(root.wm_frame(), 16), shots / f"dlss-{args.scale:g}x-{name}.png")
        except Exception as e:
            print("   (capture failed:", e, ")")

    def texts() -> list[str]:
        return [c.itemcget(i, "text") for i in c.find_withtag("page") if c.type(i) == "text"]

    def has(sub) -> bool:
        return any(sub in t for t in texts())

    def row_y(name):
        for i in c.find_withtag("page"):
            if c.type(i) == "text" and c.itemcget(i, "text") == name:
                return c.bbox(i)
        return None

    def control_near(label, name, kind="button"):
        box = row_y(name)
        if not box:
            return None
        for tag, k, lab in kit.controls(kind):
            b = c.bbox(tag)
            if lab == label and b and b[1] < box[3] + 90 and b[3] > box[1] - 20:
                return tag
        return None

    def press(tag):
        sb.reveal(shell, tag)
        return sb.click(c, tag)

    import gui_lint
    from core.ui import theme as T

    def lint(where):
        """gui_lint's rules on this page: edges, palette, text over controls, labels."""
        allowed, derived = gui_lint.palette()
        gui_lint.ISSUES.clear()
        gui_lint.lint_window(app, where, allowed, derived, set(T.MONO_FAMILIES) | set(T.ICON_FAMILIES))
        check(f"gui_lint finds nothing on the page: {where}", not gui_lint.ISSUES, gui_lint.ISSUES[:3])

    def answer(yes=True):
        def go():
            d = shell.dialog
            if d is None:
                root.after(100, go)
                return
            d.card.event_generate("<Return>" if yes else "<Escape>")
        root.after(250, go)

    try:
        # -- no library -------------------------------------------------------
        sb.click(shell.rail_c, "nav_dlss")
        check("the rail item 'dlss' opens the page (real click)", shell.page.name == "dlss",
              shell.page.name if shell.page else None)
        check("no library: says so and offers the way to the games",
              has("no games yet") and kit.find("go to games", "button") is not None, texts())
        shot("1-no-library")
        press(kit.find("go to games", "button"))
        check("...and its button goes to the library", shell.page.name == "library")

        # -- never scanned ------------------------------------------------------
        app.all_games = [A, B, C, D, E, F]
        with patch.object(ctl_dlss.DlssControl, "dlss_due", lambda self: False):
            sb.click(shell.rail_c, "nav_dlss")
        check("never scanned: a first check is offered",
              has("not checked yet") and kit.find("check my games", "button") is not None, texts())
        shot("2-never-scanned")

        # -- scanning -------------------------------------------------------------
        hold["scan"] = True
        gate.clear()
        press(kit.find("check my games", "button"))
        sb.until(root, lambda: has("reading "), 5)
        check("scanning: the page says what it is reading",
              app.dlss_scanning and has("reading your games...") and any(t.startswith("reading ") for t in texts()),
              texts())
        shot("3-scanning")
        hold["scan"] = False
        gate.set()
        sb.until(root, lambda: not app.dlss_scanning, 20)
        sb.pump(root, 0.3)

        # -- some behind, anti-cheat, running -------------------------------------
        rows = {r["g"].name: r for r in app.dlss_rows()}
        check("the scan lists the four 64-bit games that ship DLSS, not the 32-bit one or the one without",
              set(rows) == {"Alpha Unreal", "Bravo Current", "Charlie Online", "Delta Running"}, set(rows))
        check("...Alpha behind on two runtimes, Bravo current",
              len(rows["Alpha Unreal"]["behind"]) == 2 and not rows["Bravo Current"]["behind"])
        check("update all counts only what it may take: not the anti-cheat game, not the running one",
              kit.find("update all (1)", "button") is not None, [l for _t, _k, l in kit.controls("button")])
        check("versions shown as current -> newest",
              has("3.7.20") and has("  ->  310.9.1") and has("super resolution") and has("frame generation"))
        check("the anti-cheat game is named with its anti-cheat",
              has("easy anti-cheat") and has("Charlie Online"), texts())
        check("the running game says so, without the accent, and is not in update all",
              has("running: Delta.exe") and control_near("update", "Delta Running") is not None)
        check("the line under the title says what it does and the online caution",
              has("NVIDIA's newest dlss files for your games") and has("online games: keep their own."))
        check("the band says how many were checked and that 32-bit is not offered",
              has("6 games checked") and has("1 32-bit game skipped"), texts())
        shot("4-some-behind")
        lint("some behind")

        # -- the game page's line -----------------------------------------------------
        app.open_game(A)
        sb.pump(root, 0.5)
        link = next((t for t, k, lab in kit.controls("link") if lab.startswith("dlss 3.7.20 -> 310.9.1")), None)
        check("a game page with an older DLSS carries the line 'dlss 3.7.20 -> 310.9.1 ... update'",
              link is not None, [lab for _t, _k, lab in kit.controls("link")])
        shot("4b-game-page-line")

        # -- updating (from the game page's link) ---------------------------------------
        hold["download"] = True
        gate.clear()
        if link is not None:
            press(link)
        sb.until(root, lambda: app.dlss_pct >= 50, 5)
        check("the link goes to the dlss page and the update runs there, with progress",
              shell.page.name == "dlss" and app.dlss_job == str(A.folder)
              and has("50%"), (shell.page.name, app.dlss_job, app.dlss_pct))
        shot("5-updating")
        check("while it runs, the window's busy flag is up: an install into the same game waits",
              app.busy and app.action == "dlss")
        hold["download"] = False
        gate.set()
        sb.until(root, lambda: app.dlss_job is None, 20)
        sb.pump(root, 0.3)
        check("...and down again when it is done, with the page's action cleared", not app.busy and app.action == "")
        # a scan is not started under another job: settle renames files, a read must not
        app.busy = True
        app.dlss_scan(fresh=True)
        check("no read of the games starts while another job holds the busy flag", not app.dlss_scanning)
        app.busy = False
        # a handler that raises still ends the job
        _saved_state = app.dlss_state
        app.dlss_job, app.busy = "x", True
        app.dlss_state = lambda: (_ for _ in ()).throw(RuntimeError("drawing failed"))
        try:
            app._on_dlssdone(("x", "update", du.Report(), None))
        except RuntimeError:
            pass
        app.dlss_state = _saved_state
        check("an error while showing a job's answer still clears the job and the busy flag",
              app.dlss_job is None and not app.busy and app.action == "")
        sr = A.folder / nest / "nvngx_dlss.dll"
        check("updated: both files replaced, the originals beside them",
              du.pe.file_version(sr) == "310.9.1" and du._ours(sr).is_file()
              and du.pe.file_version(A.folder / "nvngx_dlssg.dll") == "310.9.1")
        check("...the row says what moved and offers the original back",
              has("updated") and has("game's own 3.7.20 kept")
              and control_near("restore original", "Alpha Unreal", "link"),
              texts())
        check("...and update all has nothing left", kit.find("update all", "button") is None)
        shot("6-updated")

        # -- anti-cheat: asks, names it; then a failure --------------------------------------
        cf = C.folder / "bin" / "nvngx_dlss.dll"
        os.chmod(cf, stat.S_IREAD)
        answer(yes=True)
        press(control_near("update", "Charlie Online"))
        sb.until(root, lambda: app.dlss_job is None and str(C.folder) in app.dlss_results, 20)
        sb.pump(root, 0.3)
        os.chmod(cf, stat.S_IREAD | stat.S_IWRITE)
        ok, text = app.dlss_results.get(str(C.folder), (True, ""))
        check("anti-cheat asked first; a file Windows refuses fails with where and why, nothing changed",
              not ok and "Windows refused to replace" in text and du.pe.file_version(cf) == "3.1.0"
              and not du._ours(cf).exists(), text)
        check("...and the failure is on the row", has("Windows refused to replace"), texts())
        shot("7-failed-anticheat")
        lint("failed, anti-cheat, updated")
        root.geometry(f"{int(1000 * args.scale)}x{int(700 * args.scale)}")
        sb.pump(root, 0.5)
        shell.redraw()
        sb.pump(root, 0.2)
        lint("a narrow window")
        shot("7b-narrow")
        root.geometry(f"{int(1400 * args.scale)}x{int(900 * args.scale)}")
        sb.pump(root, 0.5)
        shell.redraw()

        answer(yes=False)
        press(control_near("update", "Charlie Online"))
        sb.pump(root, 0.8)
        check("...saying no to the anti-cheat question changes nothing", du.pe.file_version(cf) == "3.1.0"
              and app.dlss_job is None)

        # -- restore ---------------------------------------------------------------------------
        press(control_near("restore original", "Alpha Unreal", "link"))
        sb.until(root, lambda: app.dlss_job is None and not du._ours(sr).exists(), 20)
        sb.pump(root, 0.3)
        check("restore original (real click) puts the game's own bytes back",
              du.pe.file_version(sr) == "3.7.20" and b"Alpha Unreal" in sr.read_bytes())

        # -- a running game: the update is refused with the reason, nothing changes -------------
        df = D.folder / "nvngx_dlss.dll"
        press(control_near("update", "Delta Running"))
        sb.until(root, lambda: app.dlss_job is None and str(D.folder) in app.dlss_results, 20)
        sb.pump(root, 0.3)
        check("pressing update on a running game says it is running and changes nothing",
              has("Delta.exe is running") and du.pe.file_version(df) == "2.5.1", texts())

        # -- the game page's line while a read of every game is due -----------------------------------
        app.dlss_state()["at"] = time.time() - 3 * 86400
        app.open_game(A)
        sb.pump(root, 0.5)
        link = next((t for t, k, lab in kit.controls("link") if lab.startswith("dlss 3.7.20 -> 310.9.1")), None)
        hold["scan"] = True
        gate.clear()
        if link is not None:
            press(link)
        sb.pump(root, 0.3)
        check("a day-old read: the game page's update waits for the new read instead of being dropped",
              link is not None and shell.page.name == "dlss" and app.dlss_scanning
              and app.dlss_pending == str(A.folder), (link, app.dlss_scanning, app.dlss_pending))
        hold["scan"] = False
        gate.set()
        sb.until(root, lambda: not app.dlss_scanning and app.dlss_job is None
                 and du.pe.file_version(sr) == "310.9.1", 20)
        sb.pump(root, 0.3)
        check("...and runs when the read ends", du.pe.file_version(sr) == "310.9.1" and app.dlss_pending is None)
        press(control_near("restore original", "Alpha Unreal", "link"))
        sb.until(root, lambda: app.dlss_job is None and not du._ours(sr).exists(), 20)

        # -- keys: Esc and Backspace go back ------------------------------------------------------
        shell.home()
        sb.click(shell.rail_c, "nav_dlss")
        sb.key(c, "Escape")
        check("Esc on the page goes back", shell.page.name == "library")
        sb.click(shell.rail_c, "nav_dlss")
        sb.key(c, "BackSpace")
        check("Backspace goes back", shell.page.name == "library")

        # -- update all: asks, takes the games it may, skips anti-cheat and running, sums up -----------
        J = game("Juliet Old", [("nvngx_dlss.dll", "2.4.0")])
        K = game("Kilo Old", [("nvngx_dlssg.dll", "3.5.0")])
        app.all_games = [J, K, C, D]
        shell.show("dlss")
        app.dlss_scan(fresh=True)
        sb.until(root, lambda: not app.dlss_scanning, 20)
        sb.pump(root, 0.3)
        answer(yes=True)
        press(kit.find("update all (2)", "button"))
        sb.until(root, lambda: app.dlss_job is None and app.dlss_batch is None
                 and du.pe.file_version(K.folder / "nvngx_dlssg.dll") == "310.9.1", 30)
        sb.pump(root, 0.3)
        check("update all (real click, confirmed): both games updated, anti-cheat and running untouched, "
              "and one line sums it up",
              du.pe.file_version(J.folder / "nvngx_dlss.dll") == "310.9.1"
              and du.pe.file_version(K.folder / "nvngx_dlssg.dll") == "310.9.1"
              and du.pe.file_version(cf) == "3.1.0" and du.pe.file_version(df) == "2.5.1"
              and shell.status_text == "dlss: 2 updated", shell.status_text)
        check("...rows keep their places after the update", [r["g"].name for r in app.dlss_rows()][:2]
              == ["Juliet Old", "Kilo Old"], [r["g"].name for r in app.dlss_rows()])

        # -- a partial failure says what went through ------------------------------------------------
        L = game("Lima Partial", [("nvngx_dlss.dll", "3.7.20"), ("nvngx_dlssg.dll", "3.5.0")])
        app.all_games = [L]
        app.dlss_scan(fresh=True)
        sb.until(root, lambda: not app.dlss_scanning, 20)
        lg = L.folder / "nvngx_dlssg.dll"
        os.chmod(lg, stat.S_IREAD)
        try:
            press(control_near("update", "Lima Partial"))
            sb.until(root, lambda: app.dlss_job is None and str(L.folder) in app.dlss_results, 20)
            sb.pump(root, 0.3)
        finally:
            os.chmod(lg, stat.S_IREAD | stat.S_IWRITE)
        ok, text = app.dlss_results.get(str(L.folder), (True, ""))
        check("one runtime updated and the next refused: the row says both, not 'nothing was changed'",
              not ok and text.startswith("super resolution updated;") and "Windows refused" in text
              and "Nothing was changed" not in text
              and du.pe.file_version(L.folder / "nvngx_dlss.dll") == "310.9.1", text)
        check("...and the reason keeps its second line on screen", has("super resolution updated;")
              and any("Windows refused" in t or "read-only" in t or "launcher" in t for t in texts()), texts())
        shot("7c-partial")

        # -- all current, reached by the day-old read that opening the page starts -------------------
        app.all_games = [B]
        app.dlss_state()["at"] = time.time() - 3 * 86400
        shell.home()
        sb.click(shell.rail_c, "nav_dlss")
        sb.until(root, lambda: not app.dlss_scanning, 20)
        sb.pump(root, 0.3)
        check("all current: opening the page after a day read the games again; said once, no update button",
              has("every game is on NVIDIA's newest") and kit.find("update", "button") is None
              and time.time() - app.dlss_state()["at"] < 60, texts())
        shot("8-all-current")

        # -- nothing found ----------------------------------------------------------------------------
        app.all_games = [F, E]
        app.dlss_scan(fresh=True)
        sb.until(root, lambda: not app.dlss_scanning, 20)
        sb.pump(root, 0.3)
        check("nothing found: one line says how many were read, the 32-bit one counted apart",
              has("none of the 1 game checked ship NVIDIA DLSS files") and has("1 32-bit game skipped"),
              texts())
        shot("9-nothing-found")
        app.all_games = [E]
        app.dlss_scan(fresh=True)
        sb.until(root, lambda: not app.dlss_scanning, 20)
        sb.pump(root, 0.3)
        check("...and a library of 32-bit games only says that", has("no 64-bit games in the library"), texts())

        # -- the newest builds never read (offline, no earlier read) ------------------------------------
        app.dlss_data = {}
        du._NEWEST = {}
        app.all_games = [A]
        with patch.object(sources, "nvidia_dlss", lambda: {}):
            app.dlss_scan(fresh=True)
            sb.until(root, lambda: not app.dlss_scanning, 20)
        sb.pump(root, 0.3)
        check("newest never read: says so, shows versions as found, claims neither 'newest' nor 'game settings'",
              has("the newest builds could not be read") and has("3.7.20")
              and "newest" not in texts()
              and control_near("game settings", "Alpha Unreal", "link") is None, texts())
        du._NEWEST = {}

        # -- copies, and a runtime the DLSS 5 install owns ---------------------------------------
        G = game("Golf Storage", [("_storage_/nvngx_dlss.dll", "310.3.0"), ("nvngx_dlss.dll", "310.3.0")])
        H = game("Hotel Installed", [("nvngx_dlss.dll", "310.8.0"),
                                     ("nvngx_dlss.dll" + installer.BACKUP_SUFFIX, "3.7.20")])
        (H.folder / installer.MANIFEST).write_text(json.dumps(
            {"files": ["nvngx_dlss.dll", "nvngx_dlss.dll" + installer.BACKUP_SUFFIX], "complete": True}),
            encoding="utf8")
        app.all_games = [G, H]
        app.dlss_scan(fresh=True)
        sb.until(root, lambda: not app.dlss_scanning, 20)
        sb.pump(root, 0.3)
        check("two copies of one build are one line that says so",
              has("2 copies") and sum(1 for t in texts() if t == "super resolution") == 2, texts())
        check("a runtime the DLSS 5 install swapped is not offered here and points to the game's settings",
              has("set by the dlss 5 install") and control_near("game settings", "Hotel Installed", "link")
              and control_near("update", "Hotel Installed") is None, texts())
        shot("10-copies-and-install")
        press(control_near("game settings", "Hotel Installed", "link"))
        check("...and that link opens the game", shell.page.name == "game" and app.game is H)
        shell.show("dlss")

        # -- offline, from the cache --------------------------------------------------------------------
        app.all_games = [A, B, C, D, E, F]
        du._NEWEST = {}
        with patch.object(sources, "nvidia_dlss", lambda: {}):
            app.dlss_scan(fresh=True)
            sb.until(root, lambda: not app.dlss_scanning, 20)
        sb.pump(root, 0.3)
        cache = json.loads(du.cache_path().read_text(encoding="utf8"))
        check("offline: the rows are read, the newest builds are the last known, and it says so",
              has("offline: newest from an earlier check") and cache.get("newest", {}).get("dlss", {}).get("version")
              == "310.9.1" and str(du.cache_path()).startswith(str(sb.dir)), texts())
        check("nothing of the page's was written outside the sandbox",
              str(du.cache_path().parent) == str(sb.dir))
        check("no Tk callback raised", not sb.errors, sb.errors[:1])
    finally:
        gate.set()
        for p in patches:
            p.stop()
        sb.destroy(root)
        sb.close()
    print()
    print("FAILED:", FAILS or "none")
    return 1 if FAILS else 0


def _enabled(kit, tag) -> bool:
    """A button's enabled flag, read from what it draws (a disabled one has DIM ink)."""
    from core.ui import theme as T
    c = kit.c
    for i in c.find_withtag(tag):
        if c.type(i) == "text" and c.itemcget(i, "fill") == T.DIM:
            return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
