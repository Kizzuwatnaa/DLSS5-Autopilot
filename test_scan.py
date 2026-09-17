"""Offline scan regressions: python -m unittest -v test_scan."""
import json
import queue
import struct
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from core import dlss, games, installer, library, log, net, pe, prefs, watch
from core.lookout import Lookout
from core.ui import app as uiapp
from core.ui import ctl_library, kit
from core.ui.ctl_game import GameControl
from core.ui.ctl_library import LibraryControl


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = tempfile.TemporaryDirectory(prefix="autopilot_scan_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.logs = self.stack.enter_context(patch.object(log, "write"))

    def test_epic_ignores_non_applications_but_keeps_older_manifests(self):
        manifests = self.root / "Epic/EpicGamesLauncher/Data/Manifests"
        manifests.mkdir(parents=True)
        entries = (
            ("Quixel Bridge", False, False),
            ("Unreal Engine", False, True),
            ("Content pack", True, False),
            ("Game", True, True),
            ("Older game", None, None),
        )
        for i, (name, application, executable) in enumerate(entries):
            folder = self.root / name
            folder.mkdir()
            (folder / "Game.exe").touch()
            data = {"DisplayName": name, "InstallLocation": str(folder),
                    "LaunchExecutable": "Game.exe"}
            if application is not None:
                data.update(bIsApplication=application, bIsExecutable=executable)
            (manifests / f"{i}.item").write_text(json.dumps(data), encoding="utf8")
        with patch.dict("os.environ", {"PROGRAMDATA": str(self.root)}):
            found = games.scan_epic()
        self.assertEqual({g.name for g in found}, {"Game", "Older game"})
        self.assertTrue(all(g.exe.name == "Game.exe" for g in found))

    def test_steam_leftovers_without_a_manifest_are_not_games(self):
        """Steam deletes the appmanifest on uninstall and leaves the folder.

        On the developer's own machine that is 46 of 63 entries - saves,
        shader caches, empty folders - every one of them offered as a game
        to install into.
        """
        lib = self.root / "SteamLibrary"
        apps = lib / "steamapps"
        common = apps / "common"
        common.mkdir(parents=True)
        (apps / "appmanifest_1.acf").write_text(
            '"AppState"{"name" "Real Game" "installdir" "RealGame" '
            '"StateFlags" "4"}', encoding="utf8")
        # Queued or still downloading: Steam writes the manifest and creates
        # the folder before there is anything in it (Battlefield 6 sat in
        # the list as an empty folder with StateFlags 1042).
        (apps / "appmanifest_2.acf").write_text(
            '"AppState"{"name" "Downloading Game" "installdir" "Downloading" '
            '"StateFlags" "1042"}', encoding="utf8")
        (common / "Downloading").mkdir()
        (common / "RealGame").mkdir()
        (common / "RealGame" / "RealGame.exe").write_bytes(b"MZ")
        # uninstalled: manifest gone, folder and its shader cache left
        (common / "GhostGame" / "ShaderCache").mkdir(parents=True)
        # no manifest, but the executable is still there
        (common / "SideloadedGame").mkdir()
        (common / "SideloadedGame" / "Game.exe").write_bytes(b"MZ")
        # uninstalled, but this tool installed into it: keep it, or there is
        # no way left to remove what we wrote
        ours = common / "OldInstall"
        ours.mkdir()
        (ours / "dlss5-autopilot.json").write_text("{}", encoding="utf8")

        with patch.object(games, "_steam_root", return_value=lib),                 patch.object(games, "_steam_libraries", return_value=[lib]):
            found = {g.name: g for g in games.scan_steam()}

        self.assertIn("Real Game", found)
        self.assertIn("SideloadedGame", found)
        self.assertIn("OldInstall", found)
        self.assertNotIn("GhostGame", found)
        self.assertNotIn("Downloading Game", found)
        self.assertNotIn("Downloading", found)

    def test_has_exe_is_one_listing_and_never_raises(self):
        d = self.root / "probe"
        d.mkdir()
        self.assertFalse(games._has_exe(d))
        (d / "a.exe").write_bytes(b"MZ")
        self.assertTrue(games._has_exe(d))
        # unreadable folders stay in the list rather than vanishing
        self.assertTrue(games._has_exe(self.root / "does-not-exist"))

    def test_progress_identifies_every_game_including_last_two(self):
        found = [games.Game(f"Game {i}", self.root / str(i)) for i in range(1, 98)]
        messages = []
        with ExitStack() as stack:
            for name in ("steam", "epic", "gog", "ea", "ubisoft", "battlenet",
                         "rockstar", "amazon", "itch", "heroic", "xbox", "folders",
                         "emulators"):
                stack.enter_context(patch.object(
                    games, f"scan_{name}", return_value=found if name == "steam" else []))
            enrich = stack.enter_context(patch.object(games, "enrich", side_effect=lambda g: g))
            result = games.scan_all(messages.append)
        self.assertEqual(len(result), 97)
        self.assertEqual(enrich.call_count, 97)
        inspecting = [m for m in messages if m.startswith("Inspecting games")]
        self.assertEqual(len(inspecting), 97)
        self.assertEqual(inspecting[-2:], ["Inspecting games... 96/97: Game 96",
                                          "Inspecting games... 97/97: Game 97"])

    def test_runtime_walk_keeps_nested_evidence_and_excludes_our_runtime(self):
        (self.root / "nvngx_dlss.dll").touch()
        engine = self.root / "Engine/Plugins/Runtime/Nvidia/DLSS/Binaries/ThirdParty/Win64"
        engine.mkdir(parents=True)
        runtime = engine / "NVNGX_DLSS.DLL"
        runtime.touch()
        # Content trees are deliberately pruned even if they contain a match.
        (self.root / "Content").mkdir()
        (self.root / "Content/nvngx_dlss.dll").touch()
        found = dlss.find_dlss_files(self.root, skip_dir=self.root)
        self.assertEqual(found, [str(runtime.relative_to(self.root))])

    def test_upscaler_walk_excludes_optiscalers_bundled_files(self):
        (self.root / "OptiScaler").mkdir()
        (self.root / "OptiScaler/libxess.dll").touch()
        (self.root / "bin").mkdir()
        runtime = self.root / "bin/ffx_fsr2_api_x64.dll"
        runtime.touch()
        found = dlss.find_upscaler_files(self.root, skip_dir=self.root)
        self.assertEqual(found, [str(runtime.relative_to(self.root))])

    def test_runtime_walk_stops_without_matches(self):
        visited = []

        def wide_tree(_folder):
            for i in range(100):
                visited.append(i)
                yield str(self.root / str(i)), [], []

        with patch("os.walk", side_effect=wide_tree), patch.object(dlss, "_WALK_DIRS", 3):
            self.assertEqual(dlss.find_dlss_files(self.root), [])
        self.assertEqual(len(visited), 4)
        self.assertIn("search budget reached", self.logs.call_args.args[0])

    def test_runtime_deadline_applies_even_when_install_folder_is_skipped(self):
        tree = [(str(self.root), ["bin"], []),
                (str(self.root / "bin"), [], ["nvngx_dlss.dll"])]
        with patch("os.walk", return_value=iter(tree)), \
                patch.object(dlss.time, "monotonic", side_effect=[0, 0, 10]):
            self.assertEqual(dlss.find_dlss_files(self.root, skip_dir=self.root), [])
        self.assertIn("search budget reached", self.logs.call_args.args[0])

    def test_runtime_budget_keeps_evidence_already_found(self):
        tree = [(str(self.root), [], ["nvngx_dlss.dll"]),
                (str(self.root / "bin"), [], ["nvngx_dlssg.dll"])]
        with patch("os.walk", return_value=iter(tree)), patch.object(dlss, "_WALK_DIRS", 1):
            self.assertEqual(dlss.find_dlss_files(self.root), ["nvngx_dlss.dll"])

    def test_runtime_walk_resolves_roots_only(self):
        for i in range(10):
            (self.root / str(i)).mkdir()
        resolve = Path.resolve
        with patch.object(Path, "resolve", autospec=True, side_effect=resolve) as resolved:
            dlss.find_dlss_files(self.root, skip_dir=self.root)
        self.assertEqual(resolved.call_count, 2)

    def make_app(self):
        # The real library controller, worker and queue pump of the 2.0
        # window, without a display, hardware probes, downloads or the
        # user's settings: only the drawing (shell) is a double.
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(prefs, "FILE", self.root / "settings.json"))
        # The real worker saves the library; this machine's saved library is
        # not the test's to overwrite.
        stack.enter_context(patch.object(library, "FILE", self.root / "library.json"))
        stack.enter_context(patch.object(ctl_library.video, "known", return_value=None))
        stack.enter_context(patch.object(log, "crashed", return_value=False))
        app = uiapp.App.__new__(uiapp.App)
        app.q = queue.Queue()
        app.root = Mock()
        app.busy = False
        app._crash_shown = False
        app._library_init()
        app.sm = 120
        app.shell = Mock()
        app.refresh = Mock()
        app.check_stale = Mock()
        app.watch_refresh = Mock()
        return app

    def test_scan_checks_compatibility_off_ui_thread_and_renders_cached_rows(self):
        app = self.make_app()
        game = games.Game("Game", self.root, exe=self.root / "Game.exe",
                          bitness=64, api="DX12")
        entered, release = threading.Event(), threading.Event()
        ui_thread = threading.get_ident()
        worker_ids = []
        workers = []
        thread_class = threading.Thread

        def thread_factory(*args, **kwargs):
            thread = thread_class(*args, **kwargs)
            workers.append(thread)
            return thread

        # **_kw: the window passes the driver in by keyword, and a double
        # that only took positionals turned 'runs off the UI thread' into a
        # dead worker and a two-second timeout.
        def slow_detect(*_args, **_kw):
            worker_ids.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release the detector")
            return dlss.Support(recommended=dlss.FEEDER)

        with ExitStack() as stack:
            stack.enter_context(patch.object(games, "scan_all", return_value=[game]))
            stack.enter_context(patch.object(ctl_library.threading, "Thread", side_effect=thread_factory))
            detect = stack.enter_context(patch.object(dlss, "detect", side_effect=slow_detect))
            try:
                app.scan(full=True)
                self.assertTrue(entered.wait(2))
                # Tk can still pump progress while detection is blocked.
                app._pump()
                self.assertTrue(app.scanning)
                app.shell.busy.assert_called_with("checking compatibility 1/1: game")
                self.assertEqual(app.card(game)["status"], "reading...")
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            app._pump()
            # Filtering, counting and redrawing the cards use the rows the
            # scan prepared; none of them reads the game folder again.
            for _ in range(2):
                self.assertEqual(app.visible(), [game])
                app.counts()
                card = app.card(game)
            self.assertEqual(detect.call_count, 1)
        self.assertEqual(len(worker_ids), 1)
        self.assertNotEqual(worker_ids[0], ui_thread)
        self.assertFalse(app.scanning)
        self.assertFalse(app.busy)
        self.assertEqual(card["kind"], "ready")
        self.assertNotIn(card["status"], ("reading...", "unreadable", "unsupported"))
        app.shell.status.assert_called_with("scan complete: 1 games")
        app.shell.busy.assert_called_with("")

    def test_unreadable_games_do_not_trigger_another_folder_search(self):
        g = games.Game("Locked game", self.root, exe=self.root / "Game.exe",
                       error=games.XBOX_HINT)
        with patch.object(dlss, "detect") as detect:
            row = LibraryControl.inspect_row(g, 120)
        detect.assert_not_called()
        self.assertFalse(row[0])
        self.assertEqual(installer.check_supported(g), (False, games.XBOX_HINT))

    def test_row_failure_does_not_discard_other_scan_results_or_manual_games(self):
        app = self.make_app()
        bad = games.Game("Unreadable", self.root, exe=self.root / "Bad.exe", bitness=64)
        manual = games.Game("Chosen folder", self.root / "manual", exe=self.root / "manual/Game.exe")
        app.all_games = [manual]
        with patch.object(dlss, "detect", side_effect=OSError("drive removed")), \
                patch.object(log, "exception"):
            failed_row = LibraryControl.inspect_row(bad, 120)
        self.assertIs(failed_row, False)
        app.q.put(("scanned", ([bad], {(str(bad.folder), str(bad.exe)): failed_row})))
        app._pump()
        self.assertEqual(app.all_games, [manual, bad])
        app._read_rows = Mock()      # the manual game's row would be read on a worker
        self.assertEqual(app.visible(), [manual, bad])
        self.assertEqual(app.card(bad)["status"], "unreadable")
        self.assertEqual(app.card(manual)["status"], "reading...")
        app.shell.status.assert_called_with("scan complete: 2 games")
        self.assertFalse(app.scanning)
        self.assertFalse(app.busy)


class ProtectedXboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autopilot_xbox_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "XboxGames/Fake/Content"
        self.folder.mkdir(parents=True)
        self.exe = self.folder / "Game.exe"
        # A real minimal x64 COFF header, not a mocked architecture result.
        data = bytearray(128)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3c, 64)
        data[64:70] = b"PE\0\0" + struct.pack("<H", pe.PE_X64)
        self.exe.write_bytes(data)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(prefs, "FILE", self.root / "settings.json"))
        self.stack.enter_context(patch.object(library, "FILE", self.root / "library.json"))
        self.stack.enter_context(patch.object(log, "write"))
        self.stack.enter_context(patch.object(installer, "_running_processes", return_value=set()))

    def protect(self):
        read = open

        def guarded(path, mode="r", *args, **kwargs):
            if Path(path) == self.exe and mode == "rb":
                raise PermissionError(13, "Permission denied", str(path))
            return read(path, mode, *args, **kwargs)

        self.stack.enter_context(patch("builtins.open", side_effect=guarded))

    def test_readable_x64_ignores_architecture_override_and_clears_stale_metadata(self):
        games.set_bitness_override(self.folder, 32)
        g = games.manual(self.folder)
        self.assertEqual(g.bitness, 64)
        self.assertFalse(g.error or g.exe_warning)
        self.assertTrue(installer.check_supported(g)[0])
        g.error, g.exe_warning = "old error", games.XBOX_EXE_HINT
        g.bitness, g.api = 32, "DX9"
        games.enrich(g)
        self.assertFalse(g.error or g.exe_warning)
        self.assertEqual((g.bitness, g.api), (64, pe.detect_api(self.exe)[0]))

    def test_protected_executable_needs_both_overrides_to_be_supported(self):
        self.protect()
        g = games.manual(self.folder)
        self.assertEqual(g.exe, self.exe)
        self.assertEqual(g.exe_warning, games.XBOX_EXE_HINT)
        self.assertFalse(g.error)
        self.assertIsNone(g.bitness)
        ok, why = installer.check_supported(g)
        self.assertFalse(ok)
        # The controls are named as the window spells them, lower case.
        self.assertIn("'architecture'", why)
        self.assertIn("'graphics api'", why)
        games.set_bitness_override(self.folder, 64)
        games.enrich(g)
        self.assertFalse(installer.check_supported(g)[0], "unknown API must not be assumed")
        games.set_api_override(self.folder, "DX12")
        games.enrich(g)
        self.assertEqual((g.bitness, g.api), (64, "DX12"))
        self.assertTrue(installer.check_supported(g)[0])
        self.assertIn(games.XBOX_EXE_CHOSEN, installer.preview(g, installer.Options()).warnings)

    def test_api_detection_can_use_adjacent_files(self):
        self.protect()
        games.set_bitness_override(self.folder, 64)
        sdk = self.folder / "D3D12/D3D12Core.dll"
        sdk.parent.mkdir()
        sdk.touch()
        g = games.manual(self.folder)
        self.assertEqual((g.bitness, g.api), (64, "DX12"))
        self.assertIn("Agility", g.api_why)
        self.assertTrue(installer.check_supported(g)[0])

    def test_preflight_writes_beside_protected_exe_and_preserves_existing_file(self):
        self.protect()
        existing = self.folder / ".dlss5-autopilot-write-test"
        existing.write_bytes(b"keep me")
        before = set(self.folder.iterdir())
        installer.preflight(games.manual(self.folder))
        self.assertEqual(set(self.folder.iterdir()), before)
        self.assertEqual(existing.read_bytes(), b"keep me")

    def test_denied_directory_blocks_preflight_and_install(self):
        self.protect()
        games.set_bitness_override(self.folder, 64)
        games.set_api_override(self.folder, "DX12")
        g = games.manual(self.folder)
        before = set(self.folder.iterdir())
        with patch.object(installer.tempfile, "NamedTemporaryFile",
                          side_effect=PermissionError(13, "Permission denied")):
            for action in (lambda: installer.preflight(g),
                           lambda: installer.install(g, installer.Options())):
                with self.assertRaises(installer.InstallError) as cm:
                    action()
                self.assertIn(str(g.install_dir), str(cm.exception))
                self.assertIn("Enable mods", str(cm.exception))
                # the reason is said in words; the raw exception goes to the log
                self.assertNotIn("Permission denied", str(cm.exception))
        self.assertEqual(set(self.folder.iterdir()), before)

    def test_invalid_architecture_overrides_are_rejected_and_ignored(self):
        for value in (0, 16, 128, "64", 64.0, True, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    games.set_bitness_override(self.folder, value)
                prefs.set_("bitness_override", {str(self.folder).lower(): value})
                self.assertIsNone(games.bitness_override(self.folder))
        prefs.set_("bitness_override", [64])
        self.assertIsNone(games.bitness_override(self.folder))

    def test_preferences_and_cache_round_trip_and_override_removal(self):
        self.protect()
        games.set_bitness_override(self.folder, 64)
        games.set_api_override(self.folder, "DX12")
        g = games.manual(self.folder)
        key = (str(g.folder), str(g.exe))
        library.save([g], {key: LibraryControl.inspect_row(g, 89)}, "test", 89)
        cached, rows, changed = library.load("test", 89)
        self.assertEqual((cached[0].bitness, cached[0].api), (64, "DX12"))
        self.assertEqual(cached[0].exe_warning, games.XBOX_EXE_HINT)
        self.assertFalse(cached[0].error)
        self.assertEqual(changed, cached)
        self.assertNotIn(key, rows)
        games.set_bitness_override(self.folder, None)
        games.set_api_override(self.folder, None)
        cached, _, _ = library.load("test", 89)
        self.assertIsNone(cached[0].bitness)
        self.assertEqual(cached[0].api, g.api_detected)
        self.assertFalse(installer.check_supported(cached[0])[0])

    def test_non_xbox_permission_error_stays_fatal_despite_overrides(self):
        plain = self.root / "Plain"
        plain.mkdir()
        self.exe = self.exe.rename(plain / "Game.exe")
        self.protect()
        games.set_bitness_override(plain, 64)
        games.set_api_override(plain, "DX12")
        g = games.manual(plain)
        self.assertTrue(g.error)
        self.assertFalse(g.exe_warning)
        self.assertFalse(installer.check_supported(g)[0])

    def test_corrupt_readable_xbox_exe_stays_fatal(self):
        self.exe.write_bytes(b"not a PE")
        games.set_bitness_override(self.folder, 64)
        games.set_api_override(self.folder, "DX12")
        g = games.manual(self.folder)
        self.assertTrue(g.error)
        self.assertFalse(g.exe_warning)
        self.assertFalse(installer.check_supported(g)[0])

    def test_window_asks_both_choices_before_install_and_relocks_when_cleared(self):
        """The 2.0 window, driven with real clicks: a protected Xbox exe shows
        'architecture' and 'graphics api' in its settings, the install button
        stays disabled (entry["ok"]) until both are chosen, no click or key
        gets past it, and clearing a choice locks it again (#157)."""
        import time
        import tkinter as tk
        self.protect()
        self.stack.enter_context(patch.object(watch, "RECORD", self.root / "sightings.json"))
        self.stack.enter_context(patch.object(log, "FILE", self.root / "autopilot.log"))
        self.stack.enter_context(patch.object(log, "exception"))
        self.stack.enter_context(patch.object(net, "CACHE", self.root / "cache"))
        self.stack.enter_context(patch("urllib.request.urlopen", side_effect=OSError("offline test")))
        self.stack.enter_context(patch("webbrowser.open"))
        for owner, name in ((uiapp.App, "check_update"), (LibraryControl, "load_board"),
                            (LibraryControl, "load_shared"), (GameControl, "load_catalog"),
                            (Lookout, "start")):
            self.stack.enter_context(patch.object(owner, name, lambda *_a, **_k: None))
        made = []
        real_button = kit.Kit.button

        def record_button(k, *a, **kw):
            b = real_button(k, *a, **kw)
            made.append(b)
            return b
        self.stack.enter_context(patch.object(kit.Kit, "button", record_button))
        install = self.stack.enter_context(patch.object(
            installer, "install", side_effect=installer.InstallError("the test stops here")))

        root = tk.Tk()
        self.addCleanup(root.destroy)
        failures = []
        root.report_callback_exception = lambda *args: failures.append(args)
        try:
            root.attributes("-alpha", 0.0)      # mapped, so events land, but not shown
        except tk.TclError:
            pass
        app = uiapp.App(root)
        root.state("normal")
        root.geometry("1400x900+10+10")
        shell, c, k = app.shell, app.shell.content, app.shell.kit

        def pump(seconds=0.25):
            end = time.monotonic() + seconds
            while True:
                root.update()
                if time.monotonic() >= end:
                    return
                time.sleep(0.01)

        def until(cond, seconds=15.0):
            end = time.monotonic() + seconds
            while time.monotonic() < end and not cond():
                pump(0.02)
            return cond()

        def click(tag, canvas=c):
            box = canvas.bbox(tag)
            self.assertTrue(box, f"{tag} is not drawn")
            top, bottom = canvas.canvasy(0), canvas.canvasy(canvas.winfo_height())
            if box[1] < top or box[3] > bottom:
                shell.scroll_to(max(0, box[1] - 120))
                pump(0.05)
                box = canvas.bbox(tag)
            x = int((box[0] + box[2]) / 2 - canvas.canvasx(0))
            y = int((box[1] + box[3]) / 2 - canvas.canvasy(0))
            for seq in ("<Motion>", "<Button-1>", "<ButtonRelease-1>"):
                canvas.event_generate(seq, x=x, y=y, when="now")
            pump()

        def button(label):
            tag = k.find(label, "button")
            return tag, next((b for b in reversed(made) if b.tag == tag), None)

        def choose(label, index):
            tag = k.find(label, "dropdown")
            self.assertIsNotNone(tag, f"no '{label}' dropdown on the page")
            click(tag)
            menu = k.top()
            self.assertTrue(menu is not None and menu.tag.startswith("menu"), f"'{label}' opened no menu")
            click(f"{menu.tag}r{index}")
            self.assertTrue(until(lambda: not app.entering and app.support is not None),
                            "the game was not read again after the choice")
            pump(0.2)

        def locked():
            tag, b = button("install")
            return b is not None and not b.enabled and not app.entry["ok"][0]

        pump(0.5)
        g = games.manual(self.folder)
        app.all_games = [g]
        shell.redraw()
        self.assertTrue(until(lambda: app.card(g)["kind"] != "reading"))
        self.assertEqual(app.card(g)["status"], "choose architecture / api")
        shell.redraw()
        pump()
        click("card0")
        self.assertEqual(shell.page.name, "game")
        self.assertTrue(until(lambda: not app.entering and app.support is not None))
        pump(0.3)
        self.assertTrue(app.game_page.settings_open, "the settings did not open for the choices")
        self.assertIsNotNone(k.find("architecture", "dropdown"))
        self.assertIsNotNone(k.find("graphics api", "dropdown"))
        self.assertTrue(locked(), "install is possible before anything was chosen")

        # no click, double-click or key gets past the disabled button
        tag, _b = button("install")
        click(tag)
        box = c.bbox(tag)
        x, y = int((box[0] + box[2]) / 2 - c.canvasx(0)), int((box[1] + box[3]) / 2 - c.canvasy(0))
        for _ in range(2):          # a double click is two presses in a row
            c.event_generate("<Button-1>", x=x, y=y, when="now")
            c.event_generate("<ButtonRelease-1>", x=x, y=y, when="now")
        for keysym in ("Return", "space", "KP_Enter"):
            c.focus_force()
            root.event_generate(f"<{keysym}>", when="now")
        pump()
        install.assert_not_called()
        self.assertFalse(app.busy)
        self.assertEqual(app.action, "")

        choose("architecture", 1)                       # 64-bit
        self.assertEqual(g.bitness, 64)
        self.assertTrue(locked(), "one choice of two unlocked the install")
        choose("graphics api", games.APIS.index("DX12") + 1)
        self.assertEqual(g.api, "DX12")
        self.assertTrue(installer.check_supported(g)[0])
        tag, b = button("install")
        self.assertTrue(b is not None and b.enabled and app.entry["ok"][0],
                        "both chosen and install is still disabled")
        self.assertTrue(until(lambda: app.card(g)["kind"] != "reading"))
        self.assertEqual(app.card(g)["kind"], "ready")
        self.assertNotEqual(app.card(g)["status"], "choose architecture / api")
        click(tag)
        self.assertTrue(until(lambda: install.call_count == 1 and not app.busy))
        pump(0.2)

        choose("architecture", 0)                       # not set
        self.assertIsNone(g.bitness)
        self.assertTrue(locked(), "clearing a choice left install enabled")
        tag, _b = button("install")
        click(tag)
        self.assertEqual(install.call_count, 1)

        # a readable executable has nothing to choose
        g.exe_warning = ""
        g.bitness = 64
        shell.redraw()
        pump()
        self.assertIsNone(k.find("architecture", "dropdown"))
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
