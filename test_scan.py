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

from core import dlss, games, gui, installer, library, log, pe, prefs


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
        # Exercise the real worker, queue pump and row rendering without a
        # display, hardware probes, downloads or the user's settings.
        app = gui.App.__new__(gui.App)
        app.q = queue.Queue()
        app.root = Mock()
        app.busy = False
        app._rows = {}
        app._recheck = set()   # games a worker is re-reading; _fill skips those
        app._fill_job = None
        app._crash_shown = False
        app.all_games = []
        app.stale = {}
        app.step = 2
        app.arch = Mock(get=Mock(return_value="all"))
        app.search = Mock(get=Mock(return_value=""))
        app.only_installed = None
        app.tree = Mock(get_children=Mock(return_value=()))
        app.scanlbl = Mock()
        app.btn_next = Mock()
        app.status = Mock()
        app.detail = Mock()
        app.protected_details = Mock()
        app._sm = Mock(return_value=120)
        app._check_stale = Mock()
        app.rail_rows = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(gui.video, "known", return_value=None))
        stack.enter_context(patch.object(log, "crashed", return_value=False))
        # The real worker saves the library; this machine's saved library is
        # not the test's to overwrite.
        stack.enter_context(patch.object(gui.library, "FILE",
                                         self.root / "library.json"))
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

        # **_kw: the window passes the driver in by keyword now, and a
        # double that only took positionals turned 'runs off the UI
        # thread' into a dead worker and a two-second timeout.
        def slow_detect(*_args, **_kw):
            worker_ids.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release the detector")
            return dlss.Support(recommended=dlss.FEEDER)

        with patch.object(games, "scan_all", return_value=[game]), \
                patch.object(gui.threading, "Thread", side_effect=thread_factory), \
                patch.object(dlss, "detect", side_effect=slow_detect) as detect:
            try:
                app._scan()
                self.assertTrue(entered.wait(2))
                # Tk can still pump progress while detection is blocked.
                app._pump()
                self.assertTrue(app.busy)
                app.scanlbl.config.assert_called_with(text="checking compatibility... 1/1: game")
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            app._pump()
            app._fill()  # Filtering/refilling must use the same prepared rows.
            self.assertEqual(detect.call_count, 1)
        self.assertEqual(len(worker_ids), 1)
        self.assertNotEqual(worker_ids[0], ui_thread)
        self.assertFalse(app.busy)
        self.assertEqual(app.shown, [game])
        self.assertEqual(app.tree.insert.call_args.kwargs["values"][-1], "ready")
        app.status.config.assert_called_with(text="scan complete")

    def test_unreadable_games_do_not_trigger_another_folder_search(self):
        g = games.Game("Locked game", self.root, exe=self.root / "Game.exe",
                       error=games.XBOX_HINT)
        with patch.object(dlss, "detect") as detect:
            row = gui.App._inspect_row(g, 120)
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
            failed_row = gui.App._inspect_row(bad, 120)
        app.q.put(("scanned", ([bad], {(str(bad.folder), str(bad.exe)): failed_row})))
        app._pump()
        self.assertEqual(app.all_games, [manual, bad])
        self.assertEqual(app.tree.insert.call_count, 2)
        self.assertEqual(app.tree.insert.call_args.kwargs["values"][-1], "unreadable")
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
        library.save([g], {key: gui.App._inspect_row(g, 89)}, "test", 89)
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

    def test_gui_metadata_choices_refresh_status_and_navigation(self):
        self.protect()
        with patch.object(gui.App, "_check_update"):
            root = gui.tk.Tk()
            self.addCleanup(root.destroy)
            root.withdraw()
            app = gui.App(root)
        failures = []
        root.report_callback_exception = lambda *args: failures.append(args)
        g = games.manual(self.folder)
        app.all_games = [g]
        app._show(2)
        app._fill()
        app.tree.selection_set("0")
        app._on_pick()
        self.assertEqual(app.protected_details.winfo_manager(), "pack")
        self.assertEqual(app.tree.item("0", "values")[-1], "choose architecture / api")
        app._next()   # double-click cannot bypass the disabled button
        self.assertEqual(app.step, 2)
        app.cb_bitness.current(1)
        app.cb_bitness.event_generate("<<ComboboxSelected>>")
        self.assertEqual(g.bitness, 64)
        self.assertEqual(str(app.btn_next["state"]), "disabled")
        app.cb_protected_api.current(games.APIS.index("DX12") + 1)
        app.cb_protected_api.event_generate("<<ComboboxSelected>>")
        self.assertEqual(g.api, "DX12")
        self.assertEqual(app.tree.item("0", "values")[-1], "ready")
        self.assertEqual(str(app.btn_next["state"]), "normal")
        with patch.object(app, "_enter_install") as enter:
            app._next()
            enter.assert_called_once()
        self.assertEqual(app.step, 3)
        app._show(2)
        app.cb_bitness.current(0)
        app.cb_bitness.event_generate("<<ComboboxSelected>>")
        self.assertIsNone(g.bitness)
        self.assertFalse(app._can_install_page())
        g.exe_warning = ""
        g.bitness = 64
        app._on_pick()
        self.assertEqual(app.protected_details.winfo_manager(), "")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
