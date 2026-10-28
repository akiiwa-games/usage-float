"""Real native meter schema, scope changes, unknowns and existing collector UI."""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import grok_bot_usage as native
import usage_float as usage


class GrokBotUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = time.time()  # Synthetic fixture clock; no captured account snapshot.
        self.slot = 'grok|test-account'
        self.value = {'kind': 'present', 'selectedTeamId': None,
                      'reading': {'readAtMs': self.now * 1000,
                                  'usage': {'percentUsed': 12.5,
                                            'nextResetMs': (self.now + 3600) * 1000,
                                            'unifiedPlanName': 'Premium', 'isSandTrial': False,
                                            'hasNonZeroIncludedLimit': True, 'onDemand': None}},
                      'expiresAtMs': (self.now + 86400) * 1000}
        for target, name, value in [(native, 'app_home', lambda: self.home),
                                    (usage, 'CACHE_PATH', self.home / 'usage-cache.json')]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.save()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf8')

    def save(self):
        self.write(self.home / 'desktop-status.json', {'version': 1, 'signedIn': True})
        self.write(native.slice_path(self.home, 'sand.client.slice.client-meta.account-slot'),
                   {'schemaVersion': 1, 'value': self.slot})
        key = 'sand.client.slice.account.' + native.quote(self.slot, safe="~()*!.'-") + '.weekly-usage.cache'
        self.quota_path = native.slice_path(self.home, key)
        self.write(self.quota_path, {'schemaVersion': 2, 'value': self.value})

    def test_real_schema_maps_units_and_vendor_time(self):
        item = native.read_usage(now=self.now)
        self.assertAlmostEqual(item.used_pct, 12.5)
        self.assertAlmostEqual(item.details['remaining_percent'], 87.5)
        self.assertEqual(item.fetched_at, native.iso_ms(self.now * 1000))
        self.assertEqual(item.resets_at, native.iso_ms((self.now + 3600) * 1000))
        self.assertEqual(item.details['unified_plan_name'], 'Premium')
        self.assertNotIn('shared_pool', item.details)
        self.assertFalse(item.stale)

    def test_missing_and_invalid_percent_or_reset_stay_unknown(self):
        for invalid in (None, True, -1, float('nan'), '20'):
            self.value['reading']['usage'].update(percentUsed=invalid, nextResetMs=None)
            self.save()
            item = native.read_usage(now=self.now)
            self.assertIsNone(item.used_pct)
            self.assertIsNone(item.details['remaining_percent'])
            self.assertIsNone(item.resets_at)

    def test_native_absent_is_unknown_not_zero_or_auth_failure(self):
        usage.collect_local_usage(['grok-bot'], force=True)
        self.value['kind'] = 'absent'
        self.save()
        item = usage.fetch_grok_bot_usage()
        self.assertTrue(item.available)
        self.assertIsNone(item.windows[0].used_pct)
        self.assertIsNone(item.fetched_at)
        self.assertIsNone(item.error)
        self.assertTrue(usage.load_usage_cache()['providers']['grok-bot']['stale'])

    def test_parked_snapshot_and_reset_never_look_fresh_or_refilled(self):
        self.value['reading']['readAtMs'] = (self.now - 900) * 1000
        self.value['reading']['usage']['nextResetMs'] = (self.now - 100) * 1000
        self.save()
        item = usage.fetch_grok_bot_usage()
        self.assertTrue(item.stale)
        row = usage.iter_display_rows([item], ['grok-bot'])[0]
        self.assertAlmostEqual(row.used_pct, 12.5)
        self.assertEqual(row.title, 'grok bot')

    def test_same_native_reading_expiry_updates_persisted_stale(self):
        with patch.object(native.time, 'time', return_value=self.now):
            [first] = usage.collect_local_usage(['grok-bot'], force=True)
        initial = usage.load_usage_cache()['providers']['grok-bot']
        self.assertFalse(first.stale)
        self.assertFalse(initial['stale'])
        with patch.object(native.time, 'time', return_value=self.now + 301):
            [second] = usage.collect_local_usage(['grok-bot'])
        persisted = usage.load_usage_cache()['providers']['grok-bot']
        self.assertTrue(second.stale)
        self.assertTrue(persisted['stale'])
        self.assertEqual(persisted['fetched_at'], initial['fetched_at'])
        self.assertEqual(persisted['windows'], initial['windows'])
        self.assertEqual(second.fetched_at, first.fetched_at)
        self.assertEqual(usage.iter_display_rows([second])[0].title, 'grok bot')
        self.value['reading']['readAtMs'] = (self.now + 302) * 1000
        self.save()
        with patch.object(native.time, 'time', return_value=self.now + 302):
            usage.collect_local_usage(['grok-bot'])
        recovered = usage.load_usage_cache()['providers']['grok-bot']
        self.assertFalse(recovered['stale'])
        self.assertIsNone(recovered['error'])

    def test_account_team_switch_and_logout_invalidate_history(self):
        original = native.credential_key()
        usage.collect_local_usage(['grok-bot'], force=True)
        self.slot = 'grok|other-account'
        self.save()
        self.assertNotEqual(original, native.credential_key())
        self.assertIsNone(usage.cache_get_provider('grok-bot'))
        original = native.credential_key()
        self.value['selectedTeamId'] = 42
        self.save()
        self.assertNotEqual(original, native.credential_key())
        self.write(self.home / 'desktop-status.json', {'version': 1, 'signedIn': False})
        self.assertFalse(usage.provider_available('grok-bot'))
        self.assertIsNone(native.credential_key())
        self.assertIsNone(usage.cache_get_provider('grok-bot'))

    def test_registration_cache_and_refresh_without_other_account_or_network(self):
        with patch.object(usage, 'fetch_grok_usage', side_effect=AssertionError('wrong account')), \
                patch.object(usage, 'http_json', side_effect=AssertionError('no auth/network in adapter')):
            self.assertEqual(usage.active_providers(['grok-bot']), ['grok-bot'])
            order, enabled, _ = usage.resolve_provider_config(['codex'])
            self.assertIn('grok-bot', order)
            self.assertIn('grok-bot', enabled)
            self.assertNotIn('grok-bot', usage.resolve_provider_config(['codex'], ['grok-bot'])[1])
            [first] = usage.collect_local_usage(['grok-bot'], force=True)
            self.value['reading']['readAtMs'] += 1
            self.value['reading']['usage']['percentUsed'] = 6.25
            self.save()
            [second] = usage.collect_local_usage(['grok-bot'])
        self.assertEqual(second.windows[0].used_pct, 6.25)
        self.assertNotEqual(first.fetched_at, second.fetched_at)
        cached = usage.load_usage_cache()['providers']['grok-bot']
        self.assertEqual(cached['fetched_at'], second.fetched_at)
        self.assertEqual(cached['usage_details']['unit'], 'percent')
        self.assertNotIn(self.slot, json.dumps(cached))

    def test_corrupt_snapshot_does_not_borrow_another_slot(self):
        self.quota_path.write_text('{bad', encoding='utf8')
        item = usage.fetch_grok_bot_usage()
        self.assertFalse(item.available)
        self.assertTrue(item.stale)
        self.assertEqual(item.windows, [])

    def test_on_demand_is_currency_not_another_capacity_window(self):
        self.value['reading']['usage']['onDemand'] = {'usedCents': 123, 'limitCents': 5000}
        self.save()
        item = usage.fetch_grok_bot_usage()
        self.assertEqual(len(item.windows), 1)
        self.assertEqual(item.usage_details['on_demand'], {'unit': 'USD cents', 'used': 123, 'limit': 5000})


if __name__ == '__main__':
    unittest.main()
