"""Read the official Grok Bot account-scoped weekly meter, never its secrets.

Grok Bot 0.77 writes this snapshot from DashboardService.GetSandUsageStatus.
The desktop owns authentication and network refresh. We retain its observation
time; rereading a file is not a new quota observation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def app_home() -> Path:
    if sys.platform == 'win32':
        return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'Grok Bot'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/Grok Bot'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'Grok Bot'


def slice_path(home: Path, key: str) -> Path:
    name = base64.b32encode(key.encode()).decode().lower().rstrip('=')
    return home / 'sand-client-persistence' / (name + '.blob')


def _read(path: Path) -> dict:
    # Exact known metadata/usage files only, with bounded reads. No directory scan.
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Oversized native usage snapshot')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid native usage snapshot')
    return value


def account_slot(home: Path) -> str | None:
    try:
        status = _read(home / 'desktop-status.json')
        meta = _read(slice_path(home, 'sand.client.slice.client-meta.account-slot'))
        slot = meta.get('value')
        if (status.get('version') == 1 and status.get('signedIn') is True
                and meta.get('schemaVersion') == 1 and isinstance(slot, str)
                and 0 < len(slot) <= 256):
            return slot
    except (OSError, ValueError):
        pass
    return None


def available() -> bool:
    return account_slot(app_home()) is not None


def _source(home: Path):
    slot = account_slot(home)
    if slot is None:
        raise ValueError('Open Grok Bot and sign in')
    key = 'sand.client.slice.account.' + quote(slot, safe="~()*!.'-") + '.weekly-usage.cache'
    envelope = _read(slice_path(home, key))
    value = envelope.get('value')
    if envelope.get('schemaVersion') != 2 or not isinstance(value, dict):
        raise ValueError('Unsupported native usage snapshot')
    if 'selectedTeamId' not in value:
        raise ValueError('Native usage account scope unknown')
    team = value['selectedTeamId']
    if team is not None and (type(team) is not int or team <= 0):
        raise ValueError('Invalid native usage account scope')
    fingerprint = hashlib.sha256(json.dumps(
        [str(home.resolve()), slot, team], ensure_ascii=True).encode()).hexdigest()
    if slot != account_slot(home):
        raise ValueError('Native account changed during read')
    return value, fingerprint


def credential_key() -> str | None:
    # Provenance only: no credential is read. Account/team switches invalidate history.
    try:
        return _source(app_home())[1]
    except (OSError, ValueError):
        return None


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def iso_ms(value):
    value = number(value)
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class Reading:
    used_pct: float | None
    resets_at: str | None
    fetched_at: str | None
    plan: str | None
    stale: bool
    credential_key: str
    details: dict


def read_usage(*, freshness_seconds=300, now=None) -> Reading:
    value, fingerprint = _source(app_home())
    now = time.time() if now is None else now
    if value.get('kind') == 'absent':
        # Native "absent" also covers pooled enterprise or missing usagePercent.
        # It does not mean zero allowance or an authentication failure.
        return Reading(None, None, None, None, True, fingerprint,
                       {'source': 'grok-bot-native-cache', 'unit': 'percent', 'state': 'unknown'})
    reading = value.get('reading')
    if value.get('kind') != 'present' or not isinstance(reading, dict) or not isinstance(reading.get('usage'), dict):
        raise ValueError('Invalid native usage reading')
    usage = reading['usage']
    used = number(usage.get('percentUsed'))
    read_at = iso_ms(reading.get('readAtMs'))
    reset = iso_ms(usage.get('nextResetMs'))
    observed = (number(reading.get('readAtMs')) or 0) / 1000
    expires = (number(value.get('expiresAtMs')) or 0) / 1000
    reset_seconds = (number(usage.get('nextResetMs')) or 0) / 1000
    stale = (not read_at or not 0 <= now - observed < freshness_seconds
             or now >= expires or bool(reset_seconds and now >= reset_seconds))
    unified = usage.get('unifiedPlanName')
    plan = unified or usage.get('grokPlanLabel')
    plan = plan if isinstance(plan, str) and len(plan) <= 100 else None
    details = {'source': 'grok-bot-native-cache', 'unit': 'percent',
               'used_percent': used, 'remaining_percent': max(0, 100 - used) if used is not None else None,
               'read_at': read_at, 'resets_at': reset,
               'expires_at': iso_ms(value.get('expiresAtMs')),
               'unified_plan_name': unified if isinstance(unified, str) and len(unified) <= 100 else None}
    for name in ('isSandTrial', 'isTeamSeat', 'hasNonZeroIncludedLimit',
                 'isUnified', 'usesPooledEnterpriseAllowance'):
        if isinstance(usage.get(name), bool):
            details[name] = usage[name]
    demand = usage.get('onDemand')
    if isinstance(demand, dict):
        details['on_demand'] = {'unit': 'USD cents', 'used': number(demand.get('usedCents')),
                                'limit': number(demand.get('limitCents'))}
    return Reading(used, reset, read_at, plan, stale, fingerprint, details)
