"""Offline scan regressions: python -m unittest -v test_scan."""
import json
import queue
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from core import dlss, games, gui, installer, log


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = tempfile.TemporaryDirectory(prefix="autopilot_scan_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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
        app._sm = Mock(return_value=120)
        app._check_stale = Mock()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(gui.video, "known", return_value=None))
        stack.enter_context(patch.object(log, "crashed", return_value=False))
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

        def slow_detect(*_args):
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


if __name__ == "__main__":
    unittest.main()
