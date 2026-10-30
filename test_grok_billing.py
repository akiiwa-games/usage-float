import pytest
import grok_billing


def test_primary_usage_ignores_product_shares_and_history():
    data = {'config': {'creditUsagePercent': 42.5, 'currentPeriod': {'end': '2026-11-01T00:00:00Z'},
        'productUsage': [{'usagePercent': 90}], 'history': [{'creditUsagePercent': 99}]}}
    assert grok_billing.parse(data)[:2] == (42.5, '2026-11-01T00:00:00Z')


def test_zero_requires_current_shape_and_legacy_uses_actual_budget():
    assert grok_billing.parse({'config': {'currentPeriod': {'end': '2026-11-01'}}})[0] == 0
    assert grok_billing.parse({'config': {'monthlyLimit': {'val': 2000}, 'used': {'val': 500}}})[0] == 25
    with pytest.raises(ValueError):
        grok_billing.parse({'config': {}})
