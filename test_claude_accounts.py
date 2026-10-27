"""Second Claude account must never borrow or refresh the first account's token."""
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import usage_float as usage


class ClaudeAccountsTests(unittest.TestCase):
    def test_missing_second_account_does_not_fall_back(self):
        with TemporaryDirectory() as folder, patch.object(usage, 'CLAUDE_HOME_2', Path(folder)):
            self.assertFalse(usage.provider_available('claude-2:7d'))

    def test_fetch_second_account_uses_its_own_directory_and_cache(self):
        with TemporaryDirectory() as folder:
            home = Path(folder) / 'second'
            home.mkdir()
            (home / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
                'accessToken': 'second-token', 'subscriptionType': 'pro'}}), encoding='utf-8')
            with patch.object(usage, 'CLAUDE_HOME_2', home), patch.object(
                usage, 'CACHE_PATH', Path(folder) / 'cache.json'
            ), patch.object(
                usage, 'http_json', return_value=(200, {
                    'five_hour': {'utilization': 12}, 'seven_day': {'utilization': 34}})
            ) as call:
                items = usage.collect_local_usage(['claude-2:7d', 'claude-2:5h'])
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].provider_id, 'claude-2')
                self.assertEqual(items[0].plan_label, 'pro')
                self.assertEqual(call.call_args.kwargs['headers']['Authorization'], 'Bearer second-token')
                self.assertIsNone(usage.cache_get_provider('claude'))
                self.assertEqual(usage.cache_get_provider('claude-2').provider_id, 'claude-2')
                rows = usage.iter_display_rows(items, ['claude-2:7d', 'claude-2:5h'])
                self.assertEqual([r.used_pct for r in rows], [34, 12])
                self.assertTrue(all(r.title.startswith('claude-2') for r in rows))
                self.assertTrue(all(usage.row_opens_reset_ui(r) for r in rows))
                self.assertEqual([usage.reset_target_id(r) for r in rows], ['claude-2:7d', 'claude-2:5h'])

    def test_second_account_reset_endpoints_use_isolated_home_and_org(self):
        with TemporaryDirectory() as folder:
            first_home = Path(folder) / 'first'
            second_home = Path(folder) / 'second'
            first_home.mkdir()
            second_home.mkdir()
            (first_home / '.claude.json').write_text(
                json.dumps({'oauthAccount': {'organizationUuid': 'org-first'}}),
                encoding='utf-8',
            )
            with patch.object(usage, 'CLAUDE_HOME', first_home), patch.object(
                usage, 'HOME', first_home
            ), patch.object(usage, 'CLAUDE_HOME_2', second_home), patch.object(
                usage, 'claude_authorized_json', return_value=(401, None)
            ) as auth_call:
                # Must NOT read first_home/.claude.json when second_home/.claude.json is missing
                self.assertIsNone(usage.claude_organization_uuid('claude-2'))
                auth_call.assert_called_once_with(usage.CLAUDE_PROFILE_URL, home=second_home)

            (second_home / '.claude.json').write_text(
                json.dumps({'oauthAccount': {'organizationUuid': 'org-second'}}),
                encoding='utf-8',
            )
            calls = []

            def fake_auth(url, **kwargs):
                calls.append((url, kwargs))
                if 'at_wall=1' in url:
                    return 200, {'juniper_tide': {'eligible': True, 'arm': 'reset', 'available': True}}
                return 200, {'result': 'reset', 'resets_left': 0, 'cleared': ['seven_day']}

            with patch.object(usage, 'CLAUDE_HOME', first_home), patch.object(
                usage, 'HOME', first_home
            ), patch.object(usage, 'CLAUDE_HOME_2', second_home), patch.object(
                usage, 'claude_authorized_json', side_effect=fake_auth
            ):
                self.assertEqual(usage.claude_organization_uuid('claude-2'), 'org-second')
                claim = usage.claim_claude_reset(
                    'grant-2', request_id='req-2', provider_id='claude-2'
                )
                self.assertTrue(claim.ok)
                session_reset, err = usage.fetch_claude_session_reset('claude-2')
                self.assertIsNone(err)
                self.assertTrue(session_reset.claimable)
                session_claim = usage.claim_claude_session_reset('claude-2')
                self.assertTrue(session_claim.ok)

            self.assertEqual(len(calls), 3)
            self.assertIn('/api/organizations/org-second/reset_rate_limits', calls[0][0])
            self.assertEqual(calls[0][1].get('home'), second_home)
            self.assertEqual(calls[1][1].get('home'), second_home)
            self.assertIn('/api/organizations/org-second/reset_rate_limits', calls[2][0])
            self.assertEqual(calls[2][1].get('home'), second_home)

    def test_rate_limit_backoff_and_cached_usage_are_account_scoped(self):
        with TemporaryDirectory() as folder, patch.object(usage, 'CACHE_PATH', Path(folder) / 'cache.json'):
            usage.set_claude_backoff(provider_id='claude-2')
            self.assertTrue(usage.claude_in_backoff('claude-2'))
            self.assertFalse(usage.claude_in_backoff())
            usage.clear_claude_backoff()
            self.assertTrue(usage.claude_in_backoff('claude-2'))
            good = usage.ProviderUsage(provider_id='claude', display_name='claude', available=True,
                                       fetched_at=datetime.now(timezone.utc).isoformat())
            usage.cache_put_provider(good)
            self.assertFalse(usage._claude_rate_limited_usage('claude-2').available)

    def test_hud_does_not_rotate_isolated_account_tokens(self):
        with patch.object(usage, 'read_claude_oauth', return_value=({'accessToken': 'old', 'refreshToken': 'r'}, Path('second'))), patch.object(
            usage, 'http_json', return_value=(401, None)
        ), patch.object(usage, 'refresh_claude_token') as refresh, patch.object(usage, 'save_claude_oauth') as save:
            self.assertEqual(usage.claude_authorized_json('usage', home=Path('second')), (401, None))
            refresh.assert_not_called()
            save.assert_not_called()

    def test_hud_retries_token_refreshed_by_cli(self):
        with patch.object(usage, 'read_claude_oauth', side_effect=[({'accessToken': 'old'}, None), ({'accessToken': 'new'}, None)]), patch.object(
            usage, 'http_json', side_effect=[(401, None), (200, {})]
        ) as call:
            self.assertEqual(usage.claude_authorized_json('usage', home=Path('second')), (200, {}))
            self.assertEqual(call.call_args.kwargs['headers']['Authorization'], 'Bearer new')


if __name__ == '__main__':
    unittest.main()
