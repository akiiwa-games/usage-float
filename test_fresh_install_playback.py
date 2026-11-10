"""Fresh-install playback: hosted path resolution, mpv provisioning wiring, and web-player start failures."""
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import usage_float as uf

HERE = Path(__file__).resolve().parent


class HostedPathTests(unittest.TestCase):
    def test_own_bundle_detection(self) -> None:
        with TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "_internal"
            bundle.mkdir()
            plugin = Path(tmp) / "plugins" / "usage-float"
            plugin.mkdir(parents=True)
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(bundle), create=True):
                self.assertTrue(uf._is_own_frozen_build(bundle / "usage_float.py"))
                self.assertFalse(uf._is_own_frozen_build(plugin / "usage_float.py"))
        self.assertFalse(uf._is_own_frozen_build(HERE / "usage_float.py"))

    def test_hosted_in_a_frozen_executable_resolves_resources_next_to_the_plugin(self) -> None:
        """AgentCompass imports this file from its plugin clone inside AgentCompass.exe (sys.frozen set)."""
        with TemporaryDirectory() as tmp:
            install = Path(tmp) / "AgentCompass"
            (install / "_internal").mkdir(parents=True)
            code = textwrap.dedent(f"""
                import json, sys
                sys.frozen = True
                sys._MEIPASS = {str(install / "_internal")!r}
                sys.executable = {str(install / "AgentCompass.exe")!r}
                sys.path.insert(0, {str(HERE)!r})
                import usage_float as uf
                print(json.dumps({{"app": str(uf.APP_DIR), "res": str(uf.RESOURCE_DIR),
                                   "lua": str(uf.WALLPAPER_START_SCRIPT_PATH)}}))
            """)
            env = dict(os.environ, USERPROFILE=tmp, HOME=tmp, HOMEDRIVE="", HOMEPATH="")
            done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            paths = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(Path(paths["app"]), HERE)
        self.assertEqual(Path(paths["res"]), HERE)
        self.assertEqual(Path(paths["lua"]), HERE / "wallpaper-start.lua")

    def test_mpv_in_the_plugin_vendor_dir_is_found(self) -> None:
        with TemporaryDirectory() as tmp:
            app = Path(tmp) / "plugin"
            exe = app / "vendor" / "mpv" / "mpv.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            with patch.object(uf, "APP_DIR", app), patch.object(uf, "HOME", Path(tmp) / "home"), \
                    patch.dict(uf._MPV_EXECUTABLE_CACHE, clear=True), patch.object(uf.shutil, "which", return_value=None), \
                    patch.dict(os.environ, {"LOCALAPPDATA": str(Path(tmp) / "lad")}):
                self.assertEqual(uf._find_mpv_executable(), exe.resolve())


class MpvProvisionWiringTests(unittest.TestCase):
    def test_start_targets_the_runtime_dir_and_skips_when_mpv_exists(self) -> None:
        provisioner = uf.mpv_provision.BackgroundProvisioner()
        with TemporaryDirectory() as tmp, patch.object(uf, "_MPV_PROVISIONER", provisioner), \
                patch.object(uf, "HOME", Path(tmp)), patch.object(provisioner, "start", return_value=True) as start:
            with patch.object(uf, "_find_mpv_executable", return_value=Path("mpv.exe")):
                self.assertFalse(uf._start_mpv_provisioning())
            start.assert_not_called()
            with patch.object(uf, "_find_mpv_executable", return_value=None):
                self.assertTrue(uf._start_mpv_provisioning(retry=True))
            start.assert_called_once_with(retry=True)
            self.assertEqual(provisioner.target, Path(tmp) / ".usage-float" / "runtime" / "mpv")

    def test_finished_download_clears_the_not_found_cache(self) -> None:
        provisioner = uf.mpv_provision.BackgroundProvisioner(runner=lambda target, progress=None: Path("mpv.exe"))
        with patch.object(uf, "_MPV_PROVISIONER", provisioner), patch.dict(uf._MPV_EXECUTABLE_CACHE, {"": None}, clear=True):
            provisioner.start()
            provisioner.join(5)
            self.assertEqual(uf._mpv_provision_snapshot()["status"], "done")
            self.assertNotIn("", uf._MPV_EXECUTABLE_CACHE)

    def test_status_text(self) -> None:
        running = uf._mpv_provision_text({"status": "running", "done": 50, "total": 200})
        self.assertIn("25%", running)
        self.assertIn("offline", uf._mpv_provision_text({"status": "failed", "error": "offline"}))
        self.assertIn("mpv", uf._mpv_provision_text({"status": "idle"}))

    def test_importing_does_not_start_a_download(self) -> None:
        self.assertIn(uf._MPV_PROVISIONER.snapshot()["status"], ("idle", "done", "failed"))
        self.assertIsNone(uf._MPV_PROVISIONER._thread)


class HandoffDetectionTests(unittest.TestCase):
    def test_prefers_the_child_that_owns_a_browser_window(self) -> None:
        rows = [(10, 1, "msedge.exe"), (11, 10, "msedge.exe"), (12, 10, "msedge.exe"), (13, 12, "msedge.exe")]
        self.assertEqual(uf._find_handoff_browser_pid(10, "msedge.exe", rows, window_pid_check=lambda pid: pid == 12), 12)

    def test_falls_back_to_the_child_with_its_own_children(self) -> None:
        # 11 is a crashpad handler (no children); 12 is the relaunched browser with a renderer.
        rows = [(11, 10, "msedge.exe"), (12, 10, "MSEDGE.EXE"), (13, 12, "msedge.exe"), (14, 10, "other.exe")]
        self.assertEqual(uf._find_handoff_browser_pid(10, "msedge.exe", rows, window_pid_check=lambda pid: False), 12)

    def test_no_handoff(self) -> None:
        rows = [(11, 10, "msedge.exe"), (14, 10, "other.exe")]
        self.assertEqual(uf._find_handoff_browser_pid(10, "msedge.exe", rows, window_pid_check=lambda pid: False), 0)
        self.assertEqual(uf._find_handoff_browser_pid(0, "msedge.exe", rows), 0)


@unittest.skipUnless(sys.platform == "win32", "Windows process handoff")
class BrowserStartFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.player = uf._BrowserWebVideoPlayer()
        self.player.HANDOFF_GRACE_SECONDS = 0.3
        self.addCleanup(self.player.stop)

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        return predicate()

    def test_browser_exiting_before_devtools_names_the_exit_code(self) -> None:
        """A 'browser' that rejects the command line exits at once: the error says so, with its code."""
        with patch.object(uf, "CONFIG_PATH", self.root / ".usage-float" / "config.json"):
            ok, error = self.player.start(Path(sys.executable), 0, "https://example.com/")
        self.assertTrue(ok, error)
        alive = self.wait_for(lambda: not self.player.check_alive())
        self.assertTrue(alive, "check_alive() should report the exit after the handoff grace")
        message = self.player.last_error
        self.assertIn(Path(sys.executable).name, message)
        self.assertIn("2", message)  # argparse-style usage error exit code
        self.assertTrue("DevTools" in message or "调试端口" in message, message)

    def test_relaunched_browser_is_adopted_instead_of_reported_as_failed(self) -> None:
        pid_file = self.root / "browser.pid"
        child = self.root / "browser.py"
        child.write_text(textwrap.dedent(f"""
            import os, subprocess, sys, time
            open({str(pid_file)!r}, "w").write(str(os.getpid()))
            renderer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            time.sleep(60)
        """), encoding="utf-8")
        launcher = self.root / "launcher.py"
        launcher.write_text(textwrap.dedent(f"""
            import subprocess, sys, time, os
            subprocess.Popen([sys.executable, {str(child)!r}])
            deadline = time.time() + 10
            while not os.path.exists({str(pid_file)!r}) and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)
            sys.exit(0)
        """), encoding="utf-8")
        self.player._exe_name = Path(sys.executable).name
        self.player.process = subprocess.Popen([sys.executable, str(launcher)], creationflags=subprocess.CREATE_NO_WINDOW)
        launcher_pid = self.player.process.pid

        adopted = self.wait_for(lambda: self.player.check_alive() and self.player.process.pid != launcher_pid)
        self.assertTrue(adopted, self.player.last_error)
        browser_pid = int(pid_file.read_text())
        self.assertEqual(self.player.process.pid, browser_pid)
        self.assertTrue(self.player.running)
        handle = self.player.process

        self.player.stop()
        self.assertIsNotNone(self.wait_for(lambda: handle.poll()), "stop() must end the adopted browser")

    def test_cdp_status_describes_each_stage(self) -> None:
        self.assertTrue("DevToolsActivePort" in self.player.cdp_status())
        self.player._devtools_port = 9222
        self.player._cdp_last_error = "connection refused"
        status = self.player.cdp_status()
        self.assertIn("9222", status)
        self.assertIn("connection refused", status)
        self.player._cdp_connected = True
        self.assertNotIn("9222", self.player.cdp_status())


if __name__ == "__main__":
    unittest.main()
