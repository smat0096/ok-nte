import unittest
from unittest.mock import Mock, patch

from ok import TaskDisabledException

from src import GAME_EXE, LAUNCHER_EXE
from src.tasks.LauncherTask import LauncherTask


class TestLauncherTask(unittest.TestCase):
    def _make_task(self):
        task = object.__new__(LauncherTask)
        task.log_info = Mock()
        task.log_warning = Mock()
        task.log_info_gated = Mock()
        task.sleep = Mock()
        task.capture_config = Mock()
        task.capture_config.GAME_CAPTURE_CONFIG = {"windows": {"hwnd_class": "UnrealWindow"}}
        task.capture_config.LAUNCHER_CAPTURE_CONFIG = {
            "windows": {"hwnd_class": "Qt51517QWindowOwnDC"}
        }
        return task

    def test_hidden_launcher_is_shown_before_capture(self):
        task = self._make_task()

        with (
            patch("src.tasks.LauncherTask.win32gui.IsIconic", side_effect=[False, False]),
            patch("src.tasks.LauncherTask.win32gui.IsWindowVisible", side_effect=[False, True]),
            patch("src.tasks.LauncherTask.win32gui.ShowWindow") as show_window,
        ):
            self.assertTrue(task._restore_window_if_minimized(123, "NTEGame.exe"))

        show_window.assert_called_once_with(123, 5)

    def test_capture_stops_when_launcher_cannot_be_shown(self):
        task = self._make_task()
        task.capture_config = Mock()
        task._ensure_launcher_visible = Mock(return_value=False)

        with self.assertRaisesRegex(TaskDisabledException, "Launcher window is not visible"):
            task._capture_launcher()

    def test_find_process_window_uses_launcher_capture_window_class(self):
        task = self._make_task()
        proc = {"pid": 1, "name": LAUNCHER_EXE[0]}
        task._find_window_for_process = Mock(return_value=123)

        with patch(
            "src.tasks.LauncherTask.psutil.process_iter",
            return_value=[Mock(info=proc)],
        ):
            self.assertEqual((proc, 123), task._find_process_window(LAUNCHER_EXE))

        task._find_window_for_process.assert_called_once_with(
            proc,
            hwnd_class="Qt51517QWindowOwnDC",
            require_title=False,
        )

    def test_wait_for_launcher_treats_game_start_as_success(self):
        task = self._make_task()
        game_proc = {"pid": 2, "name": GAME_EXE, "exe": "C:\\HTGame.exe"}
        task._find_process = Mock(return_value=game_proc)

        with patch("src.tasks.LauncherTask.time.time", return_value=0):
            self.assertEqual(task._wait_for_process(LAUNCHER_EXE), game_proc)

    def test_prepare_launcher_skips_ui_when_game_process_appeared(self):
        task = self._make_task()
        game_proc = {"pid": 2, "name": GAME_EXE, "exe": "C:\\HTGame.exe"}
        task._find_process = Mock(return_value=None)
        task._get_launcher_path = Mock(return_value="C:\\NTEGame.exe")
        task._wait_for_process = Mock(return_value=game_proc)

        with patch("src.tasks.LauncherTask.execute", return_value=True):
            self.assertFalse(task._prepare_launcher())

    def test_find_process_window_falls_back_to_window_pid(self):
        task = self._make_task()
        process = Mock()
        process.name.return_value = GAME_EXE
        process.exe.return_value = "C:\\HTGame.exe"

        def enum_windows(callback, _):
            self.assertTrue(callback(123, None))

        with (
            patch("src.tasks.LauncherTask.psutil.process_iter", return_value=[]),
            patch("src.tasks.LauncherTask.psutil.Process", return_value=process),
            patch("src.tasks.LauncherTask.win32gui.EnumWindows", side_effect=enum_windows),
            patch("src.tasks.LauncherTask.win32gui.IsWindow", return_value=True),
            patch("src.tasks.LauncherTask.win32gui.IsWindowEnabled", return_value=True),
            patch("src.tasks.LauncherTask.win32gui.GetClassName", return_value="UnrealWindow"),
            patch("src.tasks.LauncherTask.win32gui.GetWindowText", return_value="异环"),
            patch(
                "src.tasks.LauncherTask.win32process.GetWindowThreadProcessId",
                return_value=(0, 999),
            ),
            patch("src.tasks.LauncherTask.win32gui.IsWindowVisible", return_value=True),
        ):
            proc, hwnd = task._find_process_window(GAME_EXE)

        self.assertEqual(hwnd, 123)
        self.assertEqual(proc, {"pid": 999, "name": GAME_EXE, "exe": "C:\\HTGame.exe"})

    def test_find_window_callback_continues_after_visible_match(self):
        task = self._make_task()

        def enum_windows(callback, _):
            self.assertTrue(callback(101, None))
            self.assertTrue(callback(102, None))

        with (
            patch("src.tasks.LauncherTask.win32gui.EnumWindows", side_effect=enum_windows),
            patch("src.tasks.LauncherTask.win32gui.IsWindow", return_value=True),
            patch("src.tasks.LauncherTask.win32gui.IsWindowEnabled", return_value=True),
            patch(
                "src.tasks.LauncherTask.win32process.GetWindowThreadProcessId",
                return_value=(0, 1),
            ),
            patch("src.tasks.LauncherTask.win32gui.IsWindowVisible", side_effect=[True, False]),
        ):
            self.assertEqual(task._find_window_for_process({"pid": 1}), 101)
