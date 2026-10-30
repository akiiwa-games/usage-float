import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import usage_float as usage_float_module


from usage_float import (
    BG_OPACITY_DEFAULT,
    DEFAULT_PROVIDERS,
    LLM_PROXY_TIMEZONE_DEFAULT,
    MonitorInfo,
    TEXT_OPACITY_DEFAULT,
    WALLPAPER_RANDOM_MODE_DIRECTORY,
    WALLPAPER_RANDOM_MODE_GLOBAL,
    _DoubleCtrlDetector,
    _PendingFileDrops,
    _apply_layer_opacities,
    _build_mpv_wallpaper_command,
    _build_wallpaper_playback_order,
    _with_preferred_wallpaper_first,
    _choose_panel_monitor,
    _clamp_opacity,
    _collect_wallpaper_media,
    _collect_wallpaper_media_folders,
    _compact_row_height,
    _commit_wallpaper_settings_before_close,
    _dropped_wallpaper_videos,
    _enabled_wallpaper_folders,
    _effective_card_height,
    _format_refresh_control,
    _install_windows_file_drop,
    _monitor_label,
    _merge_wallpaper_weight_values,
    _normalize_wallpaper_folders,
    _normalize_wallpaper_disabled_folders,
    _other_shortcut_key_down,
    _panel_grid_shape,
    _panel_layout_key,
    _panel_metrics,
    _point_in_box,
    _randomize_wallpaper_session,
    _report_tk_callback_exception,
    _tk_geometry,
    _wallpaper_path_key,
    claude_reset_status,
    consume_codex_reset_card,
    fetch_codex_reset_cards,
    format_remaining,
    iter_display_rows,
    parse_claude_reset_status,
    row_opens_reset_ui,
    ProviderUsage,
    UsageWindow,
)


def setUpModule():
    temporary = TemporaryDirectory()
    unittest.addModuleCleanup(temporary.cleanup)
    for patcher in (
        patch.object(usage_float_module, 'CACHE_PATH', Path(temporary.name) / 'cache.json'),
        patch.object(usage_float_module.urllib.request, 'urlopen', side_effect=AssertionError('Unmocked network in unit test')),
    ):
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


class LanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_path = Path(self.temporary.name) / "config.json"
        for patcher in (
            patch.object(usage_float_module, "CONFIG_PATH", self.config_path),
            patch.object(usage_float_module, "_ACTIVE_LANGUAGE", "zh"),
            patch.object(usage_float_module, "_docs_shot_active", return_value=False),
            patch.dict(os.environ, {"USAGE_FLOAT_DOCS_SHOT_ROOT": ""}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_language_is_chinese_even_after_english_was_active(self) -> None:
        usage_float_module._ACTIVE_LANGUAGE = "en"
        self.assertEqual(usage_float_module.load_config()["language"], "zh")
        self.config_path.write_text('{"font_size": 12}', encoding="utf-8")
        self.assertEqual(usage_float_module.load_config()["language"], "zh")
        self.assertEqual(usage_float_module._tr("设置", "Settings"), "设置")

    def test_language_normalization(self) -> None:
        for value, expected in (("EN", "en"), ("en-US", "en"), ("ZH-CN", "zh"),
                                ("zh", "zh"), ("en", "en"), ("fr", "zh"),
                                (None, "zh"), (42, "zh"), (" en ", "en")):
            with self.subTest(value=value):
                self.config_path.write_text(json.dumps({"language": value}), encoding="utf-8")
                self.assertEqual(usage_float_module.load_config()["language"], expected)
                self.assertEqual(usage_float_module._ACTIVE_LANGUAGE, expected)

    def test_english_round_trip_and_language_switch_preserve_other_settings(self) -> None:
        self.config_path.write_text('{"language": "en", "font_size": 12}', encoding="utf-8")
        cfg = usage_float_module.load_config()
        usage_float_module.save_config(cfg)
        self.assertEqual(json.loads(self.config_path.read_text())["language"], "en")
        self.assertEqual(usage_float_module.load_config()["language"], "en")
        state = {"font_size": 12}
        usage_float_module._set_language(cfg, state, "ZH-CN")
        self.assertEqual(state["language"], "zh")
        self.assertEqual(cfg["font_size"], 12)
        self.assertEqual(state["font_size"], 12)
        self.assertEqual(json.loads(self.config_path.read_text())["language"], "zh")
        usage_float_module._set_language(cfg, state, "EN-US")
        self.assertEqual(usage_float_module._tr("设置", "Settings"), "Settings")
        self.assertEqual(state["language"], "en")

    def test_translation_override_and_stable_labels(self) -> None:
        self.assertEqual(usage_float_module._tr("设置", "Settings", "en"), "Settings")
        self.assertEqual(usage_float_module._tr("设置", "Settings", "zh"), "设置")
        for language, expected in (("en", "Update failed"), ("zh", "更新失败")):
            usage_float_module._ACTIVE_LANGUAGE = language
            self.assertEqual(usage_float_module._ui("更新失败"), expected)
            self.assertEqual(usage_float_module._ui("Update failed"), expected)
        self.assertEqual(usage_float_module._ui("custom account name"), "custom account name")

    def test_relative_time_and_refresh_controls_in_both_languages(self) -> None:
        for language, expected in (
            ("zh", ("尚未更新", "刚刚", "30 秒前", "2 分钟前", "2 小时前", "2 天前")),
            ("en", ("Not updated yet", "just now", "30s ago", "2m ago", "2h ago", "2d ago")),
        ):
            usage_float_module._ACTIVE_LANGUAGE = language
            with self.subTest(language=language):
                actual = tuple(usage_float_module.format_relative(ts, now=200000)
                               for ts in (None, 199995, 199970, 199880, 192800, 27200))
                self.assertEqual(actual, expected)
        self.assertEqual(_format_refresh_control(199880, False, 200000),
                         "Updated 2m ago · Click to refresh")
        self.assertEqual(_format_refresh_control(199880, True, 200000), "Refreshing…")
        self.assertEqual(_format_refresh_control(199880, False, 200000, compact=True),
                         "Updated 2m ago  ↻")
        usage_float_module._ACTIVE_LANGUAGE = "zh"
        self.assertEqual(_format_refresh_control(199880, False, 200000), "2 分钟前  ↻")
        self.assertEqual(_format_refresh_control(199880, True, 200000), "更新中…  ↻")

    def test_reset_status_and_results_follow_runtime_language(self) -> None:
        for language, status, failure in (("en", "Available", "No reset credit selected"),
                                          ("zh", "可用", "没有选中任何重置卡")):
            usage_float_module._ACTIVE_LANGUAGE = language
            self.assertEqual(usage_float_module._ui(
                usage_float_module.CODEX_RESET_CARD_STATUS_TEXT["available"]), status)
            self.assertEqual(consume_codex_reset_card("", request_id="unused"),
                             (False, failure))
        usage_float_module._ACTIVE_LANGUAGE = "en"
        with patch.object(usage_float_module, "claude_organization_uuid", return_value="org"), patch.object(
            usage_float_module, "claude_authorized_json",
            return_value=(200, {"result": "not_limited"}),
        ):
            result = usage_float_module.claim_claude_reset("grant", request_id="request")
        self.assertFalse(result.ok)
        self.assertIn("Usage limit not reached", result.message)
        with patch.object(usage_float_module, "claude_organization_uuid", return_value="org"), patch.object(
            usage_float_module, "claude_authorized_json",
            return_value=(200, {"result": "already_used"}),
        ):
            result = usage_float_module.claim_claude_session_reset()
        self.assertEqual(result.message, "This week’s 5-hour reset has already been used.")
        self.assertEqual(usage_float_module._ui(usage_float_module.CLAUDE_SESSION_REASON_TEXT["tier"]),
                         "Your subscription does not include 5-hour resets")


class WallpaperBackendTests(unittest.TestCase):
    class FixedRandom:
        def __init__(self, first_name: str, percent: float = 42.5) -> None:
            self.first_name = first_name
            self.percent = percent

        def shuffle(self, values: list[Path]) -> None:
            values.sort(key=lambda path: path.name != self.first_name)

        def uniform(self, _start: float, _end: float) -> float:
            return self.percent

    def test_media_collection_is_recursive_stable_and_filtered(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "nested").mkdir()
            (root / "b.MP4").write_bytes(b"")
            (root / "nested" / "a.png").write_bytes(b"")
            (root / "ignore.txt").write_text("no", encoding="utf-8")

            files = _collect_wallpaper_media(root)

        self.assertEqual([path.name for path in files], ["b.MP4", "a.png"])

    def test_folder_list_migrates_legacy_value_and_deduplicates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            migrated = _normalize_wallpaper_folders([], root)
            deduplicated = _normalize_wallpaper_folders([root, str(root)])

        self.assertEqual(migrated, [str(root.resolve())])
        self.assertEqual(deduplicated, [str(root.resolve())])

    def test_load_config_migrates_the_saved_single_folder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            legacy_folder = Path(temp_dir) / "media"
            config_path.write_text(
                json.dumps({"wallpaper_folder": str(legacy_folder)}),
                encoding="utf-8",
            )
            with patch.object(usage_float_module, "CONFIG_PATH", config_path):
                config = usage_float_module.load_config()

        self.assertEqual(config["wallpaper_folders"], [str(legacy_folder.resolve())])
        self.assertEqual(config["wallpaper_folder"], str(legacy_folder.resolve()))
        self.assertEqual(config["wallpaper_disabled_folders"], [])

    def test_disabled_folders_are_limited_to_saved_roots_and_keep_root_order(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first"
            second = root / "second"
            missing = root / "removed"
            folders = [str(first), str(second)]

            disabled = _normalize_wallpaper_disabled_folders(
                [str(second), str(missing), str(second)],
                folders,
            )

        self.assertEqual(disabled, [str(second.resolve())])

    def test_enabled_folders_exclude_disabled_roots_without_deleting_them(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first"
            second = root / "second"
            folders = [str(first), str(second)]

            enabled = _enabled_wallpaper_folders(folders, [str(first)])

        self.assertEqual(enabled, [str(second.resolve())])
        self.assertEqual(
            _normalize_wallpaper_folders(folders),
            [str(first.resolve()), str(second.resolve())],
        )

    def test_multiple_roots_merge_media_without_overlap_duplicates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            (root / "root.jpg").write_bytes(b"")
            (nested / "nested.mp4").write_bytes(b"")

            files = _collect_wallpaper_media_folders([str(root), str(nested)])

        self.assertEqual([path.name for path in files], ["nested.mp4", "root.jpg"])

    def test_preferred_drop_stays_first_without_reshuffling_the_queue(self) -> None:
        current = [Path("queue") / "a.mp4", Path("queue") / "b.mp4"]
        dropped = Path("other") / "dragged.mp4"

        kept, start = _build_wallpaper_playback_order(
            current,
            preferred_first=dropped,
            keep_order=True,
            rng=self.FixedRandom("b.mp4"),
        )
        reshuffled, _reshuffled_start = _build_wallpaper_playback_order(
            [*current, dropped],
            preferred_first=dropped,
            keep_order=False,
            rng=self.FixedRandom("b.mp4"),
        )

        self.assertIsNone(start)
        self.assertEqual(kept, [dropped, *current])
        self.assertEqual(kept, _with_preferred_wallpaper_first(current, dropped))
        self.assertEqual(reshuffled[0], dropped)
        self.assertEqual(reshuffled[1].name, "b.mp4")

    def test_dropped_video_filter_accepts_supported_files_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "opening.MP4"
            image = root / "still.jpg"
            video.write_bytes(b"")
            image.write_bytes(b"")

            videos = _dropped_wallpaper_videos(
                [str(video), str(image), str(video), str(root / "missing.mkv")]
            )

        self.assertEqual(videos, [video.resolve()])

    def test_pending_file_drops_are_drained_without_raising_into_tk(self) -> None:
        delivered: list[list[str]] = []
        errors: list[str] = []
        queue = _PendingFileDrops(delivered.append)
        queue.push(["opening.mp4", "extra.mkv"])
        queue.push([])
        queue.drain()
        self.assertEqual(delivered, [["opening.mp4", "extra.mkv"]])

        def boom(_files: list[str]) -> None:
            raise RuntimeError("boom")

        failing = _PendingFileDrops(boom)
        with patch.object(usage_float_module, "_log_exception", lambda _prefix="error": errors.append(_prefix)):
            failing.push(["bad.mp4"])
            failing.drain()

        self.assertEqual(errors, ["file drop"])
        self.assertEqual(delivered, [["opening.mp4", "extra.mkv"]])

    def test_ole_drop_target_registers_on_a_real_tk_window(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows only")
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        cleanup = None
        try:
            root.update_idletasks()
            cleanup = _install_windows_file_drop(root, lambda _files: None)
            self.assertTrue(getattr(cleanup, "_registered", False))
        finally:
            if cleanup is not None:
                cleanup()
            root.destroy()

    def test_drop_install_failure_returns_a_safe_cleanup(self) -> None:
        class BrokenWidget:
            def update_idletasks(self) -> None:
                raise RuntimeError("no native window")

            def winfo_id(self) -> int:
                return 0

        with patch.object(usage_float_module, "_log_exception"):
            cleanup = _install_windows_file_drop(BrokenWidget(), lambda _files: None)

        self.assertTrue(callable(cleanup))
        cleanup()

    def test_tk_callback_exception_is_logged_without_reraise(self) -> None:
        with TemporaryDirectory() as temp_dir:
            log_parent = Path(temp_dir)
            with patch.object(usage_float_module, "CONFIG_PATH", log_parent / "config.json"):
                try:
                    raise RuntimeError("callback exploded")
                except RuntimeError:
                    exc, val, tb = sys.exc_info()
                    _report_tk_callback_exception(exc, val, tb)

            logged = (log_parent / "error.log").read_text(encoding="utf-8")

        self.assertIn("tk callback", logged)
        self.assertIn("callback exploded", logged)

    def test_directory_weight_controls_the_first_session_item(self) -> None:
        files = [Path("low") / "still.png", Path("high") / "opening.jpg"]
        weights = {
            _wallpaper_path_key(Path("low")): 10,
            _wallpaper_path_key(Path("high")): 20,
        }

        ordered, start_percent = _randomize_wallpaper_session(
            files,
            self.FixedRandom("still.png", percent=25),
            folder_weights=weights,
        )

        self.assertEqual(ordered[0].parent.name, "high")
        self.assertIsNone(start_percent)

    def test_pending_weight_text_is_committed_without_focus_change(self) -> None:
        folder = Path("media") / "long directory"
        key = _wallpaper_path_key(folder)

        committed = _merge_wallpaper_weight_values(
            {key: 10},
            {key: "37"},
        )
        invalid = _merge_wallpaper_weight_values(
            committed,
            {key: "not a number"},
        )

        self.assertEqual(committed[key], 37)
        self.assertEqual(invalid[key], 37)

    def test_close_commits_weights_before_other_settings_handlers(self) -> None:
        calls: list[str] = []

        _commit_wallpaper_settings_before_close(
            lambda: calls.append("weights"),
            lambda: calls.append("other"),
        )

        self.assertEqual(calls, ["weights", "other"])

    def test_single_directory_mode_keeps_the_session_in_the_selected_directory(self) -> None:
        files = [
            Path("low") / "a.png",
            Path("low") / "b.jpg",
            Path("high") / "c.png",
            Path("high") / "d.jpg",
        ]
        weights = {
            _wallpaper_path_key(Path("low")): 10,
            _wallpaper_path_key(Path("high")): 20,
        }

        ordered, _start = _randomize_wallpaper_session(
            files,
            self.FixedRandom("a.png", percent=25),
            folder_weights=weights,
            random_mode=WALLPAPER_RANDOM_MODE_DIRECTORY,
        )

        self.assertEqual(len(ordered), 2)
        self.assertEqual({path.parent.name for path in ordered}, {"high"})

    def test_global_mode_keeps_media_from_every_directory(self) -> None:
        files = [Path("one") / "a.png", Path("two") / "b.jpg"]

        ordered, _start = _randomize_wallpaper_session(
            files,
            self.FixedRandom("a.png", percent=5),
            folder_weights={},
            random_mode=WALLPAPER_RANDOM_MODE_GLOBAL,
        )

        self.assertEqual({path.parent.name for path in ordered}, {"one", "two"})

    def test_mpv_command_embeds_loops_and_uses_low_overhead_options(self) -> None:
        command = _build_mpv_wallpaper_command(
            Path(r"C:\tools\mpv.exe"),
            0x1_0000_0042,
            Path(r"C:\cache\wallpaper.m3u8"),
            image_seconds=12,
            first_file=Path(r"D:\media\opening.mp4"),
            start_percent=42.5,
            ipc_pipe=r"\\.\pipe\usage-float-test",
            paused=True,
        )

        self.assertIn("--wid=66", command)
        self.assertIn("--hwdec=auto-safe", command)
        self.assertIn("--profile=fast", command)
        self.assertIn("--mute=yes", command)
        self.assertNotIn("--no-audio", command)
        self.assertIn("--cache=yes", command)
        self.assertIn("--cache-secs=2", command)
        self.assertIn("--demuxer-readahead-secs=2", command)
        self.assertIn("--demuxer-max-bytes=16MiB", command)
        self.assertIn("--prefetch-playlist=yes", command)
        self.assertIn("--input-default-bindings=no", command)
        self.assertIn("--input-vo-keyboard=yes", command)
        self.assertIn("--input-cursor=yes", command)
        self.assertIn("--input-builtin-dragging=no", command)
        self.assertIn("--drag-and-drop=insert-next", command)
        self.assertTrue(any(item.startswith("--script=") for item in command))
        self.assertIn("--loop-playlist=inf", command)
        self.assertIn("--image-display-duration=12", command)
        self.assertIn("--pause=yes", command)
        self.assertIn(
            r"--input-ipc-server=\\.\pipe\usage-float-test",
            command,
        )
        self.assertIn("--start=42.50%", command)
        self.assertLess(command.index("--{"), command.index("--start=42.50%"))
        self.assertLess(command.index("--start=42.50%"), command.index("--}"))

    def test_session_randomizer_leaves_playback_progress_to_mpv_policy(self) -> None:
        files = [Path("still.png"), Path("clip.mp4"), Path("other.jpg")]

        video_order, video_start = _randomize_wallpaper_session(
            files,
            self.FixedRandom("clip.mp4"),
        )
        image_order, image_start = _randomize_wallpaper_session(
            files,
            self.FixedRandom("still.png"),
        )

        self.assertEqual(video_order[0].name, "clip.mp4")
        self.assertIsNone(video_start)
        self.assertEqual(image_order[0].name, "still.png")
        self.assertIsNone(image_start)

    def test_audio_can_be_disabled_for_wallpaper_sessions(self) -> None:
        command = _build_mpv_wallpaper_command(
            Path(r"C:\tools\mpv.exe"),
            42,
            None,
            first_file=Path(r"D:\media\silent.mp4"),
            audio_enabled=False,
        )

        self.assertIn("--no-audio", command)
        self.assertNotIn("--mute=yes", command)


class DoubleCtrlDetectorTests(unittest.TestCase):
    @staticmethod
    def tap(
        detector: _DoubleCtrlDetector,
        pressed_at: float,
        *,
        used: bool = False,
    ) -> bool:
        detector.update(True, False, pressed_at)
        if used:
            detector.update(True, True, pressed_at + 0.01)
        return detector.update(False, False, pressed_at + 0.05)

    def test_two_quick_clean_ctrl_taps_toggle(self) -> None:
        detector = _DoubleCtrlDetector(interval_seconds=0.45)

        self.assertFalse(self.tap(detector, 1.0))
        self.assertTrue(self.tap(detector, 1.25))

    def test_slow_or_modified_ctrl_taps_do_not_toggle(self) -> None:
        detector = _DoubleCtrlDetector(interval_seconds=0.45)

        self.assertFalse(self.tap(detector, 1.0))
        self.assertFalse(self.tap(detector, 1.8))
        self.assertFalse(self.tap(detector, 2.0, used=True))
        self.assertFalse(self.tap(detector, 2.2))
        self.assertTrue(self.tap(detector, 2.4))

    def test_sticky_ime_state_key_does_not_turn_ctrl_into_a_combo(self) -> None:
        def ime_only(vk: int) -> int:
            return 0x8000 if vk == 0x19 else 0

        def real_ctrl_c(vk: int) -> int:
            return 0x8000 if vk in (0x19, 0x43) else 0

        self.assertFalse(_other_shortcut_key_down(ime_only))
        self.assertTrue(_other_shortcut_key_down(real_ctrl_c))

    def test_three_clean_taps_toggle_when_count_is_three(self) -> None:
        detector = _DoubleCtrlDetector(interval_seconds=0.45, tap_count=3)
        self.assertFalse(self.tap(detector, 1.0))
        self.assertFalse(self.tap(detector, 1.2))
        self.assertTrue(self.tap(detector, 1.4))

    def test_combo_fires_once_on_press_and_not_while_held(self) -> None:
        detector = usage_float_module._ComboKeyDetector()
        self.assertTrue(detector.update(True))
        self.assertFalse(detector.update(True))
        self.assertFalse(detector.update(False))
        self.assertTrue(detector.update(True))


class RemainingTimeTests(unittest.TestCase):
    def test_day_and_hour_segments_have_one_space(self) -> None:
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)

        self.assertEqual(
            format_remaining((now + timedelta(days=3, hours=8)).isoformat(), now),
            "3d 8h",
        )
        self.assertEqual(
            format_remaining((now + timedelta(hours=3, minutes=31)).isoformat(), now),
            "3h 31m",
        )


class FakeWindow:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, float]] = []

    def attributes(self, name: str, value: float) -> None:
        self.calls.append((name, value))
        if self.fail:
            raise RuntimeError("layer update failed")


class OpacityTests(unittest.TestCase):
    def test_clamp_opacity_normalizes_invalid_and_out_of_range_values(self) -> None:
        self.assertEqual(_clamp_opacity(-1, BG_OPACITY_DEFAULT), 5)
        self.assertEqual(_clamp_opacity(140, BG_OPACITY_DEFAULT), 100)
        self.assertEqual(_clamp_opacity("42", BG_OPACITY_DEFAULT), 42)
        self.assertEqual(_clamp_opacity(None, BG_OPACITY_DEFAULT), BG_OPACITY_DEFAULT)

    def test_plate_and_text_receive_independent_opacity_values(self) -> None:
        text = FakeWindow()
        plate = FakeWindow()

        _apply_layer_opacities(text, plate, 20, 85)
        _apply_layer_opacities(text, plate, 60, 85)

        self.assertEqual(plate.calls, [("-alpha", 0.2), ("-alpha", 0.6)])
        self.assertEqual(text.calls, [("-alpha", 0.85), ("-alpha", 0.85)])

    def test_single_window_fallback_never_applies_plate_opacity_to_text(self) -> None:
        text = FakeWindow()

        _apply_layer_opacities(text, None, 15, 90)

        self.assertEqual(text.calls, [("-alpha", 0.9)])

    def test_plate_failure_does_not_block_text_preview_or_restack(self) -> None:
        text = FakeWindow()
        plate = FakeWindow(fail=True)
        restacks: list[bool] = []

        _apply_layer_opacities(
            text,
            plate,
            35,
            70,
            restack=lambda: restacks.append(True),
        )

        self.assertEqual(text.calls, [("-alpha", 0.7)])
        self.assertEqual(restacks, [True])

    def test_invalid_values_fall_back_per_layer(self) -> None:
        text = FakeWindow()
        plate = FakeWindow()

        _apply_layer_opacities(text, plate, object(), object())

        self.assertEqual(plate.calls, [("-alpha", BG_OPACITY_DEFAULT / 100)])
        self.assertEqual(text.calls, [("-alpha", TEXT_OPACITY_DEFAULT / 100)])


class RefreshControlTests(unittest.TestCase):
    def test_relative_time_and_refresh_icon_are_one_label(self) -> None:
        self.assertEqual(_format_refresh_control(100, False, now=142), "42 秒前  ↻")

    def test_refreshing_state_stays_in_the_same_control(self) -> None:
        self.assertEqual(_format_refresh_control(100, True, now=142), "更新中…  ↻")

    def test_never_refreshed_state_is_still_actionable(self) -> None:
        self.assertEqual(_format_refresh_control(None, False, now=142), "尚未更新  ↻")

    def test_transparent_padding_uses_the_same_click_target(self) -> None:
        hitbox = (4, 8, 84, 32)

        self.assertTrue(_point_in_box(4, 8, hitbox))
        self.assertTrue(_point_in_box(83, 31, hitbox))
        self.assertFalse(_point_in_box(84, 31, hitbox))
        self.assertFalse(_point_in_box(83, 32, hitbox))
        self.assertFalse(_point_in_box(20, 20, None))


class CompactLayoutTests(unittest.TestCase):
    def test_row_height_keeps_only_compact_safety_margin(self) -> None:
        self.assertEqual(_compact_row_height(16, 9), 18)
        self.assertEqual(_compact_row_height(21, 12), 23)

    def test_fixed_height_is_ignored_when_resizing_is_disabled(self) -> None:
        self.assertIsNone(_effective_card_height(192, False))
        self.assertEqual(_effective_card_height(192, True), 192)
        self.assertIsNone(_effective_card_height(None, True))


class AuxiliaryPanelTests(unittest.TestCase):
    @staticmethod
    def monitor(
        device: str,
        width: int,
        height: int,
        *,
        primary: bool = False,
        x: int = 0,
        y: int = 0,
        dpi: int = 96,
        monitor_id: str = "",
    ) -> MonitorInfo:
        return MonitorInfo(
            device=device,
            name="Test display",
            x=x,
            y=y,
            width=width,
            height=height,
            work_x=x,
            work_y=y,
            work_width=width,
            work_height=height,
            monitor_id=monitor_id,
            dpi_x=dpi,
            dpi_y=dpi,
            primary=primary,
        )

    def test_first_use_prefers_the_smallest_secondary_display(self) -> None:
        monitors = [
            self.monitor(r"\\.\DISPLAY1", 2560, 1440, primary=True),
            self.monitor(r"\\.\DISPLAY2", 2560, 1440, x=2560),
            self.monitor(r"\\.\DISPLAY3", 960, 640, y=1440),
        ]

        selected = _choose_panel_monitor(monitors, None)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.device, r"\\.\DISPLAY3")

    def test_disconnected_persisted_panel_does_not_cover_the_primary(self) -> None:
        monitors = [self.monitor(r"\\.\DISPLAY1", 2560, 1440, primary=True)]

        self.assertIsNone(_choose_panel_monitor(monitors, r"\\.\DISPLAY3"))

    def test_hardware_identity_survives_windows_display_renumbering(self) -> None:
        panel_id = r"MONITOR\RTK2555\instance-2"
        monitors = [
            self.monitor(r"\\.\DISPLAY1", 2560, 1440, primary=True),
            self.monitor(
                r"\\.\DISPLAY2",
                960,
                640,
                x=-960,
                monitor_id=panel_id,
            ),
        ]

        selected = _choose_panel_monitor(
            monitors,
            r"\\.\DISPLAY3",
            r"MONITOR\RTK2555\old-instance",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.device, r"\\.\DISPLAY2")

    def test_panel_layout_uses_two_columns_on_the_960_by_640_display(self) -> None:
        self.assertEqual(_panel_grid_shape(5, 960, 640), (2, 3))
        self.assertEqual(_panel_grid_shape(5, 640, 960), (1, 5))
        metrics = _panel_metrics(960, 640, 5, 96)
        self.assertEqual((metrics.columns, metrics.rows), (2, 3))
        self.assertEqual(metrics.value_points, 48)
        self.assertEqual(metrics.title_points, 28)
        self.assertEqual(metrics.meta_points, 24)
        self.assertEqual(metrics.footer_points, 20)

    def test_panel_type_shrinks_when_a_fourth_row_is_needed(self) -> None:
        metrics = _panel_metrics(960, 640, 7, 96)
        self.assertEqual((metrics.columns, metrics.rows), (2, 4))
        self.assertLess(metrics.title_points, 28)
        self.assertLess(metrics.value_points, 48)
        self.assertEqual(metrics.footer_points, 20)

    def test_changed_values_reuse_the_panel_tiles(self) -> None:
        # Values live on the tiles' labels; only the tile set forces a rebuild,
        # which blanks the dedicated screen for a frame.
        rows = [("codex", True), ("claude 5h", True), ("api-proxy", True)]
        key = _panel_layout_key(2, 3, 21, 8, rows)

        self.assertEqual(key, _panel_layout_key(2, 3, 21, 8, iter(rows)))
        self.assertNotEqual(key, _panel_layout_key(2, 3, 21, 8, rows[:2]))
        self.assertNotEqual(
            key,
            _panel_layout_key(2, 3, 21, 8, [("codex", True), ("claude 5h", False), ("api-proxy", True)]),
        )

    def test_monitor_label_and_negative_tk_geometry_are_stable(self) -> None:
        monitor = self.monitor(r"\\.\DISPLAY3", 960, 640, x=-960, dpi=144)

        self.assertEqual(_monitor_label(monitor), "显示器 3 · 960×640 · 150%")
        self.assertEqual(_tk_geometry(960, 640, -960, 0), "960x640-960+0")


class CodexResetCardTests(unittest.TestCase):
    LIST_URL = usage_float_module.CODEX_RESET_CARDS_URL
    CONSUME_URL = usage_float_module.CODEX_RESET_CONSUME_URL

    @staticmethod
    def iso(days: float) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(days=days)
        ).isoformat().replace("+00:00", "Z")

    def test_cards_are_sorted_by_usability_then_soonest_expiry(self) -> None:
        payload = {
            "credits": [
                {"id": "spent", "status": "redeemed", "expires_at": self.iso(1)},
                {"id": "late", "status": "available", "expires_at": self.iso(20)},
                {"id": "soon", "status": "available", "expires_at": self.iso(2)},
            ],
            "available_count": 2,
        }
        with patch.object(
            usage_float_module,
            "codex_authorized_json",
            return_value=(200, payload),
        ):
            cards, error = fetch_codex_reset_cards()

        self.assertIsNone(error)
        self.assertEqual([card.card_id for card in cards], ["soon", "late", "spent"])
        self.assertEqual([card.usable for card in cards], [True, True, False])

    def test_epoch_and_camel_case_payloads_are_both_accepted(self) -> None:
        granted = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        payload = {
            "credits": [
                {
                    "creditId": "camel",
                    "status": "AVAILABLE",
                    "resetType": "codex",
                    "grantedAt": granted.timestamp(),
                    "expiresAt": (granted + timedelta(days=30)).timestamp(),
                }
            ]
        }
        with patch.object(
            usage_float_module,
            "codex_authorized_json",
            return_value=(200, payload),
        ):
            cards, error = fetch_codex_reset_cards()

        self.assertIsNone(error)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.card_id, "camel")
        self.assertTrue(card.usable)
        self.assertEqual(card.reset_type, "codex")
        self.assertEqual(usage_float_module.parse_iso(card.granted_at), granted)

    def test_missing_credential_and_http_failures_report_distinct_errors(self) -> None:
        with patch.object(
            usage_float_module, "codex_authorized_json", return_value=(0, None)
        ):
            cards, error = fetch_codex_reset_cards()
        self.assertEqual(cards, [])
        self.assertEqual(error, "未找到可用的 Codex 登录凭证")

        with patch.object(
            usage_float_module,
            "codex_authorized_json",
            return_value=(403, {"detail": "not eligible"}),
        ):
            cards, error = fetch_codex_reset_cards()
        self.assertEqual(cards, [])
        self.assertIn("HTTP 403", error or "")
        self.assertIn("not eligible", error or "")

    def test_consume_posts_the_credit_id_with_a_dedupe_key(self) -> None:
        seen: dict[str, object] = {}

        def fake_call(url, *, home=None, method="GET", body=None):
            seen["url"] = url
            seen["home"] = home
            seen["method"] = method
            seen["body"] = body
            return 200, {"windows_reset": True}

        with patch.object(usage_float_module, "codex_authorized_json", fake_call):
            ok, message = consume_codex_reset_card("card-1", request_id="fixed")

        self.assertTrue(ok)
        self.assertIn("已使用", message)
        self.assertEqual(seen["url"], self.CONSUME_URL)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["home"], usage_float_module.CODEX_HOME)
        self.assertEqual(
            seen["body"], {"credit_id": "card-1", "redeem_request_id": "fixed"}
        )

    def test_each_consume_call_generates_its_own_dedupe_key(self) -> None:
        keys: list[str] = []

        def fake_call(url, *, home=None, method="GET", body=None):
            keys.append((body or {}).get("redeem_request_id"))
            return 200, None

        with patch.object(usage_float_module, "codex_authorized_json", fake_call):
            consume_codex_reset_card("card-1")
            consume_codex_reset_card("card-1")

        self.assertEqual(len(keys), 2)
        self.assertTrue(all(keys))
        self.assertNotEqual(keys[0], keys[1])

    def test_consume_failure_surfaces_the_backend_message(self) -> None:
        with patch.object(
            usage_float_module,
            "codex_authorized_json",
            return_value=(409, {"error": {"message": "already redeemed"}}),
        ):
            ok, message = consume_codex_reset_card("card-1")

        self.assertFalse(ok)
        self.assertIn("already redeemed", message)

    def test_display_rows_carry_the_provider_id_that_gates_the_click(self) -> None:
        providers = [
            ProviderUsage(
                provider_id="codex",
                display_name="codex",
                windows=[UsageWindow(id="usage", label="codex", used_pct=68.0)],
            ),
            ProviderUsage(
                provider_id="grok",
                display_name="grok",
                available=False,
                error="更新失败",
            ),
        ]

        rows = iter_display_rows(providers)

        self.assertEqual([row.provider_id for row in rows], ["codex", "grok"])


class CodexMultiAccountTests(unittest.TestCase):
    """A second Codex account is a second CODEX_HOME, never a shared default."""

    def _account(self, provider_id: str, home: Path) -> object:
        return usage_float_module.CodexAccount(provider_id, provider_id, home)

    def test_usage_is_read_from_the_account_home_and_labelled_with_its_id(self) -> None:
        seen: dict[str, object] = {}

        def fake_call(url, *, home=None, method="GET", body=None):
            seen["home"] = home
            return 200, {
                "plan_type": "pro",
                "rate_limit": {"primary_window": {"used_percent": 83.0}},
            }

        account = self._account("codex-2", Path("/tmp/.codex-2"))
        with patch.object(usage_float_module, "codex_authorized_json", fake_call):
            with patch.object(usage_float_module, "cache_put_provider", lambda _p: None):
                usage = usage_float_module.fetch_codex_usage(account)

        self.assertEqual(seen["home"], Path("/tmp/.codex-2"))
        self.assertEqual(usage.provider_id, "codex-2")
        self.assertEqual(usage.display_name, "codex-2")
        self.assertAlmostEqual(usage.windows[0].used_pct, 83.0)

    def test_a_failed_second_account_does_not_borrow_the_first_ones_identity(self) -> None:
        account = self._account("codex-2", Path("/tmp/.codex-2"))
        with patch.object(
            usage_float_module, "codex_authorized_json", return_value=(0, None)
        ):
            usage = usage_float_module.fetch_codex_usage(account)

        self.assertEqual(usage.provider_id, "codex-2")
        self.assertFalse(usage.available)

    def test_a_provider_without_credentials_is_dropped_rather_than_shown_failed(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            signed_in = Path(temp_dir) / "signed-in"
            signed_in.mkdir()
            (signed_in / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "t"}}), encoding="utf-8"
            )
            accounts = (
                usage_float_module.CodexAccount("codex", "codex", signed_in),
                usage_float_module.CodexAccount(
                    "codex-2", "codex-2", Path(temp_dir) / "missing"
                ),
            )
            with patch.object(usage_float_module, "CODEX_ACCOUNTS", accounts), patch.object(
                usage_float_module,
                "CODEX_ACCOUNT_BY_ID",
                {a.provider_id: a for a in accounts},
            ):
                self.assertTrue(usage_float_module.provider_available("codex"))
                self.assertFalse(usage_float_module.provider_available("codex-2"))
                self.assertEqual(
                    usage_float_module.active_providers(["codex", "codex-2"]),
                    ["codex"],
                )

    def test_codex_2_is_slotted_under_codex_not_appended_after_grok(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps({"providers": ["claude", "codex", "grok"]}), encoding="utf-8"
            )
            with patch.object(usage_float_module, "CONFIG_PATH", config_path):
                config = usage_float_module.load_config()

        self.assertEqual(
            config["providers"],
            ["claude:fable", "claude:7d", "claude:5h", "codex", "codex-2", "grok", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS],
        )
        self.assertEqual(
            config["provider_order"],
            ["claude:fable", "claude:7d", "claude:5h", "codex", "codex-2", "grok", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS],
        )
        self.assertEqual(config["disabled_providers"], [])

    def test_redeeming_targets_the_clicked_account_home(self) -> None:
        seen: dict[str, object] = {}

        def fake_call(url, *, home=None, method="GET", body=None):
            seen["home"] = home
            return 200, None

        with patch.object(usage_float_module, "codex_authorized_json", fake_call):
            consume_codex_reset_card("card-1", home=Path("/tmp/.codex-2"))

        self.assertEqual(seen["home"], Path("/tmp/.codex-2"))


class AntigravityUsageTests(unittest.TestCase):
    @staticmethod
    def report(buckets=None):
        if buckets is None:
            buckets = [
                {"id": "gemini-weekly", "remaining_fraction": 0.8, "reset_time": "2026-10-15T16:06:07Z"},
                {"id": "gemini-5h", "remaining_fraction": 0.25, "reset_time": "2026-10-09T05:03:30Z"},
                {"id": "3p-weekly", "remaining_fraction": 0.99, "reset_time": "2026-10-15T16:06:23Z"},
                {"id": "3p-5h", "remaining_fraction": 1, "reset_time": "2026-10-09T06:55:50Z"},
            ]
        return {
            "status": "SUCCESS", "num_turns": 0,
            "command": {"name": "usage", "data": {"groups": [{"name": "quota", "buckets": buckets}]}},
        }

    def test_four_quotas_show_used_percent_and_real_reset_times(self) -> None:
        windows = usage_float_module._parse_antigravity_usage(self.report())
        self.assertEqual([w.label for w in windows], ["gemini 7d", "gemini 5h", "claude 7d", "claude 5h"])
        for window, expected in zip(windows, [20, 75, 1, 0]):
            self.assertAlmostEqual(window.used_pct, expected)
        self.assertEqual(windows[0].resets_at, "2026-10-15T16:06:07Z")

    def test_weekly_only_account_does_not_invent_five_hour_windows(self) -> None:
        windows = usage_float_module._parse_antigravity_usage(self.report([
            {"id": "gemini-weekly", "remaining_fraction": 0, "reset_time": "invalid"},
        ]))
        provider = ProviderUsage("antigravity", "agy", windows=windows)
        rows = iter_display_rows([provider], ["antigravity"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "agy gemini 7d")
        self.assertEqual(rows[0].used_pct, 100)
        self.assertIsNone(rows[0].resets_at)
        self.assertFalse(row_opens_reset_ui(rows[0]))

    def test_invalid_and_disabled_buckets_do_not_become_quotas(self) -> None:
        for fraction in (None, True, "0.5", float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(fraction=fraction), self.assertRaises(ValueError):
                usage_float_module._parse_antigravity_usage(self.report([
                    {"id": "gemini-5h", "remaining_fraction": fraction},
                ]))
        for bucket in (
            {"id": "unknown", "remaining_fraction": 0.5},
            {"id": "gemini-5h", "remaining_fraction": 0.5, "disabled": True},
        ):
            with self.subTest(bucket=bucket), self.assertRaises(ValueError):
                usage_float_module._parse_antigravity_usage(self.report([bucket]))

    def test_prose_or_inference_response_is_not_a_quota_report(self) -> None:
        invalid = [None, {"status": "ERROR"}, {"status": "SUCCESS", "response": "99% remaining"}]
        other_command = self.report()
        other_command["command"]["name"] = "models"
        invalid.append(other_command)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                usage_float_module._parse_antigravity_usage(payload)

    def test_installer_location_is_found_without_path_entry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "agy" / "bin" / "agy.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}, clear=True), patch.object(
                usage_float_module.shutil, "which", return_value=None
            ), patch.object(usage_float_module, "HOME", root):
                self.assertEqual(usage_float_module.find_antigravity_cli(), binary)
                for override in ("", str(root / "missing.exe")):
                    with patch.dict(os.environ, {"ANTIGRAVITY_CLI_PATH": override}):
                        self.assertIsNone(usage_float_module.find_antigravity_cli())

    def test_cli_receives_the_system_proxy_unless_a_proxy_variable_is_set(self) -> None:
        system = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
        with patch.dict(os.environ, {}, clear=True), patch.object(
            usage_float_module.urllib.request, "getproxies", return_value=system
        ):
            env = usage_float_module._antigravity_env()
            self.assertEqual((env["HTTPS_PROXY"], env["HTTP_PROXY"]), (system["https"], system["http"]))
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://own:1"}, clear=True), patch.object(
            usage_float_module.urllib.request, "getproxies", return_value=system
        ):
            env = usage_float_module._antigravity_env()
            self.assertEqual(env["HTTPS_PROXY"], "http://own:1")
            self.assertNotIn("HTTP_PROXY", env)
        with patch.dict(os.environ, {}, clear=True), patch.object(
            usage_float_module.urllib.request, "getproxies", return_value={}
        ):
            self.assertNotIn("HTTPS_PROXY", usage_float_module._antigravity_env())

    def test_old_or_unknown_cli_never_receives_usage_as_a_prompt(self) -> None:
        for version in ("1.1.10", "1.0.99", "unknown"):
            with self.subTest(version=version), patch.object(
                usage_float_module, "find_antigravity_cli", return_value=Path("agy.exe")
            ), patch.object(usage_float_module, "_run_antigravity_cli", return_value=version) as run:
                result = usage_float_module.fetch_antigravity_usage()
                self.assertFalse(result.available)
                self.assertEqual(result.error, "更新失败")
                self.assertEqual([call.args[1] for call in run.call_args_list], [["--version"]])

    def test_live_schema_fetch_is_cached_and_expands_in_settings_order(self) -> None:
        with TemporaryDirectory() as directory, patch.object(
            usage_float_module, "CACHE_PATH", Path(directory) / "cache.json"
        ), patch.object(usage_float_module, "find_antigravity_cli", return_value=Path("agy.exe")), patch.object(
            usage_float_module, "_run_antigravity_cli", side_effect=["1.3.2", json.dumps(self.report())]
        ) as run:
            fetched = usage_float_module.collect_local_usage(list(usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS))
            self.assertEqual(len(fetched), 1)
            self.assertEqual(run.call_count, 2)  # version + one quota query for all four rows
            self.assertTrue(fetched[0].available)
            self.assertIsNotNone(usage_float_module.cache_get_provider("antigravity"))
            self.assertEqual(run.call_args_list[-1].args[1], ["-p", "/usage", "--output-format", "json"])
        providers = [ProviderUsage("codex", "codex", windows=[UsageWindow("primary", "5h", 10)]), *fetched]
        rows = iter_display_rows(providers, ["antigravity", "codex"])
        self.assertEqual([row.title for row in rows], [
            "agy gemini 7d", "agy gemini 5h", "agy claude 7d", "agy claude 5h", "codex",
        ])
        self.assertEqual(iter_display_rows(providers, ["codex"])[0].title, "codex")
        self.assertEqual(len(iter_display_rows(providers, ["codex"])), 1)

    def test_settings_scan_has_four_independent_entries(self) -> None:
        items = list(usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS)
        self.assertEqual(items, [
            "antigravity:gemini-weekly", "antigravity:gemini-5h",
            "antigravity:3p-weekly", "antigravity:3p-5h",
        ])
        with patch.object(usage_float_module, "find_antigravity_cli", return_value=Path("agy.exe")):
            scanned = usage_float_module.scan_providers(items, items)
            rows = [row for row in scanned if row["id"] in items]
            self.assertTrue(all(row["available"] for row in rows))
            self.assertEqual(usage_float_module.active_providers(items), items)
        self.assertEqual([row["label"] for row in rows], [
            "agy gemini 7d", "agy gemini 5h", "agy claude 7d", "agy claude 5h",
        ])
        with patch.object(usage_float_module, "find_antigravity_cli", return_value=None):
            self.assertEqual(usage_float_module.active_providers(items), [])

    def test_legacy_group_keeps_its_position_and_disabled_state(self) -> None:
        items = list(usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS)
        for disabled in ([], ["antigravity"]):
            with self.subTest(disabled=disabled):
                order, enabled, hidden = usage_float_module.resolve_provider_config(
                    ["grok", "codex"] if disabled else ["grok", "antigravity", "codex"],
                    disabled, ["grok", "antigravity", "codex"],
                )
                self.assertEqual(order[:6], ["grok", *items, "codex"])
                self.assertNotIn("antigravity", order)
                if disabled:
                    self.assertTrue(all(item in hidden and item not in enabled for item in items))
                else:
                    self.assertTrue(all(item in enabled for item in items))

    def test_individual_toggles_and_interleaved_order_survive_reload(self) -> None:
        items = list(usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS)
        enabled = [items[3], "codex", items[0]]
        order = [*enabled, items[1], items[2], *[pid for pid in DEFAULT_PROVIDERS if pid not in ["codex", *items]]]
        disabled = [pid for pid in order if pid not in enabled]
        with TemporaryDirectory() as directory, patch.object(
            usage_float_module, "CONFIG_PATH", Path(directory) / "config.json"
        ):
            usage_float_module.save_config({"providers": enabled, "provider_order": order, "disabled_providers": disabled})
            config = usage_float_module.load_config()
        self.assertEqual(config["providers"], enabled)
        self.assertEqual(config["provider_order"], order)
        self.assertEqual(config["disabled_providers"], disabled)
        provider = ProviderUsage("antigravity", "agy", windows=usage_float_module._parse_antigravity_usage(self.report()))
        codex = ProviderUsage("codex", "codex", windows=[UsageWindow("primary", "5h", 10)])
        rows = iter_display_rows([codex, provider], config["providers"])
        self.assertEqual([row.title for row in rows], ["agy claude 5h", "codex", "agy gemini 7d"])
        moved = usage_float_module.move_provider(config["providers"], items[0], items[3])
        self.assertEqual([row.title for row in iter_display_rows([codex, provider], moved)], [
            "agy gemini 7d", "agy claude 5h", "codex",
        ])

    def test_shared_failure_uses_first_enabled_antigravity_row(self) -> None:
        failed = ProviderUsage("antigravity", "agy", available=False, error="更新失败")
        codex = ProviderUsage("codex", "codex", windows=[UsageWindow("primary", "5h", 10)])
        rows = iter_display_rows([failed, codex], ["codex", "antigravity:3p-5h", "antigravity:gemini-weekly"])
        self.assertEqual([row.title for row in rows], ["codex", "agy"])
        self.assertTrue(rows[1].failed)
        self.assertEqual(iter_display_rows([failed], ["codex"]), [])

    def test_timeout_and_login_failure_show_failure_instead_of_zero_usage(self) -> None:
        for error in (subprocess.TimeoutExpired("agy", 60), ValueError("login required")):
            with self.subTest(error=error), patch.object(
                usage_float_module, "find_antigravity_cli", return_value=Path("agy.exe")
            ), patch.object(usage_float_module, "_run_antigravity_cli", side_effect=error):
                result = usage_float_module.fetch_antigravity_usage()
                rows = iter_display_rows([result], ["antigravity"])
                self.assertTrue(rows[0].failed)
                self.assertIsNone(rows[0].used_pct)

    def test_usage_failure_retries_once_and_keeps_the_second_result(self) -> None:
        with TemporaryDirectory() as directory, patch.object(
            usage_float_module, "CACHE_PATH", Path(directory) / "cache.json"
        ), patch.object(
            usage_float_module, "ANTIGRAVITY_USAGE_RETRY_SECONDS", 0
        ), patch.object(
            usage_float_module, "find_antigravity_cli", return_value=Path("agy.exe")
        ), patch.object(
            usage_float_module, "_run_antigravity_cli", side_effect=[
                "1.3.2", ValueError("Antigravity CLI query failed"), json.dumps(self.report()),
            ]
        ) as run, patch.object(usage_float_module.time, "sleep") as sleep:
            result = usage_float_module.fetch_antigravity_usage()
            cached = usage_float_module.cache_get_provider("antigravity")

        self.assertTrue(result.available)
        self.assertFalse(result.stale)
        self.assertEqual([window.label for window in result.windows], [
            "gemini 7d", "gemini 5h", "claude 7d", "claude 5h",
        ])
        self.assertIsNotNone(cached)
        self.assertEqual([call.args[1] for call in run.call_args_list], [
            ["--version"],
            ["-p", "/usage", "--output-format", "json"],
            ["-p", "/usage", "--output-format", "json"],
        ])
        sleep.assert_called_once_with(0)

    def test_repeated_usage_failure_shows_the_cached_read(self) -> None:
        with TemporaryDirectory() as directory, patch.object(
            usage_float_module, "CACHE_PATH", Path(directory) / "cache.json"
        ), patch.object(
            usage_float_module, "ANTIGRAVITY_USAGE_RETRY_SECONDS", 0
        ), patch.object(
            usage_float_module, "find_antigravity_cli", return_value=Path("agy.exe")
        ), patch.object(usage_float_module.time, "sleep") as sleep:
            good = ProviderUsage(
                "antigravity", "agy",
                windows=usage_float_module._parse_antigravity_usage(self.report()),
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            usage_float_module.cache_put_provider(good)
            with patch.object(
                usage_float_module, "_run_antigravity_cli", side_effect=[
                    "1.3.2", ValueError("Antigravity CLI query failed"),
                    ValueError("Antigravity CLI query failed"),
                ]
            ) as run:
                result = usage_float_module.fetch_antigravity_usage()

        self.assertTrue(result.available)
        self.assertTrue(result.stale)
        self.assertIsNone(result.error)
        self.assertEqual(
            [(window.id, window.used_pct) for window in result.windows],
            [(window.id, window.used_pct) for window in good.windows],
        )
        self.assertEqual(run.call_count, 3)
        sleep.assert_called_once_with(0)

    def test_repeated_usage_failure_without_cache_stays_a_failure(self) -> None:
        with TemporaryDirectory() as directory, patch.object(
            usage_float_module, "CACHE_PATH", Path(directory) / "cache.json"
        ), patch.object(
            usage_float_module, "ANTIGRAVITY_USAGE_RETRY_SECONDS", 0
        ), patch.object(
            usage_float_module, "find_antigravity_cli", return_value=Path("agy.exe")
        ), patch.object(
            usage_float_module, "_run_antigravity_cli", side_effect=[
                "1.3.2", ValueError("Antigravity CLI query failed"),
                ValueError("Antigravity CLI query failed"),
            ]
        ) as run, patch.object(usage_float_module.time, "sleep") as sleep:
            result = usage_float_module.fetch_antigravity_usage()

        self.assertFalse(result.available)
        self.assertEqual(result.error, "更新失败")
        self.assertFalse(result.stale)
        self.assertEqual(result.windows, [])
        self.assertEqual(run.call_count, 3)
        sleep.assert_called_once_with(0)
        rows = iter_display_rows([result], ["antigravity"])
        self.assertTrue(rows[0].failed)
        self.assertEqual(rows[0].summary, "更新失败")

    def test_missing_cli_does_not_retry(self) -> None:
        with TemporaryDirectory() as directory, patch.object(
            usage_float_module, "CACHE_PATH", Path(directory) / "cache.json"
        ), patch.object(
            usage_float_module, "find_antigravity_cli", return_value=None
        ), patch.object(usage_float_module, "_run_antigravity_cli") as run, patch.object(
            usage_float_module.time, "sleep"
        ) as sleep:
            usage_float_module.cache_put_provider(ProviderUsage(
                "antigravity", "agy",
                windows=usage_float_module._parse_antigravity_usage(self.report()),
                fetched_at=datetime.now(timezone.utc).isoformat(),
            ))
            result = usage_float_module.fetch_antigravity_usage()

        self.assertFalse(result.available)
        self.assertEqual(result.error, "更新失败")
        self.assertFalse(result.stale)
        run.assert_not_called()
        sleep.assert_not_called()

    def test_runner_caps_output(self) -> None:
        with patch.object(usage_float_module, "ANTIGRAVITY_MAX_OUTPUT", 64), self.assertRaises(ValueError):
            usage_float_module._run_antigravity_cli(Path(sys.executable), ["-c", "print('x' * 128)"], timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup")
    def test_runner_reaps_plugin_children_on_success_and_timeout(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        code = (
            "import subprocess,sys,time; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(float(sys.argv[2]))"
        )
        for delay in (0.2, 60):
            with self.subTest(delay=delay), TemporaryDirectory() as directory:
                pid_file = Path(directory) / "child.pid"
                try:
                    if delay == 60:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            usage_float_module._run_antigravity_cli(
                                Path(sys.executable), ["-c", code, str(pid_file), str(delay)], timeout=2
                            )
                    else:
                        usage_float_module._run_antigravity_cli(
                            Path(sys.executable), ["-c", code, str(pid_file), str(delay)], timeout=5
                        )
                    self.assertTrue(pid_file.exists(), "Child process was not launched")
                finally:
                    if pid_file.exists():
                        handle = kernel.OpenProcess(0x100001, False, int(pid_file.read_text()))
                        if handle:
                            try:
                                stopped = kernel.WaitForSingleObject(handle, 2000) == 0
                                if not stopped:
                                    kernel.TerminateProcess(handle, 1)
                                    kernel.WaitForSingleObject(handle, 2000)
                                self.assertTrue(stopped, "CLI left its child running")
                            finally:
                                kernel.CloseHandle(handle)


class LlmProxyUsageTests(unittest.TestCase):
    def test_openai_base_keeps_or_adds_v1(self) -> None:
        self.assertEqual(
            usage_float_module._llm_proxy_openai_base(
                "https://llm-proxy.example.com/v1"
            ),
            "https://llm-proxy.example.com/v1",
        )
        self.assertEqual(
            usage_float_module._llm_proxy_openai_base(
                "https://llm-proxy.example.com"
            ),
            "https://llm-proxy.example.com/v1",
        )
        self.assertEqual(usage_float_module._llm_proxy_openai_base(""), "")

    def test_spend_headers_are_read_case_insensitively(self) -> None:
        spend, budget, cost = usage_float_module.parse_llm_proxy_spend_headers(
            {
                "X-Litellm-Key-Spend": "10.5",
                "X-Litellm-Key-Max-Budget": "200",
                "X-Litellm-Response-Cost": "0.01",
            }
        )
        self.assertAlmostEqual(spend or 0, 10.5)
        self.assertAlmostEqual(budget or 0, 200)
        self.assertAlmostEqual(cost or 0, 0.01)

    def test_spend_is_shown_as_integer_dollars(self) -> None:
        self.assertEqual(usage_float_module.format_llm_proxy_spend(2.65, 200), "$3/200")
        self.assertEqual(usage_float_module.format_llm_proxy_spend(2.5, 200), "$3/200")
        self.assertEqual(usage_float_module.format_llm_proxy_spend(2.4, 200), "$2/200")
        self.assertEqual(usage_float_module.format_llm_proxy_spend(2.0, 200), "$2/200")
        self.assertEqual(usage_float_module.format_llm_proxy_spend(12.3), "$12")

    def test_stale_spend_is_the_current_request_cost(self) -> None:
        self.assertTrue(
            usage_float_module._spend_looks_like_this_request(2e-07, 2e-07)
        )
        self.assertFalse(
            usage_float_module._spend_looks_like_this_request(0.1047, 2e-07)
        )

    def test_config_proxy_key_makes_the_row_available(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cfg = Path(temp_dir) / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "llm_proxy_api_key": "sk-test-key",
                        "llm_proxy_endpoint": "https://llm-proxy.example.com/v1",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"LLM_PROXY_API_KEY": "", "LLM_PROXY_ENDPOINT": ""},
                clear=False,
            ), patch.object(usage_float_module, "CONFIG_PATH", cfg):
                self.assertTrue(usage_float_module.provider_available("api-proxy"))
                key, base = usage_float_module.read_llm_proxy_credentials()
        self.assertEqual(key, "sk-test-key")
        self.assertEqual(base, "https://llm-proxy.example.com/v1")

    def test_missing_key_hides_the_row(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with patch.object(
                usage_float_module, "CONFIG_PATH", Path(temp_dir) / "cfg.json"
            ), patch.dict(
                "os.environ",
                {"LLM_PROXY_API_KEY": "", "LLM_PROXY_ENDPOINT": ""},
                clear=False,
            ):
                self.assertFalse(usage_float_module.provider_available("api-proxy"))

    def test_fetch_retries_when_spend_equals_this_request(self) -> None:
        calls: list[int] = []

        def fake_probe(_base, _key, *, model, kind):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return 200, {
                    "x-litellm-key-spend": "0.0000002",
                    "x-litellm-key-max-budget": "200.0",
                    "x-litellm-response-cost": "0.0000002",
                }
            return 200, {
                "x-litellm-key-spend": "20.0",
                "x-litellm-key-max-budget": "200.0",
                "x-litellm-response-cost": "0.0000002",
            }

        with patch.object(
            usage_float_module,
            "read_llm_proxy_credentials",
            return_value=("sk-test", "https://llm-proxy.example.com/v1"),
        ), patch.object(
            usage_float_module, "_llm_proxy_probe", fake_probe
        ), patch.object(
            usage_float_module, "_llm_proxy_budget_settings", return_value=("7d", "UTC")
        ), patch.object(usage_float_module, "cache_put_provider", lambda _p: None):
            usage = usage_float_module.fetch_api_proxy_usage()

        self.assertEqual(len(calls), 2)
        self.assertTrue(usage.available)
        self.assertAlmostEqual(usage.windows[0].used_pct, 10.0)
        self.assertEqual(usage.summary, "$20/200")
        self.assertIsNotNone(usage.windows[0].resets_at)
        rows = iter_display_rows([usage])
        self.assertEqual(rows[0].title, "api-proxy")
        self.assertEqual(rows[0].summary, "$20/200")
        self.assertIsNotNone(rows[0].resets_at)
        self.assertTrue(format_remaining(rows[0].resets_at))

    def test_default_budget_duration_is_weekly(self) -> None:
        self.assertEqual(usage_float_module.LLM_PROXY_BUDGET_DURATION_DEFAULT, "7d")
        with patch.object(
            usage_float_module, "CONFIG_PATH", Path("/missing.json")
        ), patch.dict(
            "os.environ",
            {"LLM_PROXY_BUDGET_DURATION": "", "LLM_PROXY_TIMEZONE": ""},
            clear=False,
        ):
            duration, timezone_name = usage_float_module._llm_proxy_budget_settings()
        self.assertEqual(duration, "7d")
        self.assertEqual(timezone_name, "UTC+8")
        self.assertEqual(LLM_PROXY_TIMEZONE_DEFAULT, "UTC+8")

    def test_utc_plus_eight_weekly_reset_is_monday_local_midnight(self) -> None:
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
        reset = usage_float_module.next_llm_proxy_budget_reset(
            "7d", now=now, timezone_name="UTC+8"
        )
        self.assertIsNotNone(reset)
        assert reset is not None
        self.assertEqual(reset.utcoffset(), timedelta(hours=8))
        self.assertEqual(reset.weekday(), 0)
        self.assertEqual(reset.hour, 0)
        self.assertEqual(
            reset.astimezone(timezone.utc),
            datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc),
        )

    def test_monthly_budget_resets_on_the_first(self) -> None:
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
        reset = usage_float_module.next_llm_proxy_budget_reset(
            "30d", now=now, timezone_name="UTC"
        )
        self.assertEqual(reset, datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
        on_the_first = usage_float_module.next_llm_proxy_budget_reset(
            "1mo",
            now=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
            timezone_name="UTC",
        )
        self.assertEqual(on_the_first, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))

    def test_daily_and_weekly_budget_resets_align_to_midnight(self) -> None:
        wednesday = datetime(2026, 9, 16, 15, 30, tzinfo=timezone.utc)
        self.assertEqual(
            usage_float_module.next_llm_proxy_budget_reset(
                "1d", now=wednesday, timezone_name="UTC"
            ),
            datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            usage_float_module.next_llm_proxy_budget_reset(
                "7d", now=wednesday, timezone_name="UTC"
            ),
            datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc),
        )

    def test_header_reset_timestamp_wins_over_duration(self) -> None:
        iso = usage_float_module.resolve_llm_proxy_resets_at(
            {"x-litellm-key-budget-reset-at": "2026-10-01T00:00:00Z"},
            now=datetime(2026, 9, 17, tzinfo=timezone.utc),
            duration="1d",
            timezone_name="UTC",
        )
        self.assertIsNotNone(iso)
        parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        self.assertEqual(parsed.date(), datetime(2026, 10, 1).date())


    def test_shortcut_config_migrates_legacy_double_ctrl_flag(self) -> None:
        disabled = usage_float_module._normalize_shortcut_config(
            {"double_ctrl_toggle": False}
        )
        self.assertFalse(disabled["shortcut_enabled"])
        enabled = usage_float_module._normalize_shortcut_config({})
        self.assertTrue(enabled["shortcut_enabled"])
        self.assertEqual(enabled["shortcut_mode"], "repeat")
        self.assertEqual(enabled["shortcut_key"], "ctrl")
        self.assertEqual(enabled["shortcut_repeat_count"], 2)

    def test_shortcut_combo_normalizes_and_keeps_modifiers_first(self) -> None:
        self.assertEqual(
            usage_float_module._normalize_shortcut_combo("w+ctrl+shift"),
            ["ctrl", "shift", "w"],
        )
        self.assertEqual(
            usage_float_module._normalize_shortcut_combo(["ctrl"]),
            ["ctrl", "alt"],
        )


class ProviderConfigTests(unittest.TestCase):
    def test_new_defaults_are_enabled_unless_already_disabled(self) -> None:
        order, enabled, disabled = usage_float_module.resolve_provider_config(
            ["claude", "codex", "grok"],
            [],
            ["claude", "codex", "grok"],
        )
        self.assertEqual(order, ["claude:fable", "claude:7d", "claude:5h", "codex", "codex-2", "grok", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS])
        self.assertEqual(enabled, ["claude:fable", "claude:7d", "claude:5h", "codex", "codex-2", "grok", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS])
        self.assertEqual(disabled, [])

    def test_disabled_providers_keep_order_and_stay_hidden(self) -> None:
        order, enabled, disabled = usage_float_module.resolve_provider_config(
            ["grok", "codex"],
            ["claude"],
            ["grok", "codex", "claude"],
        )
        self.assertEqual(order, ["grok", "codex", "codex-2", "claude:fable", "claude:7d", "claude:5h", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS])
        self.assertEqual(enabled, ["grok", "codex", "codex-2", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS])
        self.assertEqual(disabled, ["claude:fable", "claude:7d", "claude:5h"])

    def test_unchecking_every_row_stays_empty(self) -> None:
        order, enabled, disabled = usage_float_module.resolve_provider_config(
            [],
            list(DEFAULT_PROVIDERS),
            list(DEFAULT_PROVIDERS),
        )
        self.assertEqual(order, list(DEFAULT_PROVIDERS))
        self.assertEqual(enabled, [])
        self.assertEqual(disabled, list(DEFAULT_PROVIDERS))

    def test_move_provider_places_source_on_target_index(self) -> None:
        self.assertEqual(
            usage_float_module.move_provider(
                ["codex", "grok", "claude:5h"], "codex", "claude:5h"
            ),
            ["grok", "claude:5h", "codex"],
        )
        self.assertEqual(
            usage_float_module.move_provider(
                ["codex", "grok", "claude:5h"], "claude:5h", "codex"
            ),
            ["claude:5h", "codex", "grok"],
        )

    def test_load_config_preserves_explicit_disabled_list(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "providers": ["grok", "codex"],
                        "provider_order": ["grok", "codex", "claude"],
                        "disabled_providers": ["claude"],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(usage_float_module, "CONFIG_PATH", config_path):
                config = usage_float_module.load_config()

        self.assertEqual(
            config["provider_order"],
            ["grok", "codex", "codex-2", "claude:fable", "claude:7d", "claude:5h", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS],
        )
        self.assertEqual(config["providers"], ["grok", "codex", "codex-2", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS])
        self.assertEqual(config["disabled_providers"], ["claude:fable", "claude:7d", "claude:5h"])

    def test_scan_providers_reports_login_state_in_saved_order(self) -> None:
        availability = {
            "codex": True,
            "codex-2": False,
            "grok": True,
            "claude": False,
        }
        with patch.object(
            usage_float_module,
            "provider_available",
            side_effect=lambda pid: availability.get(
                usage_float_module.provider_fetch_id(pid), False
            ),
        ), patch.object(
            usage_float_module,
            "discovered_default_providers",
            return_value=list(DEFAULT_PROVIDERS),
        ):
            rows = usage_float_module.scan_providers(
                ["grok", "codex"],
                ["grok", "codex", "claude"],
            )

        self.assertEqual(
            [row["id"] for row in rows],
            ["grok", "codex", "codex-2", "claude:fable", "claude:7d", "claude:5h", *usage_float_module.ANTIGRAVITY_DISPLAY_ITEMS],
        )
        self.assertEqual(rows[0]["label"], "Grok")
        self.assertEqual(rows[3]["label"], "fable 7d")
        self.assertTrue(rows[0]["available"])
        self.assertFalse(rows[3]["available"])

    def test_discover_litellm_proxies_from_router_override_and_pi_usage(self) -> None:
        with TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "accounts-override.json"
            override.write_text(
                json.dumps(
                    {
                        "accounts": {
                            "team-proxy": {
                                "backend_kind": "litellm",
                                "endpoint": "https://proxy.example.test",
                                "api_key": "sk-team-proxy",
                                "label": "Team Proxy",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            pi_home = Path(temp_dir) / "pi-agent"
            pi_home.mkdir()
            (pi_home / "auth.json").write_text(
                json.dumps(
                    {
                        "openai-codex": {
                            "type": "oauth",
                            "access": "pi-access-tok",
                            "refresh": "pi-refresh-tok",
                            "accountId": "distinct-pi-account",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                usage_float_module.os.environ,
                {
                    "ROUTE_MANAGER_ACCOUNTS_OVERRIDE": str(override),
                    "PI_CODING_AGENT_DIR": str(pi_home),
                },
                clear=False,
            ), patch.object(usage_float_module, "HOME", Path(temp_dir)), patch.object(
                usage_float_module, "CONFIG_PATH", Path(temp_dir) / "config.json"
            ):
                proxies = usage_float_module.discover_litellm_proxies()
                self.assertIn("team-proxy", proxies)
                self.assertEqual(
                    usage_float_module.read_llm_proxy_credentials("team-proxy"),
                    ("sk-team-proxy", "https://proxy.example.test/v1"),
                )
                self.assertTrue(usage_float_module.provider_available("team-proxy"))
                self.assertTrue(usage_float_module.provider_available("pi"))
                discovered = usage_float_module.discovered_default_providers()
                self.assertIn("team-proxy", discovered)
                self.assertIn("pi", discovered)



class ClaudeResetTests(unittest.TestCase):
    # Shape captured live from /api/oauth/usage?cedar_ember=1&skip_spend=1.
    LIVE_GRANT = {
        "id": "opus55-launch-promax-20260921",
        "label": "Claude Opus 5.5 launch: one usage-limit reset for Pro and Max",
        "resets_total": 1,
        "resets_left": 1,
        "starts_at": "2026-09-22T16:00:00+00:00",
        "ends_at": "2026-10-22T16:00:00+00:00",
        "clears": ["five_hour", "seven_day", "seven_day_overage_included"],
        "paused": False,
        "usable_now": True,
        "use_requires_limit": False,
        "blocking": [],
    }

    def block(self, **overrides):
        payload = {
            "eligible": True,
            "ineligible_reason": None,
            "at_limit": False,
            "exhausted": [],
            "grants": [dict(self.LIVE_GRANT)],
            "next_grant_id": self.LIVE_GRANT["id"],
            "weekly_resets_at": "2026-09-24T21:00:00+00:00",
            "cooldown_until": None,
        }
        payload.update(overrides)
        return payload

    def test_live_grant_shape_is_read_into_the_fields_the_hud_shows(self) -> None:
        status = parse_claude_reset_status(self.block())

        self.assertTrue(status.available)
        self.assertEqual(status.resets_left, 1)
        self.assertTrue(status.usable_now)
        self.assertFalse(status.use_requires_limit)
        self.assertEqual(status.expires_at, "2026-10-22T16:00:00+00:00")
        self.assertEqual(status.weekly_resets_at, "2026-09-24T21:00:00+00:00")
        self.assertIn("Opus 5.5", status.label or "")
        self.assertEqual(
            status.clears, ("five_hour", "seven_day", "seven_day_overage_included")
        )

    def test_an_ineligible_account_reports_its_reason_and_no_resets(self) -> None:
        status = parse_claude_reset_status(
            {
                "eligible": False,
                "ineligible_reason": "surface",
                "at_limit": False,
                "grants": [],
                "next_grant_id": None,
                "weekly_resets_at": None,
                "cooldown_until": None,
            }
        )

        self.assertFalse(status.available)
        self.assertEqual(status.resets_left, 0)
        self.assertEqual(status.ineligible_reason, "surface")

    def test_spent_and_paused_grants_do_not_count_as_available(self) -> None:
        spent = dict(self.LIVE_GRANT, resets_left=0)
        paused = dict(self.LIVE_GRANT, id="paused", paused=True)

        status = parse_claude_reset_status(self.block(grants=[spent, paused]))

        self.assertFalse(status.available)
        self.assertEqual(status.resets_left, 0)

    def test_the_grant_that_lapses_first_supplies_the_headline(self) -> None:
        late = dict(
            self.LIVE_GRANT,
            id="late",
            label="late one",
            ends_at="2026-12-01T00:00:00+00:00",
            clears=["seven_day"],
        )
        soon = dict(
            self.LIVE_GRANT,
            id="soon",
            label="soon one",
            resets_left=2,
            ends_at="2026-10-01T00:00:00+00:00",
            clears=["five_hour"],
        )

        status = parse_claude_reset_status(self.block(grants=[late, soon]))

        self.assertEqual(status.resets_left, 3)
        self.assertEqual(status.label, "soon one")
        self.assertEqual(status.expires_at, "2026-10-01T00:00:00+00:00")
        self.assertEqual(status.clears, ("five_hour",))

    def test_camel_case_and_epoch_grants_are_accepted(self) -> None:
        grant = {
            "grantId": "camel",
            "resetsLeft": 2,
            "endsAt": 1792000000,
            "usableNow": True,
            "useRequiresLimit": True,
            "limitTypes": ["five_hour"],
        }

        status = parse_claude_reset_status({"grants": [grant]})

        self.assertEqual(status.resets_left, 2)
        self.assertTrue(status.usable_now)
        self.assertTrue(status.use_requires_limit)
        self.assertEqual(status.clears, ("five_hour",))
        self.assertIsNotNone(status.expires_at)

    def test_a_missing_block_degrades_to_nothing_available(self) -> None:
        for value in (None, {}, [], "nope"):
            status = parse_claude_reset_status(value)
            self.assertFalse(status.available)
            self.assertEqual(status.resets_left, 0)

    def claude_provider(self, block) -> ProviderUsage:
        return ProviderUsage(
            provider_id="claude",
            display_name="claude",
            windows=[
                UsageWindow(id="weekly_fable", label="fable 7d", used_pct=66.0),
                UsageWindow(id="five_hour", label="5h", used_pct=16.0),
            ],
            reset_block=block,
        )

    def test_settings_items_order_and_filter_claude_rows(self) -> None:
        provider = self.claude_provider(self.block())
        provider.windows.insert(
            1, UsageWindow(id="seven_day", label="7d", used_pct=40.0)
        )
        codex = ProviderUsage(
            provider_id="codex",
            display_name="codex",
            windows=[UsageWindow(id="w", label="5h", used_pct=1.0)],
        )

        rows = iter_display_rows(
            [provider, codex], ["claude:5h", "codex", "claude:fable"]
        )

        self.assertEqual(
            [row.title for row in rows], ["claude 5h", "codex", "fable 7d"]
        )
        # Every row still shows when no Settings order is given.
        self.assertEqual(len(iter_display_rows([provider, codex])), 4)

    def test_a_failed_claude_line_takes_its_first_rows_place(self) -> None:
        failed = ProviderUsage(
            provider_id="claude", display_name="claude", available=False, error="更新失败"
        )
        codex = ProviderUsage(
            provider_id="codex",
            display_name="codex",
            windows=[UsageWindow(id="w", label="5h", used_pct=1.0)],
        )

        rows = iter_display_rows([codex, failed], ["claude:5h", "codex"])
        self.assertEqual([row.title for row in rows], ["claude", "codex"])
        self.assertEqual(iter_display_rows([codex, failed], ["codex"])[0].title, "codex")
        self.assertEqual(len(iter_display_rows([codex, failed], ["codex"])), 1)

    def test_claude_items_fetch_claude_once(self) -> None:
        calls = []
        with patch.object(
            usage_float_module,
            "fetch_claude_usage",
            side_effect=lambda **_kw: calls.append(1) or ProviderUsage(provider_id="claude"),
        ):
            out = usage_float_module.collect_local_usage(["claude:fable", "claude:5h"])

        self.assertEqual(len(calls), 1)
        self.assertEqual([p.provider_id for p in out], ["claude"])

    def test_claude_7d_and_5h_rows_open_their_own_reset_dialogs(self) -> None:
        provider = self.claude_provider(self.block())
        provider.windows.insert(1, UsageWindow(id="seven_day", label="7d", used_pct=40.0))
        rows = iter_display_rows([provider])
        targets = {
            row.window_id: usage_float_module.reset_target_id(row)
            for row in rows
            if row_opens_reset_ui(row)
        }

        # fable 7d is a model-scoped window that no reset refills.
        self.assertEqual(targets, {"seven_day": "claude:7d", "five_hour": "claude:5h"})

    # Shape captured live from the same usage response (account not enrolled).
    LIVE_SESSION_BLOCK = {
        "eligible": False,
        "ineligible_reason": "not_at_wall",
        "in_experiment": False,
        "arm": None,
        "available": False,
        "next_available_at": None,
        "weekly_resets_at": None,
        "resets_per_week": 1,
        "event_props": None,
    }

    def test_an_unenrolled_session_reset_is_not_offered(self) -> None:
        reset = usage_float_module.parse_claude_session_reset(self.LIVE_SESSION_BLOCK)

        self.assertFalse(reset.offered)
        self.assertFalse(reset.claimable)
        self.assertEqual(reset.ineligible_reason, "not_at_wall")

    def test_a_session_reset_is_claimable_only_when_available_in_the_reset_arm(self) -> None:
        live = dict(self.LIVE_SESSION_BLOCK, eligible=True, ineligible_reason=None,
                    in_experiment=True, arm="reset", available=True)
        self.assertTrue(usage_float_module.parse_claude_session_reset(live).claimable)
        for change in ({"arm": "control"}, {"available": False}, {"eligible": False}):
            reset = usage_float_module.parse_claude_session_reset(dict(live, **change))
            self.assertFalse(reset.claimable, change)

    def test_a_rate_limited_read_shows_the_last_cached_numbers(self) -> None:
        with TemporaryDirectory() as temp_dir, patch.object(
            usage_float_module, "CACHE_PATH", Path(temp_dir) / "cache.json"
        ), patch.object(
            usage_float_module, "read_claude_oauth", return_value=({"accessToken": "t"}, None)
        ), patch.object(
            usage_float_module, "claude_authorized_json", return_value=(429, None)
        ):
            self.assertFalse(usage_float_module.fetch_claude_usage(force=True).available)

            good = self.claude_provider(self.block())
            good.available = True
            good.fetched_at = datetime.now(timezone.utc).isoformat()
            usage_float_module.cache_put_provider(good)
            shown = usage_float_module.fetch_claude_usage(force=True)
            # Backoff is now set, so the unforced refresh uses the cache too.
            backed_off = usage_float_module.fetch_claude_usage()

        for result in (shown, backed_off):
            self.assertTrue(result.available)
            self.assertTrue(result.stale)
            self.assertEqual([w.id for w in result.windows], ["weekly_fable", "five_hour"])

    def test_session_reset_is_read_from_the_at_wall_usage_query(self) -> None:
        # The regular usage read returns juniper_tide as null.
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            return 200, {"juniper_tide": self.LIVE_SESSION_BLOCK}

        with patch.object(usage_float_module, "claude_authorized_json", side_effect=fake):
            reset, error = usage_float_module.fetch_claude_session_reset()

        self.assertIsNone(error)
        self.assertEqual(reset.ineligible_reason, "not_at_wall")
        self.assertIn("at_wall=1", calls[0])

    def test_session_claim_posts_only_the_program(self) -> None:
        calls = []

        def fake(url, **kwargs):
            calls.append((url, kwargs))
            return 200, {"result": "already_used"}

        # Mocked: a real claim would spend this week's reset.
        with patch.object(
            usage_float_module, "claude_organization_uuid", return_value="org-1"
        ), patch.object(usage_float_module, "claude_authorized_json", side_effect=fake):
            result = usage_float_module.claim_claude_session_reset()

        self.assertFalse(result.ok)
        self.assertIn("已经用过", result.message)
        url, kwargs = calls[0]
        self.assertTrue(url.endswith("/api/organizations/org-1/reset_rate_limits"))
        self.assertEqual(kwargs["body"], {"program": "juniper_tide"})

    def test_reset_block_survives_the_usage_cache_round_trip(self) -> None:
        block = self.block()
        restored = usage_float_module._provider_from_dict(
            usage_float_module._provider_to_dict(self.claude_provider(block))
        )

        self.assertEqual(restored.reset_block, block)
        self.assertEqual(claude_reset_status(restored).resets_left, 1)

    def test_the_usage_request_identifies_as_the_installed_cli(self) -> None:
        # Without this the server answers ineligible_reason="surface".
        headers = usage_float_module.claude_request_headers("token")
        version = usage_float_module.claude_cli_version()

        self.assertEqual(headers["anthropic-client-platform"], "cli")
        self.assertEqual(headers["anthropic-client-version"], version)
        self.assertTrue(headers["User-Agent"].startswith(f"claude-cli/{version}"))
        self.assertIn("cedar_ember=1", usage_float_module.CLAUDE_USAGE_RESET_URL)

    def test_only_the_next_usable_grant_is_claimable(self) -> None:
        queued = dict(self.LIVE_GRANT, id="queued", ends_at="2026-12-01T00:00:00+00:00")
        status = parse_claude_reset_status(
            self.block(grants=[queued, dict(self.LIVE_GRANT)])
        )

        self.assertEqual(
            [g.grant_id for g in status.grants], [self.LIVE_GRANT["id"], "queued"]
        )
        self.assertTrue(status.claimable(status.grants[0]))
        self.assertFalse(status.claimable(status.grants[1]))

        waiting = parse_claude_reset_status(
            self.block(grants=[dict(self.LIVE_GRANT, usable_now=False)])
        )
        self.assertFalse(waiting.claimable(waiting.grants[0]))

    # No test below reaches the network: a real claim would spend the reset.
    def claim(self, response, *, grant_id=None, request_id="req-1"):
        calls = []

        def fake(url, **kwargs):
            calls.append((url, kwargs))
            return response

        with patch.object(
            usage_float_module, "claude_organization_uuid", return_value="org-1"
        ), patch.object(usage_float_module, "claude_authorized_json", side_effect=fake):
            result = usage_float_module.claim_claude_reset(
                grant_id or self.LIVE_GRANT["id"], request_id=request_id
            )
        return result, calls

    def test_claim_posts_the_cli_payload_to_the_org_endpoint(self) -> None:
        result, calls = self.claim(
            (200, {"result": "reset", "resets_left": 0, "cleared": ["five_hour"]})
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.settled)
        self.assertIn("5 小时会话", result.message)
        url, kwargs = calls[0]
        self.assertEqual(
            url, "https://api.anthropic.com/api/organizations/org-1/reset_rate_limits"
        )
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(
            kwargs["body"],
            {
                "program": "cedar_ember",
                "grant_id": self.LIVE_GRANT["id"],
                "request_id": "req-1",
            },
        )

    def test_a_refused_claim_is_settled_and_not_reported_as_success(self) -> None:
        result, _calls = self.claim((200, {"result": "not_limited"}))

        self.assertFalse(result.ok)
        self.assertTrue(result.settled)
        self.assertIn("未消耗", result.message)

    def test_an_unanswered_claim_stays_unsettled_so_its_id_is_reused(self) -> None:
        for response in ((0, "timed out"), (502, None), (429, None)):
            result, _calls = self.claim(response)
            self.assertFalse(result.ok)
            self.assertFalse(result.settled)

    def test_malformed_ids_are_never_sent(self) -> None:
        for grant_id, request_id in (("Bad Id", "req"), ("ok", "has space"), ("ok", "")):
            result, calls = self.claim(
                (200, {"result": "reset"}), grant_id=grant_id, request_id=request_id
            )
            self.assertFalse(result.ok)
            self.assertEqual(calls, [])


class UnifiedInterfaceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_dir = Path(temporary.name)
        for patcher in (
            patch.object(usage_float_module, 'CACHE_PATH', self.temp_dir / 'cache.json'),
            patch.object(usage_float_module, 'provider_credential_key', return_value='local-slot'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_fetch_all_collects_local_usage_directly(self):
        row = ProviderUsage(provider_id='claude-2', display_name='claude-2',
                            credential_key='local-slot', windows=[UsageWindow('five_hour', '5h', 12)],
                            stale=True, fetched_at='2026-10-15T00:00:00Z')
        with patch.object(usage_float_module, 'collect_local_usage', return_value=[row]) as direct, patch.object(
            usage_float_module, 'refresh_local_activity'
        ) as activity:
            result = usage_float_module.fetch_all(['claude-2:5h', 'claude-2:7d'])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].windows[0].used_pct, 12)
        self.assertTrue(result[0].stale)
        activity.assert_called_once()
        direct.assert_called_once_with(['claude-2:5h', 'claude-2:7d'], force=False)

    def test_manual_refresh_passes_force_to_collect_local_usage(self):
        row = ProviderUsage(provider_id='claude', display_name='claude',
                            credential_key='local-slot', windows=[UsageWindow('five_hour', '5h', 34)],
                            fetched_at=datetime.now(timezone.utc).isoformat())
        with patch.object(usage_float_module, 'collect_local_usage', return_value=[row]) as direct, patch.object(
            usage_float_module, 'refresh_local_activity'
        ) as activity:
            result = usage_float_module.fetch_all(['claude:5h'], force=True)
        self.assertEqual(result[0].windows[0].used_pct, 34)
        activity.assert_called_once()
        direct.assert_called_once_with(['claude:5h'], force=True)

    def test_dynamic_discovery_supports_custom_provider_names_and_identifies_backend(self):
        magpie_home = self.temp_dir / '.claude-magpie'
        magpie_home.mkdir()
        (magpie_home / 'settings.json').write_text(json.dumps({
            'env': {
                'ANTHROPIC_BASE_URL': 'https://gateway.example.com/api/anthropic',
                'ANTHROPIC_AUTH_TOKEN': 'magpie-secret-token',
            }
        }), encoding='utf-8')
        api_home = self.temp_dir / '.claude-api'
        api_home.mkdir()
        (api_home / 'settings.json').write_text(json.dumps({
            'env': {
                'ANTHROPIC_BASE_URL': 'https://api.anthropic.com',
                'ANTHROPIC_API_KEY': 'sk-ant-api03-test',
            }
        }), encoding='utf-8')
        paseo_cfg = {
            'claude-magpie': {'extends': 'claude', 'env': {'CLAUDE_CONFIG_DIR': str(magpie_home)}},
            'claude-api': {'extends': 'claude', 'env': {'CLAUDE_CONFIG_DIR': str(api_home)}},
        }
        with patch.object(usage_float_module, 'HOME', self.temp_dir), patch.object(
            usage_float_module, 'CLAUDE_HOME', self.temp_dir / '.claude'
        ), patch.object(
            usage_float_module, 'CLAUDE_HOME_2', self.temp_dir / '.claude-2'
        ), patch.object(
            usage_float_module, '_read_paseo_providers', return_value=paseo_cfg
        ):
            homes = usage_float_module.discover_claude_homes()
            self.assertIn('claude-magpie', homes)
            self.assertIn('claude-api', homes)
            self.assertNotIn('claude-2', homes)
            magpie_info = usage_float_module.claude_backend_info('claude-magpie')
            self.assertEqual(magpie_info['backend_kind'], 'gateway')
            self.assertEqual(magpie_info['billing'], 'subscription')
            api_info = usage_float_module.claude_backend_info('claude-api')
            self.assertEqual(api_info['backend_kind'], 'api')
            self.assertEqual(api_info['billing'], 'metered')

    def test_non_magpie_third_party_gateway_falls_back_after_probe_and_activity_writes_default(self) -> None:
        third_party = self.temp_dir / '.claude-openrouter'
        third_party.mkdir()
        (third_party / 'settings.json').write_text(json.dumps({
            'env': {
                'ANTHROPIC_BASE_URL': 'https://openrouter.example.com/api',
                'ANTHROPIC_AUTH_TOKEN': 'or-token',
            }
        }), encoding='utf-8')
        default_cache = self.temp_dir / '.usage-float' / 'usage-cache.json'
        with patch.object(usage_float_module, 'HOME', self.temp_dir), patch.object(
            usage_float_module, 'CACHE_PATH', default_cache
        ), patch.object(
            usage_float_module, '_read_paseo_providers', return_value={}
        ), patch.object(
            usage_float_module, 'http_json', return_value=(404, {'error': 'not found'})
        ):
            res = usage_float_module.fetch_claude_usage(provider_id='claude-openrouter')
            self.assertFalse(res.available)
            info = usage_float_module.claude_backend_info('claude-openrouter')
            self.assertEqual(info['backend_kind'], 'gateway')
            self.assertEqual(info['billing'], 'metered')
            usage_float_module.refresh_local_activity()
            self.assertTrue((self.temp_dir / '.agent-compass' / 'usage-activity.json').is_file())


class ProviderColumnTests(unittest.TestCase):
    def test_resolve_provider_columns_splits_enabled_items_into_two_columns(self) -> None:
        all_ids = list(DEFAULT_PROVIDERS)
        enabled = all_ids[:4]
        disabled = all_ids[4:]
        left, right = usage_float_module.resolve_provider_columns(
            enabled, disabled, all_ids, None
        )
        self.assertEqual(left[:2], enabled[:2])
        self.assertEqual(right[:2], enabled[2:4])
        self.assertEqual(set(left + right), set(all_ids))

    def test_resolve_provider_columns_respects_saved_two_column_assignment(self) -> None:
        all_ids = list(DEFAULT_PROVIDERS)
        enabled = ["claude:7d", "codex", "grok"]
        disabled = [pid for pid in all_ids if pid not in set(enabled)]
        saved_cols = [["codex"], ["claude:7d", "grok", *disabled]]
        left, right = usage_float_module.resolve_provider_columns(
            enabled, disabled, all_ids, saved_cols
        )
        self.assertEqual(left, ["codex"])
        self.assertEqual(right[:2], ["claude:7d", "grok"])
        self.assertEqual(set(left + right), set(all_ids))

    def test_move_provider_in_columns_moves_across_and_within_columns(self) -> None:
        left = ["claude:7d", "claude:5h"]
        right = ["codex", "api-proxy"]
        new_left, new_right = usage_float_module.move_provider_in_columns(
            left, right, "codex", 0, 1
        )
        self.assertEqual(new_left, ["claude:7d", "codex", "claude:5h"])
        self.assertEqual(new_right, ["api-proxy"])

        reordered_left, reordered_right = usage_float_module.move_provider_in_columns(
            new_left, new_right, "claude:5h", 1, 0
        )
        self.assertEqual(reordered_left, ["claude:7d", "codex"])
        self.assertEqual(reordered_right, ["claude:5h", "api-proxy"])


class WallpaperDirectoryAndIndexTests(unittest.TestCase):
    def test_middle_elide_path_preserves_first_and_last_segments(self) -> None:
        short = r"E:\Wallpapers\Anime"
        self.assertEqual(usage_float_module._middle_elide_path(short, 38), short)

        long_path = r"E:\MediaLibrary\DynamicWallpapers\2026_Collection\Season_Autumn\4K_Vertical\SubFolder_Final"
        elided = usage_float_module._middle_elide_path(long_path, 38)
        self.assertTrue(elided.startswith(r"E:\MediaLibrary"))
        self.assertTrue(elided.endswith("SubFolder_Final"))
        self.assertIn("…", elided)

        rel_label = "DynamicWallpapers / 2026_Collection / Season_Autumn / 4K_Vertical / SubFolder_Final"
        elided_rel = usage_float_module._middle_elide_path(rel_label, 38)
        self.assertTrue(elided_rel.startswith("DynamicWallpapers"))
        self.assertTrue(elided_rel.endswith("SubFolder_Final"))
        self.assertIn("…", elided_rel)

    def test_wallpaper_bucket_directory_respects_recursion_depth_per_root(self) -> None:
        root_a = Path(r"E:\Wallpapers\PackA")
        deep_file = root_a / "Cat1" / "Sub2" / "Sub3" / "clip.mp4"
        direct_file = root_a / "root_clip.mp4"

        # Depth 0: everything under root_a is grouped into root_a itself
        self.assertEqual(
            usage_float_module._wallpaper_bucket_directory(
                deep_file, [str(root_a)], {str(root_a): 0}
            ),
            root_a,
        )
        # Depth 1 (default): grouped into first-level subdirectory Cat1
        self.assertEqual(
            usage_float_module._wallpaper_bucket_directory(
                deep_file, [str(root_a)], {str(root_a): 1}
            ),
            root_a / "Cat1",
        )
        # Depth 2: grouped into second-level subdirectory Cat1/Sub2
        self.assertEqual(
            usage_float_module._wallpaper_bucket_directory(
                deep_file, [str(root_a)], {str(root_a): 2}
            ),
            root_a / "Cat1" / "Sub2",
        )
        # Direct file in root_a stays in root_a at any depth
        self.assertEqual(
            usage_float_module._wallpaper_bucket_directory(
                direct_file, [str(root_a)], {str(root_a): 2}
            ),
            root_a,
        )

    def test_media_directories_and_weighted_random_use_configured_depths(self) -> None:
        root_a = Path(r"E:\Wallpapers\PackA")
        files = [
            root_a / "A1" / "Deep1" / "v1.mp4",
            root_a / "A1" / "Deep2" / "v2.mp4",
            root_a / "A2" / "Deep3" / "v3.mp4",
        ]
        dirs_depth_1 = usage_float_module._wallpaper_media_directories(
            files, [str(root_a)], {str(root_a): 1}
        )
        self.assertEqual(
            [d for d, _count in dirs_depth_1],
            [root_a / "A1", root_a / "A2"],
        )
        self.assertEqual([count for _d, count in dirs_depth_1], [2, 1])

        dirs_depth_2 = usage_float_module._wallpaper_media_directories(
            files, [str(root_a)], {str(root_a): 2}
        )
        self.assertEqual(
            [d for d, _count in dirs_depth_2],
            [
                root_a / "A1" / "Deep1",
                root_a / "A1" / "Deep2",
                root_a / "A2" / "Deep3",
            ],
        )

        # Weighting A2=100 and A1=0 at depth 1 only picks files under A2
        chosen, _offset = usage_float_module._randomize_wallpaper_session(
            files,
            folder_weights={str(root_a / "A1"): 0, str(root_a / "A2"): 100},
            random_mode=usage_float_module.WALLPAPER_RANDOM_MODE_DIRECTORY,
            root_folders=[str(root_a)],
            folder_depths={str(root_a): 1},
        )
        self.assertEqual(chosen, [root_a / "A2" / "Deep3" / "v3.mp4"])

    def test_wallpaper_index_cache_roundtrip_and_lookup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_file = Path(temp_dir) / "wallpaper-index.json"
            folder_a = str(Path(temp_dir) / "FolderA")
            file_1 = str(Path(folder_a) / "sub" / "a.mp4")
            file_2 = str(Path(folder_a) / "sub" / "b.png")
            raw_cache = {
                folder_a: {
                    "files": [file_1, file_2],
                    "updated_at": 1700000000.0,
                }
            }
            usage_float_module.save_wallpaper_index_cache(raw_cache, cache_file)
            loaded = usage_float_module.load_wallpaper_index_cache(cache_file)
            media, unindexed = usage_float_module._cached_wallpaper_media_for_folders(
                [folder_a, str(Path(temp_dir) / "UnindexedFolder")],
                loaded,
            )
            self.assertEqual(media, [Path(file_1), Path(file_2)])
            self.assertEqual(unindexed, [str(Path(temp_dir) / "UnindexedFolder")])

    def test_scan_wallpaper_folder_slow_finds_media_and_honors_stop_check(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "sub").mkdir()
            f1 = root / "a.mp4"
            f2 = root / "sub" / "b.jpg"
            ignore = root / "readme.txt"
            f1.write_bytes(b"0")
            f2.write_bytes(b"0")
            ignore.write_text("hi", encoding="utf-8")

            found = usage_float_module._scan_wallpaper_folder_slow(
                root, throttle_every=1, throttle_sleep=0.0
            )
            self.assertEqual(found, [f1, f2])

            stopped = usage_float_module._scan_wallpaper_folder_slow(
                root, stop_check=lambda: True
            )
            self.assertEqual(stopped, [])

    def test_large_directory_cached_lookup_and_grouping_is_fast(self) -> None:
        import time

        root_a = Path(r"E:\LargeWardrobe\Originals")
        folder_key = usage_float_module._wallpaper_path_key(root_a)
        raw_files = [
            str(root_a / f"Category_{i % 12}" / f"img_{i:05d}.png")
            for i in range(10500)
        ]
        index_cache = {
            folder_key: {
                "folder": str(root_a),
                "files": raw_files,
                "scanned_at": 1700000000.0,
            }
        }
        # Warm up parsed path cache once
        media_first, uncached = usage_float_module._cached_wallpaper_media_for_folders(
            [str(root_a)],
            index_cache,
        )
        self.assertEqual(len(media_first), 10500)
        self.assertEqual(uncached, [])

        t0 = time.perf_counter()
        media_second, _ = usage_float_module._cached_wallpaper_media_for_folders(
            [str(root_a)],
            index_cache,
        )
        dirs = usage_float_module._wallpaper_media_directories(
            media_second,
            [str(root_a)],
            {str(root_a): 1},
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertEqual(len(dirs), 12)
        self.assertEqual(sum(count for _d, count in dirs), 10500)
        self.assertLess(elapsed_ms, 150.0)

        with TemporaryDirectory() as temp_dir:
            playlist_path = Path(temp_dir) / "wallpaper.m3u"
            t1 = time.perf_counter()
            usage_float_module._write_wallpaper_playlist(media_second, playlist_path)
            write_ms = (time.perf_counter() - t1) * 1000.0
            lines = playlist_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(
                len(lines),
                1 + usage_float_module.WALLPAPER_PLAYLIST_MAX_ITEMS,
            )
            self.assertLess(write_ms, 150.0)

    def test_settings_wallpaper_directory_list_does_not_jitter_on_refresh_or_scroll(self) -> None:
        source = Path(usage_float_module.__file__).read_text(encoding="utf-8")
        self.assertIn("def _configure_font_if_changed(", source)
        self.assertIn("content_host.pack_propagate(False)", source)
        self.assertIn("wp_content_host.pack_propagate(False)", source)
        self.assertIn("def _bind_scrollable_canvas(", source)
        self.assertIn("notify_start=False", source)




class BrowserWebVideoWallpaperTests(unittest.TestCase):
    def test_dropped_wallpaper_urls_parses_links_moz_url_shortcuts_and_html(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shortcut = root / "bili.url"
            shortcut.write_text(
                "[InternetShortcut]\nIconIndex=0\nURL=https://www.bilibili.com/video/BV1xx411c7mD\n",
                encoding="utf-8",
            )
            html_file = root / "local_player.html"
            html_file.write_text("<html><body><video src='a.mp4'></video></body></html>", encoding="utf-8")
            mp4_file = root / "clip.mp4"
            mp4_file.write_bytes(b"00")

            urls = usage_float_module._dropped_wallpaper_urls(
                [
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://www.bilibili.com/video/BV1Vbnn6iEfG\n哔哩哔哩视频标题",
                    "www.bilibili.com/video/BV1ab411c7mD",
                    str(shortcut),
                    str(html_file),
                    str(mp4_file),
                    "not a url at all",
                ]
            )

        self.assertEqual(
            urls[:4],
            [
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "https://www.bilibili.com/video/BV1Vbnn6iEfG",
                "https://www.bilibili.com/video/BV1ab411c7mD",
                "https://www.bilibili.com/video/BV1xx411c7mD",
            ],
        )
        self.assertEqual(len(urls), 5)
        self.assertTrue(urls[4].startswith("file:///"))
        self.assertTrue(urls[4].endswith("/local_player.html"))

    def test_choose_ole_drop_effect_accepts_browser_link_and_explorer_copy(self) -> None:
        # DROPEFFECT_COPY = 1, DROPEFFECT_MOVE = 2, DROPEFFECT_LINK = 4
        self.assertEqual(usage_float_module._choose_ole_drop_effect(1 | 4), 1)
        self.assertEqual(usage_float_module._choose_ole_drop_effect(4), 4)
        self.assertEqual(usage_float_module._choose_ole_drop_effect(2), 2)
        self.assertEqual(usage_float_module._choose_ole_drop_effect(0), 1)

    def test_build_browser_wallpaper_command_uses_app_mode_and_isolated_profile(self) -> None:
        cmd = usage_float_module._build_browser_wallpaper_command(
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            "https://www.bilibili.com/video/BV1Vbnn6iEfG",
            Path(r"C:\Temp\uf-profile"),
            width=1080,
            height=1080,
            audio_enabled=False,
        )
        self.assertIn("--app=https://www.bilibili.com/video/BV1Vbnn6iEfG", cmd)
        self.assertIn(r"--user-data-dir=C:\Temp\uf-profile", cmd)
        self.assertIn("--remote-debugging-port=0", cmd)
        self.assertIn("--autoplay-policy=no-user-gesture-required", cmd)
        self.assertIn("--window-size=1080,1080", cmd)
        self.assertIn("--mute-audio", cmd)

    def test_browser_web_video_player_queues_controls_and_cleans_up_on_stop(self) -> None:
        player = usage_float_module._BrowserWebVideoPlayer()
        fake_proc = unittest.mock.MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 999999
        with TemporaryDirectory() as temp_dir:
            profile_dir = Path(temp_dir) / "web-video-test"
            profile_dir.mkdir()
            player.process = fake_proc
            player.profile_dir = profile_dir

            self.assertTrue(player.running)
            self.assertTrue(player.set_muted(True))
            self.assertTrue(player.set_paused(True))
            self.assertTrue(player.seek(-5.0))
            self.assertTrue(player.adjust_volume(5))
            self.assertFalse(player._muted)
            self.assertEqual(len(player._pending_js), 4)

            with patch.object(usage_float_module.subprocess, "run") as mock_run:
                player.stop()
                self.assertFalse(player.running)
                for _ in range(30):
                    if not profile_dir.exists():
                        break
                    import time
                    time.sleep(0.02)
                self.assertFalse(profile_dir.exists())
                mock_run.assert_called_once()


    def test_dropped_regular_video_loops_single_file_without_playlist(self) -> None:
        player = usage_float_module._MpvWallpaperPlayer()
        fake_proc = unittest.mock.MagicMock()
        fake_proc.poll.return_value = None
        dropped = Path(r"D:\Media\dropped.mp4")
        other = Path(r"D:\Media\other.mp4")
        with patch.object(usage_float_module.subprocess, "Popen", return_value=fake_proc) as mock_popen:
            ok, err = player.start(
                Path(r"C:\tools\mpv.exe"),
                12345,
                [dropped, other],
                preferred_first=dropped,
                keep_order=True,
                loop_file=True,
            )
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(player.last_order, [dropped])
        cmd = mock_popen.call_args.args[0]
        self.assertIn("--loop-file=inf", cmd)
        self.assertIn(str(dropped), cmd)
        self.assertFalse(any(arg.startswith("--playlist=") for arg in cmd))

    def test_dropped_wallpaper_urls_extracts_cf_html_and_ignores_static_cover_images(self) -> None:
        cf_html_card = (
            "Version:0.9\r\n"
            "StartHTML:00000100\r\n"
            "EndHTML:00000300\r\n"
            "SourceURL:https://www.bilibili.com/\r\n"
            "<html><body><!--StartFragment-->"
            '<a href="//www.bilibili.com/video/BV1Vbnn6iEfG?spm_id_from=333.1007">'
            '<img src="https://i0.hdslb.com/bfs/archive/cover123.avif">'
            "</a>"
            "<!--EndFragment--></body></html>"
        )
        urls = usage_float_module._dropped_wallpaper_urls(
            [
                "https://i0.hdslb.com/bfs/archive/cover123.avif",
                cf_html_card,
            ]
        )
        self.assertEqual(
            urls,
            [
                "https://www.bilibili.com/video/BV1Vbnn6iEfG?spm_id_from=333.1007",
                "https://www.bilibili.com/",
            ],
        )

    def test_dropped_wallpaper_urls_falls_back_to_cf_html_source_url_on_video_page(self) -> None:
        cf_html_selection = (
            "Version:0.9\r\n"
            "SourceURL:https://www.bilibili.com/video/BV1xx411c7mD\r\n"
            "<html><body><!--StartFragment--><span>视频标题文字</span><!--EndFragment--></body></html>"
        )
        urls = usage_float_module._dropped_wallpaper_urls([cf_html_selection])
        self.assertEqual(urls, ["https://www.bilibili.com/video/BV1xx411c7mD"])

    def test_browser_window_drop_tracker_detects_moved_browser_window_released_on_target(self) -> None:
        tracker = usage_float_module._BrowserWindowDropTracker(min_move_px=16)
        target = (1000, 200, 1260, 400)
        # Start dragging browser window on main screen
        tracker.update_down(
            fg_hwnd=0x2094C,
            is_browser=True,
            window_rect=(100, 100, 900, 700),
            cursor_pos=(400, 115),
        )
        # Move window toward target (same width=800, height=600)
        tracker.update_down(
            fg_hwnd=0x2094C,
            is_browser=True,
            window_rect=(800, 220, 1600, 820),
            cursor_pos=(1100, 235),
        )
        self.assertTrue(tracker.moved)
        result = tracker.finish_up(cursor_pos=(1100, 235), target_rect=target)
        self.assertEqual(result, (0x2094C, (100, 100, 900, 700), True))
        self.assertFalse(tracker.active)

    def test_browser_window_drop_tracker_ignores_window_resize_or_release_outside(self) -> None:
        tracker = usage_float_module._BrowserWindowDropTracker(min_move_px=16)
        target = (1000, 200, 1260, 400)
        # Resize left edge without moving whole window
        tracker.update_down(
            fg_hwnd=0x2094C,
            is_browser=True,
            window_rect=(100, 100, 900, 700),
            cursor_pos=(100, 300),
        )
        tracker.update_down(
            fg_hwnd=0x2094C,
            is_browser=True,
            window_rect=(50, 100, 900, 700),
            cursor_pos=(1100, 300),
        )
        self.assertFalse(tracker.moved)
        self.assertIsNone(tracker.finish_up(cursor_pos=(1100, 300), target_rect=target))


class FloatingMenuStackingTests(unittest.TestCase):
    def test_show_menu_guards_floating_topmost_restack_while_popup_is_open(self) -> None:
        import tkinter as tk
        from types import SimpleNamespace
        from unittest import mock

        captured: dict[str, object] = {}

        def fake_mainloop(root, *args, **kwargs):
            frame = sys._getframe(1)
            scope = frame.f_locals
            state = scope["state"]
            menu = scope["menu"]
            stack_text_above_plate = scope["stack_text_above_plate"]
            enforce_window_band = scope["enforce_window_band"]
            do_quit = scope["do_quit"]

            state["panel_active"] = False
            state["display_mode"] = usage_float_module.DISPLAY_MODE_FLOAT
            state["always_on_top"] = True
            stack_text_above_plate()

            seen_menu_open: list[bool] = []
            swp_calls: list[tuple[int, int]] = []

            def fake_tk_popup(_x: int, _y: int) -> None:
                seen_menu_open.append(bool(state.get("menu_open")))
                if sys.platform == "win32":
                    import ctypes
                    orig_swp = ctypes.windll.user32.SetWindowPos

                    def spy_swp(hwnd, insert_after, x, y, cx, cy, flags):
                        h_val = int(getattr(hwnd, "value", hwnd) or 0)
                        ia_val = int(getattr(insert_after, "value", insert_after) or 0)
                        if ia_val in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
                            ia_val = -1
                        swp_calls.append((h_val, ia_val))
                        return orig_swp(hwnd, insert_after, x, y, cx, cy, flags)

                    with mock.patch.object(ctypes.windll.user32, "SetWindowPos", side_effect=spy_swp):
                        stack_text_above_plate()
                        enforce_window_band()
                else:
                    stack_text_above_plate()
                    enforce_window_band()

            with mock.patch.object(menu, "tk_popup", side_effect=fake_tk_popup), \
                    mock.patch.object(menu, "grab_release"):
                res = scope["show_menu"](SimpleNamespace(x_root=120, y_root=140))

            captured["res"] = res
            captured["seen_menu_open"] = seen_menu_open
            captured["menu_open_after"] = bool(state.get("menu_open"))
            captured["swp_calls"] = swp_calls
            captured["layer_hwnds"] = dict(scope["layer_hwnds"])
            do_quit()

        with TemporaryDirectory() as temp_dir, \
                patch.object(usage_float_module, "CONFIG_PATH", Path(temp_dir) / "config.json"), \
                patch.object(usage_float_module, "fetch_all", return_value=[]), \
                patch.object(usage_float_module.threading.Thread, "start"), \
                patch.object(tk.Tk, "mainloop", fake_mainloop):
            usage_float_module.run_ui()

        self.assertEqual(captured.get("res"), "break")
        self.assertEqual(captured.get("seen_menu_open"), [True])
        self.assertFalse(captured.get("menu_open_after"))
        if sys.platform == "win32":
            layer_hwnds = captured["layer_hwnds"]
            hud_hwnds = {int(layer_hwnds.get("text", 0)), int(layer_hwnds.get("plate", 0))}
            # Neither text_hwnd nor plate_hwnd may be re-inserted at HWND_TOPMOST (-1)
            # while the context menu is open, or they will cover the #32768 popup menu.
            topmost_hud_calls = [
                (hwnd, ia) for (hwnd, ia) in captured["swp_calls"]
                if hwnd in hud_hwnds and ia == -1
            ]
            self.assertEqual(topmost_hud_calls, [])


class SharedGrokDisplayTests(unittest.TestCase):
    def test_retired_bot_meter_is_removed_from_saved_settings(self):
        order, enabled, disabled = usage_float_module.resolve_provider_config(
            ['grok', 'grok-bot'], ['grok-bot'], ['grok-bot', 'grok'])
        self.assertNotIn('grok-bot', order + enabled + disabled)
        self.assertIn('grok', enabled)

    def test_either_runner_keeps_one_ordinary_grok_meter_and_one_fetch(self):
        native = usage_float_module
        reset = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        for selected, disabled in ((['grok'], ['grok-bot']), (['grok-bot'], ['grok']),
                                   (['grok', 'grok-bot'], [])):
            with self.subTest(selected=selected):
                _, enabled, _ = native.resolve_provider_config(selected, disabled, selected)
                with patch.object(native, 'provider_available', side_effect=lambda name: name == 'grok'):
                    active = native.active_providers(enabled)
                self.assertEqual(active, ['grok'])
                item = native.ProviderUsage(provider_id='grok', display_name='grok',
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    windows=[native.UsageWindow('credits', 'Quota', 56, reset)])
                with TemporaryDirectory() as directory, \
                     patch.object(native, 'CACHE_PATH', Path(directory) / 'usage.json'), \
                     patch.object(native, 'discover_claude_homes', return_value={}), \
                     patch.object(native, 'discover_codex_accounts', return_value={}), \
                     patch.object(native, 'discover_litellm_proxies', return_value={}), \
                     patch.object(native, 'provider_poll_seconds', return_value=300), \
                     patch.object(native, 'provider_credential_key', return_value=None), \
                     patch.object(native, 'fetch_grok_usage', return_value=item) as fetch:
                    observations = native.collect_local_usage(selected, force=True)
                fetch.assert_called_once()
                rows = native.iter_display_rows(observations, enabled)
                self.assertEqual([(r.provider_id, r.title, r.used_pct, r.resets_at) for r in rows],
                                 [('grok', 'grok', 56, reset)])


if __name__ == "__main__":
    unittest.main()
