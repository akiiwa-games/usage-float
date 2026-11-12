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


class WallpaperSourceChangeTests(unittest.TestCase):
    """Real run_ui callbacks; fake download and fake players (no mpv or browser is started)."""

    def run_scenario(self, scenario, *, fail_download=False):
        import threading
        import tkinter as tk

        release = threading.Event()

        def fake_runner(target, progress=None):
            calls["downloads"] += 1
            if progress:
                progress(25, 100)
            release.wait(30)
            if fail_download:
                raise uf.mpv_provision.ProvisionError("test: download failed")
            target = Path(target)
            target.mkdir(parents=True, exist_ok=True)
            (target / "mpv.exe").write_bytes(b"fake")
            return target / "mpv.exe"

        calls = {"web_start": 0, "web_stop_while_running": 0, "mpv_start": [], "downloads": 0}

        class FakeProc:
            pid = 4

            def poll(self):
                return None

        def web_start(player, executable, host_hwnd, url, **kw):
            calls["web_start"] += 1
            player.process = FakeProc()
            return True, ""

        def web_stop(player):
            if player.process is not None:
                calls["web_stop_while_running"] += 1
            player.process = None
            player.browser_hwnd = 0

        def web_poll_window(player, host_hwnd, width, height, *, offscreen):
            if player.process is None:
                return 0
            player.browser_hwnd = 0x7FFF0001
            return player.browser_hwnd

        def mpv_start(player, executable, host_hwnd, files, **kw):
            calls["mpv_start"].append([str(f) for f in files])
            return False, "(test: mpv start intercepted)"

        real_thread_start = threading.Thread.start
        result: dict = {}

        def fake_mainloop(root, *args, **kwargs):
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_name != "run_ui":
                frame = frame.f_back
            scope = frame.f_locals
            state, web = scope["state"], scope["browser_wallpaper_player"]

            def pump(seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    root.update()
                    time.sleep(0.02)

            try:
                scenario(scope, state, web, release, pump, result, calls)
            finally:
                release.set()
                scope["do_quit"]()

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"\0" * 64)
            result["clip"] = str(clip)
            provisioner = uf.mpv_provision.BackgroundProvisioner(runner=fake_runner)
            with patch.object(uf, "CONFIG_PATH", Path(tmp) / "config.json"), \
                    patch.object(uf, "HOME", Path(tmp)), \
                    patch.object(uf, "fetch_all", return_value=[]), \
                    patch.object(uf, "_MPV_PROVISIONER", provisioner), \
                    patch.object(uf, "_find_mpv_executable", lambda configured=None: (
                        (Path(tmp) / ".usage-float" / "runtime" / "mpv" / "mpv.exe")
                        if (Path(tmp) / ".usage-float" / "runtime" / "mpv" / "mpv.exe").is_file() else None)), \
                    patch.object(uf, "_find_browser_executable", lambda configured=None: Path(sys.executable)), \
                    patch.object(uf._BrowserWebVideoPlayer, "start", web_start), \
                    patch.object(uf._BrowserWebVideoPlayer, "stop", web_stop), \
                    patch.object(uf._BrowserWebVideoPlayer, "check_alive", lambda player: player.process is not None), \
                    patch.object(uf._BrowserWebVideoPlayer, "poll_window", web_poll_window), \
                    patch.object(uf._BrowserWebVideoPlayer, "is_ready", lambda player: player.process is not None), \
                    patch.object(uf._MpvWallpaperPlayer, "start", mpv_start), \
                    patch.object(threading.Thread, "start",
                                 lambda thread: real_thread_start(thread) if thread.name == "mpv-provision" else None), \
                    patch.object(tk.Tk, "mainloop", fake_mainloop):
                uf.run_ui()
        return result, calls

    def test_web_page_dropped_while_mpv_downloads_keeps_playing(self) -> None:
        """Owner's order: video needs mpv -> download starts -> user drops a web page -> download ends."""

        def scenario(scope, state, web, release, pump, result, calls):
            scope["handle_dropped_files"]([result["clip"]])
            pump(1.0)
            result["waiting_after_video"] = bool(state.get("wallpaper_waiting_for_mpv"))
            scope["handle_dropped_files"](["https://example.com/watch"])
            pump(2.0)
            result["waiting_after_web"] = bool(state.get("wallpaper_waiting_for_mpv"))
            release.set()
            pump(2.0)
            result["web_running"] = web.running
            result["active"] = bool(state.get("wallpaper_active"))
            result["web_mode"] = bool(state.get("wallpaper_web_mode"))
            result["host_placed"] = bool(scope["wallpaper_host"].winfo_manager())
            result["stops_before_quit"] = calls["web_stop_while_running"]

        result, calls = self.run_scenario(scenario)
        self.assertTrue(result["waiting_after_video"])
        self.assertFalse(result["waiting_after_web"], "a new source cancels the pending mpv resume")
        self.assertTrue(result["web_running"], "the web page must survive the download finishing")
        self.assertTrue(result["active"])
        self.assertTrue(result["web_mode"])
        self.assertTrue(result["host_placed"])
        self.assertEqual(result["stops_before_quit"], 0)
        self.assertEqual(calls["mpv_start"], [])

    def test_video_still_resumes_when_nothing_else_was_dropped(self) -> None:
        def scenario(scope, state, web, release, pump, result, calls):
            scope["handle_dropped_files"]([result["clip"]])
            pump(1.0)
            release.set()
            pump(2.0)

        result, calls = self.run_scenario(scenario)
        self.assertEqual(calls["mpv_start"], [[result["clip"]]])

    def test_first_visit_to_the_lazily_built_wallpaper_tab_starts_the_download(self) -> None:
        def scenario(scope, state, web, release, pump, result, calls):
            scope["open_settings"]("用量")
            pump(0.5)
            win = state["settings_win"]
            result["children_at_open"] = len(win.winfo_children())
            result["status_at_open"] = uf._MPV_PROVISIONER.snapshot().get("status")
            win._select_settings_tab("动态壁纸")
            pump(0.5)
            result["status_after_visit"] = uf._MPV_PROVISIONER.snapshot().get("status")
            release.set()
            pump(1.5)
            result["status_after_release"] = uf._MPV_PROVISIONER.snapshot().get("status")
            win._select_settings_tab("用量")
            win._select_settings_tab("动态壁纸")
            pump(0.5)
            result["status_after_revisit"] = uf._MPV_PROVISIONER.snapshot().get("status")

        result, _calls = self.run_scenario(scenario)
        self.assertGreater(result["children_at_open"], 0)
        self.assertEqual(result["status_at_open"], "idle", "other tabs must not start the download")
        self.assertEqual(result["status_after_visit"], "running")
        self.assertEqual(result["status_after_release"], "done")
        self.assertEqual(result["status_after_revisit"], "done", "mpv is present; no second download")

    def test_a_failed_download_shows_a_visible_retry_link(self) -> None:
        """A failed download keeps the retry link on screen and records why.

        233ea78 (Simplify settings copy and move details into hover hints) is the
        rule for the folder summary in ``update_wallpaper_folder_status``: the
        visible line is ``{enabled} 个目录 · {cached} 个媒体 · mpv {state}``, and
        the long line, including ``mpv 下载失败：{error}``, is that label's hover
        hint (``_detail``). 899c067 keeps the retry control in the button row so
        the clipped lower edge of the folder card cannot hide it.
        """
        import tkinter as tk

        def find_label(widget, prefix):
            for child in widget.winfo_children():
                if isinstance(child, tk.Label) and str(child.cget("text")).startswith(prefix):
                    return child
                found = find_label(child, prefix)
                if found is not None:
                    return found
            return None

        def scenario(scope, state, web, release, pump, result, calls):
            scope["open_settings"]("动态壁纸")
            pump(0.3)
            release.set()
            pump(1.0)
            win = state["settings_win"]
            result["status"] = uf._MPV_PROVISIONER.snapshot().get("status")
            link = find_label(win, "重试下载 mpv")
            result["link_mapped"] = bool(link is not None and link.winfo_ismapped())
            # No folders and a failed provisioner: the short line names the state.
            status_label = find_label(win, "0 个目录 · 0 个媒体 · mpv 下载失败")
            result["status_text"] = None if status_label is None else str(status_label.cget("text"))
            result["status_detail"] = None if status_label is None else str(getattr(status_label, "_detail", ""))
            if link is not None:
                link.event_generate("<Button-1>")
                pump(0.3)
            result["status_after_click"] = uf._MPV_PROVISIONER.snapshot().get("status")

        result, calls = self.run_scenario(scenario, fail_download=True)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["link_mapped"], "the retry link must be on screen, not clipped")
        self.assertEqual(result["status_text"], "0 个目录 · 0 个媒体 · mpv 下载失败")
        self.assertEqual(
            result["status_detail"],
            "已启用 0 / 共 0 个目录 · 已缓存 0 个媒体 · mpv 下载失败：test: download failed",
        )
        self.assertEqual(calls["downloads"], 2, "clicking the link retries the download")

    def test_opening_settings_straight_onto_the_wallpaper_tab_starts_the_download(self) -> None:
        def scenario(scope, state, web, release, pump, result, calls):
            scope["open_settings"]("动态壁纸")
            pump(0.5)
            result["status"] = uf._MPV_PROVISIONER.snapshot().get("status")

        result, _calls = self.run_scenario(scenario)
        self.assertEqual(result["status"], "running")


class HandoffDetectionTests(unittest.TestCase):
    PROFILE = r"C:\Users\u\.usage-float\browser-sessions\web-video-abc123"
    LAUNCHED = 1_000

    def identity(self, table):
        return lambda pid: table.get(pid)

    def test_adopts_only_a_child_created_after_launch_with_our_profile(self) -> None:
        rows = [(11, 10, "msedge.exe"), (12, 10, "msedge.exe"), (13, 12, "msedge.exe")]
        table = {
            11: (2_000, f'"msedge.exe" --type=crashpad-handler --database={self.PROFILE}\\Crashpad'),
            12: (2_000, f'"msedge.exe" --user-data-dir={self.PROFILE} --app=https://x'),
            13: (2_100, f'"msedge.exe" --type=renderer --user-data-dir={self.PROFILE}'),
        }
        self.assertEqual(uf._find_handoff_browser_pid(10, "msedge.exe", self.PROFILE, self.LAUNCHED, rows, self.identity(table)), 12)

    def test_pid_reuse_never_adopts_the_users_browser(self) -> None:
        """The user's browser lists a long-dead parent whose pid our launcher happened to reuse."""
        rows = [(20808, 39280, "chrome.exe"), (20900, 20808, "chrome.exe")]
        older = {20808: (500, '"chrome.exe" --profile-directory=Default'), 20900: (600, '"chrome.exe" --type=renderer')}
        self.assertEqual(uf._find_handoff_browser_pid(39280, "chrome.exe", self.PROFILE, self.LAUNCHED, rows, self.identity(older)), 0)
        # Created after our launch (a fresh user window) but without our temp profile: not ours.
        newer = {20808: (5_000, '"chrome.exe" --profile-directory=Default')}
        self.assertEqual(uf._find_handoff_browser_pid(39280, "chrome.exe", self.PROFILE, self.LAUNCHED, rows, self.identity(newer)), 0)
        # Our profile string but created before our launch: not ours either.
        stale = {20808: (999, f'"chrome.exe" --user-data-dir={self.PROFILE}')}
        self.assertEqual(uf._find_handoff_browser_pid(39280, "chrome.exe", self.PROFILE, self.LAUNCHED, rows, self.identity(stale)), 0)

    def test_unreadable_or_missing_identity_is_not_ours(self) -> None:
        rows = [(12, 10, "msedge.exe")]
        self.assertEqual(uf._find_handoff_browser_pid(10, "msedge.exe", self.PROFILE, self.LAUNCHED, rows, lambda pid: None), 0)
        self.assertEqual(uf._find_handoff_browser_pid(10, "msedge.exe", None, self.LAUNCHED, rows), 0)
        self.assertEqual(uf._find_handoff_browser_pid(0, "msedge.exe", self.PROFILE, self.LAUNCHED, rows), 0)

    def test_stop_sweep_lists_only_profile_processes(self) -> None:
        rows = [(1, 0, "msedge.exe"), (2, 0, "msedge.exe"), (3, 0, "msedge.exe"), (4, 0, "other.exe")]
        table = {
            1: (2_000, f"msedge.exe --user-data-dir={self.PROFILE}"),
            2: (2_000, "msedge.exe --profile-directory=Default"),
            3: (10, f"msedge.exe --user-data-dir={self.PROFILE}"),
            4: (2_000, f"other.exe {self.PROFILE}"),
        }
        self.assertEqual(uf._list_profile_processes("msedge.exe", self.PROFILE, self.LAUNCHED, rows, self.identity(table)), [1])


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

    def test_relaunched_browser_is_adopted_and_stop_ends_only_our_processes(self) -> None:
        profile = self.root / "web-video-test"
        profile.mkdir()
        pid_file = self.root / "browser.pid"
        child = self.root / "browser.py"
        child.write_text(textwrap.dedent(f"""
            import os, subprocess, sys, time
            open({str(pid_file)!r}, "w").write(str(os.getpid()))
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--type=renderer",
                              {"--user-data-dir=" + str(profile)!r}])
            time.sleep(60)
        """), encoding="utf-8")
        launcher = self.root / "launcher.py"
        launcher.write_text(textwrap.dedent(f"""
            import subprocess, sys, time, os
            subprocess.Popen([sys.executable, {str(child)!r}, {"--user-data-dir=" + str(profile)!r}])
            deadline = time.time() + 10
            while not os.path.exists({str(pid_file)!r}) and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)
            sys.exit(0)
        """), encoding="utf-8")
        # Carries our profile string but predates our launch: must never be treated as ours.
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={profile}"],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(bystander.kill)
        time.sleep(0.05)
        exe = Path(sys.executable).name
        self.player._exe_name = exe
        self.player.profile_dir = profile
        self.player._launched_at = uf._filetime_now()
        self.player.process = subprocess.Popen([sys.executable, str(launcher), f"--user-data-dir={profile}"],
                                               creationflags=subprocess.CREATE_NO_WINDOW)
        launcher_pid = self.player.process.pid
        launched_at = self.player._launched_at

        adopted = self.wait_for(lambda: self.player.check_alive() and self.player.process.pid != launcher_pid)
        self.assertTrue(adopted, self.player.last_error)
        browser_pid = int(pid_file.read_text())
        self.assertEqual(self.player.process.pid, browser_pid)
        owned = uf._list_profile_processes(exe, profile, launched_at)
        self.assertIn(browser_pid, owned)
        self.assertNotIn(bystander.pid, owned)
        handle = self.player.process

        self.player.stop()
        self.assertIsNotNone(self.wait_for(lambda: handle.poll()), "stop() must end the adopted browser")
        self.assertTrue(self.wait_for(lambda: uf._list_profile_processes(exe, profile, launched_at) == []),
                        "stop() must end every helper of our profile")
        self.assertIsNone(bystander.poll(), "a process that predates our launch must survive stop()")

    def test_pid_reuse_does_not_adopt_an_unrelated_process(self) -> None:
        """Reviewer's orphan probe: the launcher pid equals the dead parent pid of an unrelated process."""
        parent = subprocess.run(
            [sys.executable, "-c", "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', "
             "'import time; time.sleep(60)'], creationflags=0x08000000); print(p.pid)"],
            capture_output=True, text=True, timeout=30,
        )
        unrelated_pid = int(parent.stdout.strip())
        parents = {pid: ppid for pid, ppid, _image in uf._list_windows_processes()}
        dead_parent = parents.get(unrelated_pid)
        self.assertIsNotNone(dead_parent)
        unrelated = uf._AdoptedBrowserProcess(unrelated_pid)
        self.addCleanup(unrelated.terminate)

        class DeadLauncher:
            pid = dead_parent
            returncode = 0

            def poll(self):
                return 0

        profile = self.root / "web-video-x"
        profile.mkdir()
        self.player._exe_name = Path(sys.executable).name
        self.player.profile_dir = profile
        self.player._launched_at = uf._filetime_now()
        self.player.process = DeadLauncher()

        self.assertTrue(self.wait_for(lambda: not self.player.check_alive()), "must report the exit, not adopt")
        self.assertIsInstance(self.player.process, DeadLauncher)
        self.player.stop()
        time.sleep(0.6)
        self.assertIsNone(unrelated.poll(), "stop() must not touch the unrelated process")

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
