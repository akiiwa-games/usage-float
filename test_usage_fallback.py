"""Offline regressions for display continuity across failed refreshes."""
import ast
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import usage_float as usage


class UsageFallbackTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'cache.json'
        for patcher in (
            patch.object(usage, 'CACHE_PATH', self.path),
            patch.object(usage, 'provider_credential_key', return_value='account-a'),
            patch.object(usage, 'discover_claude_homes', return_value={'claude': Path(folder.name)}),
            patch.object(usage, 'discover_codex_accounts', return_value={}),
            patch.object(usage, 'discover_litellm_proxies', return_value={}),
            patch.object(usage, 'provider_poll_seconds', return_value=300),
            patch.object(usage, 'http_json', side_effect=AssertionError('Unexpected network')),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def good(self, pid='claude', value=42, age=0):
        return usage.ProviderUsage(
            provider_id=pid, display_name=pid, credential_key='account-a',
            windows=[usage.UsageWindow('seven_day', '7d', value)],
            fetched_at=(datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat())

    def assert_reading(self, item, value=42):
        self.assertEqual(item.windows[0].used_pct, value)
        [row] = usage.iter_display_rows([item])
        self.assertFalse(row.failed)
        self.assertEqual(row.used_pct, value)

    def test_windows_render_despite_error_unavailable_and_stale(self):
        for available in (False, True):
            for error in ('更新失败', 'HTTP 429', None):
                with self.subTest(available=available, error=error):
                    item = self.good()
                    item.available, item.error, item.stale = available, error, True
                    self.assert_reading(item)

    def test_summary_renders_despite_error_and_unavailable(self):
        item = self.good()
        item.windows, item.summary = [], '10 credits remaining'
        item.available, item.error = False, '更新失败'
        [row] = usage.iter_display_rows([item])
        self.assertFalse(row.failed)
        self.assertEqual(row.summary, item.summary)

    def test_no_history_still_renders_failure(self):
        [item] = usage.merge_display_usage(None, [], ['claude'])
        self.assertTrue(usage.iter_display_rows([item])[0].failed)

    def test_all_refresh_failures_preserve_memory_without_readable_cache(self):
        old = self.good()
        failures = (RuntimeError('collector crashed'), TimeoutError(),
                    subprocess.CalledProcessError(1, ['collector']),
                    subprocess.TimeoutExpired(['collector'], 1),
                    json.JSONDecodeError('bad payload', '', 0))
        for failure in failures:
            with self.subTest(error=type(failure).__name__), \
                    patch.object(usage, 'fetch_all', side_effect=failure), \
                    patch.object(usage, 'load_usage_cache', side_effect=PermissionError()):
                [item] = usage.fetch_display_usage(['claude'], [old])
            self.assert_reading(item)
            self.assertTrue(item.stale)
            self.assertIn(type(failure).__name__, item.error)
            self.assertEqual(item.fetched_at, old.fetched_at)
        self.assertFalse(old.stale)
        self.assertIsNone(old.error)

    def test_whole_refresh_failure_recovers_disk_providers_or_display_history(self):
        for section in ('providers', 'display_snapshots'):
            with self.subTest(section=section):
                self.path.write_text(json.dumps({section: {'claude': usage._provider_to_dict(self.good())}}))
                with patch.object(usage, 'fetch_all', side_effect=RuntimeError()):
                    [item] = usage.fetch_display_usage(['claude'], [])
                self.assert_reading(item)
                self.assertTrue(item.stale)
                self.assertEqual(list(usage.load_usage_cache()), [section])

    def test_empty_malformed_and_partial_results_keep_requested_accounts(self):
        previous = [self.good(), self.good('codex')]
        for response in (None, [], {}, ['invalid'], [None], [self.good('claude', 25)]):
            with self.subTest(response=response), patch.object(usage, 'fetch_all', return_value=response):
                items = usage.fetch_display_usage(['claude:7d', 'codex'], previous)
            self.assertEqual([p.provider_id for p in items], ['claude', 'codex'])
            self.assert_reading(items[-1])
            if response and isinstance(response, list) and isinstance(response[0], usage.ProviderUsage):
                self.assert_reading(items[0], 25)
                self.assertFalse(items[0].stale)

    def test_latest_history_wins_and_disabled_accounts_are_not_resurrected(self):
        usage.cache_put_provider(self.good(value=55))
        items = usage.merge_display_usage([], [self.good(age=60), self.good('codex')], ['claude'])
        self.assertEqual(len(items), 1)
        self.assert_reading(items[0], 55)

    def test_known_login_change_does_not_reuse_old_account_memory(self):
        with patch.object(usage, 'provider_credential_key', return_value='account-b'):
            [item] = usage.merge_display_usage([], [self.good()], ['claude'])
        self.assertFalse(item.windows)

    def test_temporarily_unreadable_credentials_keep_memory(self):
        with patch.object(usage, 'provider_credential_key', return_value=None):
            [item] = usage.merge_display_usage([], [self.good()], ['claude'])
        self.assert_reading(item)

    def test_repeated_failures_then_success_restore_fresh_reading(self):
        old = self.good()
        with patch.object(usage, 'fetch_all', side_effect=[[], RuntimeError(), [self.good(value=60)]]):
            items = usage.fetch_display_usage(['claude'], [old])
            items = usage.fetch_display_usage(['claude'], items)
            self.assert_reading(items[0])
            items = usage.fetch_display_usage(['claude'], items)
        self.assert_reading(items[0], 60)
        self.assertFalse(items[0].stale)
        self.assertIsNone(items[0].error)

    def test_stale_mark_write_failure_does_not_defeat_disk_fallback(self):
        usage.cache_put_provider(self.good())
        for failure in (PermissionError(), TimeoutError()):
            with self.subTest(error=type(failure).__name__), \
                    patch.object(usage, 'mark_provider_stale', side_effect=failure):
                item = usage.stale_provider_usage('claude', 'API failed')
            self.assert_reading(item)
            self.assertTrue(item.stale)

    def test_successful_read_survives_cache_publication_failure(self):
        for failure in (PermissionError(), TimeoutError()):
            with self.subTest(error=type(failure).__name__), \
                    patch.object(usage, 'fetch_claude_usage', return_value=self.good()), \
                    patch.object(usage, 'cache_put_provider', side_effect=failure):
                [item] = usage.collect_local_usage(['claude'], force=True)
            self.assert_reading(item)
            self.assertTrue(item.stale)

    def test_failed_fetch_retains_previously_read_cache_when_disk_becomes_unreadable(self):
        usage.cache_put_provider(self.good())
        def fail(**kwargs):
            self.path.write_text('{broken')
            raise RuntimeError('collector failed')
        with patch.object(usage, 'fetch_claude_usage', side_effect=fail):
            [item] = usage.collect_local_usage(['claude'], force=True)
        self.assert_reading(item)
        self.assertEqual(self.path.read_text(), '{broken')

    def test_corrupt_or_locked_cache_is_not_overwritten_with_empty_history(self):
        for raw in ('{broken', '[]'):
            self.path.write_text(raw)
            with self.assertRaises(ValueError):
                usage.update_usage_cache(lambda c: c.update({'poll': {}}))
            self.assertEqual(self.path.read_text(), raw)
        usage.save_usage_cache({'display_snapshots': {'claude': usage._provider_to_dict(self.good())}})
        original = self.path.read_bytes()
        with patch.object(Path, 'read_text', side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                usage.update_usage_cache(lambda c: c.update({'poll': {}}))
        self.assertEqual(self.path.read_bytes(), original)

    def test_actual_refresh_and_apply_callbacks_preserve_state_on_collection_failure(self):
        # Run the actual nested callbacks without opening a window or creating
        # threads, so this covers the UI boundary rather than only its helper.
        tree = ast.parse(Path(usage.__file__).read_text(encoding='utf-8'))
        run_ui = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_ui')
        callbacks = [n for n in run_ui.body if isinstance(n, ast.FunctionDef)
                     and n.name in ('refresh_async', 'apply_data')]
        state = {'providers': [self.good()], 'refreshing': False}
        rendered = Mock()
        scope = dict(vars(usage), state=state, cfg={'providers': ['claude']},
                     root=SimpleNamespace(after=lambda delay, callback: callback()),
                     render_footer=Mock(), render=rendered,
                     threading=SimpleNamespace(Thread=lambda target, **kw: SimpleNamespace(start=target)))
        exec(compile(ast.Module(body=callbacks, type_ignores=[]), usage.__file__, 'exec'), scope)
        for discovery in (Mock(return_value=['claude']), Mock(return_value=[]), Mock(side_effect=OSError())):
            with self.subTest(discovery=discovery), \
                    patch.object(usage, 'fetch_all', side_effect=subprocess.CalledProcessError(1, ['collector'])), \
                    patch.dict(scope, active_providers=discovery):
                scope['refresh_async'](force=True)
            self.assert_reading(state['providers'][0])
            self.assertTrue(state['providers'][0].stale)
            self.assertFalse(state['refreshing'])
        self.assertEqual(rendered.call_count, 3)


if __name__ == '__main__':
    unittest.main()
