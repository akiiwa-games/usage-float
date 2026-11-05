"""A Magpie pool shows its largest plan: N such accounts read as one window set worth N accounts (×N)."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import magpie_usage
import usage_float as usage


class MagpiePoolTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.source = magpie_usage.Source('https://example.com', 'test-key', '*')
        self.a = self.account('a', 100, 50, 1)
        self.b = self.account('b', 0, 0, 3)

    def account(self, name, short, week, reset_hours):
        return {'provider': 'claude', 'kind': 'subscription', 'user': name,
                'plan': 'pro', 'readAt': self.now.isoformat(), 'windows': [
                    {'name': '5 hours', 'used': short,
                     'resetsAt': (self.now + timedelta(hours=reset_hours)).isoformat()},
                    {'name': '7 days', 'used': week,
                     'resetsAt': (self.now + timedelta(days=reset_hours)).isoformat()}]}

    def parse(self, *rows):
        return magpie_usage.parse_quota({'data': list(rows)}, self.source)

    def test_same_plan_accounts_add_up(self):
        quota, windows = self.parse(self.a, self.b)
        self.assertEqual([w['used_pct'] for w in windows], [25, 50])  # mean; the window holds two accounts' worth
        self.assertEqual([w['accounts'] for w in windows], [2, 2])
        self.assertEqual([w['label'] for w in windows], ['7d×2', '5h×2'])
        self.assertEqual(windows[1]['resets_at'], self.b['windows'][0]['resetsAt'])  # the last account to refill
        self.assertEqual(quota['plan'], 'pro ×2')
        details = quota['usage_details']
        self.assertEqual((details['account_count'], details['plan_count'], details['aggregation']), (2, 2, 'plan_sum'))
        self.assertNotIn('test-key', json.dumps(quota))
        self.assertNotIn('user', details['members'][0])
        shown = usage.UsageWindow(**windows[0])
        self.assertEqual(usage._provider_from_dict(usage._provider_to_dict(usage.ProviderUsage(
            provider_id='claude-2', display_name='claude-2', windows=[shown]))).windows[0].accounts, 2)

    def test_largest_plan_wins_over_smaller_plans(self):
        self.a['plan'] = 'max'
        quota, windows = self.parse(self.a, self.b, self.account('c', 0, 0, 2))
        self.assertEqual([w['used_pct'] for w in windows], [50, 100])
        self.assertEqual([w['label'] for w in windows], ['7d', '5h'])
        self.assertEqual([w['accounts'] for w in windows], [1, 1])
        self.assertEqual(quota['plan'], 'max')
        self.assertEqual(quota['usage_details']['account_count'], 3)
        self.assertGreater(magpie_usage.plan_rank('Max 20x'), magpie_usage.plan_rank('max'))
        self.assertGreater(magpie_usage.plan_rank('pro'), magpie_usage.plan_rank('plus'))

    def test_without_a_configured_account_every_subscription_is_read(self):
        source = magpie_usage.Source('https://example.com', 'test-key', '')
        quota, windows = magpie_usage.parse_quota({'data': [self.a, self.b]}, source)
        self.assertEqual(quota['plan'], 'pro ×2')
        single, _ = magpie_usage.parse_quota({'data': [self.a]}, source)
        self.assertNotIn('usage_details', single)

    def test_only_selected_provider_subscriptions(self):
        foreign = dict(self.b, provider='codex')
        wallet = dict(self.b, kind='api')
        _, windows = self.parse(self.a, foreign, wallet)
        self.assertEqual([w['used_pct'] for w in windows], [50, 100])

    def test_unread_account_is_skipped_and_bad_data_is_rejected(self):
        for changes in ({'error': 'offline'}, {'readAt': None}):
            with self.subTest(changes=changes):
                quota, windows = self.parse(self.a, dict(self.b, **changes))
                self.assertEqual([w['used_pct'] for w in windows], [50, 100])
                self.assertEqual(quota['plan'], 'pro')
        with self.assertRaises(ValueError):
            self.parse(dict(self.a, error='offline'), dict(self.b, error='offline'))
        for changes in ({'readAt': 'bad'}, {'windows': []}, {'user': None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.parse(self.a, dict(self.b, **changes))

    def test_duplicates_and_invalid_percentages_are_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(self.a, self.a)
        for number in (-1, 101, True, float('nan')):
            bad = copy.deepcopy(self.b)
            bad['windows'][0]['used'] = number
            with self.subTest(number=number), self.assertRaises(ValueError):
                self.parse(self.a, bad)

    def test_order_does_not_change_pool_identity(self):
        left, _ = self.parse(self.a, self.b)
        right, _ = self.parse(self.b, self.a)
        self.assertEqual(left['usage_details']['pool_key'], right['usage_details']['pool_key'])

    def test_changed_membership_replaces_cache_even_with_older_vendor_time(self):
        with TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'settings.json').write_text(json.dumps({'env': {
                'ANTHROPIC_BASE_URL': self.source.url,
                'ANTHROPIC_AUTH_TOKEN': self.source.token}}), encoding='utf-8')
            (home / 'usage-float.json').write_text(json.dumps({
                'source': 'magpie', 'account': '*'}), encoding='utf-8')
            self.b['readAt'] = (self.now - timedelta(hours=1)).isoformat()
            with patch.object(usage, 'CLAUDE_HOME_2', home), \
                    patch.object(usage, 'CACHE_PATH', home / 'cache.json'), \
                    patch.object(usage, 'http_json') as request:
                request.return_value = (200, {'data': [self.a, self.b]})
                first = usage.fetch_claude_usage(provider_id='claude-2')
                self.assertTrue(first.stale)  # the older member reading (b, an hour ago) dates the pool
                self.assertEqual(first.fetched_at, self.b['readAt'])
                request.return_value = (200, {'data': [self.b]})
                usage.fetch_claude_usage(provider_id='claude-2')
                cached = usage.cache_get_provider('claude-2')
                self.assertFalse(cached.usage_details)  # one account left: a plain reading replaces the pool
                self.assertEqual([w.used_pct for w in cached.windows], [0, 0])
                self.assertEqual(cached.fetched_at, self.b['readAt'])


    def test_a_new_reading_rule_replaces_the_cached_pool_at_the_same_vendor_time(self):
        with TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'settings.json').write_text(json.dumps({'env': {
                'ANTHROPIC_BASE_URL': self.source.url,
                'ANTHROPIC_AUTH_TOKEN': self.source.token}}), encoding='utf-8')
            (home / 'usage-float.json').write_text(json.dumps({'source': 'magpie'}), encoding='utf-8')
            self.a['readAt'] = self.b['readAt'] = (self.now - timedelta(hours=6)).isoformat()  # stale, same time
            with patch.object(usage, 'CLAUDE_HOME_2', home),                     patch.object(usage, 'CACHE_PATH', home / 'cache.json'),                     patch.object(usage, 'http_json', return_value=(200, {'data': [self.a, self.b]})):
                usage.fetch_claude_usage(provider_id='claude-2')
                def as_mean(cache):
                    row = cache['providers']['claude-2']
                    row['usage_details']['aggregation'] = 'account_mean'
                    row['windows'][0]['label'] = '7d×2均'
                usage.update_usage_cache(as_mean)
                usage.fetch_claude_usage(provider_id='claude-2')
                cached = usage.cache_get_provider('claude-2')
                self.assertEqual(cached.usage_details['aggregation'], 'plan_sum')
                self.assertEqual(cached.windows[0].label, '7d×2')

    def test_rows_show_what_is_left_summed_over_the_pool(self):
        self.assertEqual(usage.DisplayRow('claude-2 7d×2', used_pct=25.0, accounts=2).left_pct, 150)
        self.assertEqual(usage.DisplayRow('claude-2 5h×2', used_pct=0.0, accounts=2).left_pct, 200)
        self.assertEqual(usage.DisplayRow('claude 7d', used_pct=27.0).left_pct, 73)
        self.assertEqual(usage.DisplayRow('claude 5h', used_pct=101.0).left_pct, 0)
        self.assertIsNone(usage.DisplayRow('claude').left_pct)

    def test_pool_rows_carry_one_bar_per_added_account(self):
        self.a['plan'] = 'max'
        self.b['plan'] = 'max'
        small = self.account('c', 10, 10, 2)  # a smaller plan is listed in the hover, not drawn as a bar
        quota, windows = self.parse(self.a, self.b, small)
        pool = usage.ProviderUsage(provider_id='claude-2', display_name='claude-2', plan_label=quota['plan'],
                                   windows=[usage.UsageWindow(**w) for w in windows],
                                   usage_details=quota['usage_details'], fetched_at=quota['readAt'])
        rows = {r.window_id: r for r in usage.iter_display_rows([pool], ['claude-2:7d', 'claude-2:5h'])}
        self.assertEqual(rows['seven_day'].member_used, (50.0, 0.0))
        self.assertEqual(rows['five_hour'].member_used, (100.0, 0.0))
        single = usage.ProviderUsage(provider_id='claude', display_name='claude', plan_label='pro',
                                     windows=[usage.UsageWindow(id='seven_day', label='7d', used_pct=10.0)])
        self.assertEqual(usage.iter_display_rows([single], ['claude'])[0].member_used, ())

    def test_a_pool_member_read_before_its_reset_counts_as_refilled(self):
        self.a['readAt'] = (self.now - timedelta(hours=2)).isoformat()
        self.a['windows'][0]['resetsAt'] = (self.now - timedelta(hours=1)).isoformat()
        quota, _ = self.parse(self.a, self.b)
        self.assertEqual(usage.pool_member_used(quota['usage_details'], 'five_hour'), (0.0, 0.0))
        self.assertEqual(usage.pool_member_used(quota['usage_details'], 'seven_day'), (50.0, 0.0))

    def test_pool_bars_are_thinner_but_stay_visible(self):
        self.assertEqual(usage.panel_pool_bar_geometry(8, 1), (8, 0))
        self.assertEqual(usage.panel_pool_bar_geometry(8, 3), (5, 2))
        self.assertEqual(usage.panel_pool_bar_geometry(6, 4), (4, 2))

    def test_a_week_only_account_is_titled_by_name_like_codex(self):
        week = lambda wid, label: usage.UsageWindow(id=wid, label=label, used_pct=10.0)
        ultra = usage.ProviderUsage(provider_id='claude', display_name='claude', plan_label='max',
                                    windows=[week('weekly_fable', 'fable 7d'), week('seven_day', '7d')])
        pro = usage.ProviderUsage(provider_id='claude', display_name='claude', plan_label='pro',
                                  windows=[week('seven_day', '7d'), week('five_hour', '5h')])
        titles = lambda p: [r.title for r in usage.iter_display_rows([p], ['claude'])]
        self.assertEqual(titles(ultra), ['fable 7d', 'claude'])
        self.assertEqual(titles(pro), ['claude 7d', 'claude 5h'])

    def test_auto_discovered_gateway_passes_force_to_magpie_refresh(self):
        with TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'settings.json').write_text(json.dumps({'env': {
                'ANTHROPIC_BASE_URL': self.source.url,
                'ANTHROPIC_AUTH_TOKEN': self.source.token}}), encoding='utf-8')
            calls = []
            def fake_http(url, *, method='GET', **kwargs):
                calls.append((method, url))
                return 200, {'data': [self.a, self.b]}
            with patch.object(usage, 'CLAUDE_HOME_2', home), \
                    patch.object(usage, 'CACHE_PATH', home / 'cache.json'), \
                    patch.object(usage, 'http_json', side_effect=fake_http):
                usage.fetch_claude_usage(provider_id='claude-2', force=True)
            post_urls = [url for method, url in calls if method == 'POST']
            self.assertEqual(len(post_urls), 2)

if __name__ == '__main__':
    unittest.main()
