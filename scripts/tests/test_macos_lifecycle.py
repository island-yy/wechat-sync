"""Isolated application lifecycle tests; never launch or modify real WeChat."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if sys.platform != "darwin":
    raise unittest.SkipTest("Requires the macOS exporter")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wechat_export as exporter
import wechat_sync as controller
from db_export import ExportError


class MacLifecycleTests(unittest.TestCase):
    def test_same_executable_name_is_filtered_by_application_path(self):
        original = Path("/Applications/WeChat.app")
        temporary = Path("/tmp/wechat-test/WeChat-debug.app").resolve()
        paths = {101: str(original / "Contents/MacOS/WeChat"),
                 202: str(temporary / "Contents/MacOS/WeChat")}
        with patch.object(exporter.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 0, "101\n202\n", "")), \
                patch.object(exporter, "process_path", side_effect=paths.__getitem__):
            self.assertEqual(exporter.app_pids("WeChat", app=original), [101])
            self.assertEqual(exporter.app_pids("WeChat", app=temporary), [202])
            self.assertEqual(exporter.app_pids("WeChat"), [101, 202])

    def run_setup(self, *, cleanup_fails=False, reopen_fails=False):
        with tempfile.TemporaryDirectory(prefix="wechat-lifecycle-test-") as directory:
            home = Path(directory)
            app = home / "WeChat.app"
            account = home / "xwechat_files/test/db_storage"
            debug_apps = []
            running = {"original": True}

            def make_copy(_app, workspace):
                debug = workspace / "WeChat-debug.app"
                debug.mkdir()
                debug_apps.append(debug)
                return debug

            def pids(_executable, *, app=None):
                if app == home / "WeChat.app":
                    return [101] if running["original"] else []
                return [202] if app in debug_apps else []

            def quit_app(path, _executable):
                if path == app:
                    running["original"] = False
                elif cleanup_fails:
                    raise ExportError("Synthetic cleanup failure")

            with patch.object(exporter.Path, "home", return_value=home), \
                    patch.object(exporter.sys, "argv", ["wechat_export.py", "--setup-sync",
                        "--db-root", str(home / "xwechat_files")]), \
                    patch.object(exporter, "preflight", return_value=
                        (app, {"CFBundleExecutable": "WeChat"})), \
                    patch.object(exporter, "select_account", return_value=account), \
                    patch.object(exporter, "app_pids", side_effect=pids), \
                    patch.object(exporter, "quit_app", side_effect=quit_app), \
                    patch.object(exporter, "make_debug_copy", side_effect=make_copy), \
                    patch.object(exporter, "capture", return_value=bytes(range(32))), \
                    patch.object(controller, "configure") as configure, \
                    patch("cipher_reader.library"), \
                    patch("builtins.input", return_value=""), \
                    patch.object(exporter.subprocess, "run", return_value=
                        subprocess.CompletedProcess([], 1 if reopen_fails else 0)) as opened, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = exporter.main()
            configure.assert_called_once()
            return result, opened.call_args_list, debug_apps[0].exists()

    def test_setup_does_not_report_success_if_temporary_app_cannot_close(self):
        code, opened, retained = self.run_setup(cleanup_fails=True)
        self.assertEqual(code, 1)
        self.assertFalse(opened)
        self.assertTrue(retained)

    def test_setup_reopens_original_after_successful_cleanup(self):
        code, opened, retained = self.run_setup()
        self.assertEqual(code, 0)
        self.assertFalse(retained)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].args[0][:2], ["/usr/bin/open", "-n"])
        self.assertTrue(opened[0].args[0][2].endswith("/WeChat.app"))

    def test_setup_reports_failure_if_original_cannot_reopen(self):
        code, _opened, retained = self.run_setup(reopen_fails=True)
        self.assertEqual(code, 1)
        self.assertFalse(retained)


if __name__ == "__main__":
    unittest.main()
