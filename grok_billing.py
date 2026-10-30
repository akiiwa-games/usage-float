"""Read Grok CLI subscription billing, without sending inference requests.

Official wire contract: xai-org/grok-build, extensions/billing.rs and xai-grok-env.
OIDC tokens belong at the CLI proxy, not grok.com's browser gRPC endpoint.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(request):
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        return json.load(response)


def fetch(home: Path):
    path = home / 'auth.json'
    doc = json.loads(path.read_text(encoding='utf-8'))
    entry = next((v for k, v in doc.items() if k.startswith('https://auth.x.ai::') and isinstance(v, dict)), None)
    if not entry:
        return None
    original_key = entry.get('key')
    expires = entry.get('expires_at')
    if expires and datetime.fromisoformat(expires.replace('Z', '+00:00')) <= datetime.now(timezone.utc) + timedelta(minutes=5):
        if not entry.get('refresh_token'):
            raise ValueError('Grok login expired; sign in again')
        data = urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': entry['refresh_token'],
                                      'client_id': entry['oidc_client_id']}).encode()
        refreshed = read_json(urllib.request.Request('https://auth.x.ai/oauth2/token', data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST'))
        entry['key'] = refreshed['access_token']
        entry['refresh_token'] = refreshed.get('refresh_token', entry['refresh_token'])
        entry['expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=refreshed['expires_in'])).isoformat()
        # Reload before writing so unrelated account edits are preserved.
        latest = json.loads(path.read_text(encoding='utf-8'))
        scope = next(k for k, v in doc.items() if v is entry)
        if (latest.get(scope) or {}).get('key') != original_key:
            raise ValueError('Grok credentials changed; retry refresh')
        latest[scope] = entry
        temporary = path.with_suffix('.refresh.tmp')
        temporary.write_text(json.dumps(latest, indent=2), encoding='utf-8')
        temporary.replace(path)
    return read_json(urllib.request.Request('https://cli-chat-proxy.grok.com/v1/billing?format=credits',
        headers={'Authorization': 'Bearer ' + entry['key'], 'X-XAI-Token-Auth': 'xai-grok-cli',
                 'x-userid': entry.get('user_id', ''), 'Accept': 'application/json'}))


def parse(payload):
    config = payload.get('config')
    if not isinstance(config, dict):
        raise ValueError('Missing Grok quota config')
    period = config.get('currentPeriod') or {}
    percent = config.get('creditUsagePercent')
    if percent is None:
        # An omitted proto3 float is zero only on the known current-period shape.
        if period:
            percent = 0.0
        else:
            limit = (config.get('monthlyLimit') or {}).get('val')
            used = (config.get('used') or {}).get('val', 0)
            if not isinstance(limit, (int, float)) or limit <= 0:
                raise ValueError('Unknown Grok allowance')
            percent = 100 * used / limit
    if not isinstance(percent, (int, float)) or percent < 0:
        raise ValueError('Invalid Grok usage')
    return min(100.0, percent), period.get('end') or config.get('billingPeriodEnd'), payload.get('subscriptionTier')
