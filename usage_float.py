#!/usr/bin/env python3
"""
usage-float — light translucent usage HUD for Claude / Codex / Grok / Antigravity / LLM Proxy.

  • Usage windows per provider; providers without local credentials are hidden
  • Antigravity uses the signed-in official agy CLI
  • Multiple Codex accounts, one per CODEX_HOME (~/.codex, ~/.codex-2, …)
  • Optional LiteLLM / OpenAI-compatible proxy spend (key headers)
  • Compact floating rows or a full-screen auxiliary-display instrument grid
  • Bottom bar: one clickable relative-time + refresh control
  • Auto refresh every 5 minutes; click the whole control to refresh now
  • On failure, keep the last usage reading when one is available
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from usage_lock import file_lock
import magpie_usage

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

SUPPORTED_LANGUAGES = ("zh", "en")
_ACTIVE_LANGUAGE = "zh"


def _lang(cfg_or_state: Any = None) -> str:
    """Normalize a persisted language; a missing/unknown value always means zh."""
    value = cfg_or_state.get("language") if isinstance(cfg_or_state, dict) else cfg_or_state
    value = str(value or "zh").strip().lower()
    value = {"zh-cn": "zh", "en-us": "en"}.get(value, value)
    return value if value in SUPPORTED_LANGUAGES else "zh"


def _tr(zh: str, en: str, lang: str | None = None) -> str:
    """Translate UI text using the active language unless explicitly supplied."""
    return en if _lang(_ACTIVE_LANGUAGE if lang is None else lang) == "en" else zh


# Stable labels from cached data and module-level status tables are localized
# at the display boundary, so language changes do not alter protocol values.
_EN_TEXT = {
    '用量': 'Usage',
    '副屏': 'Aux Display',
    '动态壁纸': 'Wallpaper',
    '悬浮窗': 'Floating HUD',
    '目录': 'Folders & Playback',
    '随机权重': 'Random Weights',
    '更新失败': 'Update failed',
    '登录凭据需由 Claude Code 更新': 'Login credentials must be updated by Claude Code',
    '登录凭据需由 Codex 更新': 'Login credentials must be updated by Codex',
    '可用': 'Available',
    '正在使用': 'Redeeming',
    '已使用': 'Redeemed',
    '5 小时会话': '5-hour session',
    '7 天周额度': '7-day weekly quota',
    '7 天周额度（含超额）': '7-day weekly quota (including overage)',
    'Opus 周额度': 'Opus weekly quota',
    'Sonnet 周额度': 'Sonnet weekly quota',
    '当前客户端不被允许读取重置额度': 'This client cannot read reset credits',
    'Claude Code 版本过低，升级后才会发放': 'Update Claude Code to receive reset credits',
    '当前订阅方案不包含用量重置': 'Your subscription does not include usage resets',
    '登录凭证缺少所需权限': 'Login credentials lack the required permissions',
    '账号未获授权': 'Account not authorized',
    '这次重置已经用过了。': 'This reset has already been used.',
    '还没到用量上限，暂时不能使用，额度未消耗。': 'Usage limit not reached. Reset unavailable; no credit was spent.',
    '刚用过一次重置，冷却中，稍后再试。': 'A reset was just used. Please try again after the cooldown.',
    '当前账号不符合使用条件。': 'This account is not eligible.',
    '重置暂不可用，稍后再试。': 'Reset temporarily unavailable. Please try again later.',
    '周额度已用完，5 小时重置无法使用': 'Weekly quota exhausted; 5-hour reset unavailable',
    '账号没有周额度限制，不需要重置': 'No weekly limit; a reset is not needed',
    '还没到 5 小时上限，暂时不能使用，额度未消耗。': '5-hour limit not reached. Reset unavailable; no credit was spent.',
    '限流退避中': 'Rate limit cooldown',
    '用量来源已变化，等待下次更新': 'Usage source changed; waiting for the next update',
    '登录凭据已变化，等待下次更新': 'Login credentials changed; waiting for the next update',
    'Magpie HTTPS 用量来源配置不完整': 'Magpie HTTPS usage source is not fully configured',
    '正在读取重置额度…': 'Loading reset credits…',
    '额度': 'Quota',
    '额度重置时区': 'Quota Reset Timezone',
    '正在由其他采集器更新': 'Another collector is updating usage',
    '读取重置额度失败': 'Could not load reset credits',
    '当前订阅方案不包含 5 小时重置': 'Your subscription does not include 5-hour resets',
    '账号注册时间还不够': 'This account is too new',
    '当前客户端不被允许使用': 'This client is not eligible',
    '移动端不可用': 'Unavailable on mobile',
    'Claude Code 版本过低': 'Please update Claude Code',
    '到达 5 小时上限后才会开放': 'Available after reaching the 5-hour limit',
    '账号在其他实验中': 'This account is in another experiment',
    '已开启超额用量，不需要重置': 'Extra usage is enabled; a reset is not needed',
    '本周的 5 小时重置已经用过了。': 'This week’s 5-hour reset has already been used.',
}


def _ui(text: str) -> str:
    if _lang(_ACTIVE_LANGUAGE) == "en":
        return _EN_TEXT.get(text, text)
    return next((zh for zh, en in _EN_TEXT.items() if en == text), text)


HOME = Path.home()
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
VERSION = "1.3.5"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", HOME / ".claude"))
CLAUDE_HOME_2 = Path(os.environ.get("CLAUDE_HOME_2", HOME / ".claude-2"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
CODEX_HOME_2 = Path(os.environ.get("CODEX_HOME_2", HOME / ".codex-2"))
GROK_HOME = Path(os.environ.get("GROK_HOME", HOME / ".grok"))
CONFIG_PATH = HOME / ".usage-float" / "config.json"
CACHE_PATH = HOME / ".usage-float" / "usage-cache.json"
WALLPAPER_INDEX_CACHE_PATH = HOME / ".usage-float" / "wallpaper-index.json"
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", str(APP_DIR)))
WALLPAPER_START_SCRIPT_PATH = RESOURCE_DIR / "wallpaper-start.lua"
if not WALLPAPER_START_SCRIPT_PATH.exists():
    WALLPAPER_START_SCRIPT_PATH = APP_DIR / "wallpaper-start.lua"
_INSTANCE_MUTEX_HANDLE: Any = None

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# "cedar-ember" is Claude Code's internal name for the weekly /limit-reset
# entitlement. Its status rides along on the usage endpoint as a query flag,
# so reading it costs no extra call against an API that answers 429 easily.
CLAUDE_USAGE_RESET_URL = f"{CLAUDE_USAGE_URL}?cedar_ember=1&skip_spend=1"
# The 5h session reset (juniper_tide) is only filled in on the read the CLI
# makes at a limit; the regular read above returns it as null.
CLAUDE_USAGE_AT_WALL_URL = f"{CLAUDE_USAGE_URL}?at_wall=1&skip_spend=1"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# Redeem call used by the CLI's /limit-reset, keyed by the OAuth organization.
CLAUDE_RESET_CLAIM_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"
CLAUDE_RESET_PROGRAM = "cedar_ember"
# The CLI refuses to send ids outside these shapes; so does the HUD.
CLAUDE_RESET_GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
CLAUDE_RESET_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# The server only hands that block to the CLI surface: a plain request comes
# back with ineligible_reason="surface". Report the version actually installed
# so the gate reflects this machine instead of a number frozen into the file.
CLAUDE_CLI_VERSION_FALLBACK = "2.1.278"
CLAUDE_CLI_PACKAGE_PATHS: tuple[Path, ...] = (
    Path(os.environ.get("APPDATA", str(HOME)))
    / "npm"
    / "node_modules"
    / "@anthropic-ai"
    / "claude-code"
    / "package.json",
    CLAUDE_HOME / "local" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json",
    HOME / ".npm-global" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json",
)

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Banked rate-limit resets ("重置卡"): OpenAI grants them to eligible plans and
# they stay redeemable for 30 days instead of firing the moment they arrive.
CODEX_RESET_CARDS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CODEX_RESET_CONSUME_URL = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
)
CODEX_RESET_CARD_STATUS_AVAILABLE = "available"
CODEX_RESET_CARD_STATUS_TEXT = {
    "available": "可用",
    "redeeming": "正在使用",
    "redeemed": "已使用",
}



@dataclass(frozen=True)
class CodexAccount:
    """One signed-in Codex account, identified by its own CODEX_HOME.

    Codex stores exactly one credential per home directory, so a second account
    is simply a second home. Every Codex call takes the account it belongs to
    rather than reading the process-wide default.
    """

    provider_id: str
    display_name: str
    home: Path

    @property
    def auth_path(self) -> Path:
        return self.home / "auth.json"


CODEX_ACCOUNTS: tuple[CodexAccount, ...] = (
    CodexAccount("codex", "codex", CODEX_HOME),
    CodexAccount("codex-2", "codex-2", CODEX_HOME_2),
)
CODEX_ACCOUNT_BY_ID: dict[str, CodexAccount] = {
    account.provider_id: account for account in CODEX_ACCOUNTS
}

# Claude and Antigravity share a login across several independently configurable rows.
CLAUDE_DISPLAY_ITEMS: tuple[str, ...] = ("claude:fable", "claude:7d", "claude:5h")
CLAUDE_WINDOW_ITEMS: dict[str, str] = {
    "seven_day": "claude:7d",
    "five_hour": "claude:5h",
}
ANTIGRAVITY_WINDOW_ITEMS: dict[str, str] = {
    "gemini-weekly": "antigravity:gemini-weekly",
    "gemini-5h": "antigravity:gemini-5h",
    "3p-weekly": "antigravity:3p-weekly",
    "3p-5h": "antigravity:3p-5h",
}
ANTIGRAVITY_DISPLAY_ITEMS: tuple[str, ...] = tuple(ANTIGRAVITY_WINDOW_ITEMS.values())
PROVIDER_DISPLAY_ITEMS: dict[str, tuple[str, ...]] = {
    "claude": CLAUDE_DISPLAY_ITEMS,
    "claude-2": ("claude-2:fable", "claude-2:7d", "claude-2:5h"),
    "antigravity": ANTIGRAVITY_DISPLAY_ITEMS,
}

# Display order. A provider needs local credentials (Antigravity needs its CLI),
# so a machine with one Codex account shows one Codex row.
DEFAULT_PROVIDERS: tuple[str, ...] = (
    "codex",
    "codex-2",
    "grok",
    *CLAUDE_DISPLAY_ITEMS,
    *ANTIGRAVITY_DISPLAY_ITEMS,
)
PROVIDER_LABELS: dict[str, str] = {
    "codex": "Codex",
    "codex-2": "Codex 2",
    "grok": "Grok",
    "claude": "Claude",
    "claude:fable": "fable 7d",
    "claude:7d": "claude 7d",
    "claude:5h": "claude 5h",
    "claude-2": "Claude 2",
    "claude-2:fable": "Claude 2 fable 7d",
    "claude-2:7d": "Claude 2 7d",
    "claude-2:5h": "Claude 2 5h",
    "pi": "Pi",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
    "antigravity:gemini-weekly": "agy gemini 7d",
    "antigravity:gemini-5h": "agy gemini 5h",
    "antigravity:3p-weekly": "agy claude 7d",
    "antigravity:3p-5h": "agy claude 5h",
}


def _read_paseo_providers() -> dict[str, dict[str, Any]]:
    paseo_path = Path(os.environ.get("PASEO_CONFIG") or HOME / ".paseo" / "config.json")
    try:
        if not paseo_path.is_file():
            return {}
        doc = json.loads(paseo_path.read_text(encoding="utf-8-sig"))
        providers = ((doc.get("agents") or {}).get("providers") or {}) if isinstance(doc, dict) else {}
        return {str(k): v for k, v in providers.items() if isinstance(k, str) and isinstance(v, dict)}
    except Exception:
        return {}


def _register_claude_display_items(provider_id: str, label: str | None = None) -> None:
    if provider_id not in PROVIDER_DISPLAY_ITEMS:
        PROVIDER_DISPLAY_ITEMS[provider_id] = (
            f"{provider_id}:fable",
            f"{provider_id}:7d",
            f"{provider_id}:5h",
        )
    if provider_id != "claude" and provider_id not in PROVIDER_LABELS:
        base = (label or provider_id.replace("-", " ").title()).strip()
        PROVIDER_LABELS[provider_id] = base
        PROVIDER_LABELS[f"{provider_id}:fable"] = f"{base} fable 7d"
        PROVIDER_LABELS[f"{provider_id}:7d"] = f"{base} 7d"
        PROVIDER_LABELS[f"{provider_id}:5h"] = f"{base} 5h"


def discover_claude_homes() -> dict[str, Path]:
    """Return {provider_id: config_dir} for all configured or discovered Claude accounts."""
    homes: dict[str, Path] = {"claude": CLAUDE_HOME}
    if CLAUDE_HOME_2 != HOME / ".claude-2" or CLAUDE_HOME_2.is_dir():
        homes["claude-2"] = CLAUDE_HOME_2
    for pid, spec in _read_paseo_providers().items():
        if spec.get("enabled") is False:
            continue
        extends = str(spec.get("extends") or "").strip().lower()
        if pid == "claude" or extends == "claude" or (not extends and pid.startswith("claude-")):
            env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
            cfg = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
            if pid == "claude" and CLAUDE_HOME != HOME / ".claude":
                homes[pid] = CLAUDE_HOME
            elif pid == "claude-2" and CLAUDE_HOME_2 != HOME / ".claude-2":
                homes[pid] = CLAUDE_HOME_2
            elif cfg:
                homes[pid] = Path(cfg).expanduser()
            elif pid not in homes:
                homes[pid] = HOME / f".{pid}"
            _register_claude_display_items(pid, str(spec.get("label") or "").strip() or None)
    try:
        for entry in sorted(HOME.glob(".claude-*")):
            if entry.is_dir():
                pid = entry.name.lstrip(".")
                if pid and pid not in homes:
                    homes[pid] = entry
                    _register_claude_display_items(pid)
    except OSError:
        pass
    for pid in homes:
        _register_claude_display_items(pid)
    return homes


def claude_account_home(provider_id: str = "claude") -> Path:
    if provider_id == "claude":
        return CLAUDE_HOME
    if provider_id == "claude-2" and CLAUDE_HOME_2 != HOME / ".claude-2":
        return CLAUDE_HOME_2
    homes = discover_claude_homes()
    if provider_id not in homes and provider_id.startswith("claude-"):
        _register_claude_display_items(provider_id)
    return homes.get(provider_id, HOME / f".{provider_id}")


def claude_account_paseo_env(provider_id: str) -> dict[str, str]:
    spec = _read_paseo_providers().get(provider_id) or {}
    env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
    return {str(k): str(v) for k, v in env.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}


def is_claude_provider(provider_id: str) -> bool:
    return provider_id == "claude" or provider_id.startswith("claude-") or provider_id in discover_claude_homes()


def discover_codex_accounts() -> dict[str, CodexAccount]:
    """Return {provider_id: CodexAccount} for all configured or discovered Codex accounts."""
    accounts: dict[str, CodexAccount] = {
        pid: acc for pid, acc in CODEX_ACCOUNT_BY_ID.items()
        if pid == "codex" or acc.home != HOME / f".{pid}" or acc.home.is_dir()
    }
    for pid, spec in _read_paseo_providers().items():
        if spec.get("enabled") is False or pid in accounts:
            continue
        extends = str(spec.get("extends") or "").strip().lower()
        if pid == "codex" or extends == "codex" or (not extends and pid.startswith("codex-")):
            env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
            cfg = str(env.get("CODEX_HOME") or "").strip()
            home = Path(cfg).expanduser() if cfg else (HOME / (".codex" if pid == "codex" else f".{pid}"))
            accounts[pid] = CodexAccount(pid, pid, home)
            PROVIDER_LABELS.setdefault(pid, str(spec.get("label") or pid.replace("-", " ").title()).strip())
    try:
        for entry in sorted(HOME.glob(".codex-*")):
            if entry.is_dir() and not entry.name.startswith((".codex-chatgpt-web", ".codex-pro-consult")):
                pid = entry.name.lstrip(".")
                if pid and pid not in accounts:
                    accounts[pid] = CodexAccount(pid, pid, entry)
                    PROVIDER_LABELS.setdefault(pid, pid.replace("-", " ").title())
    except OSError:
        pass
    CODEX_ACCOUNT_BY_ID.update(accounts)
    return accounts


def provider_fetch_id(item: str) -> str:
    """The provider a display item is fetched from ("claude:5h" -> "claude")."""
    if item == 'grok-bot':
        return 'grok'  # Saved runner selection uses the ordinary Grok meter.
    provider, sep, _ = item.partition(":")
    if sep and (provider in PROVIDER_DISPLAY_ITEMS or is_claude_provider(provider)):
        _register_claude_display_items(provider)
        return provider
    return item

# SuperGrok / Grok Build credit window (same endpoint CC Switch / CodexBar use).
GROK_BILLING_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"


# Many LiteLLM virtual keys cannot call /key/info. Spend and budget come back
# on actual LLM responses as x-litellm-key-spend / x-litellm-key-max-budget.
# Probe models are overridable; leave empty unless env or config supplies an endpoint.
LLM_PROXY_DEFAULT_ENDPOINT = ""
LLM_PROXY_EMBED_MODEL = "gemini-embedding-2"
LLM_PROXY_CHAT_FALLBACK_MODEL = "glm-5.3-flash"
# Virtual keys often cannot call /key/info. Weekly (7d) budgets reset Monday
# midnight in the proxy timezone (UTC+8 unless configured).
LLM_PROXY_BUDGET_DURATION_DEFAULT = "7d"
LLM_PROXY_TIMEZONE_DEFAULT = "UTC+8"
LLM_PROXY_RESET_HEADERS = (
    "x-litellm-key-budget-reset-at",
    "x-litellm-budget-reset-at",
    "x-litellm-key-reset-at",
)
LLM_PROXY_DURATION_HEADERS = (
    "x-litellm-key-budget-duration",
    "x-litellm-budget-duration",
)

REFRESH_SECONDS = 300
IDLE_REFRESH_SECONDS = 1800
UNKNOWN_ACTIVITY_REFRESH_SECONDS = 900
CLAUDE_429_BACKOFF_SECONDS = 900
WINDOW_ALPHA = 0.92  # higher alpha = sharper GDI text on Windows layered windows
WARN_PCT = 70
DANGER_PCT = 90
# Defaults (overridable in ~/.usage-float/config.json via 设置)
CARD_WIDTH = 248
CARD_PADX = 10
ROW_HEIGHT = 24  # derived from font_size at runtime
FONT_SIZE_DEFAULT = 12
FONT_SIZE_MIN = 9
FONT_SIZE_MAX = 22
CARD_WIDTH_MIN = 180
CARD_WIDTH_MAX = 480
CARD_HEIGHT_MIN = 56
CARD_HEIGHT_MAX = 720
DISPLAY_MODE_FLOAT = "floating"
DISPLAY_MODE_PANEL = "panel"
MONITOR_POLL_MS = 3000
DOUBLE_CTRL_INTERVAL_SECONDS = 0.45
DOUBLE_CTRL_POLL_MS = 20
SHORTCUT_MODE_REPEAT = "repeat"
SHORTCUT_MODE_COMBO = "combo"
SHORTCUT_KEY_DEFAULT = "ctrl"
SHORTCUT_REPEAT_COUNT_DEFAULT = 2
SHORTCUT_REPEAT_COUNT_MIN = 2
SHORTCUT_REPEAT_COUNT_MAX = 8
SHORTCUT_COMBO_DEFAULT: tuple[str, ...] = ("ctrl", "alt")
CTRL_VIRTUAL_KEYS = frozenset((0x11, 0xA2, 0xA3))
_MODIFIER_VIRTUAL_KEYS: dict[str, tuple[int, ...]] = {
    "ctrl": (0x11, 0xA2, 0xA3),
    "shift": (0x10, 0xA0, 0xA1),
    "alt": (0x12, 0xA4, 0xA5),
    "win": (0x5B, 0x5C),
}
_NAMED_VIRTUAL_KEYS: dict[str, tuple[int, ...]] = {
    **_MODIFIER_VIRTUAL_KEYS,
    "esc": (0x1B,),
    "escape": (0x1B,),
    "tab": (0x09,),
    "space": (0x20,),
    "enter": (0x0D,),
}
# IME state/mode virtual keys can remain reported as physically down by
# GetAsyncKeyState (notably VK_KANJI=0x19 with Sogou). They are not real
# Ctrl combinations and must not poison double-Ctrl detection.
IME_STATE_VIRTUAL_KEYS = frozenset(
    (0x15, 0x17, 0x18, 0x19, 0x1C, 0x1D, 0x1E, 0x1F, 0xE5, 0xE7)
)
WALLPAPER_IMAGE_SECONDS = 10
WALLPAPER_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".gif",
        ".avif",
    }
)
WALLPAPER_VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".m4v",
        ".wmv",
        ".flv",
        ".ts",
        ".mpeg",
        ".mpg",
    }
)
WALLPAPER_MEDIA_EXTENSIONS = WALLPAPER_IMAGE_EXTENSIONS | WALLPAPER_VIDEO_EXTENSIONS
WALLPAPER_READY_POLL_MS = 25
WALLPAPER_FIRST_FRAME_SETTLE_SECONDS = 0.18
WALLPAPER_VIDEO_PREROLL_MS = 120
WALLPAPER_DIR_WEIGHT_DEFAULT = 10
WALLPAPER_DIR_WEIGHT_MIN = 0
WALLPAPER_DIR_WEIGHT_MAX = 100
WALLPAPER_DIR_DEPTH_DEFAULT = 1
WALLPAPER_DIR_DEPTH_MIN = 0
WALLPAPER_DIR_DEPTH_MAX = 8
WALLPAPER_RANDOM_MODE_DIRECTORY = "directory"
WALLPAPER_RANDOM_MODE_GLOBAL = "global"

# Light card colors
BG = "#ffffff"  # plate color (bg layer)
FG = "#2c2c2c"
FG_MUTED = "#9a9a9a"
FG_LABEL = "#555555"
GREEN = "#22a06b"
YELLOW = "#c98900"
RED = "#d14343"
TRACK = "#e1e5e9"
DIVIDER = "#d5dade"
PANEL_BG = "#000000"
PANEL_FG = "#f5f7fa"
PANEL_LABEL = "#e6edf3"
PANEL_MUTED = "#c7d0d9"
PANEL_TRACK = "#252b32"
PANEL_GREEN = "#35d07f"
PANEL_YELLOW = "#f5bd35"
PANEL_RED = "#ff6b6b"
# Match the original plate colour so ClearType anti-aliasing keeps the same
# neutral edge colours.  A saturated chroma key bleeds into every glyph.
TRANSPARENT_KEY = BG
BG_OPACITY_DEFAULT = 88  # 0–100 plate
TEXT_OPACITY_DEFAULT = 100  # 0–100 text layer
OPACITY_MIN = 5
OPACITY_MAX = 100


def _canonical_key_name(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    aliases = {
        "control": "ctrl",
        "ctl": "ctrl",
        "vk_control": "ctrl",
        "option": "alt",
        "meta": "win",
        "super": "win",
        "windows": "win",
        "return": "enter",
        "escape": "esc",
    }
    return aliases.get(text, text)


def _vk_codes_for_key(name: str) -> tuple[int, ...]:
    key = _canonical_key_name(name)
    if not key:
        return ()
    if key in _NAMED_VIRTUAL_KEYS:
        return _NAMED_VIRTUAL_KEYS[key]
    if len(key) == 1 and "a" <= key <= "z":
        return (ord(key.upper()),)
    if len(key) == 1 and "0" <= key <= "9":
        return (ord(key),)
    if key.startswith("f") and key[1:].isdigit():
        index = int(key[1:])
        if 1 <= index <= 24:
            return (0x70 + index - 1,)
    return ()


def _named_key_down(get_key_state: Callable[[int], int], name: str) -> bool:
    return any(bool(get_key_state(vk) & 0x8000) for vk in _vk_codes_for_key(name))


def _shortcut_keys_currently_down(get_key_state: Callable[[int], int]) -> list[str]:
    found: list[str] = []
    for name in ("ctrl", "shift", "alt", "win"):
        if _named_key_down(get_key_state, name):
            found.append(name)
    for code in range(ord("A"), ord("Z") + 1):
        name = chr(code).lower()
        if _named_key_down(get_key_state, name):
            found.append(name)
    for code in range(ord("0"), ord("9") + 1):
        name = chr(code)
        if _named_key_down(get_key_state, name):
            found.append(name)
    for index in range(1, 13):
        name = f"f{index}"
        if _named_key_down(get_key_state, name):
            found.append(name)
    for name in ("esc", "tab", "space", "enter"):
        if _named_key_down(get_key_state, name) and name not in found:
            found.append(name)
    return found


def _normalize_shortcut_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {SHORTCUT_MODE_COMBO, "chord", "hotkey", "组合", "组合键"}:
        return SHORTCUT_MODE_COMBO
    return SHORTCUT_MODE_REPEAT


def _normalize_shortcut_key(value: Any) -> str:
    key = _canonical_key_name(str(value or ""))
    if key in _MODIFIER_VIRTUAL_KEYS:
        return key
    return SHORTCUT_KEY_DEFAULT


def _normalize_shortcut_repeat_count(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = SHORTCUT_REPEAT_COUNT_DEFAULT
    return max(SHORTCUT_REPEAT_COUNT_MIN, min(SHORTCUT_REPEAT_COUNT_MAX, count))


def _normalize_shortcut_combo(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = [
            _canonical_key_name(part)
            for part in value.replace("+", " ").replace("-", " ").split()
        ]
    elif isinstance(value, (list, tuple)):
        parts = [_canonical_key_name(str(part)) for part in value]
    else:
        parts = []
    seen: list[str] = []
    for part in parts:
        if not part or part in seen or not _vk_codes_for_key(part):
            continue
        seen.append(part)
    order = list(_MODIFIER_VIRTUAL_KEYS)
    mods = [name for name in order if name in seen]
    rest = [name for name in seen if name not in order]
    normalized = mods + rest
    return normalized if len(normalized) >= 2 else list(SHORTCUT_COMBO_DEFAULT)


def _format_shortcut_combo(keys: list[str] | tuple[str, ...]) -> str:
    labels = {
        "ctrl": "Ctrl",
        "shift": "Shift",
        "alt": "Alt",
        "win": "Win",
        "esc": "Esc",
        "tab": "Tab",
        "space": "Space",
        "enter": "Enter",
    }
    return "+".join(labels.get(name, name.upper()) for name in keys)


def _normalize_shortcut_config(data: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    enabled = raw.get("shortcut_enabled")
    if enabled is None:
        enabled = raw.get("double_ctrl_toggle", True)
    return {
        "shortcut_enabled": bool(enabled),
        "shortcut_mode": _normalize_shortcut_mode(raw.get("shortcut_mode")),
        "shortcut_key": _normalize_shortcut_key(raw.get("shortcut_key")),
        "shortcut_repeat_count": _normalize_shortcut_repeat_count(
            raw.get("shortcut_repeat_count")
        ),
        "shortcut_combo": _normalize_shortcut_combo(raw.get("shortcut_combo")),
        "double_ctrl_toggle": bool(enabled),
    }


@dataclass
class _RepeatKeyDetector:
    """N clean taps of one key, ignoring combos like Ctrl+C."""

    interval_seconds: float = DOUBLE_CTRL_INTERVAL_SECONDS
    tap_count: int = SHORTCUT_REPEAT_COUNT_DEFAULT
    key_down: bool = False
    press_used: bool = False
    taps: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self.key_down = False
        self.press_used = False
        self.taps.clear()

    def update(self, key_down: bool, other_key_down: bool, now: float) -> bool:
        needed = max(1, int(self.tap_count))
        if key_down:
            if not self.key_down:
                self.press_used = bool(other_key_down)
            elif other_key_down:
                self.press_used = True
            self.key_down = True
            return False

        if not self.key_down:
            if self.taps and now - self.taps[-1] > self.interval_seconds:
                self.taps.clear()
            return False

        self.key_down = False
        if self.press_used:
            self.press_used = False
            self.taps.clear()
            return False

        if self.taps and now - self.taps[-1] > self.interval_seconds:
            self.taps.clear()
        self.taps.append(now)
        if len(self.taps) < needed:
            return False
        recent = self.taps[-needed:]
        if all(
            0.0 <= recent[index + 1] - recent[index] <= self.interval_seconds
            for index in range(needed - 1)
        ):
            self.taps.clear()
            return True
        return False


class _DoubleCtrlDetector(_RepeatKeyDetector):
    """Back-compat name for the default two-tap Ctrl detector."""

    @property
    def ctrl_down(self) -> bool:
        return self.key_down

    @property
    def last_tap_at(self) -> float | None:
        return self.taps[-1] if self.taps else None


@dataclass
class _ComboKeyDetector:
    held: bool = False

    def reset(self) -> None:
        self.held = False

    def update(self, active: bool) -> bool:
        if active:
            if self.held:
                return False
            self.held = True
            return True
        self.held = False
        return False


def _other_shortcut_key_down(
    get_key_state: Callable[[int], int],
    exclude_keys: list[str] | None = None,
) -> bool:
    excluded = set(IME_STATE_VIRTUAL_KEYS)
    for name in exclude_keys if exclude_keys is not None else ["ctrl"]:
        excluded.update(_vk_codes_for_key(name))
    return any(
        bool(get_key_state(vk) & 0x8000)
        for vk in range(1, 256)
        if vk not in excluded
    )


def _collect_wallpaper_media(folder: str | Path | None) -> list[Path]:
    """Return a stable recursive image/video playlist from a user folder."""
    if not folder:
        return []
    root = Path(folder).expanduser()
    if not root.is_dir():
        return []
    try:
        files = [
            path.resolve()
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in WALLPAPER_MEDIA_EXTENSIONS
        ]
    except OSError:
        return []
    return sorted(files, key=lambda path: str(path).casefold())


def _scan_wallpaper_folder_slow(
    folder: str | Path | None,
    *,
    throttle_every: int = 32,
    throttle_sleep: float = 0.002,
    stop_check: Callable[[], bool] | None = None,
) -> list[Path]:
    """Incrementally walk a wallpaper directory with gentle I/O throttling."""
    if not folder:
        return []
    try:
        root = Path(folder).expanduser().resolve()
    except OSError:
        return []
    if not root.is_dir():
        return []
    files: list[Path] = []
    inspected = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            if stop_check is not None and stop_check():
                return []
            base = Path(dirpath)
            for fname in filenames:
                if stop_check is not None and stop_check():
                    return []
                inspected += 1
                if throttle_every > 0 and throttle_sleep > 0 and inspected % throttle_every == 0:
                    time.sleep(throttle_sleep)
                ext = os.path.splitext(fname)[1].casefold()
                if ext in WALLPAPER_MEDIA_EXTENSIONS:
                    try:
                        files.append((base / fname).resolve())
                    except OSError:
                        continue
    except OSError:
        return []
    return sorted(files, key=lambda path: str(path).casefold())


def _wallpaper_path_key(path: str | Path) -> str:
    """Stable case-insensitive key for persisted Windows media directories."""
    expanded = os.path.abspath(os.path.normpath(str(Path(path).expanduser())))
    return os.path.normcase(expanded)


def load_wallpaper_index_cache(
    cache_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load persisted wallpaper media index cache from disk."""
    target = cache_path or WALLPAPER_INDEX_CACHE_PATH
    try:
        if not target.exists():
            return {}
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        folders_raw = raw.get("folders") if isinstance(raw.get("folders"), dict) else raw
        result: dict[str, dict[str, Any]] = {}
        for k, entry in folders_raw.items():
            if not isinstance(entry, dict):
                continue
            files_list = entry.get("files")
            if not isinstance(files_list, list):
                continue
            folder_str = str(entry.get("folder") or k)
            key = _wallpaper_path_key(folder_str)
            clean_files = [str(f) for f in files_list if f]
            result[key] = {
                "folder": folder_str,
                "files": clean_files,
                "scanned_at": float(entry.get("scanned_at") or 0.0),
            }
        return result
    except Exception:
        return {}


def save_wallpaper_index_cache(
    cache: dict[str, dict[str, Any]],
    cache_path: Path | None = None,
) -> None:
    """Persist wallpaper media index cache atomically (excluding in-memory _ keys)."""
    target = cache_path or WALLPAPER_INDEX_CACHE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        clean_folders: dict[str, dict[str, Any]] = {}
        for k, entry in (cache or {}).items():
            if not isinstance(entry, dict):
                continue
            clean_folders[k] = {
                ek: ev
                for ek, ev in entry.items()
                if not str(ek).startswith("_")
            }
        payload = {"version": 1, "folders": clean_folders}
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception:
        pass


def _ensure_entry_parsed_paths(entry: dict[str, Any]) -> tuple[list[Path], list[tuple[str, str, Path]]]:
    """Lazily build and cache sorted Path objects on an index_cache entry."""
    raw_list = entry.get("files")
    if not isinstance(raw_list, list):
        return [], []
    if (
        entry.get("_parsed_ref") is raw_list
        and entry.get("_parsed_len") == len(raw_list)
        and isinstance(entry.get("_only_paths"), list)
        and isinstance(entry.get("_parsed_tuples"), list)
    ):
        return entry["_only_paths"], entry["_parsed_tuples"]

    tuples: list[tuple[str, str, Path]] = []
    seen_local: set[str] = set()
    for raw_file in raw_list:
        if not raw_file:
            continue
        s = str(raw_file)
        fkey = os.path.normcase(s)
        if fkey in seen_local:
            continue
        seen_local.add(fkey)
        tuples.append((s.casefold(), fkey, Path(s)))
    tuples.sort(key=lambda item: item[0])
    only_paths = [item[2] for item in tuples]
    entry["_parsed_ref"] = raw_list
    entry["_parsed_len"] = len(raw_list)
    entry["_parsed_tuples"] = tuples
    entry["_only_paths"] = only_paths
    return only_paths, tuples


def _cached_wallpaper_media_for_folders(
    folders: list[str] | tuple[str, ...] | None,
    index_cache: dict[str, dict[str, Any]] | None,
) -> tuple[list[Path], list[str]]:
    """Return (cached_media_files, uncached_folders) without touching the filesystem."""
    normalized = _normalize_wallpaper_folders(folders)
    cache = index_cache if isinstance(index_cache, dict) else {}
    uncached: list[str] = []
    valid_entries: list[dict[str, Any]] = []
    for folder in normalized:
        key = _wallpaper_path_key(folder)
        entry = cache.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("files"), list):
            uncached.append(folder)
            continue
        valid_entries.append(entry)

    if not valid_entries:
        return [], uncached
    if len(valid_entries) == 1:
        only_paths, _ = _ensure_entry_parsed_paths(valid_entries[0])
        return list(only_paths), uncached

    merged_tuples: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for entry in valid_entries:
        _, tuples = _ensure_entry_parsed_paths(entry)
        for cfold, fkey, p_obj in tuples:
            if fkey in seen:
                continue
            seen.add(fkey)
            merged_tuples.append((cfold, p_obj))
    merged_tuples.sort(key=lambda item: item[0])
    return [item[1] for item in merged_tuples], uncached


def _middle_elide_path(path_or_text: str | Path, max_chars: int = 38) -> str:
    """Elide middle path segments when too long, always keeping first and last segments."""
    text = str(path_or_text or "").strip()
    if max_chars < 8:
        max_chars = 8
    if len(text) <= max_chars:
        return text

    # Case 1: Display label separated by " / " (e.g. "Root / SubA / SubB / Leaf")
    if " / " in text:
        sep = " / "
        parts = [p.strip() for p in text.split(" / ") if p.strip()]
        if len(parts) >= 2:
            first_seg = parts[0]
            last_seg = parts[-1]
            tail_parts = [last_seg]
            for seg in reversed(parts[1:-1]):
                candidate_tail = sep.join([seg, *tail_parts])
                candidate = f"{first_seg}{sep}…{sep}{candidate_tail}"
                if len(candidate) <= max_chars:
                    tail_parts.insert(0, seg)
                else:
                    break
            elided = f"{first_seg}{sep}…{sep}{sep.join(tail_parts)}"
            if len(elided) <= max_chars:
                return elided
            return f"{first_seg}{sep}…{sep}{last_seg}"

    # Case 2: Filesystem path separated by "\" or "/"
    sep = "\\" if "\\" in text else "/"
    norm = text.replace("/", sep).replace("\\", sep)
    raw_parts = [p for p in norm.split(sep) if p]
    leading = sep if norm.startswith(sep) and not norm.startswith(sep * 2) else ""
    if norm.startswith(sep * 2):
        leading = sep * 2

    if len(raw_parts) >= 2:
        # On Windows drive paths like "E:\FolderA\FolderB\Leaf", include drive + first folder
        # as the first segment when there are >= 3 parts so "E:\" alone isn't ambiguous.
        if len(raw_parts) >= 3 and raw_parts[0].endswith(":"):
            first_seg = f"{raw_parts[0]}{sep}{raw_parts[1]}"
            middle_parts = raw_parts[2:-1]
        else:
            first_seg = f"{leading}{raw_parts[0]}"
            middle_parts = raw_parts[1:-1]
        last_seg = raw_parts[-1]

        if middle_parts:
            tail_parts = [last_seg]
            for seg in reversed(middle_parts):
                candidate_tail = sep.join([seg, *tail_parts])
                candidate = f"{first_seg}{sep}…{sep}{candidate_tail}"
                if len(candidate) <= max_chars:
                    tail_parts.insert(0, seg)
                else:
                    break
            elided = f"{first_seg}{sep}…{sep}{sep.join(tail_parts)}"
            if len(elided) <= max_chars:
                return elided

        return f"{first_seg}{sep}…{sep}{last_seg}"

    head = max(3, (max_chars - 1) // 2)
    tail = max(3, max_chars - 1 - head)
    return f"{text[:head]}…{text[-tail:]}"


def _normalize_wallpaper_folders(
    value: Any,
    legacy_folder: str | Path | None = None,
) -> list[str]:
    raw: list[Any]
    if isinstance(value, (str, Path)):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    if not raw and legacy_folder:
        raw = [legacy_folder]
    folders: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not item:
            continue
        try:
            path = os.path.abspath(os.path.normpath(str(Path(item).expanduser())))
            key = os.path.normcase(path)
        except (OSError, TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        folders.append(path)
    return folders


def _normalize_wallpaper_disabled_folders(
    value: Any,
    folders: list[str] | tuple[str, ...],
) -> list[str]:
    """Keep disabled entries canonical, ordered, and limited to saved roots."""
    normalized_folders = _normalize_wallpaper_folders(folders)
    try:
        disabled_keys = {
            _wallpaper_path_key(folder)
            for folder in _normalize_wallpaper_folders(value)
        }
    except (OSError, TypeError, ValueError):
        disabled_keys = set()
    return [
        folder
        for folder in normalized_folders
        if _wallpaper_path_key(folder) in disabled_keys
    ]


def _enabled_wallpaper_folders(
    folders: list[str] | tuple[str, ...],
    disabled_folders: Any,
) -> list[str]:
    normalized_folders = _normalize_wallpaper_folders(folders)
    disabled = _normalize_wallpaper_disabled_folders(
        disabled_folders,
        normalized_folders,
    )
    disabled_keys = {_wallpaper_path_key(folder) for folder in disabled}
    return [
        folder
        for folder in normalized_folders
        if _wallpaper_path_key(folder) not in disabled_keys
    ]


def _normalize_wallpaper_folder_depths(
    value: Any,
    folders: list[str] | tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Normalize per-root subdirectory recursion depth (0..8, default 1)."""
    normalized: dict[str, int] = {}
    if isinstance(value, dict):
        for folder, raw_depth in value.items():
            if not folder:
                continue
            try:
                depth = int(raw_depth)
                key = _wallpaper_path_key(str(folder))
            except (OSError, TypeError, ValueError):
                continue
            normalized[key] = max(
                WALLPAPER_DIR_DEPTH_MIN,
                min(WALLPAPER_DIR_DEPTH_MAX, depth),
            )
    if folders is not None:
        valid_keys = {_wallpaper_path_key(f) for f in _normalize_wallpaper_folders(folders)}
        normalized = {k: v for k, v in normalized.items() if k in valid_keys}
    return normalized


def _prepare_root_depth_specs(
    root_folders: list[str] | tuple[str, ...] | None,
    folder_depths: dict[str, int] | None,
) -> list[tuple[int, Path, Path, int]]:
    """Pre-parse (parts_len, root_path, abs_root_path, depth) once per operation."""
    if not root_folders:
        return []
    normalized_depths = _normalize_wallpaper_folder_depths(folder_depths)
    specs: list[tuple[int, Path, Path, int]] = []
    for root_text in root_folders:
        if not root_text:
            continue
        root_path = Path(root_text)
        abs_root = Path(os.path.abspath(str(root_path)))
        root_key = _wallpaper_path_key(root_path)
        depth = normalized_depths.get(root_key, WALLPAPER_DIR_DEPTH_DEFAULT)
        specs.append((len(root_path.parts), root_path, abs_root, depth))
    return specs


def _resolve_parent_bucket_from_specs(
    parent: Path,
    specs: list[tuple[int, Path, Path, int]],
) -> Path:
    if not specs:
        return parent
    candidates: list[tuple[int, Path, Path, int]] = []
    abs_parent: Path | None = None
    for parts_len, root_path, abs_root, depth in specs:
        try:
            rel = parent.relative_to(root_path)
            matched_root = root_path
        except ValueError:
            try:
                if abs_parent is None:
                    abs_parent = Path(os.path.abspath(str(parent)))
                rel = abs_parent.relative_to(abs_root)
                matched_root = abs_root
            except ValueError:
                continue
        candidates.append((parts_len, matched_root, rel, depth))
    if not candidates:
        return parent
    _, best_root, rel_parent, depth = max(candidates, key=lambda item: item[0])
    if depth <= 0 or not rel_parent.parts:
        return best_root
    return best_root.joinpath(*rel_parent.parts[:depth])


def _wallpaper_bucket_directory(
    file_path: Path,
    root_folders: list[str] | tuple[str, ...] | None = None,
    folder_depths: dict[str, int] | None = None,
) -> Path:
    """Resolve the weight/display bucket directory for a media file respecting recursion depth."""
    parent = file_path.parent
    if not root_folders:
        return parent
    specs = _prepare_root_depth_specs(root_folders, folder_depths)
    return _resolve_parent_bucket_from_specs(parent, specs)


def _wallpaper_directory_label(
    directory: Path,
    root_folders: list[str] | tuple[str, ...],
    *,
    max_chars: int = 42,
) -> str:
    """Format a bucket directory relative to its root folder and elide middle segments if long."""
    candidates: list[tuple[int, Path, Path]] = []
    for root_text in root_folders:
        if not root_text:
            continue
        root_path = Path(root_text)
        try:
            relative = directory.relative_to(root_path)
        except ValueError:
            try:
                relative = Path(os.path.abspath(str(directory))).relative_to(
                    Path(os.path.abspath(str(root_path)))
                )
                root_path = Path(os.path.abspath(str(root_path)))
            except ValueError:
                continue
        candidates.append((len(root_path.parts), root_path, relative))
    if not candidates:
        return _middle_elide_path(str(directory), max_chars=max_chars)
    _, root_path, relative = max(candidates, key=lambda item: item[0])
    root_name = root_path.name or str(root_path)
    if not relative.parts:
        raw = _tr(f"{root_name} /（根目录）", f'{root_name} / (root folder)')
    else:
        raw = f"{root_name} / {' / '.join(relative.parts)}"
    return _middle_elide_path(raw, max_chars=max_chars)


def _normalize_wallpaper_weights(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for folder, raw_weight in value.items():
        if not folder:
            continue
        try:
            weight = int(raw_weight)
            key = _wallpaper_path_key(str(folder))
        except (OSError, TypeError, ValueError):
            continue
        normalized[key] = max(
            WALLPAPER_DIR_WEIGHT_MIN,
            min(WALLPAPER_DIR_WEIGHT_MAX, weight),
        )
    return normalized


def _merge_wallpaper_weight_values(
    current: Any,
    pending: Any,
) -> dict[str, int]:
    """Merge raw settings-entry values without relying on Tk focus changes."""
    merged = _normalize_wallpaper_weights(current)
    if not isinstance(pending, dict):
        return merged
    for folder, raw_weight in pending.items():
        if not folder:
            continue
        try:
            key = _wallpaper_path_key(folder)
        except (OSError, TypeError, ValueError):
            continue
        fallback = merged.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT)
        try:
            weight = int(str(raw_weight).strip())
        except (TypeError, ValueError):
            weight = fallback
        merged[key] = max(
            WALLPAPER_DIR_WEIGHT_MIN,
            min(WALLPAPER_DIR_WEIGHT_MAX, weight),
        )
    return merged


def _commit_wallpaper_settings_before_close(
    commit_weights: Callable[[], None],
    apply_other_settings: Callable[[], None],
) -> None:
    """Preserve pending entry text before another handler can rebuild its rows."""
    commit_weights()
    apply_other_settings()


def _collect_wallpaper_media_folders(folders: list[str] | tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for folder in folders:
        for path in _collect_wallpaper_media(folder):
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return sorted(files, key=lambda path: str(path).casefold())


def _wallpaper_media_directories(
    files: list[Path],
    root_folders: list[str] | tuple[str, ...] | None = None,
    folder_depths: dict[str, int] | None = None,
) -> list[tuple[Path, int]]:
    # Group by path.parent first so 10,000+ files only resolve ~10-30 unique parent directories
    parent_counts: dict[Path, int] = {}
    for path in files:
        parent = path.parent
        parent_counts[parent] = parent_counts.get(parent, 0) + 1
    if not parent_counts:
        return []
    specs = _prepare_root_depth_specs(root_folders, folder_depths)
    counts: dict[str, tuple[Path, int]] = {}
    for parent, p_count in parent_counts.items():
        bucket = _resolve_parent_bucket_from_specs(parent, specs)
        key = _wallpaper_path_key(bucket)
        existing = counts.get(key)
        counts[key] = (bucket, (existing[1] if existing else 0) + p_count)
    return sorted(counts.values(), key=lambda item: str(item[0]).casefold())


def _normalize_wallpaper_random_mode(value: Any) -> str:
    mode = str(value or WALLPAPER_RANDOM_MODE_GLOBAL).strip().casefold()
    if mode == WALLPAPER_RANDOM_MODE_DIRECTORY:
        return WALLPAPER_RANDOM_MODE_DIRECTORY
    return WALLPAPER_RANDOM_MODE_GLOBAL


def _randomize_wallpaper_session(
    files: list[Path],
    rng: random.Random | random.SystemRandom | None = None,
    *,
    folder_weights: dict[str, int] | None = None,
    random_mode: str = WALLPAPER_RANDOM_MODE_GLOBAL,
    root_folders: list[str] | tuple[str, ...] | None = None,
    folder_depths: dict[str, int] | None = None,
) -> tuple[list[Path], float | None]:
    """Shuffle globally or lock one session to a weighted media directory."""
    ordered = list(files)
    chooser = rng or random.SystemRandom()
    chooser.shuffle(ordered)
    random_mode = _normalize_wallpaper_random_mode(random_mode)
    if ordered and (
        folder_weights is not None
        or random_mode == WALLPAPER_RANDOM_MODE_DIRECTORY
        or root_folders is not None
    ):
        normalized_weights = _normalize_wallpaper_weights(folder_weights)
        specs = _prepare_root_depth_specs(root_folders, folder_depths)
        parent_to_key: dict[Path, str] = {}
        directories: dict[str, list[Path]] = {}
        for path in ordered:
            parent = path.parent
            b_key = parent_to_key.get(parent)
            if b_key is None:
                bucket = _resolve_parent_bucket_from_specs(parent, specs)
                b_key = _wallpaper_path_key(bucket)
                parent_to_key[parent] = b_key
            directories.setdefault(b_key, []).append(path)
        weighted = [
            (
                key,
                normalized_weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT),
            )
            for key in directories
        ]
        weighted = [(key, weight) for key, weight in weighted if weight > 0]
        # An all-zero setup cannot select a directory. Fall back to equal
        # probability so wallpaper mode still remains usable.
        if not weighted:
            weighted = [(key, 1) for key in directories]
        total = sum(weight for _, weight in weighted)
        if total > 0:
            target = chooser.uniform(0.0, float(total))
            running = 0.0
            chosen_key = weighted[-1][0]
            for key, weight in weighted:
                running += weight
                if target <= running:
                    chosen_key = key
                    break
            if random_mode == WALLPAPER_RANDOM_MODE_DIRECTORY:
                ordered = directories[chosen_key]
            else:
                chosen = directories[chosen_key][0]
                ordered.remove(chosen)
                ordered.insert(0, chosen)
    # Video sessions intentionally start at 0:00. Randomness applies to media
    # and directory selection only, never to playback progress.
    return ordered, None


def _with_preferred_wallpaper_first(
    files: list[Path],
    preferred: Path | None,
) -> list[Path]:
    """Put a dropped/requested file first without otherwise changing order."""
    if preferred is None:
        return list(files)
    preferred_key = os.path.normcase(str(preferred))
    return [
        preferred,
        *(
            path
            for path in files
            if os.path.normcase(str(path)) != preferred_key
        ),
    ]


def _build_wallpaper_playback_order(
    files: list[Path],
    *,
    preferred_first: Path | None = None,
    keep_order: bool = False,
    folder_weights: dict[str, int] | None = None,
    random_mode: str = WALLPAPER_RANDOM_MODE_GLOBAL,
    root_folders: list[str] | tuple[str, ...] | None = None,
    folder_depths: dict[str, int] | None = None,
    rng: random.Random | random.SystemRandom | None = None,
) -> tuple[list[Path], float | None]:
    if keep_order:
        return _with_preferred_wallpaper_first(files, preferred_first), None
    ordered, start_percent = _randomize_wallpaper_session(
        files,
        rng,
        folder_weights=folder_weights,
        random_mode=random_mode,
        root_folders=root_folders,
        folder_depths=folder_depths,
    )
    if preferred_first is None:
        return ordered, start_percent
    return _with_preferred_wallpaper_first(ordered, preferred_first), None


_MPV_EXECUTABLE_CACHE: dict[str, Path | None] = {}
WALLPAPER_PLAYLIST_MAX_ITEMS = 512


def _find_mpv_executable(configured: str | Path | None = None) -> Path | None:
    """Find bundled, configured, PATH, or Lively's existing mpv executable."""
    cache_key = str(configured or "")
    if cache_key in _MPV_EXECUTABLE_CACHE:
        cached = _MPV_EXECUTABLE_CACHE[cache_key]
        if cached is not None and cached.is_file():
            return cached
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            APP_DIR / "vendor" / "mpv" / "mpv.exe",
            HOME / ".usage-float" / "runtime" / "mpv" / "mpv.exe",
        ]
    )
    local_app_data = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    candidates.extend(
        [
            local_app_data / "Programs" / "Lively Wallpaper" / "plugins" / "mpv" / "mpv.exe",
            local_app_data / "Lively Wallpaper" / "Mpv" / "mpv.exe",
        ]
    )
    from_path = shutil.which("mpv.exe") or shutil.which("mpv")
    if from_path:
        candidates.append(Path(from_path))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved.is_file():
                _MPV_EXECUTABLE_CACHE[cache_key] = resolved
                return resolved
        except OSError:
            continue
    _MPV_EXECUTABLE_CACHE[cache_key] = None
    return None


def _fast_file_uri(path: Path) -> str:
    """Convert a local Path to a file:// URI without triggering Win32 resolve() disk syscalls."""
    if not path.is_absolute():
        path = Path(os.path.abspath(str(path)))
    return path.as_uri()


def _write_wallpaper_playlist(files: list[Path], destination: Path) -> None:
    """Write our trusted local paths as file URIs for mpv's M3U parser."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    sliced = files[:WALLPAPER_PLAYLIST_MAX_ITEMS]
    lines = ["#EXTM3U", *(_fast_file_uri(path) for path in sliced)]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")



def _build_mpv_wallpaper_command(
    executable: Path,
    host_hwnd: int,
    playlist_path: Path | None,
    *,
    image_seconds: int = WALLPAPER_IMAGE_SECONDS,
    first_file: Path | None = None,
    start_percent: float | None = None,
    ipc_pipe: str | None = None,
    paused: bool = False,
    audio_enabled: bool = True,
    loop_file: bool = False,
) -> list[str]:
    """Low-overhead mpv command adapted from Lively's documented backend."""
    hwnd_u32 = int(host_hwnd) & 0xFFFFFFFF
    command = [
        str(executable),
        "--no-config",
        "--profile=fast",
        "--hwdec=auto-safe",
        "--sub=no",
        "--cache=yes",
        "--cache-secs=2",
        "--demuxer-readahead-secs=2",
        "--demuxer-max-bytes=16MiB",
        "--demuxer-max-back-bytes=0",
        "--demuxer-hysteresis-secs=1",
        "--prefetch-playlist=yes",
        "--autoload-files=no",
        "--osc=no",
        "--osd-level=0",
        "--input-default-bindings=no",
        "--input-vo-keyboard=yes",
        "--input-cursor=yes",
        "--input-builtin-dragging=no",
        "--drag-and-drop=insert-next",
        "--cursor-autohide=always",
        "--no-border",
        "--no-terminal",
        "--really-quiet",
        "--keep-open=no",
        "--loop-playlist=inf",
        f"--image-display-duration={max(1, int(image_seconds))}",
        "--panscan=1.0",
        f"--wid={hwnd_u32}",
    ]
    command.insert(4, "--mute=yes" if audio_enabled else "--no-audio")
    if WALLPAPER_START_SCRIPT_PATH.is_file():
        command.append(f"--script={WALLPAPER_START_SCRIPT_PATH}")
    if ipc_pipe:
        command.append(f"--input-ipc-server={ipc_pipe}")
    if paused:
        command.append("--pause=yes")
    if loop_file:
        command.append("--loop-file=inf")
    if first_file is not None:
        command.append("--{")
        if start_percent is not None:
            command.append(f"--start={max(0.0, min(90.0, start_percent)):.2f}%")
        command.extend([str(first_file), "--}"])
    if playlist_path is not None:
        command.append(f"--playlist={playlist_path}")
    return command


def _find_embedded_mpv_window(host_hwnd: int) -> int:
    """Return mpv's native child HWND once its render window exists."""
    if sys.platform != "win32" or not host_hwnd:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        find_window_ex = ctypes.windll.user32.FindWindowExW
        find_window_ex.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        ]
        find_window_ex.restype = wintypes.HWND
        return int(find_window_ex(int(host_hwnd), 0, "mpv", None) or 0)
    except Exception:
        return 0


def _move_native_child_window(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
    """Apply an immediate physical move to a Tk child HWND on Windows."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        move_window = ctypes.windll.user32.MoveWindow
        move_window.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.BOOL,
        ]
        move_window.restype = wintypes.BOOL
        return bool(
            move_window(
                int(hwnd),
                int(x),
                int(y),
                max(1, int(width)),
                max(1, int(height)),
                True,
            )
        )
    except Exception:
        return False


def _set_native_window_enabled(hwnd: int, enabled: bool) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        enable_window = user32.EnableWindow
        enable_window.argtypes = [wintypes.HWND, wintypes.BOOL]
        enable_window.restype = wintypes.BOOL
        enable_window(wintypes.HWND(hwnd), wintypes.BOOL(bool(enabled)))
        if not enabled:
            try:
                ctypes.windll.ole32.RevokeDragDrop(wintypes.HWND(hwnd))
            except Exception:
                pass
            try:
                user32.RemovePropW(wintypes.HWND(hwnd), "OleDropTargetInterface")
                user32.RemovePropW(wintypes.HWND(hwnd), "OleEndPointID")
            except Exception:
                pass
        return True
    except Exception:
        return False


WALLPAPER_SHORTCUT_EXTENSIONS = frozenset({".url", ".website"})
WALLPAPER_HTML_EXTENSIONS = frozenset({".html", ".htm"})
WALLPAPER_IGNORED_URL_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".avif",
        ".gif",
        ".bmp",
        ".svg",
        ".ico",
        ".css",
        ".js",
        ".woff",
        ".woff2",
        ".ttf",
        ".json",
        ".xml",
    }
)
_DROP_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'`\x00-\x1f]+",
    re.IGNORECASE,
)
_DROP_BARE_DOMAIN_URL_PATTERN = re.compile(
    r"^(?:localhost:\d+|(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?|www\.[a-z0-9-]+(?:\.[a-z0-9-]+)+|(?:[a-z0-9-]+\.)+(?:com|cn|tv|net|org|io|ai|gg|me|cc|co|kr|jp|uk|de|fr|ru|tw|hk|xyz|top|live|vip|dev|app|be|ly|sh|so|info|biz|edu|gov)(?::\d+)?|(?:[a-z0-9-]+\.)+[a-z]{2,16}:\d+)(?:/[^\s<>\"'`\x00-\x1f]*)?$",
    re.IGNORECASE,
)
_CF_HTML_SOURCE_URL_PATTERN = re.compile(
    r"^\s*SourceURL\s*:\s*(\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_HTML_TAG_LINK_PATTERN = re.compile(
    r"<(?:a|video|iframe|source)\b[^>]*?\b(?:href|src)\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
    re.IGNORECASE,
)


def _is_static_asset_url(url: str) -> bool:
    """Return True when a URL points to a static image/style/script asset rather than a webpage or video."""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except Exception:
        return False
    scheme = parsed.scheme.casefold()
    if scheme not in ("http", "https"):
        return False
    path = parsed.path.casefold().rstrip("/")
    if not path:
        return False
    dot = path.rfind(".")
    if dot < 0:
        return False
    ext = path[dot:]
    if ext in WALLPAPER_VIDEO_EXTENSIONS:
        return False
    return ext in WALLPAPER_IGNORED_URL_EXTENSIONS


def _extract_urls_from_html_payload(raw_html: str) -> list[str]:
    """Extract target links and SourceURL from a Windows CF_HTML or HTML snippet payload."""
    text = str(raw_html or "").strip().strip("\x00")
    if not text:
        return []
    lower = text.casefold()
    if (
        "sourceurl:" not in lower
        and "<a" not in lower
        and "<video" not in lower
        and "<iframe" not in lower
    ):
        return []
    source_url: str | None = None
    source_match = _CF_HTML_SOURCE_URL_PATTERN.search(text)
    if source_match:
        candidate_source = source_match.group(1).strip()
        if candidate_source.casefold().startswith(("http://", "https://", "file://")):
            source_url = candidate_source
    results: list[str] = []
    for match in _HTML_TAG_LINK_PATTERN.finditer(text):
        raw_link = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if not raw_link:
            continue
        raw_link = html.unescape(raw_link).strip()
        if not raw_link or raw_link.casefold().startswith(("javascript:", "mailto:", "data:", "#")):
            continue
        if raw_link.startswith("//"):
            resolved = f"https:{raw_link}"
        elif raw_link.casefold().startswith(("http://", "https://", "file://")):
            resolved = raw_link
        elif source_url:
            resolved = urllib.parse.urljoin(source_url, raw_link)
        else:
            continue
        if not _is_static_asset_url(resolved):
            results.append(resolved)
    if source_url and not _is_static_asset_url(source_url):
        results.append(source_url)
    return results


def _dropped_wallpaper_videos(paths: Any) -> list[Path]:
    if not isinstance(paths, (list, tuple)):
        return []
    videos: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        if not isinstance(raw_path, (str, Path)):
            continue
        raw_text = str(raw_path).strip()
        if not raw_text or raw_text.casefold().startswith(("http://", "https://", "file://")):
            continue
        try:
            path = Path(raw_text).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            continue
        if not path.is_file() or path.suffix.casefold() not in WALLPAPER_VIDEO_EXTENSIONS:
            continue
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        videos.append(path)
    return videos


def _extract_url_from_shortcut_file(path: Path) -> str | None:
    """Read the target URL from a Windows .url or .website shortcut file."""
    try:
        raw_bytes = path.read_bytes()[:65536]
    except OSError:
        return None
    for encoding in ("utf-8-sig", "utf-16", "utf-8", "cp936", "latin-1"):
        try:
            text = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.casefold().startswith("url="):
            candidate = stripped[4:].strip().strip('"').strip("'")
            if candidate.casefold().startswith(("http://", "https://", "file://")):
                return candidate
    return None


def _normalize_dropped_url_candidate(raw: str) -> str | None:
    """Extract a playable http(s):// or file:// webpage URL from a dropped string."""
    text = str(raw or "").strip().strip("\x00")
    if not text:
        return None
    for line in text.splitlines():
        token = line.strip().strip('"').strip("'")
        if not token:
            continue
        if token.casefold().startswith(("http://", "https://", "file://")):
            return token.split()[0]
        match = _DROP_URL_PATTERN.search(token)
        if match:
            return match.group(0).rstrip(").,;]>\"'")
        if _DROP_BARE_DOMAIN_URL_PATTERN.match(token):
            if re.match(r"^(?:localhost:|(?:\d{1,3}\.){3}\d{1,3})", token, re.IGNORECASE):
                return f"http://{token}"
            return f"https://{token}"
    return None


def _dropped_wallpaper_urls(items: Any) -> list[str]:
    """Extract webpage/video URLs from dropped text, browser links, CF_HTML, .url shortcuts, or .html files."""
    if not isinstance(items, (list, tuple)):
        return []
    urls: list[str] = []
    seen: set[str] = set()

    def _add(url: str | None) -> None:
        if not url:
            return
        cleaned = url.strip()
        if not cleaned or cleaned in seen or _is_static_asset_url(cleaned):
            return
        seen.add(cleaned)
        urls.append(cleaned)

    for raw_item in items:
        if raw_item is None:
            continue
        raw_text = str(raw_item).strip().strip("\x00")
        if not raw_text:
            continue
        html_urls = _extract_urls_from_html_payload(raw_text)
        if html_urls:
            for html_url in html_urls:
                _add(html_url)
            continue
        if not raw_text.casefold().startswith(("http://", "https://", "file://")):
            try:
                path = Path(raw_text).expanduser()
                if path.is_file():
                    suffix = path.suffix.casefold()
                    if suffix in WALLPAPER_VIDEO_EXTENSIONS:
                        continue
                    if suffix in WALLPAPER_SHORTCUT_EXTENSIONS:
                        _add(_extract_url_from_shortcut_file(path.resolve()))
                        continue
                    if suffix in WALLPAPER_HTML_EXTENSIONS:
                        _add(_fast_file_uri(path.resolve()))
                        continue
            except (OSError, TypeError, ValueError):
                pass
        _add(_normalize_dropped_url_candidate(raw_text))
    return urls


_BROWSER_WINDOW_CLASSES = frozenset({"Chrome_WidgetWin_1", "MozillaWindowClass"})
_BROWSER_PROCESS_NAMES = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "brave.exe",
        "vivaldi.exe",
        "opera.exe",
        "arc.exe",
        "firefox.exe",
        "waterfox.exe",
        "zen.exe",
    }
)
_browser_hwnd_cache: dict[int, tuple[int, bool]] = {}


def _get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


def _close_native_window(hwnd: int) -> bool:
    """Close a top-level window by posting WM_CLOSE."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        WM_CLOSE = 0x0010
        post_message = ctypes.windll.user32.PostMessageW
        post_message.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        post_message.restype = wintypes.BOOL
        return bool(post_message(wintypes.HWND(int(hwnd)), WM_CLOSE, 0, 0))
    except Exception:
        return False


def _is_external_browser_window(hwnd: int, *, exclude_hwnds: Iterable[int] = ()) -> bool:
    """Return True when hwnd is a visible top-level window of an external web browser."""
    if sys.platform != "win32" or not hwnd:
        return False
    hwnd_int = int(hwnd)
    if hwnd_int in set(exclude_hwnds):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.IsWindowVisible(wintypes.HWND(hwnd_int)):
            return False
        GWL_STYLE = -16
        WS_CHILD = 0x40000000
        style = int(user32.GetWindowLongW(wintypes.HWND(hwnd_int), GWL_STYLE) or 0)
        if style & WS_CHILD:
            return False
        cls_buf = ctypes.create_unicode_buffer(64)
        if not user32.GetClassNameW(wintypes.HWND(hwnd_int), cls_buf, 64):
            return False
        if cls_buf.value not in _BROWSER_WINDOW_CLASSES:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd_int), ctypes.byref(pid))
        pid_int = int(pid.value or 0)
        if not pid_int or pid_int == os.getpid():
            return False
        cached = _browser_hwnd_cache.get(hwnd_int)
        if cached is not None and cached[0] == pid_int:
            return cached[1]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int)
        if not hproc:
            return False
        try:
            path_buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not kernel32.QueryFullProcessImageNameW(
                hproc, 0, path_buf, ctypes.byref(size)
            ):
                return False
            exe_name = Path(path_buf.value).name.casefold()
            result = exe_name in _BROWSER_PROCESS_NAMES
            if len(_browser_hwnd_cache) > 256:
                _browser_hwnd_cache.clear()
            _browser_hwnd_cache[hwnd_int] = (pid_int, result)
            return result
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:
        return False


def _extract_browser_window_url(hwnd: int) -> str | None:
    """Read the active webpage URL from a Chrome/Edge/Firefox window via UI Automation."""
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        ole32 = ctypes.windll.ole32
        oleaut32 = ctypes.windll.oleaut32
        oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
        oleaut32.SysFreeString.restype = None

        co_hr = int(ole32.CoInitialize(None))
        co_owned = co_hr >= 0

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        clsid_uia = GUID(
            0xFF48DBA4,
            0x60EF,
            0x4201,
            (0xAA, 0x87, 0x54, 0x10, 0x3E, 0xEF, 0x59, 0x4E),
        )
        iid_uia = GUID(
            0x30CBE57D,
            0xD9D0,
            0x452A,
            (0xAB, 0x13, 0x7A, 0xC5, 0xAC, 0x48, 0x25, 0xEE),
        )

        def vtbl_fn(obj_ptr: int, slot_index: int, restype: Any, *argtypes: Any) -> Any:
            vtbl = ctypes.c_void_p.from_address(int(obj_ptr)).value
            fn_addr = ctypes.c_void_p.from_address(
                int(vtbl) + slot_index * ctypes.sizeof(ctypes.c_void_p)
            ).value
            proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
            return proto(fn_addr)

        def release_com(ptr: int | None) -> None:
            if ptr:
                try:
                    vtbl_fn(int(ptr), 2, wintypes.ULONG)(int(ptr))
                except Exception:
                    pass

        def bstr_to_str(bstr_ptr: int | None) -> str:
            if not bstr_ptr:
                return ""
            try:
                return ctypes.wstring_at(bstr_ptr)
            finally:
                oleaut32.SysFreeString(bstr_ptr)

        uia = ctypes.c_void_p()
        walker = ctypes.c_void_p()
        root_el = ctypes.c_void_p()
        try:
            hr = int(
                ole32.CoCreateInstance(
                    ctypes.byref(clsid_uia),
                    None,
                    1,
                    ctypes.byref(iid_uia),
                    ctypes.byref(uia),
                )
            )
            if hr < 0 or not uia.value:
                return None
            ElementFromHandle = vtbl_fn(
                uia.value,
                6,
                ctypes.c_long,
                wintypes.HWND,
                ctypes.POINTER(ctypes.c_void_p),
            )
            get_ControlViewWalker = vtbl_fn(
                uia.value, 14, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
            )
            if (
                get_ControlViewWalker(uia.value, ctypes.byref(walker)) < 0
                or not walker.value
            ):
                return None
            if (
                ElementFromHandle(
                    uia.value, wintypes.HWND(int(hwnd)), ctypes.byref(root_el)
                )
                < 0
                or not root_el.value
            ):
                return None
            GetFirstChild = vtbl_fn(
                walker.value,
                4,
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )
            GetNextSibling = vtbl_fn(
                walker.value,
                6,
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )

            skip_classes = {
                "TabStripRegionView",
                "TabStrip",
                "BookmarkBarView",
                "SidePanel",
            }
            omnibox_candidate: str | None = None
            doc_candidate: str | None = None
            edit_candidate: str | None = None

            def read_value(el_ptr: int) -> str:
                get_pat = vtbl_fn(
                    el_ptr,
                    16,
                    ctypes.c_long,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p),
                )
                pat = ctypes.c_void_p()
                val_str = ""
                if get_pat(el_ptr, 10002, ctypes.byref(pat)) >= 0 and pat.value:
                    get_val = vtbl_fn(
                        pat.value, 4, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
                    )
                    b_val = ctypes.c_void_p()
                    if get_val(pat.value, ctypes.byref(b_val)) >= 0:
                        val_str = bstr_to_str(b_val.value)
                    release_com(pat.value)
                return val_str.strip()

            def search(el_ptr: int, depth: int) -> bool:
                nonlocal omnibox_candidate, doc_candidate, edit_candidate
                if depth > 9 or not el_ptr:
                    return False
                get_ct = vtbl_fn(
                    el_ptr, 21, ctypes.c_long, ctypes.POINTER(ctypes.c_int)
                )
                get_cls = vtbl_fn(
                    el_ptr, 30, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
                )
                ct = ctypes.c_int()
                b_cls = ctypes.c_void_p()
                get_ct(el_ptr, ctypes.byref(ct))
                get_cls(el_ptr, ctypes.byref(b_cls))
                cls_name = bstr_to_str(b_cls.value)
                if cls_name in skip_classes:
                    return False
                if cls_name == "OmniboxViewViews":
                    val = read_value(el_ptr)
                    norm = _normalize_dropped_url_candidate(val)
                    if norm and not _is_static_asset_url(norm):
                        omnibox_candidate = norm
                        return True
                if ct.value == 50030:
                    if doc_candidate is None:
                        val = read_value(el_ptr)
                        norm = _normalize_dropped_url_candidate(val)
                        if norm and not _is_static_asset_url(norm):
                            doc_candidate = norm
                    # Never descend into the web page DOM tree
                    return False
                if ct.value == 50004 and edit_candidate is None:
                    val = read_value(el_ptr)
                    norm = _normalize_dropped_url_candidate(val)
                    if norm and not _is_static_asset_url(norm):
                        edit_candidate = norm

                child = ctypes.c_void_p()
                if (
                    GetFirstChild(walker.value, el_ptr, ctypes.byref(child)) >= 0
                    and child.value
                ):
                    cur = child.value
                    while cur:
                        done = search(cur, depth + 1)
                        nxt = ctypes.c_void_p()
                        if not done:
                            GetNextSibling(walker.value, cur, ctypes.byref(nxt))
                        release_com(cur)
                        if done:
                            return True
                        cur = nxt.value
                return False

            search(root_el.value, 0)
            return omnibox_candidate or doc_candidate or edit_candidate
        finally:
            release_com(root_el.value)
            release_com(walker.value)
            release_com(uia.value)
            if co_owned:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass
    except Exception:
        return None


class _BrowserWindowDropTracker:
    """Detect when an external browser window is dragged and released onto UsageFloat."""

    def __init__(self, *, min_move_px: int = 16) -> None:
        self.min_move_px = max(4, int(min_move_px))
        self.reset()

    def reset(self) -> None:
        self.hwnd: int = 0
        self.start_rect: tuple[int, int, int, int] | None = None
        self.last_rect: tuple[int, int, int, int] | None = None
        self.start_cursor: tuple[int, int] | None = None
        self.move_distance: int = 0
        self.moved: bool = False

    @property
    def active(self) -> bool:
        return bool(self.hwnd)

    def update_down(
        self,
        *,
        fg_hwnd: int,
        is_browser: bool,
        window_rect: tuple[int, int, int, int] | None,
        cursor_pos: tuple[int, int],
    ) -> None:
        if not fg_hwnd or not is_browser or window_rect is None:
            self.reset()
            return
        if self.hwnd != int(fg_hwnd):
            self.hwnd = int(fg_hwnd)
            self.start_rect = window_rect
            self.last_rect = window_rect
            self.start_cursor = cursor_pos
            self.move_distance = 0
            self.moved = False
            return
        if self.last_rect is not None:
            l, t, r, b = window_rect
            ll, lt, lr, lb = self.last_rect
            w, h = r - l, b - t
            lw, lh = lr - ll, lb - lt
            dx, dy = l - ll, t - lt
            # Window move preserves width & height; window resize changes them.
            if (dx != 0 or dy != 0) and abs(w - lw) <= 2 and abs(h - lh) <= 2:
                self.move_distance += abs(dx) + abs(dy)
                if self.move_distance >= self.min_move_px:
                    self.moved = True
        self.last_rect = window_rect

    def finish_up(
        self,
        *,
        cursor_pos: tuple[int, int],
        target_rect: tuple[int, int, int, int],
    ) -> tuple[int, tuple[int, int, int, int] | None, bool] | None:
        if not self.hwnd or not self.moved:
            self.reset()
            return None
        hwnd = self.hwnd
        start_rect = self.start_rect
        start_cursor = self.start_cursor
        self.reset()
        cx, cy = int(cursor_pos[0]), int(cursor_pos[1])
        tl, tt, tr, tb = (
            int(target_rect[0]),
            int(target_rect[1]),
            int(target_rect[2]),
            int(target_rect[3]),
        )
        if not (tl <= cx < tr and tt <= cy < tb):
            return None
        start_outside = True
        if start_cursor is not None:
            sx, sy = int(start_cursor[0]), int(start_cursor[1])
            start_outside = not (tl <= sx < tr and tt <= sy < tb)
        return (hwnd, start_rect, start_outside)


def _choose_ole_drop_effect(allowed_mask: int) -> int:
    """Pick a drop effect permitted by the drag source (Explorer uses COPY; browsers often use LINK)."""
    DROPEFFECT_COPY = 1
    DROPEFFECT_MOVE = 2
    DROPEFFECT_LINK = 4
    mask = int(allowed_mask or 0)
    if mask & DROPEFFECT_COPY:
        return DROPEFFECT_COPY
    if mask & DROPEFFECT_LINK:
        return DROPEFFECT_LINK
    if mask & DROPEFFECT_MOVE:
        return DROPEFFECT_MOVE
    return DROPEFFECT_COPY


class _PendingFileDrops:
    """Hand Explorer paths from a native drop callback to the Tk thread.

    Native drop callbacks must not call into Tcl/Tk. Queue the paths here and
    let a Tk ``after`` poller drain them on the regular event loop.
    """

    def __init__(self, deliver: Callable[[list[str]], None]) -> None:
        self._deliver = deliver
        self._pending: list[list[str]] = []
        self._lock = threading.Lock()

    def push(self, files: list[str]) -> None:
        if not files:
            return
        with self._lock:
            self._pending.append(list(files))

    def drain(self) -> None:
        with self._lock:
            batches = self._pending
            self._pending = []
        for files in batches:
            try:
                self._deliver(files)
            except Exception:
                _log_exception("file drop")


def _install_windows_file_drop(
    widget: Any,
    on_files: Callable[[list[str]], None],
) -> Callable[[], None]:
    """Accept Explorer file drops and browser URL/link drops on a Tk top-level.

    OLE ``IDropTarget`` is used instead of subclassing the Tk WNDPROC.
    Replacing Tk's window procedure with a ctypes callback re-enters Tcl from
    inside DispatchMessage; with IME/antivirus hooks that abort()s pythonw
    (BEX64 / FAST_FAIL_FATAL_APP_EXIT) as soon as a file is dropped.
    """
    if sys.platform != "win32":
        return lambda: None
    try:
        import ctypes
        from ctypes import wintypes

        if ctypes.sizeof(ctypes.c_void_p) != 8:
            return lambda: None

        widget.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        if not hwnd:
            return lambda: None

        HRESULT = ctypes.c_long
        S_OK = 0
        E_NOINTERFACE = HRESULT(0x80004002).value
        E_POINTER = HRESULT(0x80004003).value
        E_UNEXPECTED = HRESULT(0x8000FFFF).value
        DROPEFFECT_NONE = 0
        CF_TEXT = 1
        CF_UNICODETEXT = 13
        CF_HDROP = 15
        DVASPECT_CONTENT = 1
        TYMED_HGLOBAL = 1

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class FORMATETC(ctypes.Structure):
            _fields_ = [
                ("cfFormat", wintypes.WORD),
                ("ptd", ctypes.c_void_p),
                ("dwAspect", wintypes.DWORD),
                ("lindex", wintypes.LONG),
                ("tymed", wintypes.DWORD),
            ]

        class STGMEDIUM(ctypes.Structure):
            _fields_ = [
                ("tymed", wintypes.DWORD),
                ("hGlobal", wintypes.HGLOBAL),
                ("pUnkForRelease", ctypes.c_void_p),
            ]

        class IDropTargetVtbl(ctypes.Structure):
            pass

        class IDropTarget(ctypes.Structure):
            _fields_ = [("lpVtbl", ctypes.POINTER(IDropTargetVtbl))]

        QueryInterfaceFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )
        AddRefFn = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
        ReleaseFn = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
        # POINTL is two LONGs. On x64 that 8-byte struct is passed in one
        # register; declaring it as Structure would make ctypes pass a pointer.
        DragEnterFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_uint64,
            ctypes.POINTER(wintypes.DWORD),
        )
        DragOverFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_uint64,
            ctypes.POINTER(wintypes.DWORD),
        )
        DragLeaveFn = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
        DropFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_uint64,
            ctypes.POINTER(wintypes.DWORD),
        )
        GetDataFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(FORMATETC),
            ctypes.POINTER(STGMEDIUM),
        )
        QueryGetDataFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(FORMATETC),
        )

        IDropTargetVtbl._fields_ = [
            ("QueryInterface", QueryInterfaceFn),
            ("AddRef", AddRefFn),
            ("Release", ReleaseFn),
            ("DragEnter", DragEnterFn),
            ("DragOver", DragOverFn),
            ("DragLeave", DragLeaveFn),
            ("Drop", DropFn),
        ]

        iid_iunknown = GUID(
            0x00000000,
            0x0000,
            0x0000,
            (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46),
        )
        iid_idroptarget = GUID(
            0x00000122,
            0x0000,
            0x0000,
            (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46),
        )

        ole32 = ctypes.windll.ole32
        ole32.OleInitialize.argtypes = [ctypes.c_void_p]
        ole32.OleInitialize.restype = HRESULT
        ole32.OleUninitialize.argtypes = []
        ole32.OleUninitialize.restype = None
        ole32.RegisterDragDrop.argtypes = [wintypes.HWND, ctypes.c_void_p]
        ole32.RegisterDragDrop.restype = HRESULT
        ole32.RevokeDragDrop.argtypes = [wintypes.HWND]
        ole32.RevokeDragDrop.restype = HRESULT
        ole32.ReleaseStgMedium.argtypes = [ctypes.POINTER(STGMEDIUM)]
        ole32.ReleaseStgMedium.restype = None
        drag_query_file = ctypes.windll.shell32.DragQueryFileW
        drag_query_file.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        ]
        drag_query_file.restype = wintypes.UINT
        user32 = ctypes.windll.user32
        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        cf_url_w = int(user32.RegisterClipboardFormatW("UniformResourceLocatorW") or 0)
        cf_url_a = int(user32.RegisterClipboardFormatW("UniformResourceLocator") or 0)
        cf_moz_url = int(user32.RegisterClipboardFormatW("text/x-moz-url") or 0)
        cf_html = int(user32.RegisterClipboardFormatW("HTML Format") or 0)

        def _guid_bytes(guid: GUID) -> bytes:
            return bytes(guid)

        def _vtbl_slot(this: int, index: int) -> int:
            vtbl = ctypes.c_void_p.from_address(int(this)).value
            if not vtbl:
                return 0
            slot = ctypes.c_void_p.from_address(
                int(vtbl) + index * ctypes.sizeof(ctypes.c_void_p)
            ).value
            return int(slot or 0)

        def _make_cf(cf_format: int) -> FORMATETC:
            fmt = FORMATETC()
            fmt.cfFormat = int(cf_format) & 0xFFFF
            fmt.ptd = None
            fmt.dwAspect = DVASPECT_CONTENT
            fmt.lindex = -1
            fmt.tymed = TYMED_HGLOBAL
            return fmt

        def _hdrop_cf() -> FORMATETC:
            return _make_cf(CF_HDROP)

        def _hdrop_paths(handle: int) -> list[str]:
            if not handle:
                return []
            drop_handle = wintypes.HANDLE(handle)
            count = int(drag_query_file(drop_handle, 0xFFFFFFFF, None, 0))
            paths: list[str] = []
            for index in range(count):
                length = int(drag_query_file(drop_handle, index, None, 0))
                if length <= 0:
                    continue
                buffer = ctypes.create_unicode_buffer(length + 1)
                if drag_query_file(drop_handle, index, buffer, length + 1):
                    paths.append(buffer.value)
            return paths

        def _hglobal_text(handle: int, *, wide: bool) -> str:
            if not handle:
                return ""
            hglobal = wintypes.HGLOBAL(handle)
            size = int(kernel32.GlobalSize(hglobal) or 0)
            if size <= 0:
                return ""
            ptr = kernel32.GlobalLock(hglobal)
            if not ptr:
                return ""
            try:
                raw = ctypes.string_at(ptr, min(size, 65536))
            finally:
                kernel32.GlobalUnlock(hglobal)
            if wide:
                text = raw.decode("utf-16-le", errors="ignore")
            else:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1", errors="ignore")
            return text.split("\x00", 1)[0].strip()

        text_formats: list[tuple[int, bool]] = []
        for fmt_id, is_wide in (
            (cf_url_w, True),
            (cf_moz_url, True),
            (cf_html, False),
            (CF_UNICODETEXT, True),
            (cf_url_a, False),
            (CF_TEXT, False),
        ):
            if fmt_id > 0:
                text_formats.append((fmt_id, is_wide))

        def _dataobject_has_drop_payload(pdata: int) -> bool:
            slot = _vtbl_slot(pdata, 5)
            if not slot:
                return False
            query = QueryGetDataFn(slot)
            if int(query(pdata, ctypes.byref(_hdrop_cf()))) >= 0:
                return True
            for fmt_id, _is_wide in text_formats:
                if int(query(pdata, ctypes.byref(_make_cf(fmt_id)))) >= 0:
                    return True
            return False

        def _dataobject_items(pdata: int) -> list[str]:
            slot = _vtbl_slot(pdata, 3)
            if not slot:
                return []
            get_data = GetDataFn(slot)
            hdrop_paths: list[str] = []
            medium = STGMEDIUM()
            hr = int(get_data(pdata, ctypes.byref(_hdrop_cf()), ctypes.byref(medium)))
            if hr >= 0:
                try:
                    hdrop_paths = _hdrop_paths(int(medium.hGlobal or 0))
                finally:
                    ole32.ReleaseStgMedium(ctypes.byref(medium))
                if hdrop_paths and (
                    _dropped_wallpaper_videos(hdrop_paths)
                    or _dropped_wallpaper_urls(hdrop_paths)
                ):
                    return hdrop_paths
            collected_texts: list[str] = []
            for fmt_id, is_wide in text_formats:
                medium = STGMEDIUM()
                hr = int(
                    get_data(
                        pdata,
                        ctypes.byref(_make_cf(fmt_id)),
                        ctypes.byref(medium),
                    )
                )
                if hr < 0:
                    continue
                try:
                    text = _hglobal_text(int(medium.hGlobal or 0), wide=is_wide)
                finally:
                    ole32.ReleaseStgMedium(ctypes.byref(medium))
                if text:
                    collected_texts.append(text)
            if collected_texts:
                urls = _dropped_wallpaper_urls(collected_texts)
                if urls:
                    return urls
            return hdrop_paths

        def _set_effect(pdw_effect: Any, effect: int) -> None:
            if pdw_effect:
                pdw_effect[0] = effect

        def _allowed_effect(pdw_effect: Any) -> int:
            if not pdw_effect:
                return 1
            return _choose_ole_drop_effect(int(pdw_effect[0]))

        ole_hr = int(ole32.OleInitialize(None))
        if ole_hr < 0:
            return lambda: None
        ole_owned = ole_hr == S_OK
        pending = _PendingFileDrops(on_files)
        active = True
        accepting = False
        refcount = 1

        @QueryInterfaceFn
        def query_interface(this: int, riid: Any, ppv: Any) -> int:
            try:
                if not ppv:
                    return E_POINTER
                guid = riid if isinstance(riid, GUID) else riid.contents
                requested = _guid_bytes(guid)
                if requested in (
                    _guid_bytes(iid_iunknown),
                    _guid_bytes(iid_idroptarget),
                ):
                    ppv[0] = this
                    add_ref(this)
                    return S_OK
                ppv[0] = None
                return E_NOINTERFACE
            except Exception:
                return E_UNEXPECTED

        @AddRefFn
        def add_ref(_this: int) -> int:
            nonlocal refcount
            refcount += 1
            return refcount

        @ReleaseFn
        def release(_this: int) -> int:
            nonlocal refcount
            refcount = max(0, refcount - 1)
            return refcount

        @DragEnterFn
        def drag_enter(
            _this: int,
            pdata: int,
            _key_state: int,
            _point: int,
            pdw_effect: Any,
        ) -> int:
            nonlocal accepting
            try:
                accepting = bool(active and pdata and _dataobject_has_drop_payload(pdata))
                _set_effect(
                    pdw_effect,
                    _allowed_effect(pdw_effect) if accepting else DROPEFFECT_NONE,
                )
                return S_OK
            except Exception:
                accepting = False
                _set_effect(pdw_effect, DROPEFFECT_NONE)
                return E_UNEXPECTED

        @DragOverFn
        def drag_over(
            _this: int,
            _key_state: int,
            _point: int,
            pdw_effect: Any,
        ) -> int:
            try:
                _set_effect(
                    pdw_effect,
                    _allowed_effect(pdw_effect) if (active and accepting) else DROPEFFECT_NONE,
                )
                return S_OK
            except Exception:
                _set_effect(pdw_effect, DROPEFFECT_NONE)
                return E_UNEXPECTED

        @DragLeaveFn
        def drag_leave(_this: int) -> int:
            nonlocal accepting
            accepting = False
            return S_OK

        @DropFn
        def drop(
            _this: int,
            pdata: int,
            _key_state: int,
            _point: int,
            pdw_effect: Any,
        ) -> int:
            try:
                items = _dataobject_items(pdata) if active and pdata else []
                if items:
                    pending.push(items)
                _set_effect(
                    pdw_effect,
                    _allowed_effect(pdw_effect) if items else DROPEFFECT_NONE,
                )
                return S_OK
            except Exception:
                _set_effect(pdw_effect, DROPEFFECT_NONE)
                return E_UNEXPECTED

        vtbl = IDropTargetVtbl(
            query_interface,
            add_ref,
            release,
            drag_enter,
            drag_over,
            drag_leave,
            drop,
        )
        target = IDropTarget(ctypes.pointer(vtbl))
        target_ptr = ctypes.cast(ctypes.pointer(target), ctypes.c_void_p)
        register_hr = int(ole32.RegisterDragDrop(wintypes.HWND(hwnd), target_ptr))
        if register_hr < 0:
            if ole_owned:
                ole32.OleUninitialize()
            return lambda: None

        poll_job: Any = None

        def poll_pending() -> None:
            nonlocal poll_job
            poll_job = None
            if not active:
                return
            pending.drain()
            try:
                poll_job = widget.after(40, poll_pending)
            except Exception:
                poll_job = None

        try:
            poll_job = widget.after(40, poll_pending)
        except Exception:
            ole32.RevokeDragDrop(wintypes.HWND(hwnd))
            if ole_owned:
                ole32.OleUninitialize()
            return lambda: None

        def uninstall() -> None:
            nonlocal active, poll_job
            if not active:
                return
            active = False
            job = poll_job
            poll_job = None
            if job is not None:
                try:
                    widget.after_cancel(job)
                except Exception:
                    pass
            try:
                ole32.RevokeDragDrop(wintypes.HWND(hwnd))
            except Exception:
                pass
            if ole_owned:
                try:
                    ole32.OleUninitialize()
                except Exception:
                    pass

        setattr(uninstall, "_registered", True)
        # ctypes function-pointer fields do not keep the Python callables alive.
        setattr(
            uninstall,
            "_drop_state",
            (
                vtbl,
                target,
                target_ptr,
                pending,
                query_interface,
                add_ref,
                release,
                drag_enter,
                drag_over,
                drag_leave,
                drop,
            ),
        )
        return uninstall
    except Exception:
        _log_exception("file drop install")
        return lambda: None


def _send_mpv_ipc_command(pipe_name: str, command: list[Any]) -> bool:
    """Send one JSON IPC command to a Windows mpv named pipe."""
    if sys.platform != "win32" or not pipe_name:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        write_file = ctypes.windll.kernel32.WriteFile
        write_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        write_file.restype = wintypes.BOOL
        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        generic_write = 0x40000000
        open_existing = 3
        handle = create_file(pipe_name, generic_write, 0, None, open_existing, 0, None)
        if int(handle) == ctypes.c_void_p(-1).value:
            return False
        try:
            payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
            written = wintypes.DWORD(0)
            return bool(
                write_file(handle, payload, len(payload), ctypes.byref(written), None)
                and written.value == len(payload)
            )
        finally:
            close_handle(handle)
    except Exception:
        return False


class _MpvWallpaperPlayer:
    """Thin lifecycle wrapper; decoding/rendering remains entirely in mpv."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.playlist_path = CONFIG_PATH.parent / "wallpaper-playlist.m3u8"
        self.last_error = ""
        self.last_order: list[Path] = []
        self.last_start_percent: float | None = None
        self.ipc_pipe = ""
        self._session_number = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(
        self,
        executable: Path,
        host_hwnd: int,
        files: list[Path],
        *,
        image_seconds: int = WALLPAPER_IMAGE_SECONDS,
        audio_enabled: bool = True,
        folder_weights: dict[str, int] | None = None,
        random_mode: str = WALLPAPER_RANDOM_MODE_GLOBAL,
        root_folders: list[str] | tuple[str, ...] | None = None,
        folder_depths: dict[str, int] | None = None,
        preferred_first: Path | None = None,
        keep_order: bool = False,
        loop_file: bool = False,
    ) -> tuple[bool, str]:
        self.stop()
        if not files:
            self.last_error = _tr("文件夹中没有支持的图片或视频", 'No supported images or videos in this folder')
            return False, self.last_error
        try:
            if loop_file:
                ordered = [preferred_first if preferred_first is not None else files[0]]
                start_percent = None
            else:
                ordered, start_percent = _build_wallpaper_playback_order(
                    files,
                    preferred_first=preferred_first,
                    keep_order=keep_order,
                    folder_weights=folder_weights,
                    random_mode=random_mode,
                    root_folders=root_folders,
                    folder_depths=folder_depths,
                )
            first_file = ordered[0]
            remaining = [] if loop_file else ordered[1:]
            if remaining:
                _write_wallpaper_playlist(remaining, self.playlist_path)
            self._session_number += 1
            self.ipc_pipe = (
                rf"\\.\pipe\usage-float-{os.getpid()}-{self._session_number}"
            )
            command = _build_mpv_wallpaper_command(
                executable,
                host_hwnd,
                self.playlist_path if remaining else None,
                image_seconds=image_seconds,
                first_file=first_file,
                start_percent=start_percent,
                ipc_pipe=self.ipc_pipe,
                paused=True,
                audio_enabled=audio_enabled,
                loop_file=loop_file,
            )
            creationflags = 0
            if sys.platform == "win32":
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self.last_order = ordered
            self.last_start_percent = start_percent
            self.last_error = ""
            return True, ""
        except Exception as exc:
            self.process = None
            self.last_error = _tr(f"动态壁纸启动失败：{exc}", f'Live wallpaper failed to start: {exc}')
            return False, self.last_error

    def set_paused(self, paused: bool) -> bool:
        if not self.running:
            return False
        return _send_mpv_ipc_command(
            self.ipc_pipe,
            ["set_property", "pause", bool(paused)],
        )

    def set_muted(self, muted: bool) -> bool:
        if not self.running:
            return False
        return _send_mpv_ipc_command(
            self.ipc_pipe,
            ["set_property", "mute", bool(muted)],
        )

    def stop(self) -> None:
        process = self.process
        self.process = None
        self.ipc_pipe = ""
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except Exception:
            return

        def _reap(proc: Any = process) -> None:
            try:
                proc.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

        threading.Thread(
            target=_reap,
            daemon=True,
            name="mpv-stop-reaper",
        ).start()


_BROWSER_EXECUTABLE_CACHE: dict[str, Path | None] = {}

_BROWSER_VIDEO_CONTROLLER_JS = r"""
(() => {
    if (window.__uf_controller_installed__) return;
    window.__uf_controller_installed__ = true;
    window.__uf_paused_by_host__ = false;
    if (typeof window.__uf_muted_by_host__ !== "boolean") {
        window.__uf_muted_by_host__ = false;
    }
    if (typeof window.__uf_volume__ !== "number") {
        window.__uf_volume__ = 1.0;
    }
    window.__uf_video_ready__ = false;
    window.__uf_loop_count__ = 0;

    try {
        const rawProfile = localStorage.getItem("bpx_player_profile");
        if (rawProfile) {
            const parsed = JSON.parse(rawProfile);
            if (parsed && typeof parsed === "object") {
                parsed.media = Object.assign({}, parsed.media || {}, { handoff: 0, autoplay: true });
                localStorage.setItem("bpx_player_profile", JSON.stringify(parsed));
            }
        }
        localStorage.setItem("recommend_auto_play", "close");
    } catch (_e) {}

    const origPause = HTMLMediaElement.prototype.pause;
    const origPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.pause = function(...args) {
        if (window.__uf_paused_by_host__) {
            return origPause.apply(this, args);
        }
        if (this === window.__uf_active_video__ && !this.ended) {
            return;
        }
        return origPause.apply(this, args);
    };

    window.addEventListener("ended", (e) => {
        const t = e && e.target;
        if (t && (t === window.__uf_active_video__ || t.tagName === "VIDEO" || t.tagName === "BWP-VIDEO")) {
            e.stopImmediatePropagation();
            e.stopPropagation();
            window.__uf_loop_count__++;
            try { t.currentTime = 0; } catch (_err) {}
            try { origPlay.call(t); } catch (_err) {}
        }
    }, true);

    function ensureStyle() {
        if (!document.head && !document.documentElement) return;
        if (document.getElementById("__uf_video_style__")) return;
        const style = document.createElement("style");
        style.id = "__uf_video_style__";
        style.textContent = `
            html, body {
                margin: 0 !important;
                padding: 0 !important;
                width: 100vw !important;
                height: 100vh !important;
                overflow: hidden !important;
                background: #000 !important;
                scrollbar-width: none !important;
                cursor: none !important;
                user-select: none !important;
            }
            ::-webkit-scrollbar { display: none !important; }
            body.__uf_video_locked__ *:not(.__uf_video_ancestor__):not(.__uf_active_video__):not(.__uf_active_video__ *) {
                visibility: hidden !important;
                opacity: 0 !important;
                pointer-events: none !important;
            }
            .__uf_video_ancestor__ {
                transform: none !important;
                filter: none !important;
                perspective: none !important;
                backdrop-filter: none !important;
                contain: none !important;
                clip-path: none !important;
                mask: none !important;
                overflow: visible !important;
                opacity: 1 !important;
                visibility: visible !important;
                margin: 0 !important;
                padding: 0 !important;
                z-index: 2147483647 !important;
            }
            video.__uf_active_video__, bwp-video.__uf_active_video__, iframe.__uf_active_video__ {
                position: fixed !important;
                inset: 0 !important;
                left: 0 !important;
                top: 0 !important;
                right: 0 !important;
                bottom: 0 !important;
                margin: 0 !important;
                padding: 0 !important;
                width: 100vw !important;
                height: 100vh !important;
                max-width: 100vw !important;
                max-height: 100vh !important;
                object-fit: cover !important;
                object-position: center center !important;
                z-index: 2147483647 !important;
                background: #000 !important;
                transform: none !important;
                visibility: visible !important;
                opacity: 1 !important;
                display: block !important;
                border: 0 !important;
            }
        `;
        (document.head || document.documentElement).appendChild(style);
    }

    function queryAllVideos(root) {
        const out = [];
        try {
            for (const el of Array.from(root.querySelectorAll("video, bwp-video"))) {
                out.push(el);
            }
            for (const node of Array.from(root.querySelectorAll("*"))) {
                if (node.shadowRoot) {
                    for (const inner of queryAllVideos(node.shadowRoot)) {
                        out.push(inner);
                    }
                }
            }
        } catch (_e) {}
        return out;
    }

    function pickBestVideo() {
        const candidates = queryAllVideos(document);
        let best = null;
        let bestScore = -1;
        for (const v of candidates) {
            const rect = v.getBoundingClientRect();
            const vw = v.videoWidth || rect.width || 0;
            const vh = v.videoHeight || rect.height || 0;
            const hasSrc = Boolean(
                v.currentSrc || v.src || v.srcObject || v.querySelector("source") || v.tagName === "BWP-VIDEO"
            );
            const ready = Number(v.readyState || 0);
            if (!hasSrc && ready === 0 && vw === 0) continue;
            const score = (ready >= 2 ? 1e9 : 0) + (hasSrc ? 1e7 : 0) + (vw * vh) + (rect.width * rect.height);
            if (score > bestScore) {
                bestScore = score;
                best = v;
            }
        }
        return best;
    }

    function pickBestVideoIframe() {
        const iframes = Array.from(document.querySelectorAll("iframe"));
        let best = null;
        let bestArea = 0;
        for (const f of iframes) {
            const r = f.getBoundingClientRect();
            const area = Math.max(0, r.width) * Math.max(0, r.height);
            if (area >= 30000 && area > bestArea) {
                bestArea = area;
                best = f;
            }
        }
        return best;
    }

    function isAncestorChainLocked(el) {
        if (!el || !el.classList.contains("__uf_active_video__")) return false;
        if (!document.body || !document.body.classList.contains("__uf_video_locked__")) return false;
        let cur = el.parentElement;
        while (cur) {
            if (!cur.classList.contains("__uf_video_ancestor__")) return false;
            cur = cur.parentElement;
        }
        return true;
    }

    function lockElement(el) {
        if (isAncestorChainLocked(el)) return;
        for (const node of Array.from(document.querySelectorAll(".__uf_active_video__, .__uf_video_ancestor__"))) {
            node.classList.remove("__uf_active_video__", "__uf_video_ancestor__");
        }
        el.classList.add("__uf_active_video__");
        let cur = el.parentElement;
        while (cur) {
            cur.classList.add("__uf_video_ancestor__");
            cur = cur.parentElement;
        }
        if (document.body) {
            document.body.classList.add("__uf_video_locked__");
        }
    }

    function bindVideoEvents(v) {
        if (v.__uf_bound__) return;
        v.__uf_bound__ = true;
        v.addEventListener("timeupdate", () => {
            const dur = Number(v.duration || 0);
            const cur = Number(v.currentTime || 0);
            if (dur > 0.6 && isFinite(dur)) {
                const margin = dur > 2.5 ? 0.32 : 0.12;
                if (cur >= dur - margin) {
                    window.__uf_loop_count__++;
                    try { v.currentTime = 0; } catch (_e) {}
                    if (!window.__uf_paused_by_host__) {
                        try { origPlay.call(v); } catch (_e) {}
                    }
                }
            }
        }, true);
    }

    function tick() {
        ensureStyle();
        const skipBtn = document.querySelector(".ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button");
        if (skipBtn) {
            try { skipBtn.click(); } catch (_e) {}
        }
        const autonav = document.querySelector(".ytp-autonav-toggle-button[aria-checked='true']");
        if (autonav) {
            try { autonav.click(); } catch (_e) {}
        }

        const v = pickBestVideo();
        if (!v) {
            const f = pickBestVideoIframe();
            if (f) lockElement(f);
            return;
        }
        window.__uf_active_video__ = v;
        bindVideoEvents(v);
        lockElement(v);

        v.loop = true;
        try { v.setAttribute("loop", "loop"); } catch (_e) {}
        v.muted = Boolean(window.__uf_muted_by_host__);
        if (typeof window.__uf_volume__ === "number") {
            try { v.volume = Math.max(0, Math.min(1, window.__uf_volume__)); } catch (_e) {}
        }

        const dur = Number(v.duration || 0);
        const cur = Number(v.currentTime || 0);
        if (dur > 0.6 && isFinite(dur)) {
            const margin = dur > 2.5 ? 0.32 : 0.12;
            if (cur >= dur - margin || v.ended) {
                window.__uf_loop_count__++;
                try { v.currentTime = 0; } catch (_e) {}
            }
        }

        if (v.paused && !window.__uf_paused_by_host__) {
            try {
                const p = origPlay.call(v);
                if (p && typeof p.catch === "function") {
                    p.catch(() => {
                        v.muted = true;
                        try { origPlay.call(v).catch(() => {}); } catch (_e2) {}
                    });
                }
            } catch (_e) {}
        }

        if ((v.readyState >= 2 || cur > 0) && (v.videoWidth > 0 || v.tagName === "BWP-VIDEO")) {
            window.__uf_video_ready__ = true;
            if (!window.__uf_locked_href__ && cur > 0.5) {
                window.__uf_locked_href__ = location.href;
                window.__uf_locked_pathname__ = location.pathname;
            }
        }
        if (window.__uf_locked_pathname__ && location.pathname !== window.__uf_locked_pathname__) {
            try { location.replace(window.__uf_locked_href__); } catch (_e) {}
        }
    }

    window.__uf_set_paused = (paused) => {
        window.__uf_paused_by_host__ = Boolean(paused);
        const v = window.__uf_active_video__;
        if (!v) return false;
        try {
            if (window.__uf_paused_by_host__) {
                origPause.call(v);
            } else {
                origPlay.call(v);
            }
            return true;
        } catch (_e) {
            return false;
        }
    };

    window.__uf_set_muted = (muted) => {
        window.__uf_muted_by_host__ = Boolean(muted);
        const v = window.__uf_active_video__;
        if (v) {
            try { v.muted = window.__uf_muted_by_host__; } catch (_e) {}
        }
        return true;
    };

    window.__uf_seek = (deltaSeconds) => {
        const v = window.__uf_active_video__;
        if (!v) return false;
        try {
            const dur = Number(v.duration || 0);
            const cur = Number(v.currentTime || 0);
            let next = Math.max(0, cur + Number(deltaSeconds || 0));
            if (dur > 1 && isFinite(dur)) {
                next = Math.min(dur - 0.35, next);
            }
            v.currentTime = next;
            return true;
        } catch (_e) {
            return false;
        }
    };

    window.__uf_adjust_volume = (deltaPercent) => {
        const step = Number(deltaPercent || 0) / 100.0;
        const curVol = typeof window.__uf_volume__ === "number" ? window.__uf_volume__ : 1.0;
        window.__uf_volume__ = Math.max(0, Math.min(1, curVol + step));
        if (step > 0 && window.__uf_muted_by_host__) {
            window.__uf_muted_by_host__ = false;
        }
        const v = window.__uf_active_video__;
        if (v) {
            try {
                v.volume = window.__uf_volume__;
                v.muted = Boolean(window.__uf_muted_by_host__);
            } catch (_e) {}
        }
        return window.__uf_volume__;
    };

    window.__uf_get_status = () => {
        const v = window.__uf_active_video__;
        return {
            ready: Boolean(window.__uf_video_ready__),
            hasVideo: Boolean(v),
            currentTime: v ? Number(v.currentTime || 0) : 0,
            duration: v ? Number(v.duration || 0) : 0,
            paused: v ? Boolean(v.paused) : true,
            pageComplete: document.readyState === "complete",
        };
    };

    setInterval(tick, 100);
    document.addEventListener("DOMContentLoaded", tick);
    window.addEventListener("load", tick);
    tick();
})();
"""


def _find_browser_executable(configured: str | Path | None = None) -> Path | None:
    """Find Microsoft Edge, Google Chrome, or Chromium for simulated browser video playback."""
    cache_key = str(configured or "")
    if cache_key in _BROWSER_EXECUTABLE_CACHE:
        cached = _BROWSER_EXECUTABLE_CACHE[cache_key]
        if cached is not None and cached.is_file():
            return cached
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    candidates.extend(
        [
            program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            local_app_data / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    )
    for binary in ("msedge.exe", "msedge", "chrome.exe", "chrome"):
        found = shutil.which(binary)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved.is_file():
                _BROWSER_EXECUTABLE_CACHE[cache_key] = resolved
                return resolved
        except OSError:
            continue
    _BROWSER_EXECUTABLE_CACHE[cache_key] = None
    return None


def _build_browser_wallpaper_command(
    executable: Path,
    url: str,
    profile_dir: Path,
    *,
    width: int = 1280,
    height: int = 720,
    audio_enabled: bool = True,
) -> list[str]:
    """Build a frameless Chromium/Edge --app command with CDP enabled for video looping."""
    w = max(320, int(width))
    h = max(240, int(height))
    command = [
        str(executable),
        f"--user-data-dir={profile_dir}",
        f"--app={url}",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--disable-infobars",
        "--disable-notifications",
        "--disable-popup-blocking",
        "--disable-session-crashed-bubble",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,HardwareMediaKeyHandling",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--window-position=-32000,-32000",
        f"--window-size={w},{h}",
    ]
    if not audio_enabled:
        command.append("--mute-audio")
    return command


def _cdp_websocket_eval(
    ws_url: str,
    expressions: list[str],
    *,
    install_new_doc_script: str | None = None,
    timeout: float = 1.5,
) -> Any:
    """Execute one or more CDP commands over a local RFC 6455 WebSocket using stdlib only."""
    import base64
    import socket
    import struct

    if not ws_url.startswith("ws://"):
        return None
    rest = ws_url[5:]
    host_port, _, raw_path = rest.partition("/")
    path = "/" + raw_path
    host, _, port_str = host_port.partition(":")
    port = int(port_str or 80)

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
        buf = buf[buf.find(b"\r\n\r\n") + 4 :]

        def _send_json(msg: dict[str, Any]) -> None:
            payload = json.dumps(msg).encode("utf-8")
            mask = os.urandom(4)
            header = bytearray([0x81])
            n = len(payload)
            if n < 126:
                header.append(0x80 | n)
            elif n < 65536:
                header.append(0x80 | 126)
                header.extend(struct.pack("!H", n))
            else:
                header.append(0x80 | 127)
                header.extend(struct.pack("!Q", n))
            header.extend(mask)
            masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            sock.sendall(bytes(header) + masked)

        def _recv_id(expected_id: int) -> dict[str, Any] | None:
            nonlocal buf

            def _rx(count: int) -> bytes:
                nonlocal buf
                while len(buf) < count:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("WebSocket closed")
                    buf += chunk
                out, buf = buf[:count], buf[count:]
                return out

            while True:
                b0, b1 = _rx(2)
                opcode = b0 & 0x0F
                masked = bool(b1 & 0x80)
                length = b1 & 0x7F
                if length == 126:
                    length = struct.unpack("!H", _rx(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", _rx(8))[0]
                mask_key = _rx(4) if masked else b""
                data = _rx(length)
                if masked:
                    data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))
                if opcode == 0x8:
                    return None
                if opcode == 0x9:
                    pong = bytearray([0x8A, 0x80, 0, 0, 0, 0])
                    sock.sendall(bytes(pong))
                    continue
                if opcode == 0x1:
                    parsed = json.loads(data.decode("utf-8", errors="replace"))
                    if parsed.get("id") == expected_id:
                        return parsed

        msg_id = 1
        if install_new_doc_script:
            _send_json({"id": msg_id, "method": "Page.enable", "params": {}})
            _recv_id(msg_id)
            msg_id += 1
            _send_json(
                {
                    "id": msg_id,
                    "method": "Page.addScriptToEvaluateOnNewDocument",
                    "params": {"source": install_new_doc_script},
                }
            )
            _recv_id(msg_id)
            msg_id += 1

        last_value: Any = None
        for expr in expressions:
            _send_json(
                {
                    "id": msg_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True},
                }
            )
            resp = _recv_id(msg_id)
            msg_id += 1
            if isinstance(resp, dict):
                last_value = (
                    resp.get("result", {}).get("result", {}).get("value")
                )
        return last_value


def _find_browser_process_window(root_pid: int, host_hwnd: int = 0) -> int:
    """Find the top-level or already-embedded Chrome_WidgetWin_1 HWND for root_pid."""
    if sys.platform != "win32" or root_pid <= 0:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        if host_hwnd:
            existing = int(
                user32.FindWindowExW(
                    wintypes.HWND(int(host_hwnd)),
                    0,
                    "Chrome_WidgetWin_1",
                    None,
                )
                or 0
            )
            if existing:
                return existing

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found: list[int] = []

        @WNDENUMPROC
        def _enum_cb(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            if int(pid.value) != int(root_pid):
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(wintypes.HWND(hwnd), cls, 64)
            if cls.value == "Chrome_WidgetWin_1":
                found.append(int(hwnd))
                return False
            return True

        user32.EnumWindows(_enum_cb, 0)
        return found[0] if found else 0
    except Exception:
        return 0


def _embed_browser_window_in_host(browser_hwnd: int, host_hwnd: int) -> bool:
    """Reparent a Chromium/Edge window into wallpaper_host and disable its own hit-testing/drop target."""
    if sys.platform != "win32" or not browser_hwnd or not host_hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        ole32 = ctypes.windll.ole32
        GWL_STYLE = -16
        GWL_EXSTYLE = -20
        WS_CHILD = 0x40000000
        WS_VISIBLE = 0x10000000
        WS_CLIPSIBLINGS = 0x04000000
        WS_CLIPCHILDREN = 0x02000000
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_NOACTIVATE = 0x08000000

        hwnd = wintypes.HWND(int(browser_hwnd))
        parent = wintypes.HWND(int(host_hwnd))

        try:
            ole32.RevokeDragDrop(hwnd)
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            @WNDENUMPROC
            def _revoke_child(child_hwnd: int, _lparam: int) -> bool:
                try:
                    ole32.RevokeDragDrop(wintypes.HWND(child_hwnd))
                    _set_native_window_enabled(int(child_hwnd), False)
                except Exception:
                    pass
                return True

            user32.EnumChildWindows(hwnd, _revoke_child, 0)
        except Exception:
            pass

        if int(user32.GetParent(hwnd) or 0) != int(host_hwnd):
            ex_style = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0)
            ex_style = (ex_style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ctypes.c_ssize_t(ex_style))
            user32.SetParent(hwnd, parent)
            style = WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE, ctypes.c_ssize_t(style))
        _set_native_window_enabled(int(browser_hwnd), False)
        return True
    except Exception:
        return False


def _fit_embedded_browser_window(
    browser_hwnd: int,
    x: int,
    y: int,
    width: int,
    height: int,
) -> bool:
    """Position Chrome_WidgetWin_1 so its inner Chrome_RenderWidgetHostHWND fills (x, y, width, height)."""
    if sys.platform != "win32" or not browser_hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = wintypes.HWND(int(browser_hwnd))
        win_rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(win_rect))
        render_hwnd = user32.FindWindowExW(hwnd, 0, "Chrome_RenderWidgetHostHWND", None)
        ox, oy, ew, eh = 0, 0, 0, 0
        if render_hwnd:
            r_rect = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(render_hwnd), ctypes.byref(r_rect))
            rw = r_rect.right - r_rect.left
            rh = r_rect.bottom - r_rect.top
            ww = win_rect.right - win_rect.left
            wh = win_rect.bottom - win_rect.top
            if rw > 0 and rh > 0 and ww >= rw and wh >= rh:
                ox = max(0, int(r_rect.left - win_rect.left))
                oy = max(0, int(r_rect.top - win_rect.top))
                ew = max(0, int(ww - rw))
                eh = max(0, int(wh - rh))
        return _move_native_child_window(
            int(browser_hwnd),
            int(x) - ox,
            int(y) - oy,
            max(1, int(width)) + ew,
            max(1, int(height)) + eh,
        )
    except Exception:
        return False


class _BrowserWebVideoPlayer:
    """Play and loop a webpage's video in an embedded Chromium/Edge browser instance."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.profile_dir: Path | None = None
        self.url: str = ""
        self.last_error: str = ""
        self.browser_hwnd: int = 0
        self.video_ready: bool = False
        self._page_complete_at: float | None = None
        self._attached_at: float | None = None
        self._stop_event = threading.Event()
        self._cmd_lock = threading.Lock()
        self._pending_js: list[str] = []
        self._muted: bool = False
        self._paused: bool = False

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(
        self,
        executable: Path,
        host_hwnd: int,
        url: str,
        *,
        width: int = 1280,
        height: int = 720,
        audio_enabled: bool = True,
    ) -> tuple[bool, str]:
        self.stop()
        cleaned_url = str(url or "").strip()
        if not cleaned_url:
            self.last_error = _tr("无效的网页链接", 'Invalid web URL')
            return False, self.last_error
        try:
            profile_base = CONFIG_PATH.parent / "browser-sessions"
            profile_base.mkdir(parents=True, exist_ok=True)
            profile_dir = Path(
                tempfile.mkdtemp(prefix="web-video-", dir=str(profile_base))
            )
            self.profile_dir = profile_dir
            self.url = cleaned_url
            self.browser_hwnd = 0
            self.video_ready = False
            self._page_complete_at = None
            self._attached_at = None
            self._muted = not bool(audio_enabled)
            self._paused = False
            self._stop_event = threading.Event()
            with self._cmd_lock:
                self._pending_js = [
                    f"window.__uf_muted_by_host__ = {'true' if self._muted else 'false'};"
                ]

            command = _build_browser_wallpaper_command(
                executable,
                cleaned_url,
                profile_dir,
                width=width,
                height=height,
                audio_enabled=True,
            )
            creationflags = 0
            startupinfo = None
            if sys.platform == "win32":
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            self.last_error = ""
            stop_ev = self._stop_event
            proc = self.process
            threading.Thread(
                target=self._cdp_worker,
                args=(proc, profile_dir, stop_ev),
                daemon=True,
                name="browser-wallpaper-cdp",
            ).start()
            return True, ""
        except Exception as exc:
            self.process = None
            self.last_error = _tr(f"网页视频启动失败：{exc}", f'Web video failed to start: {exc}')
            return False, self.last_error

    def _cdp_worker(
        self,
        proc: subprocess.Popen[bytes],
        profile_dir: Path,
        stop_event: threading.Event,
    ) -> None:
        port_file = profile_dir / "DevToolsActivePort"
        port = 0
        for _ in range(80):
            if stop_event.is_set() or proc.poll() is not None:
                return
            if port_file.is_file():
                try:
                    lines = port_file.read_text(encoding="utf-8").splitlines()
                    if lines and lines[0].strip().isdigit():
                        port = int(lines[0].strip())
                        break
                except OSError:
                    pass
            time.sleep(0.1)
        if not port:
            return

        installed_targets: set[str] = set()
        while not stop_event.is_set() and proc.poll() is None:
            try:
                raw = (
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json",
                        timeout=1.2,
                    )
                    .read()
                    .decode("utf-8", errors="replace")
                )
                targets = json.loads(raw)
            except Exception:
                time.sleep(0.15)
                continue

            with self._cmd_lock:
                queued = list(self._pending_js)
                self._pending_js.clear()

            mute_sync = (
                f"window.__uf_muted_by_host__ = {'true' if self._muted else 'false'};"
                f"window.__uf_paused_by_host__ = {'true' if self._paused else 'false'};"
            )

            for target in targets if isinstance(targets, list) else []:
                if stop_event.is_set():
                    return
                ttype = str(target.get("type") or "")
                turl = str(target.get("url") or "")
                ws_url = str(target.get("webSocketDebuggerUrl") or "")
                tid = str(target.get("id") or ws_url)
                if ttype not in ("page", "iframe") or not ws_url:
                    continue
                if turl.startswith(("edge://", "chrome://", "chrome-extension://", "devtools://")):
                    continue
                first_time = tid not in installed_targets
                exprs = [mute_sync, _BROWSER_VIDEO_CONTROLLER_JS, *queued, "window.__uf_get_status && window.__uf_get_status()"]
                try:
                    status = _cdp_websocket_eval(
                        ws_url,
                        exprs,
                        install_new_doc_script=_BROWSER_VIDEO_CONTROLLER_JS if first_time else None,
                        timeout=1.2,
                    )
                    installed_targets.add(tid)
                    if isinstance(status, dict):
                        if status.get("ready"):
                            self.video_ready = True
                        if status.get("pageComplete") and self._page_complete_at is None:
                            self._page_complete_at = time.monotonic()
                except Exception:
                    continue

            time.sleep(0.18 if not self.video_ready else 0.45)

    def poll_window(
        self,
        host_hwnd: int,
        width: int,
        height: int,
        *,
        offscreen: bool,
    ) -> int:
        if not self.running or not host_hwnd:
            return 0
        hwnd = self.browser_hwnd
        if not hwnd:
            hwnd = _find_browser_process_window(int(self.process.pid), host_hwnd)
            if not hwnd:
                return 0
            self.browser_hwnd = hwnd
            self._attached_at = time.monotonic()
        _embed_browser_window_in_host(hwnd, host_hwnd)
        if offscreen:
            _fit_embedded_browser_window(
                hwnd,
                max(1, int(width)) + 64,
                max(1, int(height)) + 64,
                width,
                height,
            )
        else:
            _fit_embedded_browser_window(hwnd, 0, 0, width, height)
        return hwnd

    def is_ready(self) -> bool:
        if not self.browser_hwnd:
            return False
        if self.video_ready:
            return True
        now = time.monotonic()
        if self._page_complete_at is not None and now - self._page_complete_at >= 0.6:
            return True
        if self._attached_at is not None and now - self._attached_at >= 1.2:
            return True
        return False

    def set_paused(self, paused: bool) -> bool:
        self._paused = bool(paused)
        if not self.running:
            return False
        with self._cmd_lock:
            self._pending_js.append(
                f"window.__uf_set_paused && window.__uf_set_paused({'true' if self._paused else 'false'});"
            )
        return True

    def set_muted(self, muted: bool) -> bool:
        self._muted = bool(muted)
        if not self.running:
            return False
        with self._cmd_lock:
            self._pending_js.append(
                f"window.__uf_set_muted && window.__uf_set_muted({'true' if self._muted else 'false'});"
            )
        return True

    def seek(self, delta_seconds: float) -> bool:
        if not self.running:
            return False
        with self._cmd_lock:
            self._pending_js.append(
                f"window.__uf_seek && window.__uf_seek({float(delta_seconds):.2f});"
            )
        return True

    def adjust_volume(self, delta_percent: int) -> bool:
        if not self.running:
            return False
        if delta_percent > 0:
            self._muted = False
        with self._cmd_lock:
            self._pending_js.append(
                f"window.__uf_adjust_volume && window.__uf_adjust_volume({int(delta_percent)});"
            )
        return True

    def stop(self) -> None:
        self._stop_event.set()
        process = self.process
        profile_dir = self.profile_dir
        self.process = None
        self.profile_dir = None
        self.browser_hwnd = 0
        self.video_ready = False
        self._page_complete_at = None
        self._attached_at = None
        with self._cmd_lock:
            self._pending_js.clear()
        if process is None and profile_dir is None:
            return

        def _reap(proc: Any = process, pdir: Path | None = profile_dir) -> None:
            if proc is not None and proc.poll() is None:
                if sys.platform == "win32":
                    try:
                        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=flags,
                            timeout=3.0,
                            check=False,
                        )
                    except Exception:
                        pass
                try:
                    proc.terminate()
                    proc.wait(timeout=0.8)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if pdir is not None:
                for _ in range(5):
                    try:
                        shutil.rmtree(pdir, ignore_errors=False)
                        break
                    except Exception:
                        time.sleep(0.15)
                shutil.rmtree(pdir, ignore_errors=True)

        threading.Thread(
            target=_reap,
            daemon=True,
            name="browser-wallpaper-stop-reaper",
        ).start()


def _clamp_opacity(value: Any, default: int) -> int:
    """Normalize a user-facing opacity percentage."""
    try:
        return max(OPACITY_MIN, min(OPACITY_MAX, int(value)))
    except (TypeError, ValueError):
        return default


def _apply_layer_opacities(
    text_window: Any,
    plate_window: Any,
    plate_opacity: Any,
    text_opacity: Any,
    *,
    restack: Callable[[], None] | None = None,
) -> None:
    """Apply plate and text opacity without coupling the two layers.

    The text layer deliberately uses ``text_opacity`` even when a separate
    plate is unavailable.  Falling back to the plate value would make the
    plate control fade the glyphs as well, which is worse than leaving the
    unsupported plate preview unchanged.
    """
    plate_alpha = _clamp_opacity(plate_opacity, BG_OPACITY_DEFAULT) / 100.0
    text_alpha = _clamp_opacity(text_opacity, TEXT_OPACITY_DEFAULT) / 100.0

    if plate_window is not None:
        try:
            plate_window.attributes("-alpha", plate_alpha)
        except Exception:
            pass

    # Keep this in a separate try block: a plate failure must never prevent a
    # live text-opacity update.
    try:
        text_window.attributes("-alpha", text_alpha)
    except Exception:
        pass

    if plate_window is not None and restack is not None:
        try:
            restack()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config / cache
# ---------------------------------------------------------------------------


def _acquire_single_instance() -> bool:
    """Hold a per-session mutex so Windows startup restoration cannot duplicate the HUD."""
    global _INSTANCE_MUTEX_HANDLE
    if _INSTANCE_MUTEX_HANDLE is not None or sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_mutex(None, False, "Local\\UsageFloat.SingleInstance")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            close_handle(handle)
            return False
        _INSTANCE_MUTEX_HANDLE = handle
        return True
    except Exception:
        # A mutex failure must not make the HUD disappear completely. Startup
        # remains available, while process diagnostics can still reveal a fault.
        return True


def provider_label(provider_id: str) -> str:
    if provider_id not in PROVIDER_LABELS:
        provider, sep, _ = provider_id.partition(":")
        if sep and is_claude_provider(provider):
            _register_claude_display_items(provider)
    return PROVIDER_LABELS.get(provider_id, provider_id)


def _unique_provider_ids(values: Any) -> list[str]:
    out: list[str] = []
    for item in values or []:
        if isinstance(item, str):
            pid = item.strip()
            if pid == "grok-bot":
                continue  # Retired duplicate meter; Grok includes Bot consumption.
            if pid and ":" not in pid and is_claude_provider(pid):
                _register_claude_display_items(pid)
            # Legacy group entries stand for all their rows, including an
            # explicit disable of the entire group.
            for expanded in PROVIDER_DISPLAY_ITEMS.get(pid, (pid,)):
                if expanded and expanded not in out:
                    out.append(expanded)
    return out


def resolve_provider_config(
    providers: Any,
    disabled: Any = None,
    order: Any = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (order, enabled, disabled) for HUD + settings.

    Newly added DEFAULT_PROVIDERS are inserted into order and stay enabled
    unless they already appear in ``disabled``.
    """
    enabled_in = _unique_provider_ids(providers)
    disabled_list = _unique_provider_ids(disabled)
    if 'grok-bot' in (providers or []) and 'grok-bot' not in (disabled or []):
        if 'grok' not in enabled_in:
            enabled_in.append('grok')
        disabled_list = [pid for pid in disabled_list if pid != 'grok']
    disabled_in = set(disabled_list)
    known = _unique_provider_ids(list(order or []) + enabled_in + disabled_list)
    if not known:
        known = list(DEFAULT_PROVIDERS)
    for index, must in enumerate(DEFAULT_PROVIDERS):
        if must not in known:
            previous = DEFAULT_PROVIDERS[index - 1] if index else None
            if previous and must.startswith(previous + "-") and previous in known:
                known.insert(known.index(previous) + 1, must)
            else:
                known.append(must)
        if must not in disabled_in and must not in enabled_in:
            enabled_in.append(must)
    enabled_set = set(enabled_in) - disabled_in
    enabled = [pid for pid in known if pid in enabled_set]
    disabled_out = [pid for pid in known if pid not in enabled_set]
    return known, enabled, disabled_out


def move_provider(order: list[str], source: str, target: str) -> list[str]:
    """Place ``source`` at the current index of ``target``."""
    items = _unique_provider_ids(order)
    if source not in items or target not in items or source == target:
        return items
    destination = items.index(target)
    items.pop(items.index(source))
    items.insert(destination, source)
    return items


def resolve_provider_columns(
    providers: Any,
    disabled: Any = None,
    order: Any = None,
    columns: Any = None,
) -> tuple[list[str], list[str]]:
    """Return (left_column, right_column) for the two-column usage layout.

    When ``columns`` was already persisted, keep every item's column and row
    position (including unchecked gray items). Otherwise seed the two columns so
    enabled items match the panel's left/right split and disabled items balance
    the remaining slots.
    """
    known, enabled, disabled_out = resolve_provider_config(providers, disabled, order)
    valid = set(known)
    raw_left: Any = None
    raw_right: Any = None
    if isinstance(columns, dict):
        raw_left = columns.get("left")
        raw_right = columns.get("right")
    elif isinstance(columns, (list, tuple)) and len(columns) >= 2:
        raw_left, raw_right = columns[0], columns[1]

    if isinstance(raw_left, (list, tuple)) and isinstance(raw_right, (list, tuple)):
        left = [pid for pid in _unique_provider_ids(raw_left) if pid in valid]
        left_set = set(left)
        right = [
            pid
            for pid in _unique_provider_ids(raw_right)
            if pid in valid and pid not in left_set
        ]
        placed = left_set | set(right)
        for pid in known:
            if pid not in placed:
                if len(left) <= len(right):
                    left.append(pid)
                else:
                    right.append(pid)
                placed.add(pid)
        return left, right

    if not disabled_out or not enabled:
        mid = (len(known) + 1) // 2
        return list(known[:mid]), list(known[mid:])

    mid_enabled = (len(enabled) + 1) // 2
    left = list(enabled[:mid_enabled])
    right = list(enabled[mid_enabled:])
    for pid in disabled_out:
        if len(left) <= len(right):
            left.append(pid)
        else:
            right.append(pid)
    return left, right


def move_provider_in_columns(
    left: list[str],
    right: list[str],
    source: str,
    target_col: int,
    target_index: int,
) -> tuple[list[str], list[str]]:
    """Move ``source`` to ``(target_col, target_index)`` across the two columns."""
    cols = [_unique_provider_ids(left), _unique_provider_ids(right)]
    src_col = -1
    for col_idx in (0, 1):
        if source in cols[col_idx]:
            src_col = col_idx
            break
    if src_col < 0:
        return cols[0], cols[1]
    cols[src_col].remove(source)
    dest_col = 0 if int(target_col) <= 0 else 1
    dest_index = max(0, min(len(cols[dest_col]), int(target_index)))
    cols[dest_col].insert(dest_index, source)
    return cols[0], cols[1]


def scan_providers(
    configured: list[str] | None = None,
    order: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan known provider slots and report which ones have credentials."""
    known, _enabled, _disabled = resolve_provider_config(
        configured if configured is not None else discovered_default_providers(),
        [],
        order,
    )
    for extra in discovered_default_providers():
        if extra not in known and (configured is None or provider_available(extra)):
            known.append(extra)
    return [
        {
            "id": pid,
            "label": provider_label(pid),
            "available": provider_available(pid),
        }
        for pid in known
    ]


def load_config() -> dict[str, Any]:
    global _ACTIVE_LANGUAGE
    defaults: dict[str, Any] = {
        "language": "zh",
        "always_on_top": True,
        "x": None,
        "y": None,
        "providers": list(DEFAULT_PROVIDERS),
        "provider_order": list(DEFAULT_PROVIDERS),
        "disabled_providers": [],
        "provider_columns": None,
        "refresh_seconds": REFRESH_SECONDS,
        "alpha": WINDOW_ALPHA,  # legacy; migrated to bg_opacity
        "bg_opacity": BG_OPACITY_DEFAULT,
        "text_opacity": TEXT_OPACITY_DEFAULT,
        "font_size": FONT_SIZE_DEFAULT,
        "card_width": CARD_WIDTH,
        "card_height": None,  # None = auto height from content
        "allow_resize": False,
        "display_mode": DISPLAY_MODE_FLOAT,
        "monitor_device": None,
        "monitor_id": None,
        "double_ctrl_toggle": True,
        "shortcut_enabled": True,
        "shortcut_mode": SHORTCUT_MODE_REPEAT,
        "shortcut_key": SHORTCUT_KEY_DEFAULT,
        "shortcut_repeat_count": SHORTCUT_REPEAT_COUNT_DEFAULT,
        "shortcut_combo": list(SHORTCUT_COMBO_DEFAULT),
        "llm_proxy_budget_duration": LLM_PROXY_BUDGET_DURATION_DEFAULT,
        "llm_proxy_timezone": LLM_PROXY_TIMEZONE_DEFAULT,
        "wallpaper_folder": None,
        "wallpaper_folders": [],
        "wallpaper_disabled_folders": [],
        "wallpaper_folder_depths": {},
        "wallpaper_subdir_weights": {},
        "wallpaper_random_mode": WALLPAPER_RANDOM_MODE_GLOBAL,
        "wallpaper_image_seconds": WALLPAPER_IMAGE_SECONDS,
        "wallpaper_audio": True,
        "mpv_path": None,
    }
    file_data: dict[str, Any] = {}
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                file_data = data
                defaults.update(data)
    except Exception:
        pass
    try:
        rs = float(defaults.get("refresh_seconds") or REFRESH_SECONDS)
    except Exception:
        rs = REFRESH_SECONDS
    defaults["refresh_seconds"] = max(REFRESH_SECONDS, rs)
    try:
        fs = int(defaults.get("font_size") or FONT_SIZE_DEFAULT)
    except Exception:
        fs = FONT_SIZE_DEFAULT
    defaults["font_size"] = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, fs))
    try:
        cw = int(defaults.get("card_width") or CARD_WIDTH)
    except Exception:
        cw = CARD_WIDTH
    defaults["card_width"] = max(CARD_WIDTH_MIN, min(CARD_WIDTH_MAX, cw))
    ch = defaults.get("card_height")
    if ch is not None:
        try:
            ch_i = int(ch)
            defaults["card_height"] = max(CARD_HEIGHT_MIN, min(CARD_HEIGHT_MAX, ch_i))
        except Exception:
            defaults["card_height"] = None
    defaults["allow_resize"] = bool(defaults.get("allow_resize", False))
    mode = str(defaults.get("display_mode") or DISPLAY_MODE_FLOAT).lower()
    defaults["display_mode"] = (
        mode if mode in (DISPLAY_MODE_FLOAT, DISPLAY_MODE_PANEL) else DISPLAY_MODE_FLOAT
    )
    monitor_device = defaults.get("monitor_device")
    defaults["monitor_device"] = (
        str(monitor_device).strip() if monitor_device else None
    )
    monitor_id = defaults.get("monitor_id")
    defaults["monitor_id"] = str(monitor_id).strip() if monitor_id else None
    shortcut = _normalize_shortcut_config(defaults)
    defaults.update(shortcut)
    defaults["llm_proxy_budget_duration"] = (
        str(defaults.get("llm_proxy_budget_duration") or "").strip()
        or LLM_PROXY_BUDGET_DURATION_DEFAULT
    )
    defaults["llm_proxy_timezone"] = _normalize_llm_proxy_timezone(
        defaults.get("llm_proxy_timezone")
    )
    wallpaper_folders = _normalize_wallpaper_folders(
        defaults.get("wallpaper_folders"),
        defaults.get("wallpaper_folder"),
    )
    defaults["wallpaper_folders"] = wallpaper_folders
    # Keep the old field as a compatibility mirror for older releases.
    defaults["wallpaper_folder"] = wallpaper_folders[0] if wallpaper_folders else None
    defaults["wallpaper_disabled_folders"] = _normalize_wallpaper_disabled_folders(
        defaults.get("wallpaper_disabled_folders"),
        wallpaper_folders,
    )
    defaults["wallpaper_folder_depths"] = _normalize_wallpaper_folder_depths(
        defaults.get("wallpaper_folder_depths"),
        wallpaper_folders,
    )
    defaults["wallpaper_subdir_weights"] = _normalize_wallpaper_weights(
        defaults.get("wallpaper_subdir_weights")
    )
    defaults["wallpaper_random_mode"] = _normalize_wallpaper_random_mode(
        defaults.get("wallpaper_random_mode")
    )
    try:
        image_seconds = int(defaults.get("wallpaper_image_seconds") or WALLPAPER_IMAGE_SECONDS)
    except (TypeError, ValueError):
        image_seconds = WALLPAPER_IMAGE_SECONDS
    defaults["wallpaper_image_seconds"] = max(2, min(300, image_seconds))
    defaults["wallpaper_audio"] = bool(defaults.get("wallpaper_audio", True))
    mpv_path = defaults.get("mpv_path")
    defaults["mpv_path"] = str(mpv_path).strip() if mpv_path else None
    # Opacity 0–100; migrate legacy alpha (0–1 float) → bg_opacity once
    if "bg_opacity" not in defaults or defaults.get("bg_opacity") is None:
        try:
            legacy = float(defaults.get("alpha") or WINDOW_ALPHA)
            defaults["bg_opacity"] = int(round(legacy * 100)) if legacy <= 1.0 else int(legacy)
        except Exception:
            defaults["bg_opacity"] = BG_OPACITY_DEFAULT
    defaults["bg_opacity"] = _clamp_opacity(
        defaults.get("bg_opacity"), BG_OPACITY_DEFAULT
    )
    defaults["text_opacity"] = _clamp_opacity(
        defaults.get("text_opacity"), TEXT_OPACITY_DEFAULT
    )
    if "provider_order" in file_data:
        order_arg: Any = file_data.get("provider_order")
    elif "providers" in file_data or "disabled_providers" in file_data:
        order_arg = _unique_provider_ids(
            list(file_data.get("providers") or [])
            + list(file_data.get("disabled_providers") or [])
        )
    else:
        order_arg = defaults.get("provider_order")
    order, enabled, disabled = resolve_provider_config(
        defaults.get("providers"),
        defaults.get("disabled_providers"),
        order_arg,
    )
    left_col, right_col = resolve_provider_columns(
        enabled,
        disabled,
        order,
        file_data.get("provider_columns"),
    )
    defaults["provider_order"] = order
    defaults["providers"] = enabled
    defaults["disabled_providers"] = disabled
    defaults["provider_columns"] = [left_col, right_col]
    shot_root = os.environ.get("USAGE_FLOAT_DOCS_SHOT_ROOT")
    if shot_root:
        root = Path(shot_root)
        defaults["wallpaper_folders"] = [str(root)]
        defaults["wallpaper_folder"] = str(root)
        defaults["wallpaper_disabled_folders"] = []
        defaults["display_mode"] = DISPLAY_MODE_FLOAT
        defaults["always_on_top"] = False
        defaults["double_ctrl_toggle"] = True
        defaults["shortcut_enabled"] = True
        defaults["shortcut_mode"] = SHORTCUT_MODE_REPEAT
        defaults["shortcut_key"] = SHORTCUT_KEY_DEFAULT
        defaults["shortcut_repeat_count"] = SHORTCUT_REPEAT_COUNT_DEFAULT
        defaults["shortcut_combo"] = list(SHORTCUT_COMBO_DEFAULT)
        weights: dict[str, int] = {}
        ordered = [root, *(sorted(root.iterdir()) if root.exists() else [])]
        values = (100, 100, 100, 10)
        index = 0
        for path in ordered:
            if not path.is_dir():
                continue
            weights[_wallpaper_path_key(path)] = values[min(index, len(values) - 1)]
            index += 1
        defaults["wallpaper_subdir_weights"] = weights
    elif _docs_shot_active():
        demo_root = r"D:\Wallpapers\△"
        demo_long = r"D:\Media\PlayStation\Library\Archive\2026\Collections\□"
        defaults["wallpaper_folders"] = [demo_root, demo_long]
        defaults["wallpaper_folder"] = demo_root
        defaults["wallpaper_disabled_folders"] = [demo_long]
        defaults["wallpaper_folder_depths"] = {
            _wallpaper_path_key(demo_root): 1,
            _wallpaper_path_key(demo_long): 2,
        }
        defaults["display_mode"] = DISPLAY_MODE_PANEL
        defaults["always_on_top"] = False
        defaults["double_ctrl_toggle"] = True
        defaults["shortcut_enabled"] = True
        defaults["shortcut_mode"] = SHORTCUT_MODE_REPEAT
        defaults["shortcut_key"] = SHORTCUT_KEY_DEFAULT
        defaults["shortcut_repeat_count"] = SHORTCUT_REPEAT_COUNT_DEFAULT
        defaults["shortcut_combo"] = list(SHORTCUT_COMBO_DEFAULT)
        defaults["wallpaper_subdir_weights"] = {
            _wallpaper_path_key(demo_root): 100,
            _wallpaper_path_key(demo_root + r"\□"): 100,
            _wallpaper_path_key(demo_root + r"\○"): 100,
            _wallpaper_path_key(demo_root + r"\✕"): 10,
        }
    defaults["language"] = _lang(defaults)
    _ACTIVE_LANGUAGE = defaults["language"]
    return defaults


def _docs_shot_active() -> bool:
    return bool(os.environ.get("USAGE_FLOAT_DOCS_SHOT"))


def _capture_widget_png(widget: Any, path: Path) -> None:
    """PrintWindow a Tk toplevel to PNG. Used only for docs screenshots."""
    import ctypes
    from ctypes import wintypes

    widget.update_idletasks()
    widget.deiconify()
    widget.lift()
    widget.update()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hwnd = user32.FindWindowW(None, str(widget.title()))
    if not hwnd:
        hwnd = int(widget.winfo_id())
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = max(1, int(rect.right - rect.left))
    height = max(1, int(rect.bottom - rect.top))
    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    gdi32.SelectObject(mem_dc, bmp)
    user32.PrintWindow(hwnd, mem_dc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(bmi), 0)
    from PIL import Image

    img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
    try:
        dwmapi = ctypes.windll.dwmapi
        ext_rect = wintypes.RECT()
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        if (
            dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
                ctypes.byref(ext_rect),
                ctypes.sizeof(ext_rect),
            )
            == 0
        ):
            c_left = max(0, int(ext_rect.left - rect.left))
            c_top = max(0, int(ext_rect.top - rect.top))
            c_right = min(width, int(ext_rect.right - rect.left))
            c_bottom = min(height, int(ext_rect.bottom - rect.top))
            if c_right > c_left and c_bottom > c_top:
                img = img.crop((c_left, c_top, c_right, c_bottom))
    except Exception:
        pass
    img.save(path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)


def save_config(cfg: dict[str, Any]) -> None:
    global _ACTIVE_LANGUAGE
    cfg["language"] = _lang(cfg)
    _ACTIVE_LANGUAGE = cfg["language"]
    if _docs_shot_active():
        return
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def _set_language(cfg: dict[str, Any], state: dict[str, Any], language: str) -> None:
    """Persist a UI selection without changing any other settings."""
    cfg["language"] = state["language"] = _lang(language)
    save_config(cfg)


def load_usage_cache(*, strict: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        raise ValueError("Invalid usage cache")
    except FileNotFoundError:
        pass
    except Exception:
        if strict:
            raise
        pass
    return {"providers": {}, "claude_backoff_until": None}


def save_usage_cache(cache: dict[str, Any]) -> None:
    """Atomic publication; read/modify/write callers hold the cache lock."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_name(f"{CACHE_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(cache, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(CACHE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def update_usage_cache(update: Callable[[dict[str, Any]], None]) -> None:
    with file_lock(CACHE_PATH.with_suffix('.lock')):
        # A transient read failure must not turn a read/modify/write into an
        # empty-cache publication that destroys all display history.
        cache = load_usage_cache(strict=True)
        update(cache)
        save_usage_cache(cache)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(t)
    except Exception:
        return None


def format_remaining(resets_at: str | None, now: datetime | None = None) -> str:
    dt = parse_iso(resets_at)
    if not dt:
        return ""
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((dt - now).total_seconds())
    if seconds <= 0:
        return "soon"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def format_relative(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return _tr("尚未更新", 'Not updated yet')
    now = now or time.time()
    sec = int(max(0, now - ts))
    if sec < 15:
        return _tr("刚刚", 'just now')
    if sec < 60:
        return _tr(f"{sec} 秒前", f'{sec}s ago')
    if sec < 3600:
        return _tr(f"{sec // 60} 分钟前", f'{sec // 60}m ago')
    if sec < 86400:
        return _tr(f"{sec // 3600} 小时前", f'{sec // 3600}h ago')
    return _tr(f"{sec // 86400} 天前", f'{sec // 86400}d ago')


def _format_refresh_control(
    last_ok_at: float | None,
    refreshing: bool,
    now: float | None = None,
    *,
    compact: bool = False,
) -> str:
    relative = _tr("更新中…", 'Refreshing…') if refreshing else format_relative(last_ok_at, now)
    if _lang(_ACTIVE_LANGUAGE) == "en":
        if refreshing:
            return "Refreshing…"
        return f"Updated {relative}  ↻" if compact else f"Updated {relative} · Click to refresh"
    return f"{relative}  ↻"


def _point_in_box(
    x: int,
    y: int,
    box: tuple[int, int, int, int] | None,
) -> bool:
    if box is None:
        return False
    left, top, right, bottom = box
    return left <= x < right and top <= y < bottom


def _compact_row_height(linespace: int, font_size: int) -> int:
    """Keep a small safety margin without turning each line into a loose row."""
    return max(linespace + 2, font_size + 9)


def _effective_card_height(
    configured_height: int | None,
    allow_resize: bool,
) -> int | None:
    # A fixed height only has meaning while manual resizing is enabled.
    return configured_height if allow_resize else None


def tone_color(pct: float) -> str:
    if pct >= DANGER_PCT:
        return RED
    if pct >= WARN_PCT:
        return YELLOW
    return GREEN


def panel_tone_color(pct: float) -> str:
    if pct >= DANGER_PCT:
        return PANEL_RED
    if pct >= WARN_PCT:
        return PANEL_YELLOW
    return PANEL_GREEN


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class UsageWindow:
    id: str
    label: str
    used_pct: float | None
    resets_at: str | None = None


@dataclass
class ProviderUsage:
    provider_id: str
    display_name: str
    plan_label: str | None = None
    windows: list[UsageWindow] = field(default_factory=list)
    error: str | None = None
    available: bool = True
    stale: bool = False
    fetched_at: str | None = None
    # Free-form one-line summary override (e.g. grok credits without %)
    summary: str | None = None
    # Raw provider-specific reset entitlement block (Claude's cedar_ember).
    # Kept as the untouched payload so a field added upstream survives the
    # cache round-trip without a schema change here.
    reset_block: dict[str, Any] | None = None
    # Local provenance, not an account identifier or a credential. Rotation
    # invalidates reuse too: only the exact credential may reuse this snapshot.
    credential_key: str | None = None
    backend_kind: str | None = None
    billing: str | None = None
    home: str | None = None
    # Sanitized native quota provenance/units, never credentials or account IDs.
    usage_details: dict[str, Any] | None = None


def claude_magpie_source(provider_id: str):
    if not is_claude_provider(provider_id):
        return None
    home = claude_account_home(provider_id)
    extra_env = claude_account_paseo_env(provider_id) if home.parent == HOME else None
    source = magpie_usage.read_source(home, extra_env=extra_env)
    if source is not None and not source.explicit:
        non_magpie = (load_usage_cache().get("non_magpie_gateways") or {}).get(provider_id)
        if non_magpie and non_magpie == source.credential_key:
            return None
    return source


def claude_backend_info(provider_id: str = "claude") -> dict[str, str | None]:
    """Identify whether a Claude account uses a subscription, gateway, or API key."""
    if not is_claude_provider(provider_id):
        return {"backend_kind": None, "billing": None, "home": None}
    home = claude_account_home(provider_id)
    extra_env = claude_account_paseo_env(provider_id) if home.parent == HOME else {}
    try:
        settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        env = dict(extra_env, **(settings.get("env") or {})) if isinstance(settings, dict) else dict(extra_env)
    except Exception:
        env = dict(extra_env)
    api_key = str(env.get("ANTHROPIC_API_KEY") or "").strip()
    auth_token = str(env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    base_url = str(env.get("ANTHROPIC_BASE_URL") or "").strip()
    remote = claude_magpie_source(provider_id)
    if remote is not None and remote.valid:
        cached_row = (load_usage_cache().get("providers") or {}).get(provider_id) or {}
        if (
            remote.explicit
            or (auth_token and not api_key)
            or (cached_row.get("credential_key") == remote.credential_key and cached_row.get("backend_kind") == "gateway")
        ):
            return {"backend_kind": "gateway", "billing": "subscription", "home": str(home)}
    if api_key or (base_url and auth_token):
        kind = "gateway" if base_url and "api.anthropic.com" not in base_url.lower() else "api"
        return {"backend_kind": kind, "billing": "metered", "home": str(home)}
    oauth, _ = read_claude_oauth(home)
    if oauth:
        return {"backend_kind": "subscription", "billing": "subscription", "home": str(home)}
    return {"backend_kind": None, "billing": None, "home": str(home)}


def codex_backend_info(provider_id: str = "codex") -> dict[str, str | None]:
    account = discover_codex_accounts().get(provider_id)
    if account is None:
        return {"backend_kind": None, "billing": None, "home": None}
    auth, _ = read_codex_auth(account.home)
    if auth:
        return {"backend_kind": "subscription", "billing": "subscription", "home": str(account.home)}
    return {"backend_kind": None, "billing": None, "home": str(account.home)}


def provider_credential_key(provider_id: str) -> str | None:
    remote = claude_magpie_source(provider_id)
    if remote is not None:
        return remote.credential_key
    if is_claude_provider(provider_id):
        home = claude_account_home(provider_id)
        oauth, _ = read_claude_oauth(home)
        token = (oauth or {}).get("accessToken")
    else:
        codex_accounts = discover_codex_accounts()
        if provider_id in codex_accounts:
            home = codex_accounts[provider_id].home
            auth, _ = read_codex_auth(home)
            tokens = (auth or {}).get("tokens") or {}
            token = str(tokens.get("access_token") or '') + ':' + str(tokens.get("account_id") or '')
            if not tokens.get("access_token"):
                token = None
        else:
            return None
    if not token:
        return None
    return hashlib.sha256((str(home.resolve()) + '\0' + token).encode()).hexdigest()


def _provider_to_dict(p: ProviderUsage) -> dict[str, Any]:
    out = {
        "provider_id": p.provider_id,
        "display_name": p.display_name,
        "plan_label": p.plan_label,
        "windows": [
            {
                "id": w.id,
                "label": w.label,
                "used_pct": w.used_pct,
                "resets_at": w.resets_at,
            }
            for w in p.windows
        ],
        "error": p.error,
        "available": p.available,
        "stale": p.stale,
        "fetched_at": p.fetched_at,
        "summary": p.summary,
        "reset_block": p.reset_block,
        "credential_key": p.credential_key,
        "usage_details": p.usage_details,
    }
    if p.backend_kind:
        out["backend_kind"] = p.backend_kind
    if p.billing:
        out["billing"] = p.billing
    if p.home:
        out["home"] = p.home
    return out


def _provider_from_dict(d: dict[str, Any]) -> ProviderUsage:
    windows = [
        UsageWindow(
            id=str(w.get("id") or ""),
            label=str(w.get("label") or ""),
            used_pct=float(w["used_pct"]) if w.get("used_pct") is not None else None,
            resets_at=w.get("resets_at"),
        )
        for w in (d.get("windows") or [])
        if isinstance(w, dict)
    ]
    return ProviderUsage(
        provider_id=str(d.get("provider_id") or ""),
        display_name=str(d.get("display_name") or "?"),
        plan_label=d.get("plan_label"),
        windows=windows,
        error=d.get("error"),
        available=bool(d.get("available", True)),
        stale=bool(d.get("stale", False)),
        fetched_at=d.get("fetched_at"),
        summary=d.get("summary"),
        reset_block=d.get("reset_block") if isinstance(d.get("reset_block"), dict) else None,
        credential_key=d.get("credential_key"),
        backend_kind=d.get("backend_kind"),
        billing=d.get("billing"),
        home=d.get("home"),
        usage_details=d.get("usage_details") if isinstance(d.get("usage_details"), dict) else None,
    )


def cache_get_provider(provider_id: str) -> ProviderUsage | None:
    raw = (load_usage_cache().get("providers") or {}).get(provider_id)
    if not isinstance(raw, dict):
        return None
    try:
        p = _provider_from_dict(raw)
    except (TypeError, ValueError):
        return None
    if p.provider_id != provider_id or p.credential_key != provider_credential_key(provider_id):
        return None
    if not p.windows and not p.summary:
        return None
    p.stale = True
    return p


def cache_put_provider(p: ProviderUsage) -> None:
    if not p.available or p.error or not p.fetched_at:
        return
    if not p.windows and not p.summary:
        return
    key = provider_credential_key(p.provider_id)
    if p.credential_key is not None and p.credential_key != key:
        return
    p.credential_key = key
    snap = _provider_to_dict(p)
    def update(cache):
        providers = cache.setdefault("providers", {})
        old = providers.get(p.provider_id) or {}
        if old.get('credential_key') == key:
            new_at, old_at = _iso_epoch(p.fetched_at, 0), _iso_epoch(old.get('fetched_at'), 0)
            # A stale observation (e.g. an old gateway readAt) is kept, still
            # flagged stale, only when it is strictly newer than what we hold.
            if old_at > new_at or (p.stale and old_at >= new_at):
                return
        providers[p.provider_id] = snap
        cache.setdefault('display_snapshots', {})[p.provider_id] = snap
    update_usage_cache(update)


def display_snapshot(provider_id: str) -> ProviderUsage | None:
    """Presentation history is never promoted into the router's quota cache."""
    raw = (load_usage_cache().get('display_snapshots') or {}).get(provider_id)
    if not isinstance(raw, dict) or raw.get('credential_key') != provider_credential_key(provider_id):
        return None
    try:
        item = _provider_from_dict(raw)
    except (TypeError, ValueError):
        return None
    if item.provider_id != provider_id or not (item.windows or item.summary):
        return None
    item.stale = True
    return item


def mark_provider_stale(provider_id: str, error: str) -> None:
    def update(cache):
        row = (cache.get('providers') or {}).get(provider_id)
        if isinstance(row, dict):
            if row.get('credential_key') != provider_credential_key(provider_id):
                cache['providers'].pop(provider_id, None)
                return
            row['stale'] = True
            row['error'] = error
    update_usage_cache(update)


def claude_in_backoff(provider_id: str = "claude") -> bool:
    cache = load_usage_cache()
    if cache.get(f'{provider_id}_backoff_credential_key') != provider_credential_key(provider_id):
        return False
    until = cache.get(f"{provider_id}_backoff_until")
    dt = parse_iso(str(until) if until else None)
    if not dt:
        return False
    return datetime.now(timezone.utc) < dt


def set_claude_backoff(seconds: int = CLAUDE_429_BACKOFF_SECONDS, *, provider_id: str = "claude") -> None:
    until = datetime.now(timezone.utc).timestamp() + seconds
    update_usage_cache(lambda cache: cache.update({
        f"{provider_id}_backoff_until": datetime.fromtimestamp(until, tz=timezone.utc).isoformat(),
        f'{provider_id}_backoff_credential_key': provider_credential_key(provider_id)
    }))


def clear_claude_backoff(provider_id: str = "claude") -> None:
    update_usage_cache(lambda cache: cache.update({f"{provider_id}_backoff_until": None}))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _parse_json_body(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def http_json_response(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any, dict[str, str]]:
    data = None
    req_headers = {"Accept": "application/json", "User-Agent": "usage-float/1.2"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return (
                resp.status,
                _parse_json_body(raw),
                {str(k).lower(): str(v) for k, v in resp.headers.items()},
            )
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return (
            e.code,
            _parse_json_body(raw),
            {str(k).lower(): str(v) for k, v in e.headers.items()},
        )
    except Exception as e:
        return 0, str(e), {}


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    status, payload, _headers = http_json_response(
        url, method=method, headers=headers, body=body, timeout=timeout
    )
    return status, payload


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def read_claude_oauth(home: Path | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    cred_path = (home if home is not None else CLAUDE_HOME) / ".credentials.json"
    if not cred_path.exists():
        return None, None
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        if oauth.get("accessToken"):
            return oauth, cred_path
    except Exception:
        return None, None
    return None, None


def save_claude_oauth(cred_path: Path, oauth: dict[str, Any]) -> None:
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        data["claudeAiOauth"] = oauth
        cred_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def refresh_claude_token(refresh_token: str) -> dict[str, Any] | None:
    status, payload = http_json(
        CLAUDE_TOKEN_URL,
        method="POST",
        body={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_CLIENT_ID,
            "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers",
        },
    )
    if status != 200 or not isinstance(payload, dict):
        return None
    return payload


def claude_cli_version() -> str:
    """Version of the Claude Code CLI installed on this machine.

    The weekly-reset block is gated on the caller looking like a recent CLI, so
    a stale number would silently turn the field off. Read it fresh on every
    call — the CLI self-updates under a long-running HUD, and re-reading a 1 KB
    manifest once per refresh is cheaper than serving a version that has since
    dropped below the gate.
    """
    for path in CLAUDE_CLI_PACKAGE_PATHS:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            found = str((data or {}).get("version") or "").strip()
            if found:
                return found
        except Exception:
            continue
    return CLAUDE_CLI_VERSION_FALLBACK


def claude_request_headers(token: str) -> dict[str, str]:
    version = claude_cli_version()
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": CLAUDE_OAUTH_BETA,
        # Identify as the CLI surface; without this the response reports
        # ineligible_reason="surface" and the reset block is always empty.
        "User-Agent": f"claude-cli/{version} (external, cli)",
        "anthropic-client-platform": "cli",
        "anthropic-client-version": version,
    }


@dataclass
class ClaudeResetGrant:
    """One /limit-reset grant; the claim call names it by id."""

    grant_id: str
    resets_left: int = 0
    expires_at: str | None = None
    clears: tuple[str, ...] = ()
    usable_now: bool = False
    use_requires_limit: bool = False
    paused: bool = False


@dataclass
class ClaudeResetStatus:
    """Claude's weekly /limit-reset entitlement, as shown by the HUD."""

    eligible: bool = False
    ineligible_reason: str | None = None
    at_limit: bool = False
    resets_left: int = 0
    usable_now: bool = False
    use_requires_limit: bool = False
    expires_at: str | None = None
    weekly_resets_at: str | None = None
    cooldown_until: str | None = None
    # Describes the grant that lapses first: Anthropic ships a human label
    # ("Claude Opus 5.5 launch: one usage-limit reset…") and the list of
    # windows it refills.
    label: str | None = None
    clears: tuple[str, ...] = ()
    # Unspent grants, soonest expiry first. The server only redeems the one
    # named by next_grant_id and answers "not_next_grant" for any other.
    grants: tuple[ClaudeResetGrant, ...] = ()
    next_grant_id: str | None = None

    @property
    def available(self) -> bool:
        return self.resets_left > 0

    def claimable(self, grant: ClaudeResetGrant) -> bool:
        return (
            grant.grant_id == self.next_grant_id
            and grant.usable_now
            and not grant.paused
            and grant.resets_left > 0
        )


CLAUDE_RESET_WINDOW_TEXT = {
    "five_hour": "5 小时会话",
    "seven_day": "7 天周额度",
    "seven_day_overage_included": "7 天周额度（含超额）",
    "seven_day_opus": "Opus 周额度",
    "seven_day_sonnet": "Sonnet 周额度",
}


CLAUDE_RESET_REASON_TEXT = {
    "surface": "当前客户端不被允许读取重置额度",
    "cli_version": "Claude Code 版本过低，升级后才会发放",
    "plan": "当前订阅方案不包含用量重置",
    "no_profile_scope": "登录凭证缺少所需权限",
    "not_authorized": "账号未获授权",
}


def _claude_grant_clears(grant: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(window)
        for window in (_first_present(grant, "clears", "limit_types", "limitTypes") or [])
        if window
    )


def _claude_grant_int(grant: dict[str, Any], *names: str) -> int:
    value = _first_present(grant, *names)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_claude_reset_status(block: Any) -> ClaudeResetStatus:
    """Read the cedar_ember block into the few fields the HUD actually shows."""
    if not isinstance(block, dict):
        return ClaudeResetStatus()
    reason = _first_present(block, "ineligible_reason", "ineligibleReason")
    status = ClaudeResetStatus(
        eligible=bool(_first_present(block, "eligible") or False),
        ineligible_reason=str(reason) if reason else None,
        at_limit=bool(_first_present(block, "at_limit", "atLimit") or False),
        weekly_resets_at=_iso_timestamp(
            _first_present(block, "weekly_resets_at", "weeklyResetsAt")
        ),
        cooldown_until=_iso_timestamp(
            _first_present(block, "cooldown_until", "cooldownUntil")
        ),
    )

    next_id = _first_present(block, "next_grant_id", "nextGrantId")
    status.next_grant_id = str(next_id) if next_id else None

    total = 0
    soonest_expiry: str | None = None
    grants: list[ClaudeResetGrant] = []
    for grant in _first_present(block, "grants") or []:
        if not isinstance(grant, dict):
            continue
        left = _claude_grant_int(grant, "resets_left", "resetsLeft")
        if left <= 0 or _first_present(grant, "paused"):
            continue
        total += left
        grant_id = _first_present(grant, "id", "grant_id", "grantId")
        if grant_id:
            grants.append(
                ClaudeResetGrant(
                    grant_id=str(grant_id),
                    resets_left=left,
                    expires_at=_iso_timestamp(_first_present(grant, "ends_at", "endsAt")),
                    clears=_claude_grant_clears(grant),
                    usable_now=bool(_first_present(grant, "usable_now", "usableNow")),
                    use_requires_limit=bool(
                        _first_present(grant, "use_requires_limit", "useRequiresLimit")
                    ),
                )
            )
        if _first_present(grant, "usable_now", "usableNow"):
            status.usable_now = True
        if _first_present(grant, "use_requires_limit", "useRequiresLimit"):
            status.use_requires_limit = True
        ends_at = _iso_timestamp(_first_present(grant, "ends_at", "endsAt"))
        # Describe the deadline that lapses first — that is the one worth acting on.
        if ends_at is None and soonest_expiry is not None:
            continue
        if ends_at is not None and soonest_expiry is not None and _iso_epoch(
            ends_at, float("inf")
        ) >= _iso_epoch(soonest_expiry, float("inf")):
            continue
        soonest_expiry = ends_at or soonest_expiry
        label = _first_present(grant, "label", "title", "name")
        status.label = str(label).strip() if label else None
        status.clears = _claude_grant_clears(grant)

    # Some responses only carry the rolled-up count, with no per-grant rows.
    status.resets_left = total or _claude_grant_int(
        block, "resets_left_total", "resetsLeftTotal", "resets_left"
    )
    status.expires_at = soonest_expiry
    status.grants = tuple(
        sorted(
            grants,
            key=lambda g: (_iso_epoch(g.expires_at, float("inf")), g.grant_id),
        )
    )
    return status


def claude_reset_status(provider: ProviderUsage | None) -> ClaudeResetStatus:
    if provider is None:
        return ClaudeResetStatus()
    return parse_claude_reset_status(provider.reset_block)


def claude_authorized_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
    home: Path | None = None,
) -> tuple[int, Any]:
    """Use CLI-owned credentials read-only; re-read once after a GET auth failure.

    Returns status 0 when there are no local credentials at all.
    """
    oauth, cred_path = read_claude_oauth(home) if home is not None else read_claude_oauth()
    if not oauth:
        return 0, None

    def call(token: str) -> tuple[int, Any]:
        return http_json(
            url,
            method=method,
            headers=claude_request_headers(token),
            body=body,
            timeout=timeout,
        )

    status, payload = call(oauth["accessToken"])
    if status not in (401, 403):
        return status, payload
    # Both accounts belong to Claude Code. Never compete for rotating refresh
    # tokens, and never replay a reset claim under a newly signed-in account.
    if method.upper() == 'GET':
        latest, _ = read_claude_oauth(home) if home is not None else read_claude_oauth()
        if latest and latest.get("accessToken") != oauth["accessToken"]:
            return call(latest["accessToken"])
    return status, payload


def claude_organization_uuid(provider_id: str = "claude") -> str | None:
    """The OAuth organization the claim is filed under.

    Claude Code records it in .claude.json at login; the profile endpoint is
    the fallback when that file is somewhere unexpected.
    """
    is_primary = provider_id == "claude"
    home = None if is_primary else claude_account_home(provider_id)
    paths = (CLAUDE_HOME / ".claude.json", HOME / ".claude.json") if is_primary else (home / ".claude.json",)
    for path in paths:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            found = str(((data or {}).get("oauthAccount") or {}).get("organizationUuid") or "")
            if found.strip():
                return found.strip()
        except Exception:
            continue
    status, payload = (
        claude_authorized_json(CLAUDE_PROFILE_URL, home=home)
        if home is not None
        else claude_authorized_json(CLAUDE_PROFILE_URL)
    )
    if status == 200 and isinstance(payload, dict):
        found = str((payload.get("organization") or {}).get("uuid") or "").strip()
        if found:
            return found
    return None


@dataclass
class ClaudeResetClaim:
    ok: bool
    message: str
    # False when the outcome is unknown (network drop, 5xx): the next attempt
    # must resend the same request_id so the server can dedupe it instead of
    # spending a second reset.
    settled: bool = True


CLAUDE_RESET_RESULT_TEXT = {
    "already_used": "这次重置已经用过了。",
    "not_limited": "还没到用量上限，暂时不能使用，额度未消耗。",
    "cooldown": "刚用过一次重置，冷却中，稍后再试。",
    "ineligible": "当前账号不符合使用条件。",
    "unavailable": "重置暂不可用，稍后再试。",
}


def claim_claude_reset(
    grant_id: str, *, request_id: str, provider_id: str = "claude"
) -> ClaudeResetClaim:
    """Spend one /limit-reset grant. Only call this after the user confirms."""
    if not CLAUDE_RESET_GRANT_ID_RE.match(grant_id or "") or not (
        CLAUDE_RESET_REQUEST_ID_RE.match(request_id or "")
    ):
        return ClaudeResetClaim(False, _tr("重置数据格式异常，未发送请求。", 'Invalid reset data. No request was sent.'))
    home = None if provider_id == "claude" else claude_account_home(provider_id)
    org = claude_organization_uuid() if provider_id == "claude" else claude_organization_uuid(provider_id)
    if not org:
        return ClaudeResetClaim(False, _tr("找不到 Claude 组织信息，请在 Claude Code 里重新登录。", 'Claude organization not found. Please sign in again in Claude Code.'))
    url = CLAUDE_RESET_CLAIM_URL.format(org=urllib.parse.quote(org, safe=""))
    body = {
        "program": CLAUDE_RESET_PROGRAM,
        "grant_id": grant_id,
        "request_id": request_id,
    }
    status, payload = (
        claude_authorized_json(url, method="POST", body=body, timeout=25.0, home=home)
        if home is not None
        else claude_authorized_json(url, method="POST", body=body, timeout=25.0)
    )
    if status == 0 and payload is None:
        return ClaudeResetClaim(False, _tr("未找到可用的 Claude 登录凭证", 'No usable Claude login credentials found'))
    if status == 200 and isinstance(payload, dict):
        result = str(payload.get("result") or "unavailable")
        if result == "reset":
            cleared = _tr("、", ", ").join(
                _ui(CLAUDE_RESET_WINDOW_TEXT.get(str(w), str(w)))
                for w in payload.get("cleared") or []
            )
            message = _tr(f"已重置{cleared}。", f'Reset {cleared}.') if cleared else _tr("用量已重置。", 'Usage reset.')
            left = payload.get("resets_left")
            if isinstance(left, int):
                message += _tr(f" 剩余 {left} 次。", f' {left} remaining.')
            return ClaudeResetClaim(True, message)
        return ClaudeResetClaim(
            False, _ui(CLAUDE_RESET_RESULT_TEXT.get(result, CLAUDE_RESET_RESULT_TEXT["unavailable"]))
        )
    if status == 429:
        return ClaudeResetClaim(False, _tr("请求太频繁，稍后再试。", 'Too many requests. Please try again later.'), settled=False)
    if status in (401, 403):
        return ClaudeResetClaim(False, _tr("Claude 登录已失效，请在 Claude Code 里重新登录。", 'Claude login expired. Please sign in again in Claude Code.'))
    return ClaudeResetClaim(
        False, _tr(f"使用失败：{codex_error_text(status, payload)}", f'Redemption failed: {codex_error_text(status, payload)}'), settled=False
    )


CLAUDE_SESSION_RESET_PROGRAM = "juniper_tide"


@dataclass
class ClaudeSessionReset:
    """Claude's weekly 5h session reset (juniper_tide).

    It is an experiment: only accounts in its "reset" arm get one, and the
    server only opens it once the 5h window is actually at its limit.
    """

    eligible: bool = False
    ineligible_reason: str | None = None
    in_experiment: bool = False
    arm: str | None = None
    available: bool = False
    next_available_at: str | None = None
    resets_per_week: int = 1

    @property
    def offered(self) -> bool:
        return self.arm == "reset"

    @property
    def claimable(self) -> bool:
        return self.eligible and self.offered and self.available


CLAUDE_SESSION_REASON_TEXT = {
    "tier": "当前订阅方案不包含 5 小时重置",
    "tenure": "账号注册时间还不够",
    "surface": "当前客户端不被允许使用",
    "mobile": "移动端不可用",
    "cli_version": "Claude Code 版本过低",
    "not_at_wall": "到达 5 小时上限后才会开放",
    "weekly_limit": "周额度已用完，5 小时重置无法使用",
    "no_weekly_limit": "账号没有周额度限制，不需要重置",
    "other_experiment": "账号在其他实验中",
    "extra_usage": "已开启超额用量，不需要重置",
}


def parse_claude_session_reset(block: Any) -> ClaudeSessionReset:
    if not isinstance(block, dict):
        return ClaudeSessionReset()
    reason = _first_present(block, "ineligible_reason", "ineligibleReason")
    arm = _first_present(block, "arm")
    return ClaudeSessionReset(
        eligible=bool(_first_present(block, "eligible") or False),
        ineligible_reason=str(reason) if reason else None,
        in_experiment=bool(_first_present(block, "in_experiment", "inExperiment") or False),
        arm=str(arm) if arm else None,
        available=bool(_first_present(block, "available") or False),
        next_available_at=_iso_timestamp(
            _first_present(block, "next_available_at", "nextAvailableAt")
        ),
        resets_per_week=_claude_grant_int(block, "resets_per_week", "resetsPerWeek") or 1,
    )


def fetch_claude_session_reset(
    provider_id: str = "claude",
) -> tuple[ClaudeSessionReset | None, str | None]:
    """Read the 5h session reset. Only the 5h dialog calls this, on demand."""
    home = None if provider_id == "claude" else claude_account_home(provider_id)
    status, payload = (
        claude_authorized_json(CLAUDE_USAGE_AT_WALL_URL, home=home)
        if home is not None
        else claude_authorized_json(CLAUDE_USAGE_AT_WALL_URL)
    )
    if status == 0 and payload is None:
        return None, _tr("未找到可用的 Claude 登录凭证", 'No usable Claude login credentials found')
    if status == 429:
        return None, _tr("请求太频繁，稍后再试。", 'Too many requests. Please try again later.')
    if status != 200 or not isinstance(payload, dict):
        return None, _tr(f"读取失败：{codex_error_text(status, payload)}", f'Could not load: {codex_error_text(status, payload)}')
    return parse_claude_session_reset(payload.get("juniper_tide")), None


CLAUDE_SESSION_RESULT_TEXT = {
    "already_used": "本周的 5 小时重置已经用过了。",
    "not_limited": "还没到 5 小时上限，暂时不能使用，额度未消耗。",
    "ineligible": "当前账号不符合使用条件。",
    "unavailable": "重置暂不可用，稍后再试。",
}


def claim_claude_session_reset(provider_id: str = "claude") -> ClaudeResetClaim:
    """Spend this week's 5h session reset. Only call after the user confirms.

    The CLI sends no request id for this program; the weekly quota itself is
    what stops a retried click from spending twice ("already_used").
    """
    home = None if provider_id == "claude" else claude_account_home(provider_id)
    org = claude_organization_uuid() if provider_id == "claude" else claude_organization_uuid(provider_id)
    if not org:
        return ClaudeResetClaim(False, _tr("找不到 Claude 组织信息，请在 Claude Code 里重新登录。", 'Claude organization not found. Please sign in again in Claude Code.'))
    url = CLAUDE_RESET_CLAIM_URL.format(org=urllib.parse.quote(org, safe=""))
    body = {"program": CLAUDE_SESSION_RESET_PROGRAM}
    status, payload = (
        claude_authorized_json(url, method="POST", body=body, timeout=25.0, home=home)
        if home is not None
        else claude_authorized_json(url, method="POST", body=body, timeout=25.0)
    )
    if status == 0 and payload is None:
        return ClaudeResetClaim(False, _tr("未找到可用的 Claude 登录凭证", 'No usable Claude login credentials found'))
    if status == 200 and isinstance(payload, dict):
        result = str(payload.get("result") or "unavailable")
        if result == "reset":
            return ClaudeResetClaim(True, _tr("5 小时会话已重置。", '5-hour session reset.'))
        return ClaudeResetClaim(
            False,
            _ui(CLAUDE_SESSION_RESULT_TEXT.get(result, CLAUDE_SESSION_RESULT_TEXT["unavailable"])),
        )
    if status == 429:
        return ClaudeResetClaim(False, _tr("请求太频繁，稍后再试。", 'Too many requests. Please try again later.'), settled=False)
    if status in (401, 403):
        return ClaudeResetClaim(False, _tr("Claude 登录已失效，请在 Claude Code 里重新登录。", 'Claude login expired. Please sign in again in Claude Code.'))
    return ClaudeResetClaim(
        False, _tr(f"使用失败：{codex_error_text(status, payload)}", f'Redemption failed: {codex_error_text(status, payload)}'), settled=False
    )


def display_fallback(
    provider_id: str, error: str, previous: Iterable[ProviderUsage] = (),
) -> ProviderUsage:
    """Recover presentation history without writing it into the quota cache."""
    candidates = [p for p in previous if isinstance(p, ProviderUsage)
                  and p.provider_id == provider_id and (p.windows or p.summary)]
    try:
        key = provider_credential_key(provider_id)
        # Do not attribute a known previous login's quota to a new login.
        if key is not None:
            candidates = [p for p in candidates if p.credential_key in (None, key)]
    except Exception:
        pass  # Discovery/read failures must not erase the UI's last reading.
    for reader in (cache_get_provider, display_snapshot):
        try:
            item = reader(provider_id)
            if item is not None:
                candidates.append(item)
        except Exception:
            continue
    if candidates:
        cached = max(candidates, key=lambda p: _iso_epoch(p.fetched_at, 0))
        # Copy before marking stale: the UI may still be rendering this object.
        cached = _provider_from_dict(_provider_to_dict(cached))
        cached.stale = True
        cached.error = error
        return cached
    return ProviderUsage(provider_id=provider_id, display_name=provider_id,
                         available=False, error=error, stale=True)


def stale_provider_usage(
    provider_id: str, error: str, previous: Iterable[ProviderUsage] = (),
) -> ProviderUsage:
    cached = display_fallback(provider_id, error, previous)
    try:
        mark_provider_stale(provider_id, error)
    except Exception:
        pass  # Cache contention must not defeat presentation fallback.
    return cached


def _claude_rate_limited_usage(provider_id: str = "claude") -> ProviderUsage:
    return stale_provider_usage(provider_id, _tr("限流退避中", 'Rate limit cooldown'))


def fetch_magpie_claude_usage(source, provider_id: str, *, probe_only: bool = False) -> ProviderUsage | None:
    if not source.valid:
        return None if probe_only else stale_provider_usage(provider_id, _tr("Magpie HTTPS 用量来源配置不完整", 'Magpie HTTPS usage source is not fully configured'))
    status, payload = http_json(source.url + "/v1/magpie/quotas",
                               headers={"Authorization": "Bearer " + source.token})
    if status != 200:
        if probe_only and status in (0, 400, 404, 405, 501):
            update_usage_cache(lambda c: c.setdefault("non_magpie_gateways", {})
                               .update({provider_id: source.credential_key}))
            return None
        return stale_provider_usage(provider_id, _tr(f"VPS 用量读取失败（HTTP {status}）", f'VPS usage request failed (HTTP {status})'))
    if source.credential_key != provider_credential_key(provider_id):
        return stale_provider_usage(provider_id, _tr("用量来源已变化，等待下次更新", 'Usage source changed; waiting for the next update'))
    try:
        quota, windows = magpie_usage.parse_quota(payload, source)
    except ValueError as error:
        if probe_only and (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)):
            update_usage_cache(lambda c: c.setdefault("non_magpie_gateways", {})
                               .update({provider_id: source.credential_key}))
            return None
        return stale_provider_usage(provider_id, str(error))
    age = time.time() - _iso_epoch(quota["readAt"], 0)
    result = ProviderUsage(
        provider_id=provider_id, display_name=provider_id,
        plan_label=quota.get("plan"), windows=[UsageWindow(**w) for w in windows],
        available=True, fetched_at=quota["readAt"],
        stale=age > REFRESH_SECONDS * 2 or age < -60,
        credential_key=source.credential_key,
        backend_kind="gateway",
        billing="subscription",
        home=str(claude_account_home(provider_id)),
    )
    cache_put_provider(result)
    return result


def fetch_claude_usage(*, force: bool = False, provider_id: str = "claude") -> ProviderUsage:
    remote = claude_magpie_source(provider_id)
    if remote is not None:
        if remote.explicit:
            return fetch_magpie_claude_usage(remote, provider_id)
        probed = fetch_magpie_claude_usage(remote, provider_id, probe_only=True)
        if probed is not None:
            return probed
    home = None if provider_id == "claude" else claude_account_home(provider_id)
    if claude_in_backoff(provider_id):
        return _claude_rate_limited_usage(provider_id)

    oauth, _cred_path = read_claude_oauth(home) if home is not None else read_claude_oauth()
    if not oauth:
        return ProviderUsage(
            provider_id=provider_id,
            display_name=provider_id,
            available=False,
            error="更新失败",
        )

    source_key = provider_credential_key(provider_id)
    plan = oauth.get("subscriptionType")
    status, payload = (claude_authorized_json(CLAUDE_USAGE_RESET_URL, home=home)
                       if home is not None else claude_authorized_json(CLAUDE_USAGE_RESET_URL))

    if source_key != provider_credential_key(provider_id):
        return stale_provider_usage(provider_id, _tr('登录凭据已变化，等待下次更新', 'Login credentials changed; waiting for the next update'))
    if status == 429:
        set_claude_backoff(provider_id=provider_id)
        return _claude_rate_limited_usage(provider_id)

    if status != 200 or not isinstance(payload, dict):
        error = _tr("登录凭据需由 Claude Code 更新", 'Login credentials must be updated by Claude Code') if status in (401, 403) else "更新失败"
        return stale_provider_usage(provider_id, error)

    windows: list[UsageWindow] = []
    five = payload.get("five_hour") or {}
    # Rows read "fable 7d → claude 7d → claude 5h"; Settings decides which of
    # them show and in what order.
    #
    # Model-scoped weekly limits (e.g. Fable)
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope") or {}
        model = ((scope.get("model") or {}).get("display_name") or "").strip()
        if not model:
            continue
        # Prefer Fable; still show other scoped models if present
        pct = entry.get("percent")
        if pct is None:
            continue
        try:
            pct_f = float(pct)
        except Exception:
            continue
        model_key = model.lower().replace(" ", "")
        windows.append(
            UsageWindow(
                id=f"weekly_{model_key}",
                label=f"{model_key} 7d",
                used_pct=pct_f,
                resets_at=entry.get("resets_at"),
            )
        )

    week = payload.get("seven_day") or {}
    if isinstance(week, dict) and week.get("utilization") is not None:
        windows.append(
            UsageWindow(
                id="seven_day",
                label="7d",
                used_pct=float(week["utilization"]),
                resets_at=week.get("resets_at"),
            )
        )

    if isinstance(five, dict) and five.get("utilization") is not None:
        windows.append(
            UsageWindow(
                id="five_hour",
                label="5h",
                used_pct=float(five["utilization"]),
                resets_at=five.get("resets_at"),
            )
        )

    if not windows or any(w.used_pct is None or not math.isfinite(w.used_pct) for w in windows):
        return stale_provider_usage(provider_id, _tr('用量响应缺少有效窗口', 'Usage response has no valid windows'))
    reset_block = payload.get("cedar_ember")
    result = ProviderUsage(
        provider_id=provider_id,
        display_name=provider_id,
        plan_label=str(plan) if plan else None,
        windows=windows,
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        reset_block=reset_block if isinstance(reset_block, dict) else None,
        credential_key=source_key,
        backend_kind="subscription",
        billing="subscription",
        home=str(claude_account_home(provider_id)),
    )
    clear_claude_backoff(provider_id)
    cache_put_provider(result)
    return result


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def read_codex_auth(home: Path = CODEX_HOME) -> tuple[dict[str, Any] | None, Path | None]:
    auth_path = home / "auth.json"
    if not auth_path.exists():
        return None, None
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = (data.get("tokens") or {}) if isinstance(data, dict) else {}
        if tokens.get("access_token"):
            return data, auth_path
    except Exception:
        return None, None
    return None, None


def save_codex_auth(auth_path: Path, auth: dict[str, Any], refreshed: dict[str, Any]) -> None:
    try:
        tokens = auth.setdefault("tokens", {})
        tokens["access_token"] = refreshed.get("access_token") or tokens.get("access_token")
        if refreshed.get("refresh_token"):
            tokens["refresh_token"] = refreshed["refresh_token"]
        auth_path.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    except Exception:
        pass


def refresh_codex_token(refresh_token: str) -> dict[str, Any] | None:
    body = (
        f"grant_type=refresh_token"
        f"&refresh_token={urllib.parse.quote(refresh_token)}"
        f"&client_id={urllib.parse.quote(CODEX_CLIENT_ID)}"
    ).encode("utf-8")
    req = urllib.request.Request(
        CODEX_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "usage-float/1.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def codex_authorized_json(
    url: str,
    *,
    home: Path = CODEX_HOME,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """Call a ChatGPT backend endpoint with the local Codex OAuth token.

    Returns the same (status, payload) pair as http_json(); status 0 means the
    local credential is missing. Only the Codex CLI owns token rotation.
    """
    auth, auth_path = read_codex_auth(home)
    if not auth:
        return 0, None

    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not access:
        return 0, None

    def call(token: str, account: str | None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if account:
            headers["ChatGPT-Account-ID"] = str(account)
        return http_json(url, method=method, headers=headers, body=body)

    status, payload = call(access, account_id)
    if status in (401, 403) and method.upper() == 'GET':
        latest, _ = read_codex_auth(home)
        new = (latest or {}).get('tokens') or {}
        if new.get('access_token') and new['access_token'] != access:
            return call(new['access_token'], new.get('account_id'))
    return status, payload


def fetch_codex_usage(account: CodexAccount) -> ProviderUsage:
    source_key = provider_credential_key(account.provider_id)
    status, payload = codex_authorized_json(CODEX_USAGE_URL, home=account.home)
    if source_key != provider_credential_key(account.provider_id):
        return stale_provider_usage(account.provider_id, _tr('登录凭据已变化，等待下次更新', 'Login credentials changed; waiting for the next update'))
    if status != 200 or not isinstance(payload, dict):
        error = _tr("登录凭据需由 Codex 更新", 'Login credentials must be updated by Codex') if status in (401, 403) else "更新失败"
        return stale_provider_usage(account.provider_id, error)

    rate = payload.get("rate_limit") or {}
    # Preserve both constraints for the router; primary is not always weekly.
    windows = []
    for name, default_seconds in (("primary_window", 18000), ("secondary_window", 604800)):
        window = rate.get(name) or {}
        used = window.get('used_percent')
        if used is None:
            continue
        seconds = window.get('limit_window_seconds') or default_seconds
        wid = 'five_hour' if seconds == 18000 else 'usage' if seconds == 604800 else f'usage-{int(seconds / 60)}m'
        windows.append(UsageWindow(
            id=wid, label='5h' if seconds == 18000 else '7d' if seconds == 604800 else wid,
            used_pct=float(used), resets_at=_iso_timestamp(window.get('reset_at')),
        ))
    if not windows:
        return stale_provider_usage(account.provider_id, "更新失败")
    result = ProviderUsage(
        provider_id=account.provider_id,
        display_name=account.display_name,
        plan_label=str(payload.get("plan_type") or "") or None,
        windows=windows,
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        credential_key=source_key,
        backend_kind="subscription",
        billing="subscription",
        home=str(account.home),
    )
    cache_put_provider(result)
    return result


@dataclass
class CodexResetCard:
    """One banked rate-limit reset ("重置卡") on the signed-in Codex account."""

    card_id: str
    status: str = CODEX_RESET_CARD_STATUS_AVAILABLE
    reset_type: str | None = None
    granted_at: str | None = None
    expires_at: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == CODEX_RESET_CARD_STATUS_AVAILABLE

    @property
    def status_text(self) -> str:
        return _ui(CODEX_RESET_CARD_STATUS_TEXT.get(self.status, self.status))


def _first_present(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None:
            return value
    return None


def _iso_timestamp(value: Any) -> str | None:
    """Normalise a card timestamp to ISO-8601 so parse_iso() can read it.

    The endpoint has shipped both epoch seconds and RFC 3339 strings, so accept
    either rather than dropping a card because of its timestamp encoding.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if parse_iso(text) is not None:
        return text
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _codex_reset_card_from_dict(data: dict[str, Any]) -> CodexResetCard | None:
    raw_id = _first_present(data, "id", "credit_id", "creditId")
    card_id = str(raw_id).strip() if raw_id is not None else ""
    if not card_id:
        return None
    raw_status = _first_present(data, "status", "state")
    reset_type = _first_present(data, "reset_type", "resetType")
    return CodexResetCard(
        card_id=card_id,
        status=(
            str(raw_status).strip().lower()
            if raw_status is not None
            else CODEX_RESET_CARD_STATUS_AVAILABLE
        ),
        reset_type=str(reset_type) if reset_type else None,
        granted_at=_iso_timestamp(
            _first_present(data, "granted_at", "grantedAt")
        ),
        expires_at=_iso_timestamp(
            _first_present(data, "expires_at", "expiresAt")
        ),
    )


def _iso_epoch(ts: str | None, default: float) -> float:
    dt = parse_iso(ts)
    if dt is None:
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def sort_codex_reset_cards(cards: list[CodexResetCard]) -> list[CodexResetCard]:
    """Usable cards first, soonest expiry first — spend what would lapse next."""
    return sorted(
        cards,
        key=lambda card: (
            0 if card.usable else 1,
            _iso_epoch(card.expires_at, float("inf")),
            card.card_id,
        ),
    )


def codex_error_text(status: int, payload: Any) -> str:
    detail: str | None = None
    if isinstance(payload, dict):
        for key in ("detail", "message", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
        if detail is None:
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                detail = error.strip()
            elif isinstance(error, dict):
                value = error.get("message") or error.get("detail")
                if isinstance(value, str) and value.strip():
                    detail = value.strip()
    elif isinstance(payload, str) and payload.strip():
        detail = payload.strip()
    if detail:
        detail = detail[:160]
        return f"HTTP {status} · {detail}" if status else detail
    return f"HTTP {status}" if status else _tr("网络不可用", 'Network unavailable')


def fetch_codex_reset_cards(
    home: Path = CODEX_HOME,
) -> tuple[list[CodexResetCard], str | None]:
    """Return (cards, error). Cards include spent ones so counts stay honest."""
    status, payload = codex_authorized_json(CODEX_RESET_CARDS_URL, home=home)
    if status == 0:
        return [], _tr("未找到可用的 Codex 登录凭证", 'No usable Codex login credentials found')
    if status != 200 or not isinstance(payload, dict):
        return [], _tr(f"读取重置卡失败：{codex_error_text(status, payload)}", f'Could not load reset credits: {codex_error_text(status, payload)}')
    cards: list[CodexResetCard] = []
    for item in payload.get("credits") or []:
        if not isinstance(item, dict):
            continue
        card = _codex_reset_card_from_dict(item)
        if card is not None:
            cards.append(card)
    return sort_codex_reset_cards(cards), None


def consume_codex_reset_card(
    card_id: str,
    *,
    home: Path = CODEX_HOME,
    request_id: str | None = None,
) -> tuple[bool, str]:
    """Redeem one reset card. Returns (ok, message) for direct UI display."""
    card_id = str(card_id).strip()
    if not card_id:
        return False, _tr("没有选中任何重置卡", 'No reset credit selected')
    body = {
        "credit_id": card_id,
        # The backend dedupes on this id, so a lost response cannot burn a
        # second card when the click is retried.
        "redeem_request_id": request_id or uuid.uuid4().hex,
    }
    status, payload = codex_authorized_json(
        CODEX_RESET_CONSUME_URL,
        home=home,
        method="POST",
        body=body,
    )
    if status == 0:
        return False, _tr("未找到可用的 Codex 登录凭证", 'No usable Codex login credentials found')
    if status in (200, 201, 202, 204):
        return True, _tr("重置卡已使用，用量窗口已重置。", 'Reset redeemed. Usage windows have been reset.')
    return False, _tr(f"使用失败：{codex_error_text(status, payload)}", f'Redemption failed: {codex_error_text(status, payload)}')


# ---------------------------------------------------------------------------
# Grok
# ---------------------------------------------------------------------------


def extract_grok_token(auth: Any) -> str | None:
    """Prefer SuperGrok OIDC entry (https://auth.x.ai::…), then legacy session."""
    if auth is None or not isinstance(auth, dict):
        return None
    top = auth.get("access_token")
    if isinstance(top, str) and top:
        return top

    oidc = None
    legacy = None
    for scope, value in auth.items():
        if not isinstance(value, dict):
            continue
        key = value.get("key")
        if not isinstance(key, str) or not key:
            continue
        s = str(scope)
        if s.startswith("https://auth.x.ai::"):
            oidc = key
        elif s == "https://accounts.x.ai/sign-in" or "/sign-in" in s:
            legacy = key
    return oidc or legacy


def read_grok_token() -> str | None:
    env = os.environ.get("GROK_API_KEY") or os.environ.get("GROK_TOKEN")
    if env:
        return env
    path = GROK_HOME / "auth.json"
    if not path.exists():
        return None
    try:
        return extract_grok_token(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _read_varint(buf: bytes, index: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    i = index
    while i < len(buf) and shift < 64:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, i
        shift += 7
    return None, i


def _scan_protobuf(
    buf: bytes,
    depth: int,
    path: list[int],
    order: int,
    fixed32: list[tuple[list[int], float, int]],
    varints: list[tuple[list[int], int]],
) -> int:
    """Heuristic protobuf walker (ported from CC Switch / CodexBar)."""
    import struct as _struct

    i = 0
    next_order = order
    n = len(buf)
    while i < n:
        start = i
        key, i = _read_varint(buf, i)
        if key is None or key == 0:
            i = start + 1
            continue
        field_number = key >> 3
        wire_type = key & 0x07
        field_path = path + [field_number]

        if wire_type == 0:
            val, i = _read_varint(buf, i)
            if val is None:
                i = start + 1
                continue
            varints.append((field_path, val))
        elif wire_type == 1:
            if i + 8 > n:
                return next_order
            i += 8
        elif wire_type == 2:
            length, i = _read_varint(buf, i)
            if length is None or i + length > n:
                i = start + 1
                continue
            end = i + length
            if depth < 4:
                next_order = _scan_protobuf(
                    buf[i:end], depth + 1, field_path, next_order, fixed32, varints
                )
            i = end
        elif wire_type == 5:
            if i + 4 > n:
                return next_order
            bits = _struct.unpack_from("<I", buf, i)[0]
            value = _struct.unpack("<f", _struct.pack("<I", bits))[0]
            fixed32.append((field_path, value, next_order))
            next_order += 1
            i += 4
        else:
            i = start + 1
    return next_order


def _grpc_web_data_frames(data: bytes) -> list[bytes]:
    frames: list[bytes] = []
    i = 0
    while i < len(data):
        if i + 5 > len(data):
            return []
        flags = data[i]
        length = int.from_bytes(data[i + 1 : i + 5], "big")
        start = i + 5
        end = start + length
        if end > len(data):
            return []
        if flags & 0x80 == 0:
            frames.append(data[start:end])
        i = end
    return frames


def _parse_grok_billing_payload(data: bytes, now_secs: int) -> tuple[float, str | None]:
    """Return (used_percent, resets_at_iso). Raises ValueError on failure."""
    payloads = _grpc_web_data_frames(data)
    if not payloads and data:
        # bare protobuf fallback
        first = data[0]
        if (first >> 3) > 0 and (first & 0x07) in (0, 1, 2, 5):
            payloads = [data]
    if not payloads:
        raise ValueError("empty billing payload")

    fixed32: list[tuple[list[int], float, int]] = []
    varints: list[tuple[list[int], int]] = []
    for payload in payloads:
        _scan_protobuf(payload, 0, [], 0, fixed32, varints)

    percent_cands = [
        (path, value, order)
        for path, value, order in fixed32
        if path
        and path[-1] == 1
        and value == value  # not NaN
        and 0.0 <= value <= 100.0
    ]
    percent_cands.sort(key=lambda x: (len(x[0]), x[2]))
    parsed_percent = percent_cands[0][1] if percent_cands else None

    reset_cands = [
        (path, value)
        for path, value in varints
        if 1_700_000_000 <= value <= 2_100_000_000 and value > now_secs
    ]
    reset_ts = None
    preferred = [v for path, v in reset_cands if path == [1, 5, 1]]
    if preferred:
        reset_ts = min(preferred)
    elif reset_cands:
        reset_ts = min(v for _, v in reset_cands)

    has_usage_period = any(
        path[:2] == [1, 6] or (path == [1, 8, 1] and value in (1, 2))
        for path, value in varints
    )
    no_usage_yet = (
        parsed_percent is None
        and not fixed32
        and reset_ts is not None
        and has_usage_period
    )
    if parsed_percent is None and no_usage_yet:
        parsed_percent = 0.0
    if parsed_percent is None:
        raise ValueError("could not locate usage percent")

    resets_at = None
    if reset_ts is not None:
        resets_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
    return float(parsed_percent), resets_at


def fetch_grok_usage() -> ProviderUsage:
    """SuperGrok credit window via grok.com gRPC-web (same as CC Switch)."""
    token = read_grok_token()
    if not token:
        return ProviderUsage(
            provider_id="grok",
            display_name="grok",
            available=False,
            error="更新失败",
        )

    # Empty gRPC-web frame: flags(1) + big-endian length(4) = 5 zero bytes
    body = bytes(5)
    req = urllib.request.Request(
        GROK_BILLING_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/?_s=usage",
            "Accept": "*/*",
            "Content-Type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "User-Agent": "usage-float",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            raw = resp.read()
            grpc_status = resp.headers.get("grpc-status")
            if grpc_status and grpc_status not in ("0", "00"):
                return ProviderUsage(
                    provider_id="grok",
                    display_name="grok",
                    available=False,
                    error="更新失败",
                )
            if status != 200:
                return ProviderUsage(
                    provider_id="grok",
                    display_name="grok",
                    available=False,
                    error="更新失败",
                )
            pct, resets_at = _parse_grok_billing_payload(raw, int(time.time()))
    except Exception:
        return ProviderUsage(
            provider_id="grok",
            display_name="grok",
            available=False,
            error="更新失败",
        )

    result = ProviderUsage(
        provider_id="grok",
        display_name="grok",
        windows=[
            UsageWindow(
                id="credits",
                label=_tr("额度", 'Quota'),
                used_pct=max(0.0, min(100.0, pct)),
                resets_at=resets_at,
            )
        ],
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    cache_put_provider(result)
    return result


# ---------------------------------------------------------------------------
# Antigravity (official CLI /usage; no model inference or credential extraction)
# ---------------------------------------------------------------------------

ANTIGRAVITY_MAX_OUTPUT = 1024 * 1024
# agy refreshes its own OAuth token a few seconds after expiry. A /usage call
# in that gap is a 401, so the retry waits long enough for the new token.
ANTIGRAVITY_USAGE_RETRY_SECONDS = 8
ANTIGRAVITY_BUCKETS = {
    "gemini-weekly": "gemini 7d",
    "gemini-5h": "gemini 5h",
    "3p-weekly": "claude 7d",
    "3p-5h": "claude 5h",
}


def find_antigravity_cli() -> Path | None:
    """Also find the Windows installer location when agy is absent from PATH."""
    override = os.environ.get("ANTIGRAVITY_CLI_PATH")
    if override is not None:
        candidate = Path(override) if override.strip() else None
        return candidate.resolve() if candidate is not None and candidate.is_file() else None
    binary = shutil.which("agy")
    if binary:
        return Path(binary)
    local_app_data = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    for candidate in (local_app_data / "agy" / "bin" / "agy.exe", HOME / ".local" / "bin" / "agy"):
        if candidate.is_file():
            return candidate
    return None


def _antigravity_windows_job() -> tuple[Any, Any]:
    """Own CLI descendants, including plugin helpers, until this query ends."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    return kernel, handle


def _close_antigravity_windows_job(kernel: Any, job: Any) -> None:
    import ctypes
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
            ("total_processes", wintypes.DWORD), ("active_processes", wintypes.DWORD),
            ("terminated_processes", wintypes.DWORD),
        ]

    try:
        kernel.TerminateJobObject(job, 1)
        # Termination is asynchronous. Wait before removing their working directory.
        accounting = Accounting()
        deadline = time.monotonic() + 5
        while kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            if accounting.active_processes == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    finally:
        kernel.CloseHandle(job)


def _antigravity_env() -> dict[str, str]:
    """agy (Go) ignores the Windows system proxy; pass it on when no proxy variable is set."""
    env = os.environ.copy()
    if any(env.get(name) for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")):
        return env
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http")
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env.setdefault("HTTP_PROXY", proxies.get("http") or proxy)
    return env


def _run_antigravity_cli(binary: Path, arguments: list[str], *, timeout: float) -> str:
    """Bound runtime/output and close this query's process tree on every exit."""
    process = None
    kernel = job = None
    resources = ExitStack()
    try:
        if os.name == "nt":
            kernel, job = _antigravity_windows_job()
        # An empty directory avoids loading any project's instructions or MCP config.
        directory_resource = tempfile.TemporaryDirectory(prefix="usage-float-agy-")
        resources.callback(_cleanup_antigravity_directory, directory_resource)
        directory = directory_resource.name
        output = resources.enter_context(tempfile.TemporaryFile())
        process = subprocess.Popen(
            [str(binary), *arguments], cwd=directory, env=_antigravity_env(),
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        if kernel is not None and not kernel.AssignProcessToJobObject(job, int(process._handle)):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if os.fstat(output.fileno()).st_size > ANTIGRAVITY_MAX_OUTPUT:
                raise ValueError("Antigravity usage response exceeds output limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(str(binary), timeout)
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0:
            raise ValueError("Antigravity CLI query failed")
        output.seek(0)
        raw = output.read(ANTIGRAVITY_MAX_OUTPUT + 1)
        if len(raw) > ANTIGRAVITY_MAX_OUTPUT:
            raise ValueError("Antigravity usage response exceeds output limit")
        return raw.decode("utf-8-sig")
    finally:
        if kernel is not None and job is not None:
            _close_antigravity_windows_job(kernel, job)
        elif process is not None and os.name != "nt":
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
        resources.close()


def _cleanup_antigravity_directory(directory: tempfile.TemporaryDirectory) -> None:
    # Windows can release a terminated child's cwd handle just after the job's
    # active-process count reaches zero. Retry that short filesystem delay.
    for attempt in range(50):
        try:
            directory.cleanup()
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.02)


def _parse_antigravity_usage(payload: Any) -> list[UsageWindow]:
    """Read quota buckets, never the prose response or inference token counts."""
    if not isinstance(payload, dict) or payload.get("status") != "SUCCESS":
        raise ValueError("Antigravity usage query did not succeed")
    command = payload.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        raise ValueError("Antigravity did not return the usage command")
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise ValueError("Antigravity quota groups are missing")
    buckets: dict[str, UsageWindow] = {}
    for group in groups:
        group_buckets = group.get("buckets") if isinstance(group, dict) else None
        if not isinstance(group_buckets, list):
            continue
        for bucket in group_buckets:
            if not isinstance(bucket, dict) or bucket.get("disabled") is True:
                continue
            bucket_id = bucket.get("id")
            if not isinstance(bucket_id, str) or bucket_id not in ANTIGRAVITY_BUCKETS:
                continue
            fraction = bucket.get("remaining_fraction")
            if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
                continue
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                continue
            reset = bucket.get("reset_time")
            reset = reset if isinstance(reset, str) and parse_iso(reset) is not None else None
            window = UsageWindow(bucket_id, ANTIGRAVITY_BUCKETS[bucket_id], (1 - fraction) * 100, reset)
            if bucket_id not in buckets or window.used_pct > buckets[bucket_id].used_pct:
                buckets[bucket_id] = window
    if not buckets:
        raise ValueError("Antigravity returned no supported quota buckets")
    return [buckets[bucket_id] for bucket_id in ANTIGRAVITY_BUCKETS if bucket_id in buckets]


def fetch_antigravity_usage() -> ProviderUsage:
    try:
        binary = find_antigravity_cli()
        if binary is None:
            raise ValueError("Antigravity CLI is not installed")
        version = _run_antigravity_cli(binary, ["--version"], timeout=5)
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
        # Earlier CLIs can treat /usage as a model prompt instead of a command.
        if match is None or tuple(map(int, match.groups())) < (1, 1, 11):
            raise ValueError("Antigravity CLI 1.1.11 or later is required")
    except Exception:
        return ProviderUsage("antigravity", "agy", available=False, error="更新失败")

    usage_args = ["-p", "/usage", "--output-format", "json"]
    for attempt in range(2):
        try:
            raw = _run_antigravity_cli(binary, usage_args, timeout=60)
            windows = _parse_antigravity_usage(json.loads(raw))
            break
        except Exception:
            if attempt == 0:
                time.sleep(ANTIGRAVITY_USAGE_RETRY_SECONDS)
                continue
            cached = cache_get_provider("antigravity")
            if cached is not None:
                return cached
            return ProviderUsage("antigravity", "agy", available=False, error="更新失败")
    result = ProviderUsage(
        "antigravity", "agy", windows=windows,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    cache_put_provider(result)
    return result


# ---------------------------------------------------------------------------
# LiteLLM / OpenAI-compatible proxy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiteLLMProxyConfig:
    provider_id: str
    display_name: str
    api_key: str
    openai_base: str
    embed_model: str = LLM_PROXY_EMBED_MODEL
    chat_model: str = LLM_PROXY_CHAT_FALLBACK_MODEL
    budget_duration: str | None = None
    timezone_name: str | None = None


def _llm_proxy_openai_base(endpoint: str) -> str:
    base = (endpoint or LLM_PROXY_DEFAULT_ENDPOINT).strip().rstrip("/")
    if not base:
        return ""
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _extract_litellm_proxy_entry(pid: str, spec: dict[str, Any]) -> LiteLLMProxyConfig | None:
    if not isinstance(spec, dict) or spec.get("enabled") is False:
        return None
    sub = spec.get("litellm") if isinstance(spec.get("litellm"), dict) else spec
    kind = str(spec.get("backend_kind") or sub.get("backend_kind") or spec.get("probe") or "").strip().lower()
    endpoint = str(
        sub.get("endpoint")
        or sub.get("base_url")
        or spec.get("endpoint")
        or spec.get("base_url")
        or ""
    ).strip()
    key = str(sub.get("api_key") or spec.get("api_key") or "").strip()
    key_env = str(sub.get("api_key_env") or spec.get("api_key_env") or "").strip()
    if not key and key_env:
        key = str(os.environ.get(key_env) or "").strip()
    endpoint_env = str(sub.get("endpoint_env") or spec.get("endpoint_env") or "").strip()
    if not endpoint and endpoint_env:
        endpoint = str(os.environ.get(endpoint_env) or "").strip()
    if not endpoint and kind not in {"litellm", "proxy", "api-proxy"} and not isinstance(spec.get("litellm"), dict):
        return None
    base = _llm_proxy_openai_base(endpoint)
    if not key or not base:
        return None
    label = str(spec.get("label") or sub.get("label") or pid).strip() or pid
    PROVIDER_LABELS.setdefault(pid, label)
    return LiteLLMProxyConfig(
        provider_id=pid,
        display_name=pid,
        api_key=key,
        openai_base=base,
        embed_model=str(sub.get("embed_model") or spec.get("embed_model") or LLM_PROXY_EMBED_MODEL).strip() or LLM_PROXY_EMBED_MODEL,
        chat_model=str(sub.get("chat_model") or sub.get("probe_model") or spec.get("chat_model") or spec.get("probe_model") or LLM_PROXY_CHAT_FALLBACK_MODEL).strip() or LLM_PROXY_CHAT_FALLBACK_MODEL,
        budget_duration=str(sub.get("budget_duration") or spec.get("budget_duration") or "").strip() or None,
        timezone_name=str(sub.get("timezone") or spec.get("timezone") or "").strip() or None,
    )


def discover_litellm_proxies() -> dict[str, LiteLLMProxyConfig]:
    """Discover LiteLLM/OpenAI-compatible proxy accounts configured in Router, Paseo, or Usage Float."""
    proxies: dict[str, LiteLLMProxyConfig] = {}
    router_files = [
        Path(os.environ.get("ROUTE_MANAGER_ACCOUNTS_OVERRIDE") or HOME / ".agent-compass" / "accounts-override.json"),
        HOME / ".agent-compass" / "provider-inventory.json",
        Path(__file__).resolve().parent.parent / "skills" / "agent-compass" / "references" / "accounts.json",
    ]
    for path in router_files:
        try:
            if not path.is_file():
                continue
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(doc, dict):
                continue
            for section in ("litellm_proxies", "accounts"):
                block = doc.get(section)
                if not isinstance(block, dict):
                    continue
                for pid, spec in block.items():
                    if pid in proxies or not isinstance(pid, str):
                        continue
                    entry = _extract_litellm_proxy_entry(pid, spec)
                    if entry is not None:
                        proxies[pid] = entry
        except Exception:
            continue

    for pid, spec in _read_paseo_providers().items():
        if pid in proxies or spec.get("enabled") is False:
            continue
        if is_claude_provider(pid) and claude_magpie_source(pid) is not None:
            continue
        env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        endpoint = str(
            env.get("LLM_PROXY_ENDPOINT")
            or env.get("LITELLM_BASE_URL")
            or env.get("OPENAI_BASE_URL")
            or ""
        ).strip()
        key = str(
            env.get("LLM_PROXY_API_KEY")
            or env.get("LITELLM_API_KEY")
            or env.get("OPENAI_API_KEY")
            or ""
        ).strip()
        if endpoint and key and "api.openai.com" not in endpoint.lower():
            entry = _extract_litellm_proxy_entry(
                pid,
                {
                    "backend_kind": "litellm",
                    "endpoint": endpoint,
                    "api_key": key,
                    "label": spec.get("label") or pid,
                },
            )
            if entry is not None:
                proxies[pid] = entry

    env_key = str(os.environ.get("LLM_PROXY_API_KEY") or "").strip()
    env_endpoint = str(os.environ.get("LLM_PROXY_ENDPOINT") or "").strip()
    cfg_key = ""
    cfg_endpoint = ""
    cfg_pid = str(os.environ.get("LLM_PROXY_PROVIDER_ID") or "").strip()
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                cfg_key = str(cfg.get("llm_proxy_api_key") or "").strip()
                cfg_endpoint = str(cfg.get("llm_proxy_endpoint") or "").strip()
                cfg_pid = cfg_pid or str(cfg.get("llm_proxy_provider_id") or "").strip()
                extra_proxies = cfg.get("litellm_proxies")
                if isinstance(extra_proxies, dict):
                    for pid, spec in extra_proxies.items():
                        if isinstance(pid, str) and pid not in proxies and isinstance(spec, dict):
                            entry = _extract_litellm_proxy_entry(pid, spec)
                            if entry is not None:
                                proxies[pid] = entry
    except Exception:
        pass
    legacy_key = env_key or cfg_key
    legacy_endpoint = env_endpoint or cfg_endpoint
    if legacy_key and legacy_endpoint:
        target_pid = cfg_pid or "api-proxy"
        if target_pid not in proxies:
            base = _llm_proxy_openai_base(legacy_endpoint)
            if base:
                PROVIDER_LABELS.setdefault(target_pid, "LLM Proxy" if target_pid == "api-proxy" else target_pid)
                proxies[target_pid] = LiteLLMProxyConfig(
                    provider_id=target_pid,
                    display_name=target_pid,
                    api_key=legacy_key,
                    openai_base=base,
                )
    return proxies


def read_llm_proxy_credentials(provider_id: str = "api-proxy") -> tuple[str, str] | tuple[None, None]:
    """Return (api_key, openai_base) from router/Paseo discovery, env, or usage-float config."""
    discovered = discover_litellm_proxies()
    if provider_id in discovered:
        proxy = discovered[provider_id]
        return proxy.api_key, proxy.openai_base
    key = str(os.environ.get("LLM_PROXY_API_KEY") or "").strip()
    endpoint = str(os.environ.get("LLM_PROXY_ENDPOINT") or "").strip()
    if not key or not endpoint:
        try:
            if CONFIG_PATH.exists():
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    key = key or str(cfg.get("llm_proxy_api_key") or "").strip()
                    endpoint = endpoint or str(cfg.get("llm_proxy_endpoint") or "").strip()
        except Exception:
            pass
    if not key:
        return None, None
    base = _llm_proxy_openai_base(endpoint)
    if not base:
        return None, None
    return key, base


def _header_float(headers: dict[str, str], name: str) -> float | None:
    raw = headers.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _spend_looks_like_this_request(spend: float | None, cost: float | None) -> bool:
    """True when LiteLLM has not flushed cumulative key spend yet."""
    if spend is None:
        return False
    if cost is None:
        return spend <= 1e-5
    return spend <= max(cost * 2.0, 1e-5)


def parse_llm_proxy_spend_headers(
    headers: dict[str, str],
) -> tuple[float | None, float | None, float | None]:
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    spend = _header_float(lowered, "x-litellm-key-spend")
    budget = _header_float(lowered, "x-litellm-key-max-budget")
    cost = _header_float(lowered, "x-litellm-response-cost")
    return spend, budget, cost


def _parse_utc_offset(text: str) -> timedelta | None:
    raw = text.strip().upper().replace(" ", "")
    if raw in {"UTC", "GMT", "Z"}:
        return timedelta(0)
    if raw.startswith("UTC"):
        raw = raw[3:]
    if not raw or raw[0] not in "+-":
        return None
    sign = 1 if raw[0] == "+" else -1
    rest = raw[1:]
    hours = 0
    minutes = 0
    if ":" in rest:
        hour_text, minute_text = rest.split(":", 1)
        if not hour_text.isdigit() or not minute_text.isdigit():
            return None
        hours, minutes = int(hour_text), int(minute_text)
    elif rest.isdigit():
        if len(rest) <= 2:
            hours = int(rest)
        elif len(rest) == 4:
            hours, minutes = int(rest[:2]), int(rest[2:])
        else:
            return None
    else:
        return None
    if hours > 14 or minutes > 59:
        return None
    return timedelta(hours=sign * hours, minutes=sign * minutes)


def _normalize_llm_proxy_timezone(value: Any) -> str:
    text = str(value or "").strip() or LLM_PROXY_TIMEZONE_DEFAULT
    aliases = {
        "cst": "UTC+8",
        "prc": "UTC+8",
        "asia/shanghai": "UTC+8",
        "asia/hong_kong": "UTC+8",
        "asia/taipei": "UTC+8",
        "asia/singapore": "UTC+8",
        "asia/tokyo": "UTC+9",
        "asia/seoul": "UTC+9",
        "us/eastern": "UTC-5",
        "us/pacific": "UTC-8",
        "europe/london": "UTC",
    }
    mapped = aliases.get(text.lower())
    if mapped:
        return mapped
    offset = _parse_utc_offset(text)
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        if total_minutes == 0:
            return "UTC"
        sign = "+" if total_minutes > 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        if minutes:
            return f"UTC{sign}{hours:02d}:{minutes:02d}"
        return f"UTC{sign}{hours}"
    return text


def _llm_proxy_zoneinfo(name: str | None) -> timezone | ZoneInfo:
    text = _normalize_llm_proxy_timezone(name)
    offset = _parse_utc_offset(text)
    if offset is not None:
        return timezone(offset)
    try:
        return ZoneInfo(text)
    except Exception:
        return timezone(timedelta(hours=8))


def _normalize_llm_proxy_budget_duration(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    aliases = {
        "hourly": "1h",
        "daily": "1d",
        "weekly": "7d",
        "monthly": "30d",
        "24h": "1d",
        "1w": "7d",
        "1mo": "30d",
    }
    return aliases.get(text, text)


def _parse_llm_proxy_duration(raw: str | None) -> tuple[int, str] | None:
    text = _normalize_llm_proxy_budget_duration(raw)
    if not text:
        return None
    index = 0
    while index < len(text) and text[index].isdigit():
        index += 1
    if index == 0 or index == len(text):
        return None
    try:
        value = int(text[:index])
    except ValueError:
        return None
    unit = text[index:]
    if unit not in {"s", "m", "h", "d", "w", "mo"}:
        return None
    return value, unit


def next_llm_proxy_budget_reset(
    duration: str | None,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> datetime | None:
    """Next LiteLLM-style budget reset (1d midnight, 7d Monday, 30d 1st of month)."""
    parsed = _parse_llm_proxy_duration(duration)
    if parsed is None:
        return None
    value, unit = parsed
    tzinfo = _llm_proxy_zoneinfo(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(tzinfo)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)

    def at_midnight(moment: datetime) -> datetime:
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)

    if unit == "d" and value == 1:
        candidate = midnight
        return candidate + timedelta(days=1) if candidate <= current else candidate
    if (unit == "d" and value == 7) or (unit == "w" and value == 1):
        days_until_monday = (7 - current.weekday()) % 7
        candidate = midnight + timedelta(days=days_until_monday)
        return candidate + timedelta(days=7) if candidate <= current else candidate
    if (unit == "d" and value == 30) or (unit == "mo" and value == 1):
        candidate = midnight.replace(day=1)
        if candidate <= current:
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1)
            else:
                candidate = candidate.replace(month=candidate.month + 1)
        return candidate
    if unit == "h":
        return current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=value)
    if unit == "m":
        return current.replace(second=0, microsecond=0) + timedelta(minutes=value)
    if unit == "s":
        return current.replace(microsecond=0) + timedelta(seconds=value)
    if unit == "d":
        return at_midnight(midnight + timedelta(days=value))
    if unit == "w":
        return at_midnight(midnight + timedelta(weeks=value))
    return None


def _parse_llm_proxy_reset_timestamp(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        stamp = float(text)
        if stamp > 1e12:
            stamp /= 1000.0
        if stamp > 1e9:
            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    except ValueError:
        pass
    parsed = parse_iso(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _llm_proxy_budget_settings() -> tuple[str, str]:
    duration = str(os.environ.get("LLM_PROXY_BUDGET_DURATION") or "").strip()
    timezone_name = str(os.environ.get("LLM_PROXY_TIMEZONE") or "").strip()
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                duration = duration or str(data.get("llm_proxy_budget_duration") or "").strip()
                timezone_name = timezone_name or str(
                    data.get("llm_proxy_timezone") or ""
                ).strip()
    except Exception:
        pass
    return (
        duration or LLM_PROXY_BUDGET_DURATION_DEFAULT,
        _normalize_llm_proxy_timezone(timezone_name or LLM_PROXY_TIMEZONE_DEFAULT),
    )


def resolve_llm_proxy_resets_at(
    headers: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
    duration: str | None = None,
    timezone_name: str | None = None,
) -> str | None:
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    for key in LLM_PROXY_RESET_HEADERS:
        parsed = _parse_llm_proxy_reset_timestamp(lowered.get(key))
        if parsed:
            return parsed
    header_duration = None
    for key in LLM_PROXY_DURATION_HEADERS:
        raw = lowered.get(key)
        if raw:
            header_duration = raw
            break
    reset = next_llm_proxy_budget_reset(
        header_duration or duration or LLM_PROXY_BUDGET_DURATION_DEFAULT,
        now=now,
        timezone_name=timezone_name,
    )
    return reset.isoformat() if reset is not None else None


def _round_dollars(value: float) -> int:
    """Half-up dollar rounding (2.5 → 3), not banker's rounding or truncation."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_llm_proxy_spend(spend: float, budget: float | None = None) -> str:
    """Compact dollar readout, e.g. $2/200."""
    used = _round_dollars(spend)
    if budget is None or budget <= 0:
        return f"${used}"
    return f"${used}/{_round_dollars(budget)}"


def _llm_proxy_probe(
    base: str, key: str, *, model: str, kind: str
) -> tuple[int, dict[str, str]]:
    headers = {"Authorization": f"Bearer {key}"}
    if kind == "embeddings":
        status, _payload, resp_headers = http_json_response(
            f"{base}/embeddings",
            method="POST",
            headers=headers,
            body={"model": model, "input": "usage"},
        )
        return status, resp_headers
    status, _payload, resp_headers = http_json_response(
        f"{base}/chat/completions",
        method="POST",
        headers=headers,
        body={
            "model": model,
            "messages": [{"role": "user", "content": "."}],
            "max_tokens": 1,
        },
    )
    return status, resp_headers


def fetch_api_proxy_usage(provider_id: str = "api-proxy") -> ProviderUsage:
    proxy = discover_litellm_proxies().get(provider_id)
    creds = read_llm_proxy_credentials(provider_id) if proxy is None else (proxy.api_key, proxy.openai_base)
    display_name = proxy.display_name if proxy is not None else provider_id
    if creds[0] is None:
        return ProviderUsage(
            provider_id=provider_id,
            display_name=display_name,
            available=False,
            error="更新失败",
        )
    key, base = creds
    embed_model = proxy.embed_model if proxy is not None else LLM_PROXY_EMBED_MODEL
    chat_model = proxy.chat_model if proxy is not None else LLM_PROXY_CHAT_FALLBACK_MODEL
    probes = (
        ("embeddings", embed_model),
        ("chat", chat_model),
    )
    spend: float | None = None
    budget: float | None = None
    headers: dict[str, str] = {}
    for kind, model in probes:
        status, headers = _llm_proxy_probe(base, key, model=model, kind=kind)
        if status != 200:
            continue
        spend, budget, cost = parse_llm_proxy_spend_headers(headers)
        if _spend_looks_like_this_request(spend, cost):
            status, headers = _llm_proxy_probe(base, key, model=model, kind=kind)
            if status == 200:
                spend, budget, cost = parse_llm_proxy_spend_headers(headers)
        if spend is not None:
            break
    if spend is None:
        return ProviderUsage(
            provider_id=provider_id,
            display_name=display_name,
            available=False,
            error="更新失败",
        )

    duration, timezone_name = _llm_proxy_budget_settings()
    if proxy is not None:
        if proxy.budget_duration:
            duration = proxy.budget_duration
        if proxy.timezone_name:
            timezone_name = _normalize_llm_proxy_timezone(proxy.timezone_name)
    resets_at = resolve_llm_proxy_resets_at(
        headers,
        duration=duration,
        timezone_name=timezone_name,
    )

    if budget is None or budget <= 0:
        result = ProviderUsage(
            provider_id=provider_id,
            display_name=display_name,
            summary=format_llm_proxy_spend(spend),
            windows=[
                UsageWindow(
                    id="spend",
                    label=_tr("额度", 'Quota'),
                    used_pct=0.0,
                    resets_at=resets_at,
                )
            ]
            if resets_at
            else [],
            available=True,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            backend_kind="litellm",
            billing="metered",
        )
        cache_put_provider(result)
        return result

    used_pct = max(0.0, min(100.0, (spend / budget) * 100.0))
    result = ProviderUsage(
        provider_id=provider_id,
        display_name=display_name,
        plan_label=f"${budget:.0f}",
        summary=format_llm_proxy_spend(spend, budget),
        windows=[
            UsageWindow(
                id="spend",
                label=_tr("额度", 'Quota'),
                used_pct=used_pct,
                resets_at=resets_at,
            )
        ],
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        backend_kind="litellm",
        billing="metered",
    )
    cache_put_provider(result)
    return result


def _pi_agent_home() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or HOME / ".pi" / "agent")


def read_pi_codex_auth(pi_home: Path | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    """Read openai-codex OAuth credentials from ~/.pi/agent/auth.json."""
    home = pi_home if pi_home is not None else _pi_agent_home()
    auth_path = home / "auth.json"
    if not auth_path.is_file():
        return None, None
    try:
        doc = json.loads(auth_path.read_text(encoding="utf-8-sig"))
        if not isinstance(doc, dict):
            return None, None
        codex_oauth = doc.get("openai-codex")
        if isinstance(codex_oauth, dict) and str(codex_oauth.get("access") or "").strip():
            return {
                "tokens": {
                    "access_token": str(codex_oauth.get("access")).strip(),
                    "refresh_token": str(codex_oauth.get("refresh") or "").strip(),
                    "account_id": str(codex_oauth.get("accountId") or "").strip(),
                }
            }, auth_path
    except Exception:
        return None, None
    return None, None


def pi_linked_codex_provider(pi_home: Path | None = None) -> str | None:
    """Return the codex provider_id sharing the same OpenAI accountId as pi, if any."""
    pi_auth, _ = read_pi_codex_auth(pi_home)
    if not pi_auth:
        return None
    pi_account_id = str((pi_auth.get("tokens") or {}).get("account_id") or "").strip()
    if not pi_account_id:
        return None
    for pid, acc in discover_codex_accounts().items():
        cauth, _ = read_codex_auth(acc.home)
        if not cauth:
            continue
        cid = str((cauth.get("tokens") or {}).get("account_id") or "").strip()
        if cid and cid == pi_account_id:
            return pid
    return None


def fetch_pi_usage(provider_id: str = "pi") -> ProviderUsage:
    """Fetch quota usage for pi when signed in via openai-codex OAuth."""
    home = _pi_agent_home()
    pi_auth, auth_path = read_pi_codex_auth(home)
    if not pi_auth:
        return ProviderUsage(provider_id, provider_id, available=False, error="更新失败")
    tokens = pi_auth.get("tokens") or {}
    access_token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token:
        return ProviderUsage(provider_id, provider_id, available=False, error="更新失败")
    headers = {"Authorization": f"Bearer {access_token}"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, data = http_json(CODEX_USAGE_URL, headers=headers)
    if status == 401 and tokens.get("refresh_token") and auth_path is not None:
        refreshed = refresh_codex_token(str(tokens["refresh_token"]))
        if refreshed and refreshed.get("access_token"):
            access_token = str(refreshed["access_token"])
            try:
                raw_doc = json.loads(auth_path.read_text(encoding="utf-8-sig"))
                if isinstance(raw_doc, dict) and isinstance(raw_doc.get("openai-codex"), dict):
                    raw_doc["openai-codex"]["access"] = access_token
                    if refreshed.get("refresh_token"):
                        raw_doc["openai-codex"]["refresh"] = refreshed["refresh_token"]
                    auth_path.write_text(json.dumps(raw_doc, indent=2), encoding="utf-8")
            except Exception:
                pass
            headers["Authorization"] = f"Bearer {access_token}"
            status, data = http_json(CODEX_USAGE_URL, headers=headers)
    if status != 200 or not isinstance(data, dict):
        cached = cache_get_provider(provider_id)
        if cached is not None:
            return cached
        return ProviderUsage(provider_id, provider_id, available=False, error="更新失败")
    rl = data.get("rate_limit") or {}
    windows: list[UsageWindow] = []
    if isinstance(rl.get("secondary_window"), dict):
        w = rl["secondary_window"]
        windows.append(
            UsageWindow(
                id="secondary",
                label="7d",
                used_pct=float(w.get("used_percent", 0)),
                resets_at=_epoch_to_iso(w.get("reset_at")),
            )
        )
    if isinstance(rl.get("primary_window"), dict):
        w = rl["primary_window"]
        windows.append(
            UsageWindow(
                id="primary",
                label="5h",
                used_pct=float(w.get("used_percent", 0)),
                resets_at=_epoch_to_iso(w.get("reset_at")),
            )
        )
    result = ProviderUsage(
        provider_id=provider_id,
        display_name=provider_id,
        plan_label=data.get("plan_type"),
        windows=windows,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        backend_kind="subscription",
        billing="subscription",
        home=str(home),
    )
    cache_put_provider(result)
    return result


def read_opencode_auth() -> dict[str, Any]:
    """Read configured credentials from ~/.local/share/opencode/auth.json."""
    auth_path = HOME / ".local" / "share" / "opencode" / "auth.json"
    try:
        if auth_path.is_file():
            doc = json.loads(auth_path.read_text(encoding="utf-8-sig"))
            if isinstance(doc, dict):
                return doc
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Fetch all
# ---------------------------------------------------------------------------


def provider_available(provider_id: str) -> bool:
    """True when local credentials (or the Antigravity-managed CLI) are present.

    Providers without local credentials are hidden. Antigravity manages its
    secure login internally, so its installed CLI is checked by /usage instead.
    """
    provider_id = provider_fetch_id(provider_id)
    remote = claude_magpie_source(provider_id)
    if remote is not None:
        return bool(remote.valid)
    codex_accounts = discover_codex_accounts()
    account = codex_accounts.get(provider_id)
    if account is not None:
        return read_codex_auth(account.home)[0] is not None
    if provider_id == "claude":
        return read_claude_oauth()[0] is not None
    if is_claude_provider(provider_id):
        return read_claude_oauth(claude_account_home(provider_id))[0] is not None
    if provider_id == "grok":
        return read_grok_token() is not None
    if provider_id in discover_litellm_proxies() or provider_id == "api-proxy":
        return read_llm_proxy_credentials(provider_id)[0] is not None
    if provider_id == "pi":
        return read_pi_codex_auth()[0] is not None
    if provider_id == "opencode":
        return bool(read_opencode_auth())
    if provider_id == "antigravity":
        return find_antigravity_cli() is not None
    return False


def discovered_default_providers() -> list[str]:
    items = list(DEFAULT_PROVIDERS)
    for pid in discover_codex_accounts():
        if pid not in items:
            items.append(pid)
    for pid in discover_claude_homes():
        if pid == "claude":
            continue
        for sub in PROVIDER_DISPLAY_ITEMS.get(pid, (f"{pid}:fable", f"{pid}:7d", f"{pid}:5h")):
            if sub not in items:
                items.append(sub)
    for pid in discover_litellm_proxies():
        if pid not in items:
            items.append(pid)
    if read_pi_codex_auth()[0] is not None and pi_linked_codex_provider() is None:
        if "pi" not in items:
            items.append("pi")
    return items


def active_providers(
    configured: list[str] | None = None,
    *,
    include_discovered: bool = False,
    disabled: list[str] | None = None,
) -> list[str]:
    """Configured providers with local credentials or an installed agy CLI."""
    if configured is None:
        selected = discovered_default_providers()
    else:
        selected = list(configured)
        if include_discovered:
            dis = set(disabled or [])
            for extra in discovered_default_providers():
                if extra not in DEFAULT_PROVIDERS and extra not in dis and extra not in selected:
                    selected.append(extra)
    selected = list(dict.fromkeys('grok' if pid == 'grok-bot' else pid for pid in selected))
    return [pid for pid in selected if provider_available(pid)]


def refresh_local_activity(output: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Scan local session files and Paseo agents to publish route-manager.activity.v1."""
    clock = time.time() if now is None else float(now)
    cutoff = clock - 300.0
    accounts: dict[str, dict[str, Any]] = {}
    active_workers: dict[str, dict[str, int]] = {}
    iso_now = datetime.fromtimestamp(clock, tz=timezone.utc).isoformat()

    paseo_counts: dict[str, int] = {}
    paseo_agents_dir = Path(os.environ.get("PASEO_HOME") or HOME / ".paseo") / "agents"
    try:
        for agent_file in paseo_agents_dir.glob("*/*.json"):
            try:
                if agent_file.stat().st_mtime < cutoff:
                    continue
                doc = json.loads(agent_file.read_text(encoding="utf-8-sig"))
                if isinstance(doc, dict) and not doc.get("archivedAt") and doc.get("lastStatus") == "running":
                    prov = str(doc.get("provider") or "").strip()
                    if prov:
                        paseo_counts[prov] = paseo_counts.get(prov, 0) + 1
            except Exception:
                continue
    except OSError:
        pass

    def _recent_files(root: Path, suffix: str = ".jsonl") -> int:
        if not root.is_dir():
            return 0
        count = 0
        try:
            for dirpath, _, filenames in os.walk(root):
                for fname in filenames:
                    if not fname.endswith(suffix):
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        mtime = fpath.stat().st_mtime
                        if suffix == ".db":
                            wal = Path(str(fpath) + "-wal")
                            if wal.exists():
                                mtime = max(mtime, wal.stat().st_mtime)
                        if mtime >= cutoff:
                            count += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return count

    for pid, chome in discover_claude_homes().items():
        if not chome.is_dir() and pid != "claude":
            continue
        count = max(paseo_counts.get(pid, 0), _recent_files(chome / "projects") + _recent_files(chome / "sessions"))
        state = "active" if count > 0 else ("idle" if chome.is_dir() else "unknown")
        accounts[pid] = {"state": state, "source": "usage-float", "observed_at": iso_now}
        if count > 0:
            active_workers[pid] = {"count": count}

    for pid, cacc in discover_codex_accounts().items():
        if not cacc.home.is_dir() and pid != "codex":
            continue
        count = max(paseo_counts.get(pid, 0), _recent_files(cacc.home / "sessions"))
        state = "active" if count > 0 else ("idle" if cacc.home.is_dir() else "unknown")
        accounts[pid] = {"state": state, "source": "usage-float", "observed_at": iso_now}
        if count > 0:
            active_workers[pid] = {"count": count}

    agy_dir = HOME / ".gemini" / "antigravity-cli" / "conversations"
    agy_count = _recent_files(agy_dir, ".db")
    accounts["antigravity"] = {
        "state": "active" if agy_count > 0 else ("idle" if agy_dir.is_dir() else "unknown"),
        "source": "usage-float",
        "observed_at": iso_now,
    }
    if agy_count > 0:
        active_workers["antigravity"] = {"count": agy_count}

    grok_count = paseo_counts.get("grok", 0)
    accounts["grok"] = {
        "state": "active" if grok_count > 0 else "idle",
        "source": "usage-float",
        "observed_at": iso_now,
    }
    if grok_count > 0:
        active_workers["grok"] = {"count": grok_count}

    pi_home = _pi_agent_home()
    if pi_home.is_dir() or paseo_counts.get("pi", 0) > 0:
        pi_count = max(paseo_counts.get("pi", 0), _recent_files(pi_home / "sessions", ".jsonl"))
        accounts["pi"] = {
            "state": "active" if pi_count > 0 else ("idle" if pi_home.is_dir() else "unknown"),
            "source": "usage-float",
            "observed_at": iso_now,
        }
        if pi_count > 0:
            active_workers["pi"] = {"count": pi_count}

    oc_dir = HOME / ".local" / "share" / "opencode"
    if oc_dir.is_dir() or paseo_counts.get("opencode", 0) > 0:
        oc_count = max(paseo_counts.get("opencode", 0), _recent_files(oc_dir, ".db"))
        accounts["opencode"] = {
            "state": "active" if oc_count > 0 else ("idle" if oc_dir.is_dir() else "unknown"),
            "source": "usage-float",
            "observed_at": iso_now,
        }
        if oc_count > 0:
            active_workers["opencode"] = {"count": oc_count}

    for pid in discover_litellm_proxies():
        if pid in accounts:
            continue
        pcount = paseo_counts.get(pid, 0)
        accounts[pid] = {
            "state": "active" if pcount > 0 else "idle",
            "source": "usage-float",
            "observed_at": iso_now,
        }
        if pcount > 0:
            active_workers[pid] = {"count": pcount}

    payload = {
        "schema": "route-manager.activity.v1",
        "generated_at": iso_now,
        "active_workers": active_workers,
        "accounts": accounts,
    }
    if output is None:
        env_out = os.environ.get("ROUTE_MANAGER_ACTIVITY_FILE")
        if env_out:
            output = Path(env_out)
        elif CACHE_PATH == HOME / ".usage-float" / "usage-cache.json":
            output = HOME / ".agent-compass" / "usage-activity.json"
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            tmp = output.with_name(f"{output.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(output)
        except OSError:
            pass
    return payload


def provider_poll_seconds(provider_id: str) -> int:
    """Local session activity controls quota polling, not the panel layout."""
    if claude_magpie_source(provider_id) is not None:
        # Read from the VPS gateway: its usage moves without local sessions.
        return REFRESH_SECONDS
    path = Path(os.environ.get('ROUTE_MANAGER_ACTIVITY_FILE') or HOME / '.agent-compass' / 'usage-activity.json')
    try:
        if path.stat().st_size > 1024 * 1024:
            return UNKNOWN_ACTIVITY_REFRESH_SECONDS
        activity = json.loads(path.read_text(encoding='utf-8-sig'))
        age = time.time() - _iso_epoch(activity.get('generated_at'), 0)
        workers = activity.get('active_workers')
        account = (activity.get('accounts') or {}).get(provider_id) or {}
        if (activity.get('schema') != 'route-manager.activity.v1'
                or not 0 <= age <= 180 or not isinstance(workers, dict)):
            return UNKNOWN_ACTIVITY_REFRESH_SECONDS
        tracked = {
            *discover_claude_homes(),
            *discover_codex_accounts(),
            *discover_litellm_proxies(),
            'pi',
            'opencode',
            'grok',
            'antigravity',
        }
        state = account.get('state')
        if provider_id not in tracked or state not in ('active', 'idle'):
            return UNKNOWN_ACTIVITY_REFRESH_SECONDS
        count = (workers.get(provider_id) or {}).get('count', 0)
        if state == 'active' and count > 0:
            return REFRESH_SECONDS
        if state == 'idle' and count == 0:
            return IDLE_REFRESH_SECONDS
        return UNKNOWN_ACTIVITY_REFRESH_SECONDS
    except (OSError, ValueError, TypeError, AttributeError):
        return UNKNOWN_ACTIVITY_REFRESH_SECONDS


def record_usage_attempt(provider_id: str, key: str | None) -> None:
    def update(cache):
        cache.setdefault('attempts', {})[provider_id] = {'at': time.time(), 'credential_key': key}
    update_usage_cache(update)


def fetch_all(providers: list[str], *, force: bool = False) -> list[ProviderUsage]:
    """Collect and cache usage directly across the requested providers."""
    try:
        refresh_local_activity()
    except Exception:
        pass
    return collect_local_usage(providers, force=force)


def merge_display_usage(
    observations: Any, previous: list[ProviderUsage], selected: list[str],
    error: str = '更新失败',
) -> list[ProviderUsage]:
    """Complete partial/failed refreshes using UI memory and disk history."""
    incoming = {p.provider_id: p for p in observations if isinstance(p, ProviderUsage)} \
        if isinstance(observations, (list, tuple)) else {}
    result = []
    for pid in dict.fromkeys(provider_fetch_id(name) for name in selected):
        item = incoming.get(pid)
        if item is None or not (item.windows or item.summary):
            item = display_fallback(pid, (item.error if item else None) or error, previous)
        result.append(item)
    return result


def fetch_display_usage(
    selected: list[str], previous: list[ProviderUsage], *, force: bool = False,
) -> list[ProviderUsage]:
    """UI collection boundary, including exceptions from subprocess adapters."""
    try:
        observations = fetch_all(selected, force=force)
    except Exception as error:
        return merge_display_usage(None, previous, selected, f'更新失败 ({type(error).__name__})')
    return merge_display_usage(observations, previous, selected)


def collect_local_usage(providers: list[str], *, force: bool = False) -> list[ProviderUsage]:
    """One account-scoped collection/cache lane owned directly by Usage Float."""
    out = []
    polled = {}
    claude_homes = discover_claude_homes()
    codex_accounts = discover_codex_accounts()
    litellm_proxies = discover_litellm_proxies()
    for pid in dict.fromkeys(provider_fetch_id(item) for item in providers):
        if pid not in {*PROVIDER_DISPLAY_ITEMS, *claude_homes, *codex_accounts, *litellm_proxies, 'pi', 'grok', 'api-proxy'}:
            continue
        polled[pid] = poll = provider_poll_seconds(pid)
        cached = item = None
        try:
            with file_lock(CACHE_PATH.with_name(f'{CACHE_PATH.name}.{pid}.fetch.lock'), timeout=1):
                key = provider_credential_key(pid)
                cached = cache_get_provider(pid) or display_snapshot(pid)
                cache = load_usage_cache()
                raw = (cache.get('providers') or {}).get(pid) or {}
                attempt = (cache.get('attempts') or {}).get(pid) or {}
                last_attempt = attempt.get('at', 0) if attempt.get('credential_key') == key else 0
                observed = _iso_epoch(cached.fetched_at, 0) if cached else 0
                now_ts = time.time()
                age = now_ts - max(last_attempt, observed)
                # A window that reset after the reading is due now, not after the idle cadence.
                resets = [_iso_epoch(w.resets_at, 0) for w in (cached.windows if cached else []) if w.resets_at]
                passed = [r for r in resets if observed < r <= now_ts]
                reset_due = bool(passed) and (last_attempt < max(passed) or now_ts - last_attempt >= REFRESH_SECONDS)
                if not force and not reset_due and 0 <= age < poll:
                    if cached:
                        # Keep exactly the same numbers/titles. Staleness is
                        # metadata for routing, not an extra panel label.
                        cached.stale = bool(raw.get('stale', True)) or time.time() - observed >= REFRESH_SECONDS
                        out.append(cached)
                    else:
                        out.append(ProviderUsage(provider_id=pid, display_name=pid,
                                                 available=False, stale=True, error='更新失败'))
                    continue
                record_usage_attempt(pid, key)
                if pid == 'claude':
                    item = fetch_claude_usage(force=force)
                elif is_claude_provider(pid):
                    item = fetch_claude_usage(force=force, provider_id=pid)
                elif pid in codex_accounts:
                    item = fetch_codex_usage(codex_accounts[pid])
                elif pid == 'pi':
                    item = fetch_pi_usage('pi')
                elif pid == 'grok':
                    item = fetch_grok_usage()
                elif pid == 'api-proxy':
                    item = fetch_api_proxy_usage()
                elif pid in litellm_proxies:
                    item = fetch_api_proxy_usage(pid)
                elif pid == 'antigravity':
                    item = fetch_antigravity_usage()
                else:
                    continue
                if key != provider_credential_key(pid):
                    item = stale_provider_usage(pid, _tr('登录凭据已变化，等待下次更新', 'Login credentials changed; waiting for the next update'))
                elif item.available and not item.error:
                    # May still be stale (gateway readAt older than the
                    # freshness bound); that is a real reading, not a failure.
                    item.credential_key = key
                    cache_put_provider(item)
                elif item.error or item.stale or not item.available:
                    item = stale_provider_usage(pid, item.error or '更新失败', [item, cached])
                out.append(item)
        except TimeoutError:
            # The other process owns this fetch; don't overwrite its result.
            out.append(display_fallback(pid, _tr('正在由其他采集器更新', 'Another collector is updating usage'),
                                        [item, cached]))
        except Exception:
            out.append(stale_provider_usage(pid, '更新失败', [item, cached]))
    if polled:
        # Publish the cadence chosen this tick: an idle account is read every
        # IDLE_REFRESH_SECONDS, so its reading stays current that long. The router
        # widens freshness only while these decisions keep arriving.
        decided = time.time()
        def record(cache):
            cache.setdefault('poll', {}).update(
                {pid: {'seconds': seconds, 'at': decided} for pid, seconds in polled.items()})
        try:
            update_usage_cache(record)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Display rows (fixed-width strip: multi-window models expand to N lines)
# ---------------------------------------------------------------------------


@dataclass
class DisplayRow:
    """One visual row: left title, right usage."""

    title: str
    used_pct: float | None = None
    resets_at: str | None = None
    summary: str | None = None  # free text on the right (e.g. failure)
    failed: bool = False
    provider_id: str = ""
    window_id: str = ""


def row_opens_reset_ui(row: DisplayRow) -> bool:
    """Whether clicking this row's number should open a reset dialog.

    Every Codex account has cards to list. Claude's 7d row opens the
    usage-limit grants (they refill the weekly window) and its 5h row opens
    the weekly 5h session reset.
    """
    if row.provider_id in discover_codex_accounts():
        return True
    if claude_magpie_source(row.provider_id) is not None:
        return False  # Magpie exposes readings, not Claude reset entitlements.
    return is_claude_provider(row.provider_id) and row.window_id in ("five_hour", "seven_day")


def reset_target_id(row: DisplayRow) -> str:
    """What a click on this row's number opens: a Codex account or a Claude row."""
    if is_claude_provider(row.provider_id):
        return display_item(row) or row.provider_id
    return row.provider_id


def display_item(row: DisplayRow) -> str | None:
    """The Settings entry that shows or hides this row.

    None for a shared provider row without a specific window (a failure line).
    """
    if row.provider_id == "antigravity":
        return ANTIGRAVITY_WINDOW_ITEMS.get(row.window_id)
    if not is_claude_provider(row.provider_id):
        return row.provider_id
    if row.window_id.startswith("weekly_"):
        return f"{row.provider_id}:fable"
    item = CLAUDE_WINDOW_ITEMS.get(row.window_id)
    return item.replace("claude:", f"{row.provider_id}:") if item else None


def iter_display_rows(
    providers: list[ProviderUsage],
    items: list[str] | None = None,
) -> list[DisplayRow]:
    """Expand providers into fixed rows.

    Claude (and any multi-window model) becomes one row per time window so the
    strip width stays stable instead of growing when 5h+7d are concatenated.
    With ``items`` (the enabled Settings entries, in order) rows are filtered
    and sorted by them; a shared failure line sits where its first enabled row would.
    """
    rows = _expand_display_rows(providers)
    if items is None:
        return rows
    rank = {item: index for index, item in enumerate(_unique_provider_ids(items))}
    provider_ranks = {
        provider: [rank[item] for item in display_items if item in rank]
        for provider, display_items in PROVIDER_DISPLAY_ITEMS.items()
    }
    ranked: list[tuple[int, DisplayRow]] = []
    for row in rows:
        item = display_item(row)
        if item is None:
            positions = provider_ranks.get(row.provider_id, [])
            if not positions:
                continue
            position = min(positions)
        elif item in rank:
            position = rank[item]
        else:
            continue
        ranked.append((position, row))
    ranked.sort(key=lambda pair: pair[0])
    return [row for _position, row in ranked]


def _expand_display_rows(providers: list[ProviderUsage]) -> list[DisplayRow]:
    rows: list[DisplayRow] = []
    for p in providers:
        name = (p.display_name or p.provider_id).lower()

        if not p.windows and not p.summary:
            rows.append(
                DisplayRow(
                    title=name,
                    failed=True,
                    summary=_ui("更新失败"),
                    provider_id=p.provider_id,
                )
            )
            continue

        if p.summary and not p.windows:
            rows.append(
                DisplayRow(
                    title=name,
                    summary=p.summary.replace(name, "").strip() or p.summary,
                    provider_id=p.provider_id,
                )
            )
            continue

        if p.windows:
            shown = p.windows
            if p.provider_id in CODEX_ACCOUNT_BY_ID:
                # Retain one tile, but expose both constraints to the router.
                shown = [max(shown, key=lambda w: w.used_pct if w.used_pct is not None else -1)]
            multi = len(shown) > 1
            # Allow 5h + 7d + scoped model weeks (e.g. fable 7d)
            fetched = _iso_epoch(p.fetched_at, 0)
            for w in shown[:5]:
                used_pct, resets_at = w.used_pct, w.resets_at
                if resets_at and 0 < fetched < _iso_epoch(resets_at, 0) <= time.time():
                    # Read before a reset that has since passed: the window refilled.
                    used_pct, resets_at = 0.0, None
                # "claude 5h" / "claude 7d" / "fable 7d" (scoped rows omit provider prefix)
                if w.id.startswith("weekly_"):
                    title = f"{name} {w.label}" if p.provider_id != "claude" else w.label
                elif multi or p.provider_id == "antigravity":
                    title = f"{name} {w.label}"
                else:
                    title = name
                rows.append(
                    DisplayRow(
                        title=title,
                        used_pct=used_pct,
                        resets_at=resets_at,
                        summary=p.summary if not multi else None,
                        provider_id=p.provider_id,
                        window_id=w.id,
                    )
                )
            continue

        rows.append(
            DisplayRow(
                title=name,
                failed=True,
                summary=_ui("更新失败"),
                provider_id=p.provider_id,
            )
        )
    return rows


def _enable_dpi_awareness() -> None:
    """Mark process DPI-aware so Windows doesn't bitmap-stretch a tiny window.

    Do NOT force Tk's ``tk scaling`` to 1.0 afterwards — at 96 DPI Tk's natural
    scale is ~1.33 (96/72). Forcing 1.0 makes point-sized fonts look ~25% too small.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # Per-monitor v2 handles mixed-scale displays and WM_DPICHANGED best.
        # The negative pseudo-handle is DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        import ctypes

        # 2 = PROCESS_PER_MONITOR_DPI_AWARE (Win 8.1+)
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Autostart
# ---------------------------------------------------------------------------

AUTOSTART_VALUE = "UsageFloat"


def _script_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    script = Path(__file__).resolve()
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else Path(sys.executable))
    return f'"{exe}" "{script}"'


def is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
        try:
            winreg.QueryValueEx(key, AUTOSTART_VALUE)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def set_autostart(enabled: bool) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, _tr("仅支持 Windows 自启动", 'Start at login is only supported on Windows')
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, _script_command())
                return True, _tr("已开启开机自启", 'Start at login enabled')
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass
            return True, _tr("已关闭开机自启", 'Start at login disabled')
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        return False, _tr(f"自启动设置失败: {e}", f'Could not change start at login: {e}')


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def _log_exception(prefix: str = "error") -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with (CONFIG_PATH.parent / "error.log").open("a", encoding="utf-8") as f:
            f.write(f"\n---- {datetime.now().isoformat()} {prefix} ----\n")
            traceback.print_exc(file=f)
    except Exception:
        pass


def _report_tk_callback_exception(exc: Any, val: Any, tb: Any) -> None:
    """Log Tk callback failures without aborting on a recursive stderr printer."""
    try:
        sys.last_type = exc
        sys.last_value = val
        sys.last_traceback = tb
    except Exception:
        pass
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with (CONFIG_PATH.parent / "error.log").open("a", encoding="utf-8") as f:
            f.write(f"\n---- {datetime.now().isoformat()} tk callback ----\n")
            traceback.print_exception(exc, val, tb, limit=50, file=f)
    except Exception:
        pass


@dataclass(frozen=True)
class MonitorInfo:
    """Physical-pixel monitor geometry for a per-monitor-DPI-aware process."""

    device: str
    name: str
    x: int
    y: int
    width: int
    height: int
    work_x: int
    work_y: int
    work_width: int
    work_height: int
    monitor_id: str = ""
    dpi_x: int = 96
    dpi_y: int = 96
    primary: bool = False

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    @property
    def work_bounds(self) -> tuple[int, int, int, int]:
        return self.work_x, self.work_y, self.work_width, self.work_height

    @property
    def scale_percent(self) -> int:
        return max(100, int(round(self.dpi_y / 96 * 100)))


@dataclass(frozen=True)
class PanelMetrics:
    columns: int
    rows: int
    outer_padding: int
    cell_gap: int
    title_points: int
    value_points: int
    meta_points: int
    footer_points: int
    bar_height: int


def _display_number(device: str) -> int:
    suffix = str(device).upper().rsplit("DISPLAY", 1)[-1]
    try:
        return int(suffix)
    except (TypeError, ValueError):
        return 9999


def _monitor_label(monitor: MonitorInfo) -> str:
    number = _display_number(monitor.device)
    display = _tr(f"显示器 {number}", f'Monitor {number}') if number != 9999 else monitor.device
    primary = _tr("（主屏）", ' (primary)') if monitor.primary else ""
    return (
        f"{display} · {monitor.width}×{monitor.height} · "
        f"{monitor.scale_percent}%{primary}"
    )


def _monitor_hardware_key(monitor_id: str | None) -> str:
    """Stable model key; ignores the Windows instance suffix after rearranging."""
    parts = str(monitor_id or "").casefold().split("\\")
    return "\\".join(parts[:2]) if len(parts) >= 2 else str(monitor_id or "").casefold()


def _choose_panel_monitor(
    monitors: list[MonitorInfo],
    preferred_device: str | None,
    preferred_id: str | None = None,
) -> MonitorInfo | None:
    """Resolve the persisted panel, or pick the smallest secondary on first use.

    A missing persisted device deliberately returns ``None``. That makes a
    disconnected sensor panel fall back to the floating HUD instead of covering
    the main desktop full-screen. With no persisted choice, the smallest
    secondary is the safest AIDA64-style panel candidate.
    """
    if not monitors:
        return None
    if preferred_id:
        wanted_id = preferred_id.casefold()
        exact = next(
            (m for m in monitors if m.monitor_id and m.monitor_id.casefold() == wanted_id),
            None,
        )
        if exact is not None:
            return exact
        wanted_hardware = _monitor_hardware_key(preferred_id)
        hardware_match = next(
            (
                m
                for m in monitors
                if m.monitor_id and _monitor_hardware_key(m.monitor_id) == wanted_hardware
            ),
            None,
        )
        if hardware_match is not None:
            return hardware_match
    if preferred_device:
        wanted = preferred_device.casefold()
        return next((m for m in monitors if m.device.casefold() == wanted), None)
    if preferred_id:
        return None
    secondary = [m for m in monitors if not m.primary]
    return min(secondary or monitors, key=lambda m: (m.width * m.height, _display_number(m.device)))


def _monitor_at_point(
    monitors: list[MonitorInfo],
    x: int,
    y: int,
) -> MonitorInfo | None:
    return next(
        (
            m
            for m in monitors
            if m.x <= x < m.x + m.width and m.y <= y < m.y + m.height
        ),
        None,
    )


def _tk_position(x: int, y: int) -> str:
    x_part = f"+{x}" if x >= 0 else str(x)
    y_part = f"+{y}" if y >= 0 else str(y)
    return f"{x_part}{y_part}"


def _tk_geometry(width: int, height: int, x: int, y: int) -> str:
    """Build valid Tk geometry even when a display sits left/above the primary."""
    return f"{width}x{height}{_tk_position(x, y)}"


# Height, at 96 DPI, that one row of the tuned panel type needs incl. gaps.
PANEL_TUNED_ROW_PX = 200


def _panel_grid_shape(row_count: int, width: int, height: int) -> tuple[int, int]:
    count = max(1, int(row_count))
    columns = 2 if count >= 4 and width >= int(height * 1.15) else 1
    rows = (count + columns - 1) // columns
    return columns, rows


def _panel_layout_key(
    columns: int,
    grid_rows: int,
    cell_gap: int,
    bar_height: int,
    rows: Iterable[tuple[str, bool]],
) -> tuple[Any, ...]:
    """What forces the panel tiles to be rebuilt instead of updated in place.

    rows holds each tile's (title, has_usage_bar). Values, colors and reset
    countdowns are written to the existing labels, so they stay out of the
    key: rebuilding destroys every tile and the dedicated screen shows its
    black background for a frame.
    """
    return (columns, grid_rows, cell_gap, bar_height, tuple(rows))


def _panel_metrics(
    width: int,
    height: int,
    row_count: int,
    dpi: int = 96,
) -> PanelMetrics:
    columns, rows = _panel_grid_shape(row_count, width, height)
    dpi = max(72, int(dpi or 96))
    cell_height = max(72, int((height - max(48, height * 0.10)) / rows))
    # The type sizes below were tuned for three rows on a 640 px-tall panel.
    # Beyond the rows that height holds, shrink them together so extra rows
    # (e.g. Claude split into three) stay on screen instead of being clipped.
    fit_rows = max(1, int(height / (PANEL_TUNED_ROW_PX * dpi / 96)))
    scale = min(1.0, fit_rows / rows)

    def points(px: float, low: int, high: int) -> int:
        return max(low, min(high, int(round(px * 72 / dpi))))

    return PanelMetrics(
        columns=columns,
        rows=rows,
        outer_padding=max(16, min(36, int(round(min(width, height) * 0.035)))),
        cell_gap=max(10, min(24, int(round(min(width, height) * 0.022)))),
        # Fixed, user-tuned hierarchy for the dedicated low-resolution panel.
        title_points=max(12, int(round(28 * scale))),
        value_points=max(18, int(round(48 * scale))),
        meta_points=max(10, int(round(24 * scale))),
        footer_points=20,
        bar_height=max(6, min(10, int(round(cell_height * 0.045)))),
    )


def _panel_value_slot_width(font: Any, value_text: str) -> int:
    """Keep 100% stable, but don't clip dollar readouts like $2/200."""
    sample = value_text or "100%"
    return max(int(font.measure("100%")), int(font.measure(sample)))


def _enumerate_monitors() -> list[MonitorInfo]:
    if sys.platform != "win32":
        return [
            MonitorInfo(
                device="display-1",
                name="Display",
                x=0,
                y=0,
                width=1280,
                height=720,
                work_x=0,
                work_y=0,
                work_width=1280,
                work_height=720,
                primary=True,
            )
        ]
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shcore = ctypes.windll.shcore
        monitors: list[MonitorInfo] = []

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        monitor_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )

        def callback(hmon, _hdc, _rect, _lparam):
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                return True

            display = DISPLAY_DEVICEW()
            display.cb = ctypes.sizeof(display)
            friendly_name = ""
            try:
                if user32.EnumDisplayDevicesW(info.szDevice, 0, ctypes.byref(display), 0):
                    friendly_name = str(display.DeviceString or "")
            except Exception:
                pass

            dpi_x = ctypes.c_uint(96)
            dpi_y = ctypes.c_uint(96)
            try:
                shcore.GetDpiForMonitor(
                    hmon,
                    0,
                    ctypes.byref(dpi_x),
                    ctypes.byref(dpi_y),
                )
            except Exception:
                pass

            bounds = info.rcMonitor
            work = info.rcWork
            monitors.append(
                MonitorInfo(
                    device=str(info.szDevice),
                    name=friendly_name or str(info.szDevice),
                    x=int(bounds.left),
                    y=int(bounds.top),
                    width=int(bounds.right - bounds.left),
                    height=int(bounds.bottom - bounds.top),
                    work_x=int(work.left),
                    work_y=int(work.top),
                    work_width=int(work.right - work.left),
                    work_height=int(work.bottom - work.top),
                    monitor_id=str(display.DeviceID or ""),
                    dpi_x=int(dpi_x.value or 96),
                    dpi_y=int(dpi_y.value or 96),
                    primary=bool(info.dwFlags & 1),
                )
            )
            return True

        callback_ref = monitor_proc(callback)
        user32.EnumDisplayMonitors(None, None, callback_ref, 0)
        if monitors:
            return sorted(monitors, key=lambda m: (_display_number(m.device), m.x, m.y))
    except Exception:
        pass
    return [
        MonitorInfo(
            device="display-1",
            name="Display",
            x=0,
            y=0,
            width=1280,
            height=720,
            work_x=0,
            work_y=0,
            work_width=1280,
            work_height=720,
            primary=True,
        )
    ]


def _screen_bounds() -> list[tuple[int, int, int, int]]:
    return [monitor.work_bounds for monitor in _enumerate_monitors()]


def _clamp_position(x: int, y: int, w: int = 220, h: int = 100) -> tuple[int, int]:
    for mx, my, mw, mh in _screen_bounds():
        if mx <= x < mx + mw and my <= y < my + mh:
            return min(max(x, mx), mx + mw - w), min(max(y, my), my + mh - h)
    mx, my, mw, mh = _screen_bounds()[0]
    return mx + max(20, mw - w - 40), my + 48


def _hide_from_taskbar(root: Any) -> None:
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
    except Exception:
        pass


def run_ui() -> None:
    import tkinter as tk
    from tkinter import filedialog
    from tkinter import font as tkfont
    from tkinter import ttk

    _enable_dpi_awareness()

    cfg = load_config()
    font_size = int(cfg.get("font_size") or FONT_SIZE_DEFAULT)
    card_width = int(cfg.get("card_width") or CARD_WIDTH)
    card_height = cfg.get("card_height")  # None or int
    if card_height is not None:
        try:
            card_height = max(CARD_HEIGHT_MIN, min(CARD_HEIGHT_MAX, int(card_height)))
        except Exception:
            card_height = None
    allow_resize = bool(cfg.get("allow_resize", False))
    card_height = _effective_card_height(card_height, allow_resize)
    bg_opacity = _clamp_opacity(cfg.get("bg_opacity"), BG_OPACITY_DEFAULT)
    text_opacity = _clamp_opacity(cfg.get("text_opacity"), TEXT_OPACITY_DEFAULT)
    display_mode = str(cfg.get("display_mode") or DISPLAY_MODE_FLOAT)
    monitor_device = cfg.get("monitor_device")
    monitor_id = cfg.get("monitor_id")
    available_monitors = _enumerate_monitors()
    initial_panel_monitor = (
        _choose_panel_monitor(available_monitors, monitor_device, monitor_id)
        if display_mode == DISPLAY_MODE_PANEL
        else None
    )
    if display_mode == DISPLAY_MODE_PANEL and initial_panel_monitor:
        monitor_device = initial_panel_monitor.device
        monitor_id = initial_panel_monitor.monitor_id or monitor_id
        cfg["monitor_device"] = monitor_device
        cfg["monitor_id"] = monitor_id

    # Split plate vs text: bg_layer is translucent white; text layer uses
    # transparentcolor so glyphs stay solid (Windows). Fallback: single alpha.
    use_split_alpha = sys.platform == "win32"
    floating_ui_bg = TRANSPARENT_KEY if use_split_alpha else BG
    ui_bg = PANEL_BG if initial_panel_monitor is not None else floating_ui_bg

    root = tk.Tk()
    root.report_callback_exception = _report_tk_callback_exception
    root.title("UsageFloat")
    if _docs_shot_active():
        root.withdraw()
    if initial_panel_monitor is not None:
        try:
            root.tk.call("tk", "scaling", initial_panel_monitor.dpi_y / 72.0)
        except Exception:
            pass
    root.configure(bg=ui_bg)
    root.overrideredirect(True)  # no title bar / no system chrome
    root.attributes("-topmost", bool(cfg.get("always_on_top", True)))
    root.resizable(False, False)
    try:
        root.configure(highlightthickness=0, bd=0)
    except Exception:
        pass
    root.minsize(CARD_WIDTH_MIN, 40)
    try:
        # Floating mode is clamped in apply_geometry; panel mode may occupy a
        # complete 960×640 (or larger) auxiliary display.
        root.maxsize(10000, 10000)
    except Exception:
        pass

    bg_layer: Any = None
    if use_split_alpha:
        bg_layer = tk.Toplevel(root)
        bg_layer.overrideredirect(True)
        # The plate keeps the original background colour.  Only the content
        # window uses the chroma key, so text colours remain unchanged.
        bg_layer.configure(bg=BG, highlightthickness=0, bd=0)
        bg_layer.attributes("-topmost", bool(cfg.get("always_on_top", True)))
        try:
            # Text layer: only non-key pixels (glyphs) are drawn — fully opaque
            root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception:
            use_split_alpha = False
            ui_bg = BG
            root.configure(bg=ui_bg)
            try:
                bg_layer.destroy()
            except Exception:
                pass
            bg_layer = None

    layer_hwnds: dict[str, int] = {}

    def raise_popup_menu_above_hud() -> None:
        """Keep the native Win32 popup menu (#32768) above topmost HUD layers."""
        if not state.get("menu_open") or sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            find_window_ex = user32.FindWindowExW
            find_window_ex.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
            ]
            find_window_ex.restype = wintypes.HWND
            set_window_pos = user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            HWND_TOPMOST = -1
            flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            my_pid = os.getpid()
            hwnd: Any = None
            while True:
                hwnd = find_window_ex(None, hwnd, "#32768", None)
                if not hwnd:
                    break
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
                if int(pid.value) == my_pid and user32.IsWindowVisible(wintypes.HWND(hwnd)):
                    set_window_pos(
                        wintypes.HWND(hwnd),
                        wintypes.HWND(HWND_TOPMOST),
                        0,
                        0,
                        0,
                        0,
                        flags,
                    )
        except Exception:
            pass

    def stack_text_above_plate() -> None:
        """Keep the text window immediately above the plate on Windows."""
        if bg_layer is None or state.get("panel_active") or state.get("wallpaper_active"):
            if state.get("menu_open"):
                raise_popup_menu_above_hud()
            return
        try:
            import ctypes
            from ctypes import wintypes

            text_hwnd = layer_hwnds.get("text")
            plate_hwnd = layer_hwnds.get("plate")
            if not text_hwnd or not plate_hwnd:
                root.update_idletasks()
                text_hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                plate_hwnd = ctypes.windll.user32.GetParent(bg_layer.winfo_id()) or bg_layer.winfo_id()
                layer_hwnds["text"] = int(text_hwnd)
                layer_hwnds["plate"] = int(plate_hwnd)
                # The plate is visual only.  Never let a click activate it and
                # raise its translucent white surface over the text window.
                GWL_EXSTYLE = -20
                WS_EX_TOOLWINDOW = 0x00000080
                WS_EX_NOACTIVATE = 0x08000000
                plate_style = ctypes.windll.user32.GetWindowLongW(plate_hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    plate_hwnd,
                    GWL_EXSTYLE,
                    plate_style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                )
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            insert_after = HWND_TOPMOST if bool(state.get("always_on_top", True)) else HWND_NOTOPMOST
            flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            set_window_pos = ctypes.windll.user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            if state.get("menu_open"):
                # Keep plate behind text without re-inserting text/plate at
                # HWND_TOPMOST above the open #32768 popup menu.
                set_window_pos(
                    wintypes.HWND(plate_hwnd), wintypes.HWND(text_hwnd), 0, 0, 0, 0, flags
                )
                raise_popup_menu_above_hud()
                return
            # Put the plate in the requested band first, then the text. This
            # keeps both windows topmost while preserving their internal order.
            set_window_pos(
                wintypes.HWND(plate_hwnd), wintypes.HWND(insert_after), 0, 0, 0, 0, flags
            )
            set_window_pos(
                wintypes.HWND(text_hwnd), wintypes.HWND(insert_after), 0, 0, 0, 0, flags
            )
            # Alpha changes can remap a layered window and disturb the order.
            # Position the plate immediately behind the text layer explicitly,
            # without raising it above the settings window.
            set_window_pos(
                wintypes.HWND(plate_hwnd), wintypes.HWND(text_hwnd), 0, 0, 0, 0, flags
            )
        except Exception:
            if not state.get("menu_open"):
                try:
                    root.lift(bg_layer)
                except Exception:
                    pass

    def apply_opacity() -> None:
        if state.get("panel_active") or state.get("wallpaper_active"):
            # Dedicated mode is a single opaque surface. Besides maximizing
            # contrast, removing the translucent plate and clearing the
            # white chroma key prevents #FFFFFF pixels from becoming
            # click-through / drop-through holes on Windows.
            try:
                root.wm_attributes("-transparentcolor", "")
            except Exception:
                pass
            try:
                root.attributes("-alpha", 1.0)
            except Exception:
                pass
            if bg_layer is not None:
                try:
                    bg_layer.withdraw()
                except Exception:
                    pass
            return
        if bg_layer is not None:
            try:
                root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
            except Exception:
                pass
            try:
                bg_layer.deiconify()
            except Exception:
                pass
        _apply_layer_opacities(
            root,
            bg_layer,
            state.get("bg_opacity", bg_opacity),
            state.get("text_opacity", text_opacity),
            restack=stack_text_above_plate,
        )

    # Content root — width via geometry(); footer pinned to bottom so it
    # never covers the last provider row (e.g. grok) when height is tight.
    body = tk.Frame(root, bg=ui_bg, bd=0, padx=CARD_PADX, pady=3)
    body.pack(fill="both", expand=True)

    footer = tk.Frame(body, bg=ui_bg)
    # Pack footer FIRST with side=BOTTOM so it stays at window bottom
    footer.pack(side="bottom", fill="x", pady=0)

    lines_frame = tk.Frame(body, bg=ui_bg)
    lines_frame.pack(side="top", fill="both", expand=True, anchor="n")

    # mpv creates its own hardware-accelerated child inside this native HWND.
    # The host remains unpacked while the usage dashboard is active.
    wallpaper_host = tk.Frame(root, bg=PANEL_BG, bd=0, highlightthickness=0)
    wallpaper_status = tk.Label(
        wallpaper_host,
        text="",
        fg=PANEL_MUTED,
        bg=PANEL_BG,
        font=("Microsoft YaHei UI", 18, "bold"),
        justify="center",
    )
    wallpaper_status.place(relx=0.5, rely=0.5, anchor="center")

    # Resize grip (shown when 允许调整大小)
    grip = tk.Label(
        root,
        text="◢",
        fg=FG_MUTED,
        bg=ui_bg,
        cursor="size_nw_se",
        font=("Segoe UI", 8),
    )

    # YaHei UI renders Chinese + Latin crisply on Windows ClearType
    font_family = "Microsoft YaHei UI"
    try:
        if font_family.lower() not in {f.lower() for f in tkfont.families()}:
            font_family = "Segoe UI"
    except Exception:
        font_family = "Segoe UI"

    font_main = tkfont.Font(family=font_family, size=font_size)
    font_footer = tkfont.Font(family=font_family, size=max(FONT_SIZE_MIN, font_size - 1))
    font_grip = tkfont.Font(family="Segoe UI", size=max(8, font_size - 2))
    number_family = "Bahnschrift SemiBold"
    try:
        if number_family.lower() not in {f.lower() for f in tkfont.families()}:
            number_family = "Segoe UI Semibold"
    except Exception:
        number_family = "Segoe UI Semibold"
    initial_panel_metrics = _panel_metrics(
        initial_panel_monitor.width if initial_panel_monitor else 960,
        initial_panel_monitor.height if initial_panel_monitor else 640,
        5,
        initial_panel_monitor.dpi_y if initial_panel_monitor else 96,
    )
    font_panel_title = tkfont.Font(
        family=font_family,
        size=initial_panel_metrics.title_points,
        weight="bold",
    )
    font_panel_value = tkfont.Font(
        family=number_family,
        size=initial_panel_metrics.value_points,
        weight="bold",
    )
    font_panel_meta = tkfont.Font(
        family=font_family,
        size=initial_panel_metrics.meta_points,
        weight="bold",
    )
    font_panel_footer = tkfont.Font(
        family=font_family,
        size=initial_panel_metrics.footer_points,
        weight="bold",
    )
    grip.configure(font=font_grip)

    def _initial_row_metrics() -> tuple[int, int]:
        try:
            ls = int(font_main.metrics("linespace"))
        except Exception:
            ls = font_size + 8
        return _compact_row_height(ls, font_size), max(72, int(font_size * 6))

    _rh0, _rc0 = _initial_row_metrics()

    state: dict[str, Any] = {
        "language": _lang(cfg),
        "providers": [],
        "refreshing": False,
        "last_ok_at": None,
        "drag_ox": 0,
        "drag_oy": 0,
        "press_x": 0,
        "press_y": 0,
        "dragging": False,
        "always_on_top": bool(cfg.get("always_on_top", True)),
        "font_size": font_size,
        "card_width": card_width,
        "card_height": card_height,  # None = auto; int = user override (min content)
        "allow_resize": allow_resize,
        "display_mode": display_mode,
        "monitor_device": monitor_device,
        "monitor_id": monitor_id,
        "panel_monitor": initial_panel_monitor,
        "panel_active": display_mode == DISPLAY_MODE_PANEL and initial_panel_monitor is not None,
        "monitor_signature": None,
        "tk_dpi": initial_panel_monitor.dpi_y if initial_panel_monitor else None,
        "panel_rect_job": None,
        "floating_x": cfg.get("x"),
        "floating_y": cfg.get("y"),
        "bg_opacity": bg_opacity,
        "text_opacity": text_opacity,
        "row_height": _rh0,
        "right_col_w": _rc0,
        "settings_win": None,
        "settings_open_job": None,
        "menu_open": False,
        "resize_active": False,
        "resize_start_w": 0,
        "resize_start_h": 0,
        "resize_start_x": 0,
        "resize_start_y": 0,
        "rendering": False,
        "render_job": None,
        "refresh_control": None,
        "refresh_hitbox": None,
        "reset_click_targets": [],
        "reset_click_widgets": [],
        "reset_hitboxes": [],
        "reset_hover": False,
        "refresh_hover": False,
        "reset_cards_win": None,
        "claude_reset_win": None,
        "claude_reset_account": None,
        "claude_session_win": None,
        "claude_session_account": None,
        "claude_session_busy": False,
        "claude_reset_busy": False,
        # (account_id, grant_id, request_id) of a claim whose outcome never arrived.
        "claude_reset_unsettled": None,
        "reset_cards_account": None,
        "reset_cards_busy": False,
        "panel_structure": None,
        "panel_items": [],
        "panel_empty_label": None,
        "double_ctrl_toggle": bool(cfg.get("shortcut_enabled", cfg.get("double_ctrl_toggle", True))),
        "shortcut_enabled": bool(cfg.get("shortcut_enabled", cfg.get("double_ctrl_toggle", True))),
        "shortcut_mode": _normalize_shortcut_mode(cfg.get("shortcut_mode")),
        "shortcut_key": _normalize_shortcut_key(cfg.get("shortcut_key")),
        "shortcut_repeat_count": _normalize_shortcut_repeat_count(
            cfg.get("shortcut_repeat_count")
        ),
        "shortcut_combo": _normalize_shortcut_combo(cfg.get("shortcut_combo")),
        "llm_proxy_timezone": _normalize_llm_proxy_timezone(
            cfg.get("llm_proxy_timezone")
        ),
        "double_ctrl_job": None,
        "wallpaper_active": False,
        "wallpaper_folder": cfg.get("wallpaper_folder"),
        "wallpaper_folders": list(cfg.get("wallpaper_folders") or []),
        "wallpaper_disabled_folders": list(
            cfg.get("wallpaper_disabled_folders") or []
        ),
        "wallpaper_folder_depths": dict(
            cfg.get("wallpaper_folder_depths") or {}
        ),
        "wallpaper_subdir_weights": dict(cfg.get("wallpaper_subdir_weights") or {}),
        "wallpaper_index_cache": load_wallpaper_index_cache(),
        "wallpaper_scanning_folders": set(),
        "wallpaper_scan_callback": None,
        "wallpaper_random_mode": _normalize_wallpaper_random_mode(
            cfg.get("wallpaper_random_mode")
        ),
        "wallpaper_image_seconds": int(
            cfg.get("wallpaper_image_seconds") or WALLPAPER_IMAGE_SECONDS
        ),
        "wallpaper_audio": bool(cfg.get("wallpaper_audio", True)),
        "wallpaper_check_job": None,
        "wallpaper_ready_job": None,
        "wallpaper_background_job": None,
        "wallpaper_unpause_job": None,
        "wallpaper_preparing": False,
        "wallpaper_ready": False,
        "wallpaper_child_seen_at": None,
        "wallpaper_child_hwnd": 0,
        "wallpaper_prepare_started": None,
        "wallpaper_prepare_error": "",
        "wallpaper_dropped_video": None,
        "wallpaper_dropped_url": None,
        "wallpaper_web_mode": False,
        "wallpaper_drop_retry_job": None,
        "wallpaper_drop_cleanup": None,
        "wallpaper_bg_drop_cleanup": None,
    }
    if _docs_shot_active():
        shot_cache: dict[str, dict[str, Any]] = {}
        if os.environ.get("USAGE_FLOAT_DOCS_SHOT_ROOT"):
            for folder in list(state.get("wallpaper_folders") or []):
                key = _wallpaper_path_key(folder)
                shot_cache[key] = {
                    "folder": folder,
                    "files": [str(p) for p in _collect_wallpaper_media(folder)],
                    "scanned_at": time.time(),
                }
        else:
            demo_root = r"D:\Wallpapers\△"
            demo_long = r"D:\Media\PlayStation\Library\Archive\2026\Collections\□"
            shot_cache[_wallpaper_path_key(demo_root)] = {
                "folder": demo_root,
                "files": [
                    demo_root + r"\0.png",
                    demo_root + r"\□\0.png",
                    demo_root + r"\□\1.png",
                    demo_root + r"\□\2.png",
                    demo_root + r"\○\0.png",
                    demo_root + r"\○\1.png",
                    demo_root + r"\○\2.png",
                    demo_root + r"\✕\0.png",
                    demo_root + r"\✕\1.png",
                ],
                "scanned_at": time.time(),
            }
            shot_cache[_wallpaper_path_key(demo_long)] = {
                "folder": demo_long,
                "files": [
                    f"{demo_long}\\Scene_{i // 6}\\clip_{i:02d}.mp4"
                    for i in range(18)
                ],
                "scanned_at": time.time(),
            }
        state["wallpaper_index_cache"] = shot_cache

    repeat_detector = _RepeatKeyDetector(
        tap_count=_normalize_shortcut_repeat_count(cfg.get("shortcut_repeat_count"))
    )
    combo_detector = _ComboKeyDetector()
    wallpaper_player = _MpvWallpaperPlayer()
    browser_wallpaper_player = _BrowserWebVideoPlayer()
    get_async_key_state: Any = None
    if sys.platform == "win32":
        try:
            import ctypes

            get_async_key_state = ctypes.windll.user32.GetAsyncKeyState
            get_async_key_state.argtypes = [ctypes.c_int]
            get_async_key_state.restype = ctypes.c_short
        except Exception:
            get_async_key_state = None

    def apply_mode_surface() -> None:
        nonlocal ui_bg
        panel_mode_active = bool(state.get("panel_active"))
        ui_bg = PANEL_BG if panel_mode_active else floating_ui_bg
        try:
            root.configure(bg=ui_bg)
            body.configure(bg=ui_bg)
            footer.configure(bg=ui_bg)
            lines_frame.configure(bg=ui_bg)
            grip.configure(bg=ui_bg)
            control = state.get("refresh_control")
            if control is not None and control.winfo_exists():
                control.configure(
                    bg=ui_bg,
                    activebackground=ui_bg,
                    fg=PANEL_MUTED if panel_mode_active else FG_MUTED,
                    activeforeground=PANEL_FG if panel_mode_active else FG_LABEL,
                )
        except Exception:
            pass
        apply_opacity()

    def enforce_native_panel_rect() -> None:
        """Pin the opaque panel in physical pixels across mixed-DPI layouts.

        Tk geometry coordinates can be virtualized while Windows is applying a
        new display arrangement. SetWindowPos on the native wrapper keeps the
        selected physical monitor authoritative.
        """
        state["panel_rect_job"] = None
        panel: MonitorInfo | None = state.get("panel_monitor")
        if not state.get("panel_active") or panel is None or sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            insert_after = -1 if bool(state.get("always_on_top", True)) else -2
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            set_window_pos = ctypes.windll.user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            set_window_pos(
                wintypes.HWND(hwnd),
                wintypes.HWND(insert_after),
                panel.x,
                panel.y,
                panel.width,
                panel.height,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            if state.get("menu_open"):
                raise_popup_menu_above_hud()
        except Exception:
            pass

    def schedule_native_panel_rect(delay_ms: int = 80) -> None:
        previous = state.get("panel_rect_job")
        if previous is not None:
            try:
                root.after_cancel(previous)
            except Exception:
                pass
        state["panel_rect_job"] = root.after(delay_ms, enforce_native_panel_rect)

    def apply_shortcut_config(
        *,
        enabled: bool | None = None,
        mode: str | None = None,
        key: str | None = None,
        repeat_count: int | None = None,
        combo: list[str] | None = None,
        persist: bool = True,
    ) -> None:
        if enabled is not None:
            state["shortcut_enabled"] = bool(enabled)
            state["double_ctrl_toggle"] = bool(enabled)
        if mode is not None:
            state["shortcut_mode"] = _normalize_shortcut_mode(mode)
        if key is not None:
            state["shortcut_key"] = _normalize_shortcut_key(key)
        if repeat_count is not None:
            state["shortcut_repeat_count"] = _normalize_shortcut_repeat_count(repeat_count)
        if combo is not None:
            state["shortcut_combo"] = _normalize_shortcut_combo(combo)
        repeat_detector.tap_count = int(state["shortcut_repeat_count"])
        repeat_detector.reset()
        combo_detector.reset()
        enabled_now = bool(state.get("shortcut_enabled"))
        if not enabled_now and state.get("wallpaper_preparing") and not state.get("wallpaper_active"):
            _clear_wallpaper_state()
        elif enabled_now:
            _schedule_background_wallpaper_prepare()
        cfg["shortcut_enabled"] = enabled_now
        cfg["double_ctrl_toggle"] = enabled_now
        cfg["shortcut_mode"] = str(state["shortcut_mode"])
        cfg["shortcut_key"] = str(state["shortcut_key"])
        cfg["shortcut_repeat_count"] = int(state["shortcut_repeat_count"])
        cfg["shortcut_combo"] = list(state["shortcut_combo"])
        if persist:
            save_config(cfg)

    def set_double_ctrl_toggle(enabled: bool, *, persist: bool = True) -> None:
        apply_shortcut_config(enabled=enabled, persist=persist)

    def _cancel_wallpaper_check() -> None:
        job = state.get("wallpaper_check_job")
        state["wallpaper_check_job"] = None
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:
                pass

    def _cancel_wallpaper_prepare_jobs() -> None:
        for key in (
            "wallpaper_ready_job",
            "wallpaper_background_job",
            "wallpaper_unpause_job",
            "wallpaper_drop_retry_job",
        ):
            job = state.get(key)
            state[key] = None
            if job is not None:
                try:
                    root.after_cancel(job)
                except Exception:
                    pass

    def _clear_wallpaper_state(*, stop_player: bool = True) -> None:
        _cancel_wallpaper_prepare_jobs()
        _cancel_wallpaper_check()
        state["wallpaper_preparing"] = False
        state["wallpaper_ready"] = False
        state["wallpaper_web_mode"] = False
        state["wallpaper_child_seen_at"] = None
        state["wallpaper_child_hwnd"] = 0
        state["wallpaper_prepare_started"] = None
        state["wallpaper_prepare_error"] = ""
        if stop_player:
            wallpaper_player.stop()
            browser_wallpaper_player.stop()
        wallpaper_host.place_forget()

    def _show_wallpaper_error(message: str) -> None:
        state["wallpaper_active"] = True
        state["wallpaper_preparing"] = False
        state["wallpaper_ready"] = False
        state["wallpaper_prepare_error"] = message
        apply_opacity()
        body.pack_forget()
        wallpaper_host.place(x=0, y=0, relwidth=1, relheight=1)
        root.update_idletasks()
        _move_native_child_window(
            wallpaper_host.winfo_id(),
            0,
            0,
            root.winfo_width(),
            root.winfo_height(),
        )
        wallpaper_host.lift()
        wallpaper_status.configure(text=message)
        wallpaper_status.place(relx=0.5, rely=0.5, anchor="center")
        wallpaper_status.lift()

    def _check_wallpaper_player() -> None:
        state["wallpaper_check_job"] = None
        if not state.get("wallpaper_active"):
            return
        if state.get("wallpaper_web_mode"):
            process = browser_wallpaper_player.process
            if process is None or process.poll() is not None:
                wallpaper_status.configure(
                    text=_tr("网页视频播放后端已退出\n请检查网页链接或浏览器安装", 'Web video player exited\nCheck the URL or browser installation')
                )
                wallpaper_status.place(relx=0.5, rely=0.5, anchor="center")
                wallpaper_status.lift()
                return
            browser_wallpaper_player.poll_window(
                wallpaper_host.winfo_id(),
                max(1, root.winfo_width()),
                max(1, root.winfo_height()),
                offscreen=False,
            )
            state["wallpaper_check_job"] = root.after(1000, _check_wallpaper_player)
            return
        process = wallpaper_player.process
        if process is not None and process.poll() is not None:
            wallpaper_status.configure(
                text=_tr("动态壁纸播放器已退出\n请检查媒体格式或 mpv 安装", 'Live wallpaper player exited\nCheck the media format or mpv installation')
            )
            wallpaper_status.place(relx=0.5, rely=0.5, anchor="center")
            wallpaper_status.lift()
            return
        state["wallpaper_check_job"] = root.after(1000, _check_wallpaper_player)

    def _finish_wallpaper_reveal() -> None:
        state["wallpaper_unpause_job"] = None
        if not state.get("wallpaper_active"):
            return
        web_mode = bool(state.get("wallpaper_web_mode"))
        if web_mode:
            if not browser_wallpaper_player.running:
                return
        elif not wallpaper_player.running:
            return
        _cancel_wallpaper_prepare_jobs()
        state["wallpaper_preparing"] = False
        apply_opacity()
        wallpaper_status.place_forget()
        wallpaper_host.place(x=0, y=0, width=0, height=0, relwidth=1, relheight=1)
        root.update_idletasks()
        host_w = max(1, root.winfo_width())
        host_h = max(1, root.winfo_height())
        _move_native_child_window(
            wallpaper_host.winfo_id(),
            0,
            0,
            host_w,
            host_h,
        )
        if web_mode:
            browser_wallpaper_player.poll_window(
                wallpaper_host.winfo_id(),
                host_w,
                host_h,
                offscreen=False,
            )
            browser_wallpaper_player.set_muted(not bool(state.get("wallpaper_audio", True)))
            root.after(
                90,
                lambda: browser_wallpaper_player.poll_window(
                    wallpaper_host.winfo_id(),
                    max(1, root.winfo_width()),
                    max(1, root.winfo_height()),
                    offscreen=False,
                )
                if state.get("wallpaper_active") and state.get("wallpaper_web_mode")
                else None,
            )
        else:
            if state.get("wallpaper_audio"):
                wallpaper_player.set_muted(False)
        body.pack_forget()
        wallpaper_host.lift()
        state["wallpaper_check_job"] = root.after(1000, _check_wallpaper_player)

    def _reveal_ready_wallpaper() -> None:
        if not state.get("wallpaper_active") or not state.get("wallpaper_ready"):
            return
        if state.get("wallpaper_unpause_job") is not None:
            return
        if state.get("wallpaper_web_mode"):
            browser_wallpaper_player.set_paused(False)
            _finish_wallpaper_reveal()
            return
        if not wallpaper_player.set_paused(False):
            def retry_reveal() -> None:
                state["wallpaper_unpause_job"] = None
                _reveal_ready_wallpaper()

            state["wallpaper_unpause_job"] = root.after(
                40,
                retry_reveal,
            )
            return
        first_is_video = bool(
            wallpaper_player.last_order
            and wallpaper_player.last_order[0].suffix.casefold()
            in WALLPAPER_VIDEO_EXTENSIONS
        )
        if first_is_video:
            # Start decoding/motion while the prepared HWND is still clipped.
            # The usage panel remains visible until the short preroll completes.
            state["wallpaper_unpause_job"] = root.after(
                WALLPAPER_VIDEO_PREROLL_MS,
                _finish_wallpaper_reveal,
            )
        else:
            _finish_wallpaper_reveal()

    def _poll_wallpaper_ready() -> None:
        state["wallpaper_ready_job"] = None
        if not state.get("wallpaper_preparing"):
            return
        if state.get("wallpaper_web_mode"):
            process = browser_wallpaper_player.process
            if process is None or process.poll() is not None:
                message = _tr("网页视频播放后端启动失败\n请检查链接或浏览器安装", 'Web video player failed to start\nCheck the URL or browser installation')
                if state.get("wallpaper_active"):
                    _show_wallpaper_error(message)
                else:
                    _clear_wallpaper_state()
                return
            now = time.monotonic()
            host_w = max(1, root.winfo_width())
            host_h = max(1, root.winfo_height())
            child = browser_wallpaper_player.poll_window(
                wallpaper_host.winfo_id(),
                host_w,
                host_h,
                offscreen=True,
            )
            if child:
                state["wallpaper_child_hwnd"] = child
                if browser_wallpaper_player.is_ready():
                    state["wallpaper_ready"] = True
                    if state.get("wallpaper_active"):
                        _reveal_ready_wallpaper()
                    return
            started = state.get("wallpaper_prepare_started")
            if state.get("wallpaper_active") and started is not None and now - float(started) > 15.0:
                browser_wallpaper_player.stop()
                _show_wallpaper_error(_tr("网页视频加载超时\n请检查网络连接或链接内容", 'Web video loading timed out\nCheck your connection or URL'))
                return
            state["wallpaper_ready_job"] = root.after(
                WALLPAPER_READY_POLL_MS,
                _poll_wallpaper_ready,
            )
            return

        process = wallpaper_player.process
        if process is None or process.poll() is not None:
            message = _tr("动态壁纸播放器启动失败\n请检查媒体格式或 mpv 安装", 'Live wallpaper player failed to start\nCheck the media format or mpv installation')
            if state.get("wallpaper_active"):
                _show_wallpaper_error(message)
            else:
                _clear_wallpaper_state()
            return

        now = time.monotonic()
        child = _find_embedded_mpv_window(wallpaper_host.winfo_id())
        child_seen_at = state.get("wallpaper_child_seen_at")
        if child:
            state["wallpaper_child_hwnd"] = child
            # mpv is hosted by a Tk HWND owned by another process. Let it render,
            # but route pointer/keyboard/drop input to the Tk parent so focus and
            # Explorer drops work reliably across the process boundary.
            _set_native_window_enabled(child, False)
            if child_seen_at is None:
                state["wallpaper_child_seen_at"] = now
            elif now - float(child_seen_at) >= WALLPAPER_FIRST_FRAME_SETTLE_SECONDS:
                state["wallpaper_ready"] = True
                if state.get("wallpaper_active"):
                    _reveal_ready_wallpaper()
                return

        started = state.get("wallpaper_prepare_started")
        if state.get("wallpaper_active") and started is not None and now - float(started) > 4.0:
            wallpaper_player.stop()
            _show_wallpaper_error(_tr("动态壁纸首帧加载超时\n请检查媒体文件", 'Live wallpaper first frame timed out\nCheck the media file'))
            return
        state["wallpaper_ready_job"] = root.after(
            WALLPAPER_READY_POLL_MS,
            _poll_wallpaper_ready,
        )

    def _ensure_wallpaper_folders_indexed_async(
        folders_to_scan: list[str] | tuple[str, ...],
        *,
        force_rescan: bool = False,
        notify_start: bool = True,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        normalized = _normalize_wallpaper_folders(folders_to_scan)
        cache: dict[str, dict[str, Any]] = state.setdefault("wallpaper_index_cache", {})
        scanning: set[str] = state.setdefault("wallpaper_scanning_folders", set())
        pending: list[tuple[str, str]] = []
        for folder in normalized:
            key = _wallpaper_path_key(folder)
            if key in scanning:
                continue
            if not force_rescan and key in cache and isinstance(cache[key].get("files"), list):
                continue
            scanning.add(key)
            pending.append((key, folder))
        if not pending:
            if on_done is not None:
                on_done()
            return
        if notify_start:
            cb = state.get("wallpaper_scan_callback")
            if callable(cb):
                try:
                    cb()
                except Exception:
                    pass

        def _worker() -> None:
            for key, folder in pending:
                scanned_files = _scan_wallpaper_folder_slow(folder)
                entry = {
                    "folder": folder,
                    "files": [str(p) for p in scanned_files],
                    "scanned_at": time.time(),
                }

                def _apply_one(k: str = key, ent: dict[str, Any] = entry) -> None:
                    cache[k] = ent
                    scanning.discard(k)
                    save_wallpaper_index_cache(cache)
                    ui_cb = state.get("wallpaper_scan_callback")
                    if callable(ui_cb):
                        try:
                            ui_cb()
                        except Exception:
                            pass
                    if not scanning:
                        if on_done is not None:
                            try:
                                on_done()
                            except Exception:
                                pass
                        if not state.get("wallpaper_active"):
                            _clear_wallpaper_state()
                            _schedule_background_wallpaper_prepare()

                try:
                    root.after(0, _apply_one)
                except Exception:
                    return

        threading.Thread(
            target=_worker,
            daemon=True,
            name="wallpaper-slow-scan",
        ).start()

    def _prepare_wallpaper(*, commit: bool) -> bool:
        dropped_url = str(state.get("wallpaper_dropped_url") or "").strip() or None
        dropped_video = state.get("wallpaper_dropped_video")
        if not isinstance(dropped_video, Path) or not dropped_video.is_file():
            dropped_video = None
        if not state.get("panel_active") and dropped_url is None and dropped_video is None:
            return False
        if state.get("wallpaper_preparing"):
            # A drop must not reveal the already-preloaded random item.
            if (
                dropped_url is not None
                or dropped_video is not None
                or (
                    not wallpaper_player.running
                    and not browser_wallpaper_player.running
                )
            ):
                _clear_wallpaper_state()
                return _prepare_wallpaper(commit=commit)
            if commit:
                state["wallpaper_active"] = True
                if state.get("wallpaper_ready"):
                    _reveal_ready_wallpaper()
            return True

        if dropped_url is not None:
            browser_exe = _find_browser_executable(cfg.get("browser_path"))
            if browser_exe is None:
                error = _tr("未找到 Edge / Chrome 浏览器\n无法模拟浏览器播放网页视频", 'Edge / Chrome browser not found\nCannot play web video')
                state["wallpaper_prepare_error"] = error
                if commit:
                    _show_wallpaper_error(error)
                return True
            state["wallpaper_active"] = bool(commit)
            state["wallpaper_preparing"] = True
            state["wallpaper_ready"] = False
            state["wallpaper_web_mode"] = True
            state["wallpaper_child_seen_at"] = None
            state["wallpaper_prepare_started"] = time.monotonic()
            state["wallpaper_prepare_error"] = ""
            apply_opacity()
            host_width = max(1, root.winfo_width())
            host_height = max(1, root.winfo_height())
            body.pack_forget()
            wallpaper_host.place(x=0, y=0, width=0, height=0, relwidth=1, relheight=1)
            root.update_idletasks()
            _move_native_child_window(
                wallpaper_host.winfo_id(),
                0,
                0,
                host_width,
                host_height,
            )
            wallpaper_host.lift()
            wallpaper_status.configure(text=_tr("正在加载网页视频…", 'Loading web video…'))
            wallpaper_status.place(relx=0.5, rely=0.5, anchor="center")
            wallpaper_status.lift()
            ok, error = browser_wallpaper_player.start(
                browser_exe,
                wallpaper_host.winfo_id(),
                dropped_url,
                width=host_width,
                height=host_height,
                audio_enabled=bool(state.get("wallpaper_audio", True)),
            )
            state["wallpaper_dropped_url"] = None
            if not ok:
                state["wallpaper_preparing"] = False
                state["wallpaper_web_mode"] = False
                if commit:
                    _show_wallpaper_error(error)
                else:
                    _clear_wallpaper_state()
                return True
            state["wallpaper_ready_job"] = root.after(
                WALLPAPER_READY_POLL_MS,
                _poll_wallpaper_ready,
            )
            return True

        folders = _enabled_wallpaper_folders(
            list(state.get("wallpaper_folders") or []),
            state.get("wallpaper_disabled_folders"),
        )
        loop_single_video = dropped_video is not None
        keep_order = loop_single_video
        if loop_single_video:
            files = [dropped_video]
        else:
            cached_files, uncached_folders = _cached_wallpaper_media_for_folders(
                folders,
                state.get("wallpaper_index_cache"),
            )
            if uncached_folders:
                _ensure_wallpaper_folders_indexed_async(uncached_folders)
            if cached_files:
                files = cached_files
            elif uncached_folders and not commit:
                return False
            else:
                files = _collect_wallpaper_media_folders(folders)
        executable = _find_mpv_executable(cfg.get("mpv_path"))
        error = ""
        if not folders and dropped_video is None:
            error = _tr("请至少启用一个动态壁纸目录", 'Enable at least one live wallpaper folder')
        elif not files:
            error = _tr("所选文件夹中没有支持的图片或视频", 'No supported images or videos in the selected folders')
        elif executable is None:
            error = _tr("未找到 mpv 播放后端\n请运行 install-mpv.ps1", 'mpv playback backend not found\nPlease run install-mpv.ps1')
        if error:
            state["wallpaper_prepare_error"] = error
            if commit:
                _show_wallpaper_error(error)
            return True

        state["wallpaper_active"] = bool(commit)
        state["wallpaper_preparing"] = True
        state["wallpaper_ready"] = False
        state["wallpaper_child_seen_at"] = None
        state["wallpaper_prepare_started"] = time.monotonic()
        state["wallpaper_prepare_error"] = ""
        wallpaper_status.place_forget()
        # A native mpv child does not obey Tk's sibling stacking reliably.
        # Keep its full-size host beyond the root's clipped area while it
        # decodes the first frame, then move the host on-screen atomically.
        host_width = max(1, root.winfo_width())
        host_height = max(1, root.winfo_height())
        wallpaper_host.place(
            x=host_width + 64,
            y=host_height + 64,
            width=host_width,
            height=host_height,
        )
        root.update_idletasks()
        _move_native_child_window(
            wallpaper_host.winfo_id(),
            host_width + 64,
            host_height + 64,
            host_width,
            host_height,
        )

        ok, error = wallpaper_player.start(
            executable,
            wallpaper_host.winfo_id(),
            files,
            image_seconds=int(state["wallpaper_image_seconds"]),
            audio_enabled=bool(state.get("wallpaper_audio", True)),
            folder_weights=dict(state.get("wallpaper_subdir_weights") or {}),
            random_mode=str(state.get("wallpaper_random_mode")),
            root_folders=folders,
            folder_depths=dict(state.get("wallpaper_folder_depths") or {}),
            preferred_first=dropped_video,
            keep_order=keep_order,
            loop_file=loop_single_video,
        )
        if not ok:
            state["wallpaper_preparing"] = False
            if commit:
                _show_wallpaper_error(error)
            else:
                _clear_wallpaper_state()
            return True

        if dropped_video is not None:
            state["wallpaper_dropped_video"] = None

        state["wallpaper_ready_job"] = root.after(
            WALLPAPER_READY_POLL_MS,
            _poll_wallpaper_ready,
        )
        return True

    def _schedule_background_wallpaper_prepare(delay_ms: int = 80) -> None:
        job = state.get("wallpaper_background_job")
        state["wallpaper_background_job"] = None
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:
                pass
        if (
            not state.get("double_ctrl_toggle")
            or not state.get("panel_active")
            or state.get("wallpaper_active")
            or state.get("wallpaper_preparing")
            or not _enabled_wallpaper_folders(
                list(state.get("wallpaper_folders") or []),
                state.get("wallpaper_disabled_folders"),
            )
        ):
            return

        def prepare() -> None:
            state["wallpaper_background_job"] = None
            if not state.get("wallpaper_active"):
                _prepare_wallpaper(commit=False)

        state["wallpaper_background_job"] = root.after(delay_ms, prepare)

    def set_wallpaper_active(active: bool) -> bool:
        active = bool(active)
        if active and state.get("wallpaper_active"):
            return True

        if not active:
            state["wallpaper_active"] = False
            apply_opacity()
            if not body.winfo_manager():
                body.pack(fill="both", expand=True)
            body.lift()
            render(relayout=False)
            root.update_idletasks()
            _clear_wallpaper_state()
            _schedule_background_wallpaper_prepare()
            return True
        return _prepare_wallpaper(commit=True)

    def play_dropped_video(path: Path) -> None:
        state["wallpaper_drop_retry_job"] = None
        try:
            state["wallpaper_dropped_url"] = None
            state["wallpaper_dropped_video"] = path
            # The usage panel keeps a random session preloaded off-screen.
            # Revealing that session, or insert-next-play on top of it, plays
            # the preloaded file instead of the one that was just dropped.
            if (
                state.get("wallpaper_active")
                or state.get("wallpaper_preparing")
                or wallpaper_player.running
                or browser_wallpaper_player.running
            ):
                _clear_wallpaper_state()
                state["wallpaper_active"] = False
            set_wallpaper_active(True)
        except Exception:
            _log_exception("dropped video")
            try:
                _show_wallpaper_error(_tr("拖入视频播放失败\n请检查视频格式", 'Could not play dropped video\nCheck the video format'))
            except Exception:
                pass

    def play_dropped_webpage(url: str) -> None:
        state["wallpaper_drop_retry_job"] = None
        try:
            state["wallpaper_dropped_video"] = None
            state["wallpaper_dropped_url"] = url
            if (
                state.get("wallpaper_active")
                or state.get("wallpaper_preparing")
                or wallpaper_player.running
                or browser_wallpaper_player.running
            ):
                _clear_wallpaper_state()
                state["wallpaper_active"] = False
            set_wallpaper_active(True)
        except Exception:
            _log_exception("dropped webpage")
            try:
                _show_wallpaper_error(_tr("拖入网页播放失败\n请检查链接格式", 'Could not play dropped web URL\nCheck the URL format'))
            except Exception:
                pass

    def handle_dropped_files(paths: list[str]) -> None:
        try:
            videos = _dropped_wallpaper_videos(paths)
            urls = [] if videos else _dropped_wallpaper_urls(paths)
            if not videos and not urls:
                return
            retry_job = state.get("wallpaper_drop_retry_job")
            if retry_job is not None:
                try:
                    root.after_cancel(retry_job)
                except Exception:
                    pass
                state["wallpaper_drop_retry_job"] = None
            if videos:
                play_dropped_video(videos[0])
            else:
                play_dropped_webpage(urls[0])
        except Exception:
            _log_exception("dropped video")

    def paste_wallpaper_from_clipboard(_event: Any = None) -> str | None:
        try:
            clip_text = root.clipboard_get()
        except Exception:
            clip_text = ""
        cleaned = str(clip_text or "").strip()
        if not cleaned:
            return None
        handle_dropped_files([cleaned])
        return "break"

    state["wallpaper_drop_cleanup"] = _install_windows_file_drop(
        root,
        handle_dropped_files,
    )
    if bg_layer is not None:
        state["wallpaper_bg_drop_cleanup"] = _install_windows_file_drop(
            bg_layer,
            handle_dropped_files,
        )

    def focus_wallpaper_controls(_event: Any = None) -> None:
        if not state.get("wallpaper_active"):
            return
        try:
            root.focus_force()
        except Exception:
            pass

    def on_wallpaper_arrow(key: str) -> str | None:
        if not state.get("wallpaper_active"):
            return None
        if state.get("wallpaper_web_mode") and browser_wallpaper_player.running:
            browser_wallpaper_player.seek(-5.0 if key.upper() == "LEFT" else 5.0)
            return "break"
        if not wallpaper_player.running:
            return None
        _send_mpv_ipc_command(wallpaper_player.ipc_pipe, ["keypress", key])
        return "break"

    def on_wallpaper_wheel(event: Any) -> str | None:
        if not state.get("wallpaper_active"):
            return None
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return None
        volume_step = 5 if delta > 0 else -5
        if state.get("wallpaper_web_mode") and browser_wallpaper_player.running:
            browser_wallpaper_player.adjust_volume(volume_step)
            return "break"
        if not wallpaper_player.running:
            return None
        _send_mpv_ipc_command(
            wallpaper_player.ipc_pipe,
            ["add", "volume", volume_step],
        )
        return "break"

    root.bind("<Button-1>", focus_wallpaper_controls, add="+")
    root.bind("<Left>", lambda _event: on_wallpaper_arrow("LEFT"), add="+")
    root.bind("<Right>", lambda _event: on_wallpaper_arrow("RIGHT"), add="+")
    root.bind("<MouseWheel>", on_wallpaper_wheel, add="+")

    def toggle_wallpaper_mode() -> None:
        set_wallpaper_active(not bool(state.get("wallpaper_active")))

    browser_window_drop_tracker = _BrowserWindowDropTracker()

    def poll_browser_window_drop() -> None:
        if sys.platform != "win32" or get_async_key_state is None:
            return
        if state.get("resize_active") or state.get("drag_active"):
            browser_window_drop_tracker.reset()
            return
        try:
            lbutton_down = bool(get_async_key_state(0x01) & 0x8000)
            if not lbutton_down and not browser_window_drop_tracker.active:
                return
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            pt = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                return
            cursor_pos = (int(pt.x), int(pt.y))
            if lbutton_down:
                fg_hwnd = int(user32.GetForegroundWindow() or 0)
                own_hwnds = {
                    int(layer_hwnds.get("text") or 0),
                    int(layer_hwnds.get("plate") or 0),
                    int(root.winfo_id() or 0),
                    int(browser_wallpaper_player.browser_hwnd or 0),
                }
                is_browser = _is_external_browser_window(
                    fg_hwnd, exclude_hwnds=own_hwnds
                )
                win_rect = _get_window_rect(fg_hwnd) if is_browser else None
                browser_window_drop_tracker.update_down(
                    fg_hwnd=fg_hwnd,
                    is_browser=is_browser,
                    window_rect=win_rect,
                    cursor_pos=cursor_pos,
                )
                return
            target_rect = (
                int(root.winfo_rootx()),
                int(root.winfo_rooty()),
                int(root.winfo_rootx() + root.winfo_width()),
                int(root.winfo_rooty() + root.winfo_height()),
            )
            dropped = browser_window_drop_tracker.finish_up(
                cursor_pos=cursor_pos,
                target_rect=target_rect,
            )
            if dropped is None:
                return
            dropped_hwnd, start_rect, start_outside = dropped

            def _resolve_and_play() -> None:
                url = _extract_browser_window_url(dropped_hwnd)
                if url:
                    _close_native_window(dropped_hwnd)
                    try:
                        root.after(0, lambda u=url: handle_dropped_files([u]))
                    except Exception:
                        pass
                elif start_outside and start_rect is not None:
                    try:
                        SWP_NOSIZE = 0x0001
                        SWP_NOZORDER = 0x0004
                        SWP_NOACTIVATE = 0x0010
                        SWP_ASYNCWINDOWPOS = 0x4000
                        user32.SetWindowPos(
                            wintypes.HWND(dropped_hwnd),
                            wintypes.HWND(0),
                            int(start_rect[0]),
                            int(start_rect[1]),
                            0,
                            0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS,
                        )
                    except Exception:
                        pass

            threading.Thread(
                target=_resolve_and_play,
                name="uf-browser-win-drop",
                daemon=True,
            ).start()
        except Exception:
            browser_window_drop_tracker.reset()

    def poll_double_ctrl_toggle() -> None:
        state["double_ctrl_job"] = None
        try:
            poll_browser_window_drop()
            if (
                not state.get("shortcut_enabled")
                or get_async_key_state is None
                or state.get("shortcut_capture")
            ):
                repeat_detector.reset()
                combo_detector.reset()
                return
            toggled = False
            mode = _normalize_shortcut_mode(state.get("shortcut_mode"))
            if mode == SHORTCUT_MODE_COMBO:
                combo_keys = list(state.get("shortcut_combo") or SHORTCUT_COMBO_DEFAULT)
                if len(combo_keys) >= 2:
                    all_down = all(
                        _named_key_down(get_async_key_state, name) for name in combo_keys
                    )
                    extra = _other_shortcut_key_down(
                        get_async_key_state, exclude_keys=combo_keys
                    )
                    toggled = combo_detector.update(all_down and not extra)
            else:
                combo_detector.reset()
                key_name = _normalize_shortcut_key(state.get("shortcut_key"))
                key_down = _named_key_down(get_async_key_state, key_name)
                other_key_down = False
                if key_down:
                    other_key_down = _other_shortcut_key_down(
                        get_async_key_state, exclude_keys=[key_name]
                    )
                repeat_detector.tap_count = _normalize_shortcut_repeat_count(
                    state.get("shortcut_repeat_count")
                )
                toggled = repeat_detector.update(
                    key_down,
                    other_key_down,
                    time.monotonic(),
                )
            if toggled:
                toggle_wallpaper_mode()
        finally:
            try:
                state["double_ctrl_job"] = root.after(
                    DOUBLE_CTRL_POLL_MS,
                    poll_double_ctrl_toggle,
                )
            except Exception:
                state["double_ctrl_job"] = None

    apply_mode_surface()

    def _configure_font_if_changed(font_obj: tkfont.Font, **kwargs: Any) -> bool:
        """Only call font.configure when an option actually changed.

        In Tcl/Tk, `font configure` unconditionally fires `Tk_WorldChanged()`
        across every Toplevel in the interpreter, causing open dialogs (such as
        Settings) to recompute geometry and flicker even when the font size is
        identical.
        """
        updates: dict[str, Any] = {}
        for key, value in kwargs.items():
            try:
                current = font_obj.cget(key)
            except Exception:
                updates[key] = value
                continue
            if str(current) != str(value):
                updates[key] = value
        if not updates:
            return False
        try:
            font_obj.configure(**updates)
            return True
        except Exception:
            return False

    def metrics_for_font(size: int) -> tuple[int, int]:
        """(row_height, right_col_min) from real font metrics — avoids under-height clip."""
        try:
            # linespace is the true pixel line box for the active font
            ls = int(font_main.metrics("linespace"))
        except Exception:
            ls = size + 8
        return _compact_row_height(ls, size), max(72, int(size * 6))

    def apply_font_size(size: int, *, persist: bool = True, relayout: bool = True) -> None:
        size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, int(size)))
        state["font_size"] = size
        _configure_font_if_changed(font_main, size=size)
        _configure_font_if_changed(font_footer, size=max(FONT_SIZE_MIN, size - 1))
        _configure_font_if_changed(font_grip, size=max(8, size - 2))
        # Measure after updating the named font. Measuring first made the live
        # preview use the previous slider value until the next event/close.
        rh, rcw = metrics_for_font(size)
        state["row_height"] = rh
        state["right_col_w"] = rcw
        if persist:
            cfg["font_size"] = size
            save_config(cfg)
        if relayout:
            # Defer one frame so Scale drag never re-enters destroy mid-callback.
            schedule_render(16)

    def set_allow_resize(enabled: bool, *, persist: bool = True) -> None:
        state["allow_resize"] = bool(enabled)
        if state["allow_resize"] and not state.get("panel_active"):
            grip.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-1)
            grip.lift()
        else:
            grip.place_forget()
            if not state["allow_resize"] and state.get("card_height") is not None:
                state["card_height"] = None
                cfg["card_height"] = None
                schedule_render(0)
        if persist:
            cfg["allow_resize"] = state["allow_resize"]
            save_config(cfg)

    def clear_lines() -> None:
        # Snapshot first — never iterate children while destroying during re-entry
        kids = list(lines_frame.winfo_children())
        for w in kids:
            try:
                w.destroy()
            except Exception:
                pass
        state["panel_structure"] = None
        state["panel_items"] = []
        state["panel_empty_label"] = None
        state["reset_click_targets"] = []
        state["reset_click_widgets"] = []
        state["reset_hitboxes"] = []
        # Grid weights survive child destruction. Reset them before switching
        # between the compact strip and the auxiliary-panel instrument grid.
        try:
            for index in range(12):
                # uniform too: an emptied row left in the uniform group still
                # takes an equal share, squeezing the grid when rows drop.
                lines_frame.grid_rowconfigure(index, weight=0, minsize=0, uniform="")
                lines_frame.grid_columnconfigure(index, weight=0, minsize=0, uniform="")
        except Exception:
            pass

    def bind_hover_tracking(widget: tk.Misc) -> None:
        widget.bind("<Motion>", track_reset_hover, add="+")
        widget.bind("<Leave>", lambda _event: set_reset_hover(False), add="+")

    def bind_drag(widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", start_move)
        widget.bind("<B1-Motion>", do_move)
        widget.bind("<ButtonRelease-1>", stop_move)
        widget.bind("<Button-3>", show_menu)

    def start_move(event: tk.Event) -> None:
        stack_text_above_plate()
        if state.get("panel_active"):
            state["dragging"] = False
            return
        state["press_x"] = event.x_root
        state["press_y"] = event.y_root
        state["drag_ox"] = event.x_root - root.winfo_x()
        state["drag_oy"] = event.y_root - root.winfo_y()
        state["dragging"] = False

    def do_move(event: tk.Event) -> None:
        if state.get("panel_active"):
            return
        if abs(event.x_root - state["press_x"]) > 4 or abs(event.y_root - state["press_y"]) > 4:
            state["dragging"] = True
            x = int(event.x_root - state["drag_ox"])
            y = int(event.y_root - state["drag_oy"])
            state["floating_x"] = x
            state["floating_y"] = y
            pos = _tk_position(x, y)
            root.geometry(pos)
            if bg_layer is not None:
                try:
                    bg_layer.geometry(pos)
                except Exception:
                    pass

    def _event_local_point(event: tk.Event) -> tuple[int, int] | None:
        try:
            return (
                int(event.x_root) - int(root.winfo_rootx()),
                int(event.y_root) - int(root.winfo_rooty()),
            )
        except Exception:
            return None

    def event_hits_refresh(event: tk.Event) -> bool:
        point = _event_local_point(event)
        if point is None:
            return False
        return _point_in_box(point[0], point[1], state.get("refresh_hitbox"))

    def reset_target_at_event(event: tk.Event) -> str | None:
        """Which provider's usage number sits under the pointer, if any.

        Returns a Codex account id or a Claude row ("claude:7d" / "claude:5h");
        the caller opens whichever reset UI that target has.
        """
        point = _event_local_point(event)
        if point is None:
            return None
        for box, provider_id in state.get("reset_hitboxes") or []:
            if _point_in_box(point[0], point[1], box):
                return provider_id
        return None

    def register_reset_click_targets(targets: list[tuple[tk.Misc, str]]) -> None:
        """Track each clickable percentage label with the provider it belongs to.

        The rows are rendered exactly as before; this only remembers which
        labels are live. Transparent padding and the plate layer swallow direct
        widget events, so hover and click are resolved from window-relative
        boxes the same way the refresh control is. Carrying the provider id
        next to each box is what stops a click on one account's number from
        redeeming the other account's card, and keeps Claude's row pointed at
        its own dialog.
        """
        state["reset_click_targets"] = targets
        state["reset_click_widgets"] = [widget for widget, _ in targets]
        state["reset_hitboxes"] = []
        state["reset_hover"] = False
        for widget, _provider_id in targets:
            try:
                # Remember the rendered colour so hover can hand it back
                # untouched instead of recomputing the tone.
                setattr(widget, "_usage_rest_fg", widget.cget("fg"))
                widget.configure(cursor="hand2")
                widget.bind("<Enter>", lambda _event: set_reset_hover(True), add="+")
                widget.bind("<Leave>", lambda _event: set_reset_hover(False), add="+")
            except Exception:
                pass
        root.after_idle(update_reset_hitboxes)

    def set_reset_hover(hovering: bool) -> None:
        hovering = bool(hovering)
        if bool(state.get("reset_hover")) == hovering:
            return
        state["reset_hover"] = hovering
        if state.get("panel_active"):
            # Same trade-off as the refresh control: repainting a full-screen
            # layered surface for a hover tint is not worth it, and the hand
            # cursor already communicates the click.
            return
        for widget in state.get("reset_click_widgets") or []:
            try:
                if not widget.winfo_exists():
                    continue
                rest = getattr(widget, "_usage_rest_fg", None)
                widget.configure(fg=FG if hovering else (rest or FG))
            except Exception:
                pass
        update_plate_cursor()

    def update_reset_hitboxes(attempt: int = 0) -> None:
        boxes: list[tuple[tuple[int, int, int, int], str]] = []
        unmapped = False
        for widget, provider_id in state.get("reset_click_targets") or []:
            try:
                if not widget.winfo_exists():
                    continue
                width = widget.winfo_width()
                height = widget.winfo_height()
                if width <= 1 or height <= 1 or not widget.winfo_ismapped():
                    # Tk reports 1x1 at 0,0 until the row is actually mapped,
                    # and rows are rebuilt on every render, so measure again
                    # instead of freezing a degenerate box until the next
                    # refresh five minutes later.
                    unmapped = True
                    continue
                left = widget.winfo_rootx() - root.winfo_rootx()
                top = widget.winfo_rooty() - root.winfo_rooty()
                boxes.append(((left, top, left + width, top + height), provider_id))
            except Exception:
                continue
        if unmapped and attempt < 8:
            root.after(60, lambda: update_reset_hitboxes(attempt + 1))
            if not boxes:
                return
        state["reset_hitboxes"] = boxes

    def update_plate_cursor() -> None:
        if bg_layer is None or state.get("panel_active"):
            return
        hot = bool(state.get("refresh_hover") or state.get("reset_hover"))
        try:
            bg_layer.configure(cursor="hand2" if hot else "")
        except Exception:
            pass

    def set_refresh_hover(hovering: bool) -> None:
        control = state.get("refresh_control")
        try:
            if control is not None and control.winfo_exists():
                if state.get("panel_active"):
                    # Avoid repainting a full-screen layered surface for a tiny
                    # hover effect. The hand cursor already communicates click.
                    control.configure(fg=PANEL_MUTED)
                else:
                    control.configure(fg=FG_LABEL if hovering else FG_MUTED)
        except Exception:
            pass
        state["refresh_hover"] = bool(hovering)
        update_plate_cursor()

    def track_reset_hover(event: tk.Event) -> None:
        if state.get("dragging") or state.get("resize_active"):
            return
        set_reset_hover(reset_target_at_event(event) is not None)

    def open_reset_ui(provider_id: str) -> None:
        """Send the click to whichever reset UI this provider has."""
        acct, sep, win = provider_id.partition(":")
        if sep and is_claude_provider(acct) and win == "7d":
            open_claude_reset_info(acct)
        elif sep and is_claude_provider(acct) and win == "5h":
            open_claude_session_reset(acct)
        elif provider_id in discover_codex_accounts():
            open_codex_reset_cards(provider_id)

    def stop_move(event: tk.Event) -> None:
        if state.get("panel_active"):
            if event_hits_refresh(event) and not state["refreshing"]:
                refresh_async(force=True)
            else:
                provider_id = reset_target_at_event(event)
                if provider_id:
                    open_reset_ui(provider_id)
        elif state["dragging"]:
            state["dragging"] = False
            fx = int(state.get("floating_x") if state.get("floating_x") is not None else root.winfo_x())
            fy = int(state.get("floating_y") if state.get("floating_y") is not None else root.winfo_y())
            pos = _tk_position(fx, fy)
            try:
                root.geometry(pos)
                if bg_layer is not None:
                    bg_layer.geometry(pos)
            except Exception:
                pass
            cfg["x"] = fx
            cfg["y"] = fy
            state["floating_x"] = fx
            state["floating_y"] = fy
            save_config(cfg)
        elif event_hits_refresh(event) and not state["refreshing"]:
            refresh_async(force=True)
        else:
            provider_id = reset_target_at_event(event)
            if provider_id:
                open_reset_ui(provider_id)

    def shown_items() -> list[str]:
        return list(cfg.get("providers") or DEFAULT_PROVIDERS)

    def _right_text(dr: DisplayRow) -> tuple[str, str, str | None]:
        """Return (pct_text, time_text, fail_or_summary) for right side."""
        if dr.failed:
            return "", "", _ui(dr.summary or "更新失败")
        if dr.used_pct is not None:
            rem = format_remaining(dr.resets_at)
            pct_text = _ui(dr.summary) if dr.summary else f"{dr.used_pct:.0f}%"
            return pct_text, rem, None
        if dr.summary:
            return "", "", _ui(dr.summary)
        return "", "", ""

    def _panel_row_presentation(
        dr: DisplayRow,
    ) -> tuple[str, str, str, tkfont.Font, str]:
        pct_text, reset_text, extra = _right_text(dr)
        if pct_text:
            value_text = pct_text
            value_color = panel_tone_color(dr.used_pct or 0)
            value_font = font_panel_value
        else:
            value_text = extra or "—"
            value_color = PANEL_RED if dr.failed else PANEL_FG
            value_font = font_panel_title
        meta_text = reset_text if reset_text else (_tr("数据不可用", 'Data unavailable') if dr.failed else "")
        return pct_text, value_text, value_color, value_font, meta_text

    def _paint_panel_bar(
        canvas: tk.Canvas,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        bar_width = max(1, int(width or canvas.winfo_width()))
        bar_height = max(1, int(height or canvas.winfo_height()))
        pct = float(getattr(canvas, "_usage_pct", 0.0))
        color = str(getattr(canvas, "_usage_color", PANEL_FG))
        fill_width = int(round(bar_width * max(0.0, min(100.0, pct)) / 100.0))
        canvas.delete("usage-bar")
        canvas.create_rectangle(
            0,
            0,
            bar_width,
            bar_height,
            fill=PANEL_TRACK,
            outline="",
            tags="usage-bar",
        )
        if fill_width:
            canvas.create_rectangle(
                0,
                0,
                fill_width,
                bar_height,
                fill=color,
                outline="",
                tags="usage-bar",
            )

    def _render_panel_lines(providers: list[ProviderUsage]) -> None:
        panel: MonitorInfo | None = state.get("panel_monitor")
        width = panel.width if panel is not None else max(640, root.winfo_width())
        height = panel.height if panel is not None else max(480, root.winfo_height())
        rows = iter_display_rows(providers, shown_items()) if providers else []
        dpi_y = panel.dpi_y if panel is not None else 96

        if not rows:
            metrics = _panel_metrics(width, height, 5, dpi_y)
            target_pady = max(12, metrics.outer_padding - 4)
            if (
                int(body.cget("padx") or 0) != metrics.outer_padding
                or int(body.cget("pady") or 0) != target_pady
            ):
                body.configure(padx=metrics.outer_padding, pady=target_pady)
            _configure_font_if_changed(font_panel_title, size=metrics.title_points)
            _configure_font_if_changed(font_panel_value, size=metrics.value_points)
            _configure_font_if_changed(font_panel_meta, size=metrics.meta_points)
            _configure_font_if_changed(font_panel_footer, size=metrics.footer_points)
            message = _tr("正在读取用量…", 'Loading usage…') if state["refreshing"] else _tr("暂无用量数据", 'No usage data yet')
            empty = state.get("panel_empty_label")
            try:
                if empty is not None and empty.winfo_exists():
                    empty.configure(text=message)
                    return
            except Exception:
                pass
            clear_lines()
            empty = tk.Label(
                lines_frame,
                text=message,
                fg=PANEL_MUTED,
                bg=ui_bg,
                font=font_panel_title,
                anchor="center",
            )
            empty.pack(fill="both", expand=True)
            state["panel_structure"] = ("empty",)
            state["panel_empty_label"] = empty
            bind_drag(empty)
            bind_drag(lines_frame)
            return

        columns, grid_rows = _panel_grid_shape(max(4, len(rows)), width, height)
        col_positions: list[tuple[int, int]] | None = None
        metric_row_count = max(5, len(rows))
        raw_cols = cfg.get("provider_columns")
        if columns == 2 and isinstance(raw_cols, (list, tuple)) and len(raw_cols) >= 2:
            left_ids = set(_unique_provider_ids(raw_cols[0]))
            right_ids = set(_unique_provider_ids(raw_cols[1]))
            col0_indices: list[int] = []
            col1_indices: list[int] = []
            for idx, dr in enumerate(rows):
                item = display_item(dr)
                if item in left_ids:
                    col0_indices.append(idx)
                elif item in right_ids:
                    col1_indices.append(idx)
                elif item is None:
                    prov_items = PROVIDER_DISPLAY_ITEMS.get(dr.provider_id, ())
                    if any(pi in left_ids for pi in prov_items):
                        col0_indices.append(idx)
                    else:
                        col1_indices.append(idx)
                else:
                    if len(col0_indices) <= len(col1_indices):
                        col0_indices.append(idx)
                    else:
                        col1_indices.append(idx)
            if col0_indices and col1_indices:
                grid_rows = max(len(col0_indices), len(col1_indices), 1)
                metric_row_count = max(4, grid_rows * 2)
                pos_map: dict[int, tuple[int, int]] = {}
                for r_i, idx in enumerate(col0_indices):
                    pos_map[idx] = (0, r_i)
                for r_i, idx in enumerate(col1_indices):
                    pos_map[idx] = (1, r_i)
                col_positions = [pos_map[idx] for idx in range(len(rows))]

        metrics = _panel_metrics(width, height, metric_row_count, dpi_y)
        target_pady = max(12, metrics.outer_padding - 4)
        if (
            int(body.cget("padx") or 0) != metrics.outer_padding
            or int(body.cget("pady") or 0) != target_pady
        ):
            body.configure(padx=metrics.outer_padding, pady=target_pady)
        _configure_font_if_changed(font_panel_title, size=metrics.title_points)
        _configure_font_if_changed(font_panel_value, size=metrics.value_points)
        _configure_font_if_changed(font_panel_meta, size=metrics.meta_points)
        _configure_font_if_changed(font_panel_footer, size=metrics.footer_points)

        presentations = [_panel_row_presentation(dr) for dr in rows]
        structure = _panel_layout_key(
            columns,
            grid_rows,
            metrics.cell_gap,
            metrics.bar_height,
            (
                (
                    f"{col_positions[i][0]}:{col_positions[i][1]}:{dr.title}"
                    if col_positions is not None
                    else dr.title,
                    bool(presentation[0]),
                )
                for i, (dr, presentation) in enumerate(zip(rows, presentations))
            ),
        )
        items = state.get("panel_items") or []
        if state.get("panel_structure") == structure and len(items) == len(rows):
            try:
                for dr, presentation, item in zip(rows, presentations, items):
                    pct_text, value_text, value_color, value_font, meta_text = presentation
                    title_label = item["title"]
                    value_label = item["value"]
                    meta_label = item["meta"]
                    if not all(
                        widget.winfo_exists()
                        for widget in (title_label, value_label, meta_label)
                    ):
                        raise tk.TclError("stale panel widget cache")
                    title_label.configure(text=dr.title)
                    value_width = _panel_value_slot_width(font_panel_value, value_text)
                    value_label.configure(
                        text=value_text,
                        fg=value_color,
                        font=value_font,
                    )
                    value_label.place(
                        relx=1.0,
                        rely=1.0,
                        anchor="se",
                        y=-7,
                        width=value_width,
                    )
                    headline = item.get("headline")
                    if headline is not None and headline.winfo_exists():
                        headline.grid_columnconfigure(1, minsize=value_width)
                    meta_label.configure(text=meta_text)
                    bar = item.get("bar")
                    if pct_text and bar is not None and bar.winfo_exists():
                        bar._usage_pct = float(dr.used_pct or 0)  # type: ignore[attr-defined]
                        bar._usage_color = value_color  # type: ignore[attr-defined]
                        _paint_panel_bar(bar)
                # Nothing was removed or remapped: Tk paints the changed values
                # together after this callback, so the panel never exposes an
                # intermediate all-black frame during automatic refresh.
                return
            except (KeyError, tk.TclError):
                pass

        clear_lines()
        for column in range(columns):
            lines_frame.grid_columnconfigure(column, weight=1, uniform="panel-column")
        for row_index in range(grid_rows):
            lines_frame.grid_rowconfigure(row_index, weight=1, uniform="panel-row")

        half_gap = max(4, metrics.cell_gap // 2)
        panel_items: list[dict[str, Any]] = []
        reset_targets: list[tuple[tk.Misc, str]] = []
        for index, (dr, presentation) in enumerate(zip(rows, presentations)):
            # Column-major placement keeps Claude's related windows together.
            if col_positions is not None:
                column, row_index = col_positions[index]
            else:
                column = min(columns - 1, index // grid_rows)
                row_index = index % grid_rows
            tile = tk.Frame(lines_frame, bg=ui_bg, bd=0)
            tile.grid(
                row=row_index,
                column=column,
                sticky="nsew",
                padx=(half_gap if column else 0, half_gap if column < columns - 1 else 0),
                pady=(half_gap if row_index else 0, half_gap if row_index < grid_rows - 1 else 0),
            )

            instrument = tk.Frame(tile, bg=ui_bg, bd=0)
            instrument.pack(fill="x", expand=True, anchor="center")
            headline = tk.Frame(instrument, bg=ui_bg, bd=0)
            headline.pack(fill="x")
            pct_text, value_text, value_color, value_font, meta_text = presentation
            value_width = _panel_value_slot_width(font_panel_value, value_text)
            headline.grid_columnconfigure(0, weight=1)
            # Reserve enough room for 100% so percentage changes do not shift
            # the title. Dollar readouts like $2/200 are wider than 100%.
            headline.grid_columnconfigure(
                1,
                weight=0,
                minsize=value_width,
            )

            title_label = tk.Label(
                headline,
                text=dr.title,
                fg=PANEL_LABEL,
                bg=ui_bg,
                font=font_panel_title,
                anchor="w",
                justify="left",
            )
            title_label.grid(row=0, column=0, sticky="sw")

            value_label = tk.Label(
                headline,
                text=value_text,
                fg=value_color,
                bg=ui_bg,
                font=value_font,
                anchor="e",
                justify="right",
            )
            # Overlay the large value at bottom-right instead of letting its
            # 48pt line box stretch the title/reset rows vertically.
            value_label.place(
                relx=1.0,
                rely=1.0,
                anchor="se",
                y=-7,
                width=value_width,
            )

            meta_label = tk.Label(
                headline,
                text=meta_text,
                fg=PANEL_MUTED,
                bg=ui_bg,
                font=font_panel_meta,
                anchor="w",
                justify="left",
            )
            meta_label.grid(
                row=1,
                column=0,
                sticky="sw",
                pady=(2, 7),
            )

            if pct_text and row_opens_reset_ui(dr):
                reset_targets.append((value_label, reset_target_id(dr)))

            if pct_text:
                bar = tk.Canvas(
                    instrument,
                    height=metrics.bar_height,
                    bg=ui_bg,
                    bd=0,
                    highlightthickness=0,
                )
                bar.pack(fill="x")
                bar._usage_pct = float(dr.used_pct or 0)  # type: ignore[attr-defined]
                bar._usage_color = value_color  # type: ignore[attr-defined]
                bar.bind(
                    "<Configure>",
                    lambda event, canvas=bar: _paint_panel_bar(
                        canvas,
                        width=int(event.width),
                        height=int(event.height),
                    ),
                )
                bind_drag(bar)
            else:
                bar = None
                divider = tk.Frame(instrument, bg=PANEL_TRACK, height=1, bd=0)
                divider.pack(fill="x")
                bind_drag(divider)

            panel_items.append(
                {
                    "title": title_label,
                    "value": value_label,
                    "meta": meta_label,
                    "bar": bar,
                    "headline": headline,
                }
            )

            for widget in (tile, instrument, headline, title_label, value_label, meta_label):
                bind_drag(widget)

        bind_drag(lines_frame)
        bind_drag(body)
        state["panel_structure"] = structure
        state["panel_items"] = panel_items
        register_reset_click_targets(reset_targets)

    def render_lines() -> None:
        providers: list[ProviderUsage] = state["providers"]
        if state.get("panel_active"):
            _render_panel_lines(providers)
            return

        clear_lines()
        body.configure(padx=CARD_PADX, pady=3)
        cw = int(state["card_width"])
        content_w = max(120, cw - CARD_PADX * 2)
        row_h = int(state["row_height"])

        if not providers and state["refreshing"]:
            row = tk.Frame(lines_frame, bg=ui_bg)
            row.pack(fill="x", anchor="w")
            tk.Label(row, text=_tr("加载中…", 'Loading…'), fg=FG_MUTED, bg=ui_bg, font=font_main).pack(side="left")
            bind_drag(row)
            return
        if not providers:
            row = tk.Frame(lines_frame, bg=ui_bg)
            row.pack(fill="x", anchor="w")
            tk.Label(row, text=_tr("暂无数据", 'No data yet'), fg=FG_MUTED, bg=ui_bg, font=font_main).pack(side="left")
            bind_drag(row)
            return

        reset_targets: list[tuple[tk.Misc, str]] = []
        for dr in iter_display_rows(providers, shown_items()):
            compact_title = dr.title
            if dr.provider_id == "antigravity":
                compact_title = compact_title.replace("gemini", "gem")
            # pack order: right first (natural width, never clipped), then left expands
            row = tk.Frame(lines_frame, bg=ui_bg, height=row_h)
            row.pack(fill="x", anchor="w", pady=0)
            row.pack_propagate(False)
            # force row to card content width
            row.configure(width=content_w)

            pct_t, time_t, extra = _right_text(dr)

            right_box = tk.Frame(row, bg=ui_bg)
            # Pack RIGHT first so usage is always fully visible
            right_box.pack(side="right", padx=(8, 0))

            if extra is not None and not pct_t:
                color = RED if dr.failed else FG
                s = tk.Label(
                    right_box,
                    text=extra,
                    fg=color,
                    bg=ui_bg,
                    font=font_main,
                    anchor="e",
                    width=10,
                )
                s.pack(side="right")
                bind_drag(s)
            else:
                # Right block sticks to window edge when resizing.
                # Columns L→R inside block: [ 27% right-aligned] [4h33m left-aligned]
                # Pack order is right-first: time (outer), then percent.
                t = tk.Label(
                    right_box,
                    text=time_t or "",
                    fg=FG_LABEL,
                    bg=ui_bg,
                    font=font_main,
                    anchor="w",
                    justify="left",
                    width=6,  # fits "5d16h"; left-aligned within column
                )
                t.pack(side="right")
                bind_drag(t)
                # percent column — fixed width, digits right-aligned as a column
                pct_lbl = tk.Label(
                    right_box,
                    text=pct_t or "",
                    fg=tone_color(dr.used_pct or 0) if pct_t else FG_MUTED,
                    bg=ui_bg,
                    font=font_main,
                    anchor="e",
                    justify="right",
                    width=max(4, len(pct_t or "")),
                )
                pct_lbl.pack(side="right", padx=(0, 4))
                bind_drag(pct_lbl)
                if pct_t and row_opens_reset_ui(dr):
                    reset_targets.append((pct_lbl, reset_target_id(dr)))

            left = tk.Label(
                row,
                text=compact_title,
                fg=FG_LABEL,
                bg=ui_bg,
                font=font_main,
                anchor="w",
                justify="left",
            )
            # Left fills remainder; long titles clip on the left side only
            left.pack(side="left", fill="x", expand=True)
            # Soft clip: limit left label pixel width so it never overlaps right
            try:
                right_box.update_idletasks()
                rw = max(right_box.winfo_reqwidth(), 1)
                left.configure(wraplength=0)
                # Use a clip via max width on the label using place... pack is enough if right is packed first
                max_left = max(40, content_w - rw - 4)
                # Tk Label doesn't clip by width easily; use a fixed width in characters approx
                # Measure and truncate with ellipsis if needed
                title = compact_title
                while title and font_main.measure(title) > max_left:
                    title = title[:-1]
                if title != compact_title and len(title) > 1:
                    title = title[:-1] + "…"
                left.configure(text=title)
            except Exception:
                pass

            bind_drag(left)
            bind_drag(right_box)
            bind_drag(row)

        bind_drag(lines_frame)
        bind_drag(body)
        register_reset_click_targets(reset_targets)

    def render_footer() -> None:
        def on_refresh() -> None:
            if state["refreshing"]:
                return
            refresh_async(force=True)

        # Timestamp and icon are one control with no visual plate of their own.
        # Transparent padding clicks land on bg_layer and are forwarded via the
        # matching hitbox, so the entire block remains interactive.
        refresh_control = state.get("refresh_control")
        control_exists = False
        try:
            control_exists = bool(
                refresh_control is not None and refresh_control.winfo_exists()
            )
        except Exception:
            control_exists = False

        control_options = {
            "text": _format_refresh_control(
                state["last_ok_at"], bool(state["refreshing"]),
                compact=not state.get("panel_active"),
            ),
            "command": on_refresh,
            "fg": PANEL_MUTED if state.get("panel_active") else FG_MUTED,
            "bg": ui_bg,
            "activeforeground": PANEL_FG if state.get("panel_active") else FG_LABEL,
            "activebackground": ui_bg,
            "font": font_panel_footer if state.get("panel_active") else font_footer,
        }
        if control_exists:
            refresh_control.configure(**control_options)
        else:
            for widget in list(footer.winfo_children()):
                try:
                    widget.destroy()
                except Exception:
                    pass
            state["refresh_hitbox"] = None
            refresh_control = tk.Button(
                footer,
                **control_options,
                cursor="hand2",
                padx=0,
                pady=0,
                bd=0,
                highlightthickness=0,
                relief="flat",
                overrelief="flat",
                takefocus=False,
                anchor="w",
            )
            refresh_control.pack(side="left")
            refresh_control.bind("<Enter>", lambda _e: set_refresh_hover(True))
            refresh_control.bind("<Leave>", lambda _e: set_refresh_hover(False))
        state["refresh_control"] = refresh_control

        def update_refresh_hitbox() -> None:
            if state.get("refresh_control") is not refresh_control:
                return
            try:
                left = refresh_control.winfo_rootx() - root.winfo_rootx()
                top = refresh_control.winfo_rooty() - root.winfo_rooty()
                state["refresh_hitbox"] = (
                    left,
                    top,
                    left + refresh_control.winfo_width(),
                    top + refresh_control.winfo_height(),
                )
            except Exception:
                state["refresh_hitbox"] = None

        root.after_idle(update_refresh_hitbox)
        bind_drag(footer)
        if state["allow_resize"]:
            grip.lift()

    def content_min_height() -> int:
        """Minimum height so all rows + bottom footer fit without clipping.

        Prefer measured widget sizes after layout; fall back to row math.
        """
        n = max(
            1,
            len(iter_display_rows(state["providers"], shown_items()))
            if state["providers"]
            else 1,
        )
        rh = int(state["row_height"])
        try:
            footer_linespace = int(font_footer.metrics("linespace"))
        except Exception:
            footer_linespace = int(state["font_size"]) + 7
        footer_h = max(16, footer_linespace + 2)
        # body pady top+bottom (3*2) + compact grip slack
        estimate = 3 + 3 + n * rh + footer_h + 2

        measured = 0
        try:
            root.update_idletasks()
            lines_h = 0
            for child in lines_frame.winfo_children():
                lines_h += max(int(child.winfo_reqheight()), rh)
            foot_h = max(int(footer.winfo_reqheight()), footer_h)
            # padx doesn't affect height; pady=3 top+bottom on body
            measured = lines_h + foot_h + 6 + 2
        except Exception:
            measured = 0

        return max(CARD_HEIGHT_MIN, estimate, measured)

    def apply_geometry() -> None:
        try:
            root.update_idletasks()
            panel: MonitorInfo | None = state.get("panel_monitor")
            if state.get("panel_active") and panel is not None:
                geo = _tk_geometry(panel.width, panel.height, panel.x, panel.y)
                root.geometry(geo)
                enforce_native_panel_rect()
                # WM_DPICHANGED can arrive just after the first move; assert the
                # physical rectangle once more after that transition settles.
                schedule_native_panel_rect(120)
                grip.place_forget()
                return

            auto_h = content_min_height()

            w = max(CARD_WIDTH_MIN, min(CARD_WIDTH_MAX, int(state["card_width"])))
            state["card_width"] = w

            user_h = state.get("card_height")
            if user_h is not None:
                try:
                    user_h = int(user_h)
                except Exception:
                    user_h = None
            # Never allow a saved height shorter than content (fixes grok clip)
            if user_h is not None:
                h = max(auto_h, min(CARD_HEIGHT_MAX, user_h))
                if user_h < auto_h:
                    state["card_height"] = auto_h
                    cfg["card_height"] = auto_h
                    try:
                        save_config(cfg)
                    except Exception:
                        pass
                else:
                    state["card_height"] = h
            else:
                h = auto_h

            x, y = root.winfo_x(), root.winfo_y()
            geo = _tk_geometry(w, h, x, y)
            root.geometry(geo)
            if bg_layer is not None:
                try:
                    bg_layer.geometry(geo)
                    if not state.get("menu_open"):
                        bg_layer.attributes("-topmost", bool(state["always_on_top"]))
                    # Keep both windows in the same topmost band and place the
                    # content immediately above its plate.  lower() could put
                    # the plate behind unrelated applications.
                    stack_text_above_plate()
                except Exception:
                    pass
            if state["allow_resize"]:
                grip.lift()
        except Exception:
            pass

    def render(*, relayout: bool = True) -> None:
        if state.get("rendering"):
            return
        state["rendering"] = True
        try:
            render_lines()
            render_footer()
            if relayout:
                apply_geometry()
            # Second measure after fonts/layout settle (fixes last-row clip)
            root.after_idle(_post_layout_fix)
        finally:
            state["rendering"] = False

    def _post_layout_fix() -> None:
        if state.get("rendering"):
            return
        if state.get("panel_active"):
            return
        try:
            auto_h = content_min_height()
            cur_h = int(root.winfo_height() or 0)
            if cur_h and cur_h < auto_h:
                state["card_height"] = max(int(state.get("card_height") or 0), auto_h)
                apply_geometry()
        except Exception:
            pass

    def schedule_render(delay_ms: int = 40) -> None:
        """Debounced full relayout — safe to call from Scale drag."""
        job = state.get("render_job")
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:
                pass
        state["render_job"] = root.after(delay_ms, _run_scheduled_render)

    def _run_scheduled_render() -> None:
        state["render_job"] = None
        render()

    def _runtime_monitor_signature(monitors: list[MonitorInfo]) -> tuple[Any, ...]:
        return tuple(
            (
                monitor.device,
                monitor.bounds,
                monitor.work_bounds,
                monitor.dpi_x,
                monitor.dpi_y,
                monitor.primary,
            )
            for monitor in monitors
        )

    def _set_runtime_dpi(dpi: int) -> None:
        dpi = max(72, int(dpi or 96))
        if state.get("tk_dpi") == dpi:
            return
        try:
            root.tk.call("tk", "scaling", dpi / 72.0)
            state["tk_dpi"] = dpi
            # Named fonts keep point sizes, but row metrics must be re-read at
            # the target monitor's pixel density.
            apply_font_size(int(state["font_size"]), persist=False, relayout=False)
        except Exception:
            pass

    def _restore_floating_geometry(monitors: list[MonitorInfo]) -> None:
        x = state.get("floating_x")
        y = state.get("floating_y")
        if x is None or y is None:
            primary = next((m for m in monitors if m.primary), monitors[0])
            x = primary.work_x + max(12, primary.work_width - int(state["card_width"]) - 24)
            y = primary.work_y + 36
        x, y = _clamp_position(
            int(x),
            int(y),
            int(state["card_width"]),
            120,
        )
        state["floating_x"], state["floating_y"] = x, y
        compact_geo = _tk_geometry(int(state["card_width"]), 120, x, y)
        root.geometry(compact_geo)
        if bg_layer is not None:
            try:
                bg_layer.geometry(compact_geo)
            except Exception:
                pass

    def _sync_display_runtime(
        monitors: list[MonitorInfo] | None = None,
        *,
        persist: bool = False,
    ) -> None:
        monitors = monitors or _enumerate_monitors()
        if not monitors:
            return
        state["monitor_signature"] = _runtime_monitor_signature(monitors)

        panel = None
        if state.get("display_mode") == DISPLAY_MODE_PANEL:
            panel = _choose_panel_monitor(
                monitors,
                state.get("monitor_device"),
                state.get("monitor_id"),
            )
            if panel is not None:
                state["monitor_device"] = panel.device
                state["monitor_id"] = panel.monitor_id or state.get("monitor_id")

        was_active = bool(state.get("panel_active"))
        state["panel_monitor"] = panel
        state["panel_active"] = panel is not None
        if panel is None and (
            state.get("wallpaper_active") or state.get("wallpaper_preparing")
        ):
            set_wallpaper_active(False)
        apply_mode_surface()

        if panel is not None:
            _set_runtime_dpi(panel.dpi_y)
            apply_geometry()
        else:
            floating_monitor = _monitor_at_point(
                monitors,
                int(state.get("floating_x") or 0),
                int(state.get("floating_y") or 0),
            ) or next((m for m in monitors if m.primary), monitors[0])
            _set_runtime_dpi(floating_monitor.dpi_y)
            if was_active or state.get("display_mode") == DISPLAY_MODE_PANEL:
                _restore_floating_geometry(monitors)

        set_allow_resize(bool(state["allow_resize"]), persist=False)
        cfg["display_mode"] = str(state["display_mode"])
        cfg["monitor_device"] = state.get("monitor_device")
        cfg["monitor_id"] = state.get("monitor_id")
        if persist:
            save_config(cfg)
        schedule_render(0)
        if panel is not None and not state.get("wallpaper_active"):
            _schedule_background_wallpaper_prepare()

    def set_display_mode(
        mode: str,
        monitor_device_value: str | None = None,
        *,
        persist: bool = True,
    ) -> None:
        mode = mode if mode in (DISPLAY_MODE_FLOAT, DISPLAY_MODE_PANEL) else DISPLAY_MODE_FLOAT
        if not state.get("panel_active"):
            state["floating_x"] = root.winfo_x()
            state["floating_y"] = root.winfo_y()
            cfg["x"] = state["floating_x"]
            cfg["y"] = state["floating_y"]
        state["display_mode"] = mode
        if monitor_device_value:
            state["monitor_device"] = monitor_device_value
            selected = next(
                (
                    monitor
                    for monitor in _enumerate_monitors()
                    if monitor.device.casefold() == monitor_device_value.casefold()
                ),
                None,
            )
            if selected is not None:
                state["monitor_id"] = selected.monitor_id or state.get("monitor_id")
        _sync_display_runtime(persist=persist)

    def poll_monitors() -> None:
        try:
            monitors = _enumerate_monitors()
            signature = _runtime_monitor_signature(monitors)
            if signature != state.get("monitor_signature"):
                _sync_display_runtime(monitors, persist=False)
        finally:
            root.after(MONITOR_POLL_MS, poll_monitors)

    def refresh_async(force: bool = False) -> None:
        if state["refreshing"]:
            return
        state["refreshing"] = True
        render_footer()
        previous = list(state["providers"])

        def work() -> None:
            try:
                selected = active_providers(list(cfg.get("providers") or DEFAULT_PROVIDERS), include_discovered=True, disabled=list(cfg.get("disabled_providers") or []))
                configured = {provider_fetch_id(pid) for pid in cfg.get("providers") or DEFAULT_PROVIDERS}
                # Credential discovery can temporarily return no accounts while
                # login files are being replaced. Keep enabled UI history.
                selected += [p.provider_id for p in previous if p.provider_id in configured]
                providers = fetch_display_usage(selected, previous, force=force)
            except Exception as error:
                # Discovery itself can fail before collection starts.
                selected = [p.provider_id for p in previous] or list(cfg.get("providers") or DEFAULT_PROVIDERS)
                providers = merge_display_usage(None, previous, selected, f'更新失败 ({type(error).__name__})')
            root.after(0, lambda p=providers: apply_data(p))

        threading.Thread(target=work, daemon=True).start()

    def apply_data(providers: list[ProviderUsage]) -> None:
        state["refreshing"] = False
        providers = merge_display_usage(providers, state["providers"], [p.provider_id for p in providers])
        state["providers"] = providers
        # Mark successful overall refresh time if at least one provider is OK
        fresh_times = [_iso_epoch(p.fetched_at, 0) for p in providers if p.available and not p.stale]
        if not any(fresh_times):
            fresh_times = [_iso_epoch(p.fetched_at, 0) for p in providers if p.available]
        if any(fresh_times):
            state["last_ok_at"] = max(fresh_times)
        # A data refresh does not change the dedicated panel's native bounds.
        # Avoid SetWindowPos(...SWP_SHOWWINDOW) on every refresh; it needlessly
        # invalidates the full-screen surface on mixed-DPI monitor layouts.
        render(relayout=not state.get("panel_active"))

    # Relative time ticker
    def tick_footer() -> None:
        if not state["refreshing"]:
            render_footer()
        root.after(15_000, tick_footer)

    # Match Windows shell context menu: Segoe UI ~9pt (system menu font)
    menu_font = tkfont.Font(family="Segoe UI", size=9)
    try:
        # Prefer real system menu font when available (Windows NONCLIENTMETRICS)
        import ctypes
        from ctypes import wintypes

        class LOGFONTW(ctypes.Structure):
            _fields_ = [
                ("lfHeight", wintypes.LONG),
                ("lfWidth", wintypes.LONG),
                ("lfEscapement", wintypes.LONG),
                ("lfOrientation", wintypes.LONG),
                ("lfWeight", wintypes.LONG),
                ("lfItalic", wintypes.BYTE),
                ("lfUnderline", wintypes.BYTE),
                ("lfStrikeOut", wintypes.BYTE),
                ("lfCharSet", wintypes.BYTE),
                ("lfOutPrecision", wintypes.BYTE),
                ("lfClipPrecision", wintypes.BYTE),
                ("lfQuality", wintypes.BYTE),
                ("lfPitchAndFamily", wintypes.BYTE),
                ("lfFaceName", wintypes.WCHAR * 32),
            ]

        class NONCLIENTMETRICSW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("iBorderWidth", ctypes.c_int),
                ("iScrollWidth", ctypes.c_int),
                ("iScrollHeight", ctypes.c_int),
                ("iCaptionWidth", ctypes.c_int),
                ("iCaptionHeight", ctypes.c_int),
                ("lfCaptionFont", LOGFONTW),
                ("iSmCaptionWidth", ctypes.c_int),
                ("iSmCaptionHeight", ctypes.c_int),
                ("lfSmCaptionFont", LOGFONTW),
                ("iMenuWidth", ctypes.c_int),
                ("iMenuHeight", ctypes.c_int),
                ("lfMenuFont", LOGFONTW),
                ("lfStatusFont", LOGFONTW),
                ("lfMessageFont", LOGFONTW),
                ("iPaddedBorderWidth", ctypes.c_int),
            ]

        SPI_GETNONCLIENTMETRICS = 0x0029
        ncm = NONCLIENTMETRICSW()
        ncm.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)
        if ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETNONCLIENTMETRICS, ncm.cbSize, ctypes.byref(ncm), 0
        ):
            face = ncm.lfMenuFont.lfFaceName or "Segoe UI"
            # lfHeight is often negative pixel height; convert approx to points
            h = abs(int(ncm.lfMenuFont.lfHeight)) or 12
            # points ≈ pixels * 72 / dpi; assume 96 dpi baseline if dpi unknown
            dpi = 96
            try:
                dpi = int(ctypes.windll.user32.GetDpiForSystem())
            except Exception:
                pass
            pts = max(8, min(12, round(h * 72 / dpi)))
            menu_font = tkfont.Font(family=face, size=pts)
    except Exception:
        pass

    menu = tk.Menu(root, tearoff=0, font=menu_font)

    def place_settings_on_primary(window: Any) -> None:
        """Map settings once at its final primary-monitor position."""
        try:
            window.update_idletasks()
            monitors = _enumerate_monitors()
            host = next((monitor for monitor in monitors if monitor.primary), monitors[0])
            width = window.winfo_reqwidth()
            height = window.winfo_reqheight()
            wx = host.work_x + max(12, (host.work_width - width) // 2)
            wy = host.work_y + max(12, (host.work_height - height) // 2)
            window.geometry(_tk_position(wx, wy))
        except Exception:
            pass

    def open_settings(
        initial_tab: str = "用量",
        initial_wallpaper_tab: str = "目录",
    ) -> None:
        existing = state.get("settings_win")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    place_settings_on_primary(existing)
                    existing.deiconify()
                    existing.lift()
                    existing.focus_set()
                    return
            except Exception:
                pass

        # Flat dark palette (borderless surfaces, minimal boxes)
        S_PAGE_BG = "#161a20"
        S_CARD_BG = "#1e242d"
        S_CARD_BORDER = "#272f3b"
        S_SURFACE_ALT = "#252c37"
        S_INPUT_BG = "#14181e"
        S_TEXT = "#eef5fb"
        S_MUTED = "#9aa5b4"
        S_SUBTLE = "#687382"
        S_ACCENT = "#8ce9c2"
        S_ACCENT_BLUE = "#73bdf5"
        S_ACCENT_HOVER = "#a3f0cf"
        S_ACCENT_INK = "#0f241c"
        S_BTN_BG = "#262e39"
        S_BTN_HOVER = "#313b48"
        S_TAB_TRACK_BG = "#11151a"
        S_TAB_ACTIVE_BG = "#252c37"
        S_BAR_ON_BG = "#252c37"
        S_BAR_ON_BORDER = "#252c37"
        S_BAR_ON_HOVER = "#2c3442"
        S_BAR_OFF_BG = "#191e25"
        S_BAR_OFF_BORDER = "#191e25"
        S_BAR_OFF_FG = "#596372"
        S_BAR_DRAG_BG = "#1f352e"
        S_BAR_DRAG_BORDER = "#8ce9c2"
        S_COL_BG = "#1a1f27"
        S_COL_BORDER = "#1a1f27"

        win = tk.Toplevel(root)
        # Prevent the native wrapper from mapping for one frame at Windows'
        # default screen-edge position while the layout calculates its size.
        win.withdraw()
        state["settings_win"] = win
        win.title(_tr("UsageFloat 设置", 'UsageFloat Settings'))
        win.configure(bg=S_PAGE_BG)
        win.resizable(True, True)
        win.minsize(600, 680)

        # Settings chrome stays at a stable size while the HUD font is previewed.
        settings_font_title = tkfont.Font(family=font_family, size=13, weight="bold")
        settings_font_section = tkfont.Font(family=font_family, size=10, weight="bold")
        settings_font = tkfont.Font(family=font_family, size=10)
        settings_font_small = tkfont.Font(family=font_family, size=9)
        settings_font_badge = tkfont.Font(family=font_family, size=8)
        setattr(
            win,
            "_usage_float_fonts",
            (
                settings_font_title,
                settings_font_section,
                settings_font,
                settings_font_small,
                settings_font_badge,
            ),
        )

        style = ttk.Style(win)
        try:
            style.theme_use("clam")
            style.configure(
                "TCombobox",
                fieldbackground=S_INPUT_BG,
                background=S_BTN_BG,
                foreground=S_TEXT,
                arrowcolor=S_MUTED,
                bordercolor=S_INPUT_BG,
                lightcolor=S_INPUT_BG,
                darkcolor=S_INPUT_BG,
                relief="flat",
                padding=4,
            )
            style.map(
                "TCombobox",
                fieldbackground=[
                    ("readonly", S_INPUT_BG),
                    ("disabled", S_BAR_OFF_BG),
                    ("!disabled", S_INPUT_BG),
                ],
                background=[
                    ("readonly", S_BTN_BG),
                    ("active", S_BTN_HOVER),
                    ("disabled", S_BAR_OFF_BG),
                ],
                foreground=[
                    ("readonly", S_TEXT),
                    ("disabled", S_BAR_OFF_FG),
                    ("!disabled", S_TEXT),
                ],
                arrowcolor=[
                    ("disabled", S_BAR_OFF_FG),
                    ("active", S_ACCENT),
                    ("!disabled", S_MUTED),
                ],
            )
            style.layout(
                "Vertical.TScrollbar",
                [
                    (
                        "Vertical.Scrollbar.trough",
                        {
                            "children": [
                                (
                                    "Vertical.Scrollbar.thumb",
                                    {"expand": "1", "sticky": "nswe"},
                                )
                            ],
                            "sticky": "ns",
                        },
                    )
                ],
            )
            style.configure(
                "Vertical.TScrollbar",
                troughcolor=S_COL_BG,
                background=S_BTN_BG,
                bordercolor=S_COL_BG,
                lightcolor=S_COL_BG,
                darkcolor=S_COL_BG,
                gripcount=0,
                relief="flat",
                borderwidth=0,
                width=6,
            )
            style.map(
                "Vertical.TScrollbar",
                background=[
                    ("active", S_BTN_HOVER),
                    ("disabled", S_COL_BG),
                    ("!disabled", S_BTN_BG),
                ],
            )
            win.option_add("*TCombobox*Listbox*Background", S_INPUT_BG)
            win.option_add("*TCombobox*Listbox*Foreground", S_TEXT)
            win.option_add("*TCombobox*Listbox*selectBackground", S_TAB_ACTIVE_BG)
            win.option_add("*TCombobox*Listbox*selectForeground", S_ACCENT)
        except Exception:
            pass

        shell = tk.Frame(win, bg=S_PAGE_BG, padx=18, pady=16)
        shell.pack(fill="both", expand=True)

        # Top header bar + flat pill navigation
        header_frame = tk.Frame(shell, bg=S_PAGE_BG)
        header_frame.pack(fill="x", pady=(0, 12))
        header_left = tk.Frame(header_frame, bg=S_PAGE_BG)
        header_left.pack(side="left", fill="x", expand=True)
        tk.Label(
            header_left,
            text="UsageFloat",
            fg=S_TEXT,
            bg=S_PAGE_BG,
            font=settings_font_title,
            anchor="w",
        ).pack(side="left")
        tk.Label(
            header_left,
            text="  SETTINGS",
            fg=S_ACCENT,
            bg=S_PAGE_BG,
            font=settings_font_badge,
            anchor="w",
        ).pack(side="left", pady=(4, 0))
        tk.Label(
            header_left,
            text=_tr("   双栏排布 · 副屏仪表盘 · 动态壁纸", '   Two-Column Layout · Aux Display · Live Wallpaper'),
            wraplength=370, justify="left",
            fg=S_MUTED,
            bg=S_PAGE_BG,
            font=settings_font_small,
            anchor="w",
        ).pack(side="left", pady=(3, 0))

        language_row = tk.Frame(shell, bg=S_PAGE_BG)
        language_row.pack(fill="x", pady=(0, 10))
        tk.Label(
            language_row, text="语言 / Language", fg=S_TEXT, bg=S_PAGE_BG,
            font=settings_font,
        ).pack(side="left")
        language_var = tk.StringVar(value="English" if _lang(cfg) == "en" else "简体中文")
        language_combo = ttk.Combobox(
            language_row, textvariable=language_var, values=("简体中文", "English"),
            state="readonly", width=16, font=settings_font,
        )
        language_combo.pack(side="right")

        def change_language(_event: Any = None) -> None:
            language = "en" if language_var.get() == "English" else "zh"
            if language == _lang(cfg):
                return
            # Commit pending entries with the same semantics as Done before
            # rebuilding the dialog. Stable tab IDs survive language changes.
            main_tab = active_main_tab["title"]
            wallpaper_tab = wp_subtab_state["active"]
            on_close()
            _set_language(cfg, state, language)
            clear_lines()
            rebuild_menu()
            render()
            open_settings(initial_tab=main_tab, initial_wallpaper_tab=wallpaper_tab)
            reopened = state.get("settings_win")
            if reopened is not None:
                reopened._select_settings_tab(main_tab)
                reopened._select_wallpaper_tab(wallpaper_tab)

        language_combo.bind("<<ComboboxSelected>>", change_language)

        def make_card(
            parent: tk.Misc,
            *,
            pady: tuple[int, int] = (0, 10),
            padx_inner: int = 14,
            pady_inner: int = 12,
            expand: bool = False,
            fill: str = "x",
        ) -> tk.Frame:
            outer = tk.Frame(
                parent,
                bg=S_CARD_BG,
                highlightthickness=0,
                bd=0,
            )
            outer.pack(fill=fill, expand=expand, pady=pady)
            inner = tk.Frame(outer, bg=S_CARD_BG, padx=padx_inner, pady=pady_inner)
            inner.pack(fill="both", expand=True)
            return inner

        def make_button(
            parent: tk.Misc,
            text: str,
            command: Any,
            *,
            primary: bool = False,
            small: bool = False,
            padx: int = 12,
            pady: int = 5,
        ) -> tk.Button:
            bg_normal = S_ACCENT if primary else S_BTN_BG
            bg_hover = S_ACCENT_HOVER if primary else S_BTN_HOVER
            fg_color = S_ACCENT_INK if primary else S_TEXT
            btn_font = settings_font_section if primary else (settings_font_small if small else settings_font)
            btn = tk.Button(
                parent,
                text=text,
                command=command,
                bg=bg_normal,
                fg=fg_color,
                activebackground=bg_hover,
                activeforeground=fg_color,
                relief="flat",
                bd=0,
                highlightthickness=0,
                padx=padx,
                pady=pady,
                font=btn_font,
                cursor="hand2",
            )
            btn.bind("<Enter>", lambda _e: btn.configure(bg=bg_hover))
            btn.bind("<Leave>", lambda _e: btn.configure(bg=bg_normal))
            return btn

        # Flat segmented top navigation bar (no 3D ttk.Notebook border box)
        nav_track = tk.Frame(shell, bg=S_TAB_TRACK_BG, padx=4, pady=4, bd=0, highlightthickness=0)
        nav_track.pack(fill="x", pady=(0, 12))

        content_host = tk.Frame(
            shell,
            bg=S_PAGE_BG,
            width=564,
            height=504,
            bd=0,
            highlightthickness=0,
        )
        content_host.pack(fill="both", expand=True)
        content_host.pack_propagate(False)

        providers_tab = tk.Frame(content_host, bg=S_PAGE_BG)
        display_tab = tk.Frame(content_host, bg=S_PAGE_BG)
        wallpaper_tab = tk.Frame(content_host, bg=S_PAGE_BG)
        floating_tab = tk.Frame(content_host, bg=S_PAGE_BG)

        @contextlib.contextmanager
        def _freeze_redraw(widget: tk.Misc | None):
            hwnd = 0
            user32 = None
            try:
                if sys.platform == "win32" and widget is not None and widget.winfo_ismapped():
                    import ctypes

                    user32 = ctypes.windll.user32
                    hwnd = int(widget.winfo_id())
                    if hwnd:
                        user32.SendMessageW(hwnd, 0x000B, 0, 0)
            except Exception:
                hwnd = 0
            try:
                yield
                if hwnd and widget is not None:
                    widget.update_idletasks()
            finally:
                if hwnd and user32 is not None:
                    try:
                        user32.SendMessageW(hwnd, 0x000B, 1, 0)
                        # RDW_INVALIDATE (0x1) | RDW_ALLCHILDREN (0x80) | RDW_UPDATENOW (0x100) — no RDW_ERASE (0x4)
                        user32.RedrawWindow(hwnd, None, None, 0x0181)
                    except Exception:
                        pass

        main_tabs: list[tuple[str, tk.Frame]] = [
            ("用量", providers_tab),
            ("副屏", display_tab),
            ("动态壁纸", wallpaper_tab),
            ("悬浮窗", floating_tab),
        ]
        main_tab_btns: dict[str, tk.Label] = {}
        active_main_tab: dict[str, Any] = {"title": initial_tab, "initialized": False}

        def select_main_tab(title: str) -> None:
            if active_main_tab.get("initialized") and active_main_tab["title"] == title:
                return
            active_main_tab["title"] = title
            active_main_tab["initialized"] = True
            with _freeze_redraw(content_host):
                for tab_title, frame in main_tabs:
                    btn = main_tab_btns.get(tab_title)
                    if tab_title == title:
                        frame.pack(fill="both", expand=True)
                        if btn is not None:
                            btn.configure(
                                bg=S_TAB_ACTIVE_BG,
                                fg=S_ACCENT,
                                font=settings_font_section,
                            )
                    else:
                        frame.pack_forget()
                        if btn is not None:
                            btn.configure(
                                bg=S_TAB_TRACK_BG,
                                fg=S_MUTED,
                                font=settings_font,
                            )

        for tab_title, _frame in main_tabs:
            pill = tk.Label(
                nav_track,
                text=_ui(tab_title),
                bg=S_TAB_TRACK_BG,
                fg=S_MUTED,
                font=settings_font,
                padx=18,
                pady=6,
                cursor="hand2",
            )
            pill.pack(side="left", padx=(0, 2))
            pill.bind("<Button-1>", lambda _e, t=tab_title: select_main_tab(t))
            main_tab_btns[tab_title] = pill

        providers_pad = tk.Frame(providers_tab, bg=S_PAGE_BG)
        providers_pad.pack(fill="both", expand=True)
        display_pad = tk.Frame(display_tab, bg=S_PAGE_BG)
        display_pad.pack(fill="both", expand=True)
        floating_pad = tk.Frame(floating_tab, bg=S_PAGE_BG)
        floating_pad.pack(fill="both", expand=True)
        wallpaper_outer_pad = tk.Frame(wallpaper_tab, bg=S_PAGE_BG)
        wallpaper_outer_pad.pack(fill="both", expand=True)

        # Flat sub-navigation pill bar for Wallpaper tab
        wp_nav_row = tk.Frame(wallpaper_outer_pad, bg=S_PAGE_BG)
        wp_nav_row.pack(fill="x", pady=(0, 8))
        wp_nav_track = tk.Frame(wp_nav_row, bg=S_TAB_TRACK_BG, padx=3, pady=3, bd=0, highlightthickness=0)
        wp_nav_track.pack(side="left")

        wp_content_host = tk.Frame(
            wallpaper_outer_pad,
            bg=S_PAGE_BG,
            width=564,
            height=460,
            bd=0,
            highlightthickness=0,
        )
        wp_content_host.pack(fill="both", expand=True)
        wp_content_host.pack_propagate(False)

        directories_tab = tk.Frame(wp_content_host, bg=S_PAGE_BG)
        weights_tab = tk.Frame(wp_content_host, bg=S_PAGE_BG)
        wp_subtabs: list[tuple[str, tk.Frame]] = [
            ("目录", directories_tab),
            ("随机权重", weights_tab),
        ]
        wp_subtab_btns: dict[str, tk.Label] = {}
        wp_subtab_state: dict[str, Any] = {
            "active": initial_wallpaper_tab,
            "initialized": False,
            "weights_dirty": True,
            "refresh_weights": None,
        }

        def select_wallpaper_subtab(title: str) -> None:
            already_active = wp_subtab_state.get("initialized") and wp_subtab_state.get("active") == title
            wp_subtab_state["active"] = title
            wp_subtab_state["initialized"] = True
            if not already_active:
                with _freeze_redraw(wp_content_host):
                    for sub_title, frame in wp_subtabs:
                        btn = wp_subtab_btns.get(sub_title)
                        if sub_title == title:
                            frame.pack(fill="both", expand=True)
                            if btn is not None:
                                btn.configure(
                                    bg=S_TAB_ACTIVE_BG,
                                    fg=S_ACCENT_BLUE,
                                    font=settings_font_section,
                                )
                        else:
                            frame.pack_forget()
                            if btn is not None:
                                btn.configure(
                                    bg=S_TAB_TRACK_BG,
                                    fg=S_MUTED,
                                    font=settings_font_small,
                                )
            if (
                title == "随机权重"
                and wp_subtab_state.get("weights_dirty")
                and callable(wp_subtab_state.get("refresh_weights"))
            ):
                wp_subtab_state["refresh_weights"]()

        for sub_title, _frame in wp_subtabs:
            sub_pill = tk.Label(
                wp_nav_track,
                text=_ui(sub_title),
                bg=S_TAB_TRACK_BG,
                fg=S_MUTED,
                font=settings_font_small,
                padx=14,
                pady=4,
                cursor="hand2",
            )
            sub_pill.pack(side="left", padx=(0, 2))
            sub_pill.bind("<Button-1>", lambda _e, t=sub_title: select_wallpaper_subtab(t))
            wp_subtab_btns[sub_title] = sub_pill

        directories_pad = tk.Frame(directories_tab, bg=S_PAGE_BG)
        directories_pad.pack(fill="both", expand=True)
        weights_pad = tk.Frame(weights_tab, bg=S_PAGE_BG)
        weights_pad.pack(fill="both", expand=True)

        select_main_tab(initial_tab)
        select_wallpaper_subtab(initial_wallpaper_tab)
        setattr(win, "_select_settings_tab", select_main_tab)
        setattr(win, "_select_wallpaper_tab", select_wallpaper_subtab)

        def _bind_scrollable_canvas(
            canvas: tk.Canvas,
            rows_frame: tk.Frame,
            window_id: int,
        ) -> tuple[Callable[[], None], Callable[[Any], str]]:
            scroll_state: dict[str, Any] = {
                "width": -1,
                "scrollregion": None,
            }

            def sync_scrollregion() -> None:
                try:
                    if not canvas.winfo_exists() or not rows_frame.winfo_exists():
                        return
                    canvas_w = max(1, int(canvas.winfo_width()))
                    canvas_h = max(1, int(canvas.winfo_height()))
                    req_h = max(0, int(rows_frame.winfo_reqheight()))
                    region_h = max(req_h, canvas_h)
                    region = (0, 0, canvas_w, region_h)
                    if region != scroll_state["scrollregion"]:
                        scroll_state["scrollregion"] = region
                        canvas.configure(scrollregion=region)
                    if req_h <= canvas_h:
                        y0, _y1 = canvas.yview()
                        if y0 != 0.0:
                            canvas.yview_moveto(0.0)
                except Exception:
                    pass

            def on_rows_configure(_event: Any = None) -> None:
                sync_scrollregion()

            def on_canvas_configure(event: Any = None) -> None:
                try:
                    w = int(getattr(event, "width", 0) or canvas.winfo_width())
                    if w > 1 and w != scroll_state["width"]:
                        scroll_state["width"] = w
                        canvas.itemconfigure(window_id, width=w)
                except Exception:
                    pass
                sync_scrollregion()

            def on_mousewheel(event: Any) -> str:
                try:
                    req_h = int(rows_frame.winfo_reqheight())
                    canvas_h = int(canvas.winfo_height())
                    if req_h <= canvas_h:
                        y0, _y1 = canvas.yview()
                        if y0 != 0.0:
                            canvas.yview_moveto(0.0)
                        return "break"
                    delta = int(getattr(event, "delta", 0) or 0)
                    if delta:
                        steps = -int(delta / 120)
                        if steps == 0:
                            steps = -1 if delta > 0 else 1
                        canvas.yview_scroll(steps, "units")
                except Exception:
                    pass
                return "break"

            rows_frame.bind("<Configure>", on_rows_configure)
            canvas.bind("<Configure>", on_canvas_configure)
            return sync_scrollregion, on_mousewheel

        # ── Tab 1: 用量 (Two-column flat draggable bar grid) ──
        provider_order = list(cfg.get("provider_order") or DEFAULT_PROVIDERS)
        enabled_providers = list(cfg.get("providers") or DEFAULT_PROVIDERS)
        disabled_seed = [pid for pid in provider_order if pid not in set(enabled_providers)]
        init_order, init_enabled, _init_disabled = resolve_provider_config(
            enabled_providers,
            disabled_seed,
            provider_order,
        )
        init_left, init_right = resolve_provider_columns(
            init_enabled,
            _init_disabled,
            init_order,
            cfg.get("provider_columns"),
        )
        left_col_items: list[str] = list(init_left)
        right_col_items: list[str] = list(init_right)
        provider_order[:] = list(left_col_items) + [
            pid for pid in right_col_items if pid not in set(left_col_items)
        ]
        enabled_providers[:] = [
            pid for pid in provider_order if pid in set(init_enabled)
        ]
        scanned_cache: dict[str, dict[str, Any]] = {}

        providers_card = make_card(
            providers_pad,
            pady=(0, 10),
            padx_inner=14,
            pady_inner=12,
            expand=True,
            fill="both",
        )
        prov_header_row = tk.Frame(providers_card, bg=S_CARD_BG)
        prov_header_row.pack(fill="x", pady=(0, 2))
        tk.Label(
            prov_header_row,
            text=_tr("显示的用量（双列拖动排布）", 'Usage layout (drag between columns)'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(side="left")
        provider_status = tk.Label(
            prov_header_row,
            text="",
            fg=S_ACCENT_BLUE,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="e",
        )
        provider_status.pack(side="right")

        tk.Label(
            providers_card,
            text=_tr("按住任意栏可在左右两列间自由拖动，直接决定面板显示位置；未勾选默认置灰。", 'Drag rows between columns to set their panel position. Unchecked rows are dimmed.'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(0, 8))

        # Two-column board container (scrollable list area, direct grid children on board_inner)
        board_wrap = tk.Frame(
            providers_card,
            bg=S_COL_BG,
            highlightthickness=0,
            bd=0,
        )
        board_wrap.pack(fill="both", expand=True)

        board_canvas = tk.Canvas(
            board_wrap,
            bg=S_COL_BG,
            highlightthickness=0,
            bd=0,
            height=220,
        )
        board_scrollbar = ttk.Scrollbar(
            board_wrap,
            orient="vertical",
            command=board_canvas.yview,
            style="Vertical.TScrollbar",
        )
        board_scrollbar.pack(side="right", fill="y")
        board_canvas.pack(side="left", fill="both", expand=True)
        board_canvas.configure(yscrollcommand=board_scrollbar.set)
        board_inner = tk.Frame(board_canvas, bg=S_COL_BG, padx=8, pady=8)
        board_canvas_window = board_canvas.create_window(
            (0, 0), window=board_inner, anchor="nw"
        )
        sync_board_scrollregion, scroll_board_list = _bind_scrollable_canvas(
            board_canvas,
            board_inner,
            board_canvas_window,
        )

        def bind_board_list_wheel(widget: Any) -> None:
            widget.bind("<MouseWheel>", scroll_board_list)
            for child in widget.winfo_children():
                bind_board_list_wheel(child)

        board_inner.grid_columnconfigure(0, weight=1, uniform="prov_col")
        board_inner.grid_columnconfigure(1, weight=1, uniform="prov_col")

        col_hdr_frames: list[tk.Frame] = []
        col_count_labels: list[tk.Label] = []
        for col_idx, col_title in enumerate((_tr("左栏 · COLUMN 1", 'Left Column · COLUMN 1'), _tr("右栏 · COLUMN 2", 'Right Column · COLUMN 2'))):
            hdr = tk.Frame(board_inner, bg=S_COL_BG, padx=6, pady=2)
            hdr.grid(row=0, column=col_idx, sticky="ew", pady=(0, 4))
            tk.Label(
                hdr,
                text=col_title,
                fg=S_MUTED,
                bg=S_COL_BG,
                font=settings_font_badge,
                anchor="w",
            ).pack(side="left")
            cnt_lbl = tk.Label(
                hdr,
                text="",
                fg=S_SUBTLE,
                bg=S_COL_BG,
                font=settings_font_badge,
                anchor="e",
            )
            cnt_lbl.pack(side="right")
            col_hdr_frames.append(hdr)
            col_count_labels.append(cnt_lbl)

        bar_widgets_by_id: dict[str, tk.Frame] = {}
        bar_records: dict[str, dict[str, Any]] = {}
        slot_bounds: dict[str, tuple[int, int]] = {}
        last_packed_cols: list[tuple[str, ...] | None] = [None, None]
        empty_boxes: list[tk.Frame] = []
        for col_idx in range(2):
            eb = tk.Frame(
                board_inner,
                bg=S_BAR_OFF_BG,
                highlightthickness=0,
                padx=10,
                pady=18,
            )
            tk.Label(
                eb,
                text=_tr("拖动另一列的栏到此列", 'Drag rows from the other column here'),
                fg=S_SUBTLE,
                bg=S_BAR_OFF_BG,
                font=settings_font_small,
            ).pack()
            empty_boxes.append(eb)
        bind_board_list_wheel(board_wrap)

        provider_drag: dict[str, Any] = {
            "item": None,
            "moved": False,
            "start_x": 0,
            "start_y": 0,
            "last_col": 0,
            "last_idx": 0,
        }

        def persist_providers(*, refresh: bool = True, force_fetch: bool = False) -> None:
            enabled_set = set(enabled_providers)
            combined_order = list(left_col_items) + [
                pid for pid in right_col_items if pid not in set(left_col_items)
            ]
            for pid in provider_order:
                if pid not in combined_order:
                    combined_order.append(pid)
            disabled_list = [pid for pid in combined_order if pid not in enabled_set]
            l_col, r_col = resolve_provider_columns(
                [pid for pid in combined_order if pid in enabled_set],
                disabled_list,
                combined_order,
                [left_col_items, right_col_items],
            )
            order, enabled, disabled = resolve_provider_config(
                [pid for pid in (l_col + r_col) if pid in enabled_set],
                [pid for pid in (l_col + r_col) if pid not in enabled_set],
                l_col + r_col,
            )
            left_col_items[:] = l_col
            right_col_items[:] = r_col
            provider_order[:] = order
            enabled_providers[:] = enabled
            cfg["provider_columns"] = [list(l_col), list(r_col)]
            cfg["provider_order"] = list(order)
            cfg["providers"] = list(enabled)
            cfg["disabled_providers"] = list(disabled)
            save_config(cfg)
            if refresh:
                schedule_render(0)
                loaded_ids = {
                    str(getattr(p, "provider_id", ""))
                    for p in (state.get("providers") or [])
                }
                need_network = force_fetch or any(
                    active_id not in loaded_ids
                    for active_id in active_providers(list(enabled))
                )
                if need_network:
                    refresh_async(force=force_fetch)

        def _refresh_slot_bounds(*, redraw: bool = False) -> None:
            try:
                board_inner.update_idletasks()
                if redraw and sys.platform == "win32" and board_wrap.winfo_ismapped():
                    import ctypes

                    # RDW_INVALIDATE (0x1) | RDW_ALLCHILDREN (0x80) | RDW_UPDATENOW (0x100) — no RDW_ERASE (0x4)
                    ctypes.windll.user32.RedrawWindow(
                        int(board_wrap.winfo_id()), None, None, 0x0181
                    )
            except Exception:
                pass
            slot_bounds.clear()
            for pid, w in bar_widgets_by_id.items():
                try:
                    if w.winfo_exists() and w.winfo_ismapped():
                        h = max(24, int(w.winfo_height() or 32))
                        slot_bounds[pid] = (int(w.winfo_rooty()), h)
                except Exception:
                    pass

        def _target_column_and_index(x_root: int, y_root: int) -> tuple[int, int]:
            split_x = board_inner.winfo_rootx() + (board_inner.winfo_width() // 2)
            last_col = int(provider_drag.get("last_col", 0))
            if last_col == 0:
                target_col = 1 if x_root >= split_x + 10 else 0
            else:
                target_col = 0 if x_root < split_x - 10 else 1

            col_list = left_col_items if target_col == 0 else right_col_items
            drag_item = provider_drag.get("item")
            cur_idx_in_col = col_list.index(drag_item) if drag_item in col_list else -1
            remaining = [pid for pid in col_list if pid != drag_item]
            for idx, pid in enumerate(remaining):
                bounds = slot_bounds.get(pid)
                if bounds is not None:
                    top_y, h = bounds
                else:
                    w = bar_widgets_by_id.get(pid)
                    if w is None or not w.winfo_exists():
                        continue
                    top_y = int(w.winfo_rooty())
                    h = max(24, int(w.winfo_height() or 32))
                mid_y = top_y + (h // 2)
                if cur_idx_in_col != -1:
                    if idx < cur_idx_in_col:
                        threshold = mid_y - 6
                    else:
                        threshold = mid_y + 6
                else:
                    threshold = mid_y
                if y_root < threshold:
                    return target_col, idx
            return target_col, len(remaining)

        def toggle_provider(pid: str) -> None:
            if not pid:
                return
            enabled_set = set(enabled_providers)
            if pid in enabled_set:
                enabled_set.remove(pid)
            else:
                enabled_set.add(pid)
            combined = list(left_col_items) + list(right_col_items)
            enabled_providers[:] = [item for item in combined if item in enabled_set]
            persist_providers(refresh=True, force_fetch=False)
            render_provider_tree()

        def start_bar_drag(event: Any, pid: str, *, toggle_only: bool = False) -> str:
            if toggle_only:
                provider_drag["item"] = None
                provider_drag["moved"] = False
                toggle_provider(pid)
                return "break"
            x_root = int(getattr(event, "x_root", 0))
            y_root = int(getattr(event, "y_root", 0))
            provider_drag["item"] = pid
            provider_drag["moved"] = False
            provider_drag["start_x"] = x_root
            provider_drag["start_y"] = y_root
            if pid in left_col_items:
                provider_drag["last_col"] = 0
                provider_drag["last_idx"] = left_col_items.index(pid)
            elif pid in right_col_items:
                provider_drag["last_col"] = 1
                provider_drag["last_idx"] = right_col_items.index(pid)
            _refresh_slot_bounds()
            render_provider_tree()
            return "break"

        def motion_bar_drag(event: Any) -> str | None:
            item = provider_drag.get("item")
            if not item:
                return None
            x_root = int(getattr(event, "x_root", 0))
            y_root = int(getattr(event, "y_root", 0))
            if (
                not provider_drag.get("moved")
                and abs(x_root - int(provider_drag.get("start_x", 0))) < 4
                and abs(y_root - int(provider_drag.get("start_y", 0))) < 4
            ):
                return "break"
            if not provider_drag.get("moved"):
                provider_drag["moved"] = True

            target_col, target_idx = _target_column_and_index(x_root, y_root)
            provider_drag["last_col"] = target_col
            provider_drag["last_idx"] = target_idx
            new_left, new_right = move_provider_in_columns(
                left_col_items,
                right_col_items,
                item,
                target_col,
                target_idx,
            )
            if new_left != left_col_items or new_right != right_col_items:
                left_col_items[:] = new_left
                right_col_items[:] = new_right
                render_provider_tree()
            return "break"

        def release_bar_drag(_event: Any) -> str | None:
            item = provider_drag.get("item")
            if not item:
                return None
            moved = bool(provider_drag.get("moved"))
            provider_drag["item"] = None
            provider_drag["moved"] = False
            if moved:
                persist_providers(refresh=True, force_fetch=False)
            render_provider_tree()
            return "break"

        def _ensure_bar_record(pid: str) -> dict[str, Any]:
            rec = bar_records.get(pid)
            if rec is not None and rec["bar"].winfo_exists():
                return rec
            bar = tk.Frame(
                board_inner,
                bg=S_BAR_ON_BG,
                highlightbackground=S_BAR_ON_BORDER,
                highlightcolor=S_BAR_ON_BORDER,
                highlightthickness=0,
                bd=0,
                padx=10,
                pady=6,
                cursor="fleur",
            )
            chk_lbl = tk.Label(
                bar,
                text="☑",
                fg=S_ACCENT,
                bg=S_BAR_ON_BG,
                font=settings_font_section,
                cursor="hand2",
                padx=2,
            )
            chk_lbl.pack(side="left")
            title_lbl = tk.Label(
                bar,
                text=provider_label(pid),
                fg=S_TEXT,
                bg=S_BAR_ON_BG,
                font=settings_font_section,
                anchor="w",
                cursor="fleur",
            )
            title_lbl.pack(side="left", fill="x", expand=True, padx=(6, 4))
            grip_lbl = tk.Label(
                bar,
                text="⋮⋮",
                fg=S_MUTED,
                bg=S_BAR_ON_BG,
                font=settings_font,
                cursor="fleur",
                padx=2,
            )
            grip_lbl.pack(side="right")
            sub_lbl = tk.Label(
                bar,
                text="",
                fg=S_ACCENT,
                bg=S_BAR_ON_BG,
                font=settings_font_badge,
                anchor="e",
                cursor="fleur",
            )
            sub_lbl.pack(side="right", padx=(0, 4))

            chk_lbl.bind(
                "<ButtonPress-1>",
                lambda ev, item_id=pid: start_bar_drag(ev, item_id, toggle_only=True),
            )
            for drag_w in (bar, title_lbl, sub_lbl, grip_lbl):
                drag_w.bind(
                    "<ButtonPress-1>",
                    lambda ev, item_id=pid: start_bar_drag(ev, item_id, toggle_only=False),
                )
                drag_w.bind("<B1-Motion>", motion_bar_drag)
                drag_w.bind("<ButtonRelease-1>", release_bar_drag)
                drag_w.bind(
                    "<Double-Button-1>",
                    lambda _ev, item_id=pid: toggle_provider(item_id),
                )

            rec = {
                "bar": bar,
                "chk": chk_lbl,
                "title": title_lbl,
                "sub": sub_lbl,
                "grip": grip_lbl,
                "sig": None,
                "cell": None,
            }
            bind_board_list_wheel(bar)
            bar_records[pid] = rec
            bar_widgets_by_id[pid] = bar
            return rec

        def render_provider_tree(
            scanned: list[dict[str, Any]] | None = None,
            *,
            note: str = "",
        ) -> None:
            if scanned is not None:
                scanned_cache.clear()
                for row in scanned:
                    rid = str(row.get("id") or "")
                    if rid:
                        scanned_cache[rid] = row
            elif not scanned_cache:
                for row in scan_providers(enabled_providers, provider_order):
                    rid = str(row.get("id") or "")
                    if rid:
                        scanned_cache[rid] = row

            enabled_set = set(enabled_providers)
            dragging_id = provider_drag.get("item")

            for col_idx, col_list in enumerate((left_col_items, right_col_items)):
                enabled_in_col = sum(1 for pid in col_list if pid in enabled_set)
                col_count_labels[col_idx].configure(
                    text=_tr(f"显示 {enabled_in_col} / {len(col_list)}", f'Showing {enabled_in_col} / {len(col_list)}')
                )

                for pid in col_list:
                    info = scanned_cache.get(pid) or {
                        "id": pid,
                        "label": provider_label(pid),
                        "available": provider_available(pid),
                    }
                    checked = pid in enabled_set
                    is_dragging = pid == dragging_id
                    available = bool(info.get("available"))
                    lbl_text = str(info.get("label") or provider_label(pid))
                    sig = (checked, is_dragging, available, lbl_text)
                    rec = _ensure_bar_record(pid)
                    if rec.get("sig") == sig:
                        continue
                    rec["sig"] = sig

                    if is_dragging:
                        card_bg = S_BAR_DRAG_BG
                        border_col = S_BAR_DRAG_BORDER
                        title_fg = S_ACCENT if checked else S_TEXT
                        meta_fg = S_ACCENT
                        check_fg = S_ACCENT if checked else S_MUTED
                    elif checked:
                        card_bg = S_BAR_ON_BG
                        border_col = S_BAR_ON_BORDER
                        title_fg = S_TEXT
                        meta_fg = S_ACCENT if available else S_MUTED
                        check_fg = S_ACCENT
                    else:
                        card_bg = S_BAR_OFF_BG
                        border_col = S_BAR_OFF_BORDER
                        title_fg = S_BAR_OFF_FG
                        meta_fg = S_BAR_OFF_FG
                        check_fg = S_BAR_OFF_FG

                    if is_dragging:
                        badge_text = _tr("拖动中", 'Dragging')
                    elif checked:
                        badge_text = _tr("已就绪", 'Ready') if available else _tr("未检测", 'Not Detected')
                    else:
                        badge_text = _tr("未启用", 'Disabled') if available else _tr("未检测", 'Not Detected')

                    rec["bar"].configure(
                        bg=card_bg,
                        highlightbackground=border_col,
                        highlightcolor=border_col,
                        highlightthickness=1 if is_dragging else 0,
                    )
                    rec["chk"].configure(
                        text="☑" if checked else "☐",
                        fg=check_fg,
                        bg=card_bg,
                    )
                    rec["title"].configure(
                        text=lbl_text,
                        fg=title_fg,
                        bg=card_bg,
                        font=settings_font_section if checked else settings_font,
                    )
                    rec["grip"].configure(
                        fg=S_ACCENT if is_dragging else (S_SUBTLE if checked else S_BAR_OFF_FG),
                        bg=card_bg,
                    )
                    rec["sub"].configure(
                        text=badge_text,
                        fg=meta_fg,
                        bg=card_bg,
                    )

            cols_now = (tuple(left_col_items), tuple(right_col_items))
            if (last_packed_cols[0], last_packed_cols[1]) != cols_now:
                last_packed_cols[0] = cols_now[0]
                last_packed_cols[1] = cols_now[1]
                active_ids = set(left_col_items) | set(right_col_items)
                with _freeze_redraw(board_inner):
                    for pid, rec in bar_records.items():
                        if pid not in active_ids and rec.get("cell") is not None:
                            rec["bar"].grid_forget()
                            rec["cell"] = None
                    for col_idx, col_list in enumerate((left_col_items, right_col_items)):
                        if not col_list:
                            empty_boxes[col_idx].grid(
                                row=1, column=col_idx, sticky="ew", padx=4, pady=3
                            )
                        else:
                            empty_boxes[col_idx].grid_forget()
                            for row_idx, pid in enumerate(col_list):
                                rec = bar_records[pid]
                                target_cell = (row_idx + 1, col_idx)
                                if rec.get("cell") != target_cell:
                                    rec["bar"].grid(
                                        row=row_idx + 1,
                                        column=col_idx,
                                        sticky="ew",
                                        padx=4,
                                        pady=3,
                                    )
                                    rec["cell"] = target_cell
                    sync_board_scrollregion()
                _refresh_slot_bounds(redraw=False)

            available_count = sum(1 for row in scanned_cache.values() if row.get("available"))
            text = (
                _tr(f"已启用 {len(enabled_providers)} / {len(provider_order)}"
                f"（左 {sum(1 for p in left_col_items if p in enabled_set)} · "
                f"右 {sum(1 for p in right_col_items if p in enabled_set)}）"
                f" · 可用 {available_count}", f'Enabled {len(enabled_providers)} / {len(provider_order)} (left {sum(1 for p in left_col_items if p in enabled_set)} · right {sum(1 for p in right_col_items if p in enabled_set)}) · Available {available_count}')
            )
            if note:
                text = f"{text} · {note}"
            provider_status.configure(text=text)

        def on_scan_providers() -> None:
            scanned = scan_providers(enabled_providers, provider_order)
            changed = False
            for row in scanned:
                pid = str(row.get("id") or "")
                if not pid or pid in provider_order:
                    continue
                provider_order.append(pid)
                if len(left_col_items) <= len(right_col_items):
                    left_col_items.append(pid)
                else:
                    right_col_items.append(pid)
                if pid not in enabled_providers:
                    enabled_providers.append(pid)
                changed = True
            if changed:
                persist_providers(refresh=True, force_fetch=True)
            else:
                refresh_async(force=True)
            render_provider_tree(scanned, note=_tr("已扫描", 'Scanned'))

        def on_balance_columns() -> None:
            combined = list(left_col_items) + [
                pid for pid in right_col_items if pid not in set(left_col_items)
            ]
            enabled_set = set(enabled_providers)
            en_items = [pid for pid in combined if pid in enabled_set]
            dis_items = [pid for pid in combined if pid not in enabled_set]
            mid = (len(en_items) + 1) // 2
            left_col_items[:] = list(en_items[:mid])
            right_col_items[:] = list(en_items[mid:])
            for pid in dis_items:
                if len(left_col_items) <= len(right_col_items):
                    left_col_items.append(pid)
                else:
                    right_col_items.append(pid)
            persist_providers(refresh=True, force_fetch=False)
            render_provider_tree(note=_tr("已均分双列", 'Columns balanced'))

        provider_button_row = tk.Frame(providers_card, bg=S_CARD_BG)
        provider_button_row.pack(fill="x", pady=(8, 0))
        make_button(
            provider_button_row,
            _tr("⟳ 扫描可用账号", '⟳ Scan Accounts'),
            on_scan_providers,
            small=True,
            padx=10,
            pady=4,
        ).pack(side="left")
        make_button(
            provider_button_row,
            _tr("⇄ 自动均分两列", '⇄ Auto-Balance'),
            on_balance_columns,
            small=True,
            padx=10,
            pady=4,
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            provider_button_row,
            text=_tr("双击或点左侧方框可快速勾选/置灰", 'Double-click or check the box to toggle'),
            fg=S_SUBTLE,
            bg=S_CARD_BG,
            font=settings_font_badge,
        ).pack(side="right")

        render_provider_tree()

        # Timezone card in Tab 1
        tz_card = make_card(providers_pad, pady=(0, 0), padx_inner=14, pady_inner=10)
        tz_row = tk.Frame(tz_card, bg=S_CARD_BG)
        tz_row.pack(fill="x")
        tz_left = tk.Frame(tz_row, bg=S_CARD_BG)
        tz_left.pack(side="left", fill="x", expand=True)
        tk.Label(
            tz_left,
            text=_tr("额度重置时区", 'Quota Reset Timezone'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            tz_left,
            text=_tr("用于 LLM Proxy 重置倒计时（支持 UTC+8、UTC 或 IANA 名称）", 'LLM Proxy reset countdown (UTC+8, UTC or IANA name)'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="w",
        ).pack(fill="x", pady=(1, 0))

        timezone_var = tk.StringVar(
            value=_normalize_llm_proxy_timezone(
                state.get("llm_proxy_timezone") or cfg.get("llm_proxy_timezone")
            )
        )

        def persist_timezone(_event: Any = None) -> None:
            normalized = _normalize_llm_proxy_timezone(timezone_var.get())
            timezone_var.set(normalized)
            if normalized == state.get("llm_proxy_timezone"):
                return
            state["llm_proxy_timezone"] = normalized
            cfg["llm_proxy_timezone"] = normalized
            save_config(cfg)
            refresh_async(force=True)

        timezone_combo = ttk.Combobox(
            tz_row,
            textvariable=timezone_var,
            values=("UTC+8", "UTC", "UTC+9", "UTC-5", "UTC-8"),
            font=settings_font,
            width=12,
        )
        timezone_combo.pack(side="right", padx=(10, 0))
        timezone_combo.bind("<<ComboboxSelected>>", persist_timezone)
        timezone_combo.bind("<Return>", persist_timezone)
        timezone_combo.bind("<FocusOut>", persist_timezone)

        # ── Tab 2: 副屏 ──
        settings_monitors = _enumerate_monitors()
        monitor_by_label = {_monitor_label(monitor): monitor for monitor in settings_monitors}
        selected_monitor = _choose_panel_monitor(
            settings_monitors,
            state.get("monitor_device"),
            state.get("monitor_id"),
        )
        if selected_monitor is None:
            selected_monitor = _choose_panel_monitor(settings_monitors, None)
        selected_monitor_label = (
            _monitor_label(selected_monitor) if selected_monitor is not None else _tr("未检测到显示器", 'No monitors detected')
        )

        display_card = make_card(display_pad, pady=(0, 10))
        tk.Label(
            display_card,
            text=_tr("目标显示器与整屏仪表盘", 'Monitor & Full-Screen Dashboard'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        monitor_var = tk.StringVar(value=selected_monitor_label)

        def selected_device() -> str | None:
            selected = monitor_by_label.get(monitor_var.get())
            return selected.device if selected is not None else None

        def place_settings_window() -> None:
            place_settings_on_primary(win)

        panel_mode_var = tk.BooleanVar(
            value=state.get("display_mode") == DISPLAY_MODE_PANEL
        )

        def update_panel_status() -> None:
            active = bool(state.get("panel_active"))
            requested = bool(panel_mode_var.get())
            if requested and active:
                panel_status.configure(
                    text=_tr("● 已铺满所选副屏；在副屏任意位置右键可随时打开菜单。", '● Dashboard fills the selected display. Right-click anywhere for the menu.'),
                    fg=S_ACCENT,
                )
            elif requested:
                panel_status.configure(
                    text=_tr("● 所选副屏未连接，当前临时回退为桌面悬浮窗。", '● Selected display disconnected. Using the floating HUD temporarily.'),
                    fg="#fbbf24",
                )
            else:
                panel_status.configure(
                    text=_tr("开启后将自动铺满所选副屏；拔下副屏时自动回到桌面悬浮窗。", 'Fills the selected display. Returns to the floating HUD when disconnected.'),
                    fg=S_MUTED,
                )
            try:
                scale.configure(state="disabled" if requested else "normal")
                size_label.configure(text=_tr("自动", 'Auto') if requested else f"{size_var.get()} pt")
                chk.configure(state="disabled" if requested else "normal")
                opacity_state = "disabled" if requested else "normal"
                bg_op_scale.configure(state=opacity_state)
                tx_op_scale.configure(state=opacity_state)
                bg_op_lbl.configure(text=_tr("固定黑底", 'Solid black') if requested else f"{bg_op_var.get()}%")
                tx_op_lbl.configure(text=_tr("固定清晰", 'Full opacity') if requested else f"{tx_op_var.get()}%")
                opacity_hint.configure(
                    text=(
                        _tr("专用副屏使用不透明黑底和清晰文字；上方字号与透明度设置仅用于桌面悬浮窗。", 'Aux Display uses a solid black background and opaque text. Font and opacity controls apply to the floating HUD.')
                        if requested
                        else _tr("拖动滑块即可实时预览。数值越高越不透明，底板与文字互不影响。", 'Drag sliders to preview. Higher values are more opaque; background and text are independent.')
                    )
                )
            except Exception:
                pass

        def on_panel_mode_toggle() -> None:
            requested = bool(panel_mode_var.get())
            device = selected_device()
            if requested and device is None:
                fallback = _choose_panel_monitor(_enumerate_monitors(), None)
                device = fallback.device if fallback is not None else None
            set_display_mode(
                DISPLAY_MODE_PANEL if requested else DISPLAY_MODE_FLOAT,
                device,
                persist=True,
            )
            update_panel_status()
            win.after_idle(place_settings_window)

        panel_checkbox = tk.Checkbutton(
            display_card,
            text=_tr("启用专用副屏模式（整屏双列仪表盘）", 'Enable Dedicated Aux Display Mode'),
            variable=panel_mode_var,
            command=on_panel_mode_toggle,
            bg=S_CARD_BG,
            fg=S_TEXT,
            activebackground=S_CARD_BG,
            activeforeground=S_TEXT,
            selectcolor=S_INPUT_BG,
            font=settings_font,
            anchor="w",
            bd=0,
            highlightthickness=0,
        )
        panel_checkbox.pack(fill="x", pady=(0, 8))

        def on_monitor_selected(_event: Any = None) -> None:
            label = monitor_var.get()
            monitor = monitor_by_label.get(label)
            if monitor is None:
                return
            state["monitor_device"] = monitor.device
            state["monitor_id"] = monitor.monitor_id or state.get("monitor_id")
            cfg["monitor_device"] = monitor.device
            cfg["monitor_id"] = state.get("monitor_id")
            if panel_mode_var.get():
                set_display_mode(DISPLAY_MODE_PANEL, monitor.device, persist=True)
            else:
                save_config(cfg)
            update_panel_status()
            win.after_idle(place_settings_window)

        mon_select_row = tk.Frame(display_card, bg=S_CARD_BG)
        mon_select_row.pack(fill="x", pady=(0, 6))
        tk.Label(
            mon_select_row,
            text=_tr("副屏显示器", 'Aux Display Monitor'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            width=10,
            anchor="w",
        ).pack(side="left")
        monitor_combo = ttk.Combobox(
            mon_select_row,
            textvariable=monitor_var,
            values=tuple(monitor_by_label.keys()) or (_tr("未检测到显示器", 'No monitors detected'),),
            state="readonly",
            font=settings_font_small,
        )
        monitor_combo.pack(side="left", fill="x", expand=True)
        monitor_combo.bind("<<ComboboxSelected>>", on_monitor_selected)

        panel_status = tk.Label(
            display_card,
            text="",
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=510,
        )
        panel_status.pack(fill="x", pady=(4, 0))

        # Shortcut card in Tab 2
        shortcut_card = make_card(display_pad, pady=(0, 0))
        key_labels = (("Ctrl", "ctrl"), ("Shift", "shift"), ("Alt", "alt"), ("Win", "win"))
        key_label_to_id = {label: key for label, key in key_labels}
        key_id_to_label = {key: label for label, key in key_labels}
        shortcut_enabled_var = tk.BooleanVar(value=bool(state.get("shortcut_enabled", True)))
        shortcut_mode_var = tk.StringVar(
            value=_normalize_shortcut_mode(state.get("shortcut_mode"))
        )
        shortcut_key_var = tk.StringVar(
            value=key_id_to_label.get(
                _normalize_shortcut_key(state.get("shortcut_key")), "Ctrl"
            )
        )
        shortcut_count_var = tk.StringVar(
            value=str(_normalize_shortcut_repeat_count(state.get("shortcut_repeat_count")))
        )
        shortcut_combo_keys = list(
            state.get("shortcut_combo") or SHORTCUT_COMBO_DEFAULT
        )
        shortcut_combo_var = tk.StringVar(
            value=_format_shortcut_combo(shortcut_combo_keys)
        )

        def persist_shortcut_from_ui() -> None:
            apply_shortcut_config(
                enabled=bool(shortcut_enabled_var.get()),
                mode=shortcut_mode_var.get(),
                key=key_label_to_id.get(shortcut_key_var.get(), "ctrl"),
                repeat_count=_normalize_shortcut_repeat_count(shortcut_count_var.get()),
                combo=shortcut_combo_keys,
                persist=True,
            )
            shortcut_count_var.set(str(state["shortcut_repeat_count"]))
            shortcut_combo_var.set(_format_shortcut_combo(state["shortcut_combo"]))
            update_shortcut_controls()

        def update_shortcut_controls() -> None:
            enabled = bool(shortcut_enabled_var.get())
            mode = _normalize_shortcut_mode(shortcut_mode_var.get())
            repeat_state = "normal" if enabled and mode == SHORTCUT_MODE_REPEAT else "disabled"
            combo_state = "normal" if enabled and mode == SHORTCUT_MODE_COMBO else "disabled"
            try:
                for widget in shortcut_repeat_widgets:
                    if isinstance(widget, ttk.Combobox):
                        widget.configure(state="readonly" if repeat_state == "normal" else "disabled")
                    else:
                        widget.configure(state=repeat_state)
                for widget in shortcut_combo_widgets:
                    widget.configure(state=combo_state)
                for widget in shortcut_mode_widgets:
                    widget.configure(state="normal" if enabled else "disabled")
            except Exception:
                pass

        tk.Label(
            shortcut_card,
            text=_tr("全局切换快捷键", 'Global Toggle Shortcut'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            shortcut_card,
            text=_tr("启用全局快捷键，一键切换「用量面板 / 动态壁纸」", 'Enable a global shortcut to toggle Usage Panel / Live Wallpaper'),
            variable=shortcut_enabled_var,
            command=persist_shortcut_from_ui,
            bg=S_CARD_BG,
            fg=S_TEXT,
            activebackground=S_CARD_BG,
            activeforeground=S_TEXT,
            selectcolor=S_INPUT_BG,
            font=settings_font,
            anchor="w",
            bd=0,
            highlightthickness=0,
        ).pack(fill="x", pady=(0, 6))

        shortcut_mode_widgets: list[Any] = []
        shortcut_repeat_widgets: list[Any] = []
        shortcut_combo_widgets: list[Any] = []

        mode_row = tk.Frame(shortcut_card, bg=S_CARD_BG)
        mode_row.pack(fill="x", pady=(0, 6))
        repeat_radio = tk.Radiobutton(
            mode_row,
            text=_tr("连按修饰键", 'Repeat Key'),
            value=SHORTCUT_MODE_REPEAT,
            variable=shortcut_mode_var,
            command=persist_shortcut_from_ui,
            bg=S_CARD_BG,
            fg=S_TEXT,
            activebackground=S_CARD_BG,
            activeforeground=S_TEXT,
            selectcolor=S_INPUT_BG,
            font=settings_font_small,
            anchor="w",
            bd=0,
            highlightthickness=0,
        )
        repeat_radio.pack(side="left")
        combo_radio = tk.Radiobutton(
            mode_row,
            text=_tr("自定义组合键", 'Key Combo'),
            value=SHORTCUT_MODE_COMBO,
            variable=shortcut_mode_var,
            command=persist_shortcut_from_ui,
            bg=S_CARD_BG,
            fg=S_TEXT,
            activebackground=S_CARD_BG,
            activeforeground=S_TEXT,
            selectcolor=S_INPUT_BG,
            font=settings_font_small,
            anchor="w",
            bd=0,
            highlightthickness=0,
        )
        combo_radio.pack(side="left", padx=(16, 0))
        shortcut_mode_widgets.extend((repeat_radio, combo_radio))

        repeat_row = tk.Frame(shortcut_card, bg=S_CARD_BG)
        repeat_row.pack(fill="x", pady=(0, 6))
        tk.Label(
            repeat_row,
            text=_tr("修饰键", 'Modifier Key'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
        ).pack(side="left")
        key_combo = ttk.Combobox(
            repeat_row,
            textvariable=shortcut_key_var,
            values=tuple(label for label, _key in key_labels),
            state="readonly",
            width=8,
            font=settings_font_small,
        )
        key_combo.pack(side="left", padx=(6, 16))
        key_combo.bind("<<ComboboxSelected>>", lambda _e: persist_shortcut_from_ui())
        tk.Label(
            repeat_row,
            text=_tr("连按次数", 'Repeat Count'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
        ).pack(side="left")
        count_spin = tk.Spinbox(
            repeat_row,
            from_=SHORTCUT_REPEAT_COUNT_MIN,
            to=SHORTCUT_REPEAT_COUNT_MAX,
            width=4,
            textvariable=shortcut_count_var,
            command=persist_shortcut_from_ui,
            font=settings_font_small,
            justify="right",
            bg=S_INPUT_BG,
            fg=S_TEXT,
            insertbackground=S_TEXT,
            buttonbackground=S_BTN_BG,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=S_CARD_BORDER,
        )
        count_spin.pack(side="left", padx=(6, 0))
        count_spin.bind("<FocusOut>", lambda _event: persist_shortcut_from_ui())
        count_spin.bind("<Return>", lambda _event: persist_shortcut_from_ui())
        shortcut_repeat_widgets.extend((key_combo, count_spin))

        combo_row = tk.Frame(shortcut_card, bg=S_CARD_BG)
        combo_row.pack(fill="x", pady=(0, 6))
        combo_label = tk.Label(
            combo_row,
            textvariable=shortcut_combo_var,
            fg=S_TEXT,
            bg=S_INPUT_BG,
            font=settings_font_small,
            anchor="w",
            padx=10,
            pady=5,
        )
        combo_label.pack(side="left", fill="x", expand=True)

        def start_combo_capture() -> None:
            if get_async_key_state is None:
                shortcut_combo_var.set(_tr("当前系统无法录制快捷键", 'Shortcut recording is unavailable on this system'))
                return
            state["shortcut_capture"] = True
            shortcut_combo_var.set(_tr("请按下组合键…", 'Press a key combination…'))
            seen: list[str] = []

            def finish(keys: list[str]) -> None:
                state["shortcut_capture"] = False
                if len(keys) >= 2:
                    shortcut_combo_keys[:] = _normalize_shortcut_combo(keys)
                    persist_shortcut_from_ui()
                else:
                    shortcut_combo_var.set(_format_shortcut_combo(shortcut_combo_keys))

            def poll_capture(remaining: int) -> None:
                if not state.get("shortcut_capture"):
                    return
                down = _shortcut_keys_currently_down(get_async_key_state)
                if down:
                    seen[:] = down
                elif seen:
                    finish(list(seen))
                    return
                if remaining <= 0:
                    finish(list(seen))
                    return
                win.after(40, lambda: poll_capture(remaining - 1))

            win.after(200, lambda: poll_capture(150))

        capture_btn = make_button(
            combo_row,
            _tr("录制组合键", 'Record Key Combo'),
            start_combo_capture,
            small=True,
            padx=10,
            pady=4,
        )
        capture_btn.pack(side="left", padx=(8, 0))
        shortcut_combo_widgets.extend((combo_label, capture_btn))

        tk.Label(
            shortcut_card,
            text=_tr("连按默认两次 Ctrl；组合键按下后立即切换。仅在专用副屏模式下生效，Ctrl+C 等正常组合键不会误触。", 'Default: press Ctrl twice. Key combos toggle immediately. Only active in Aux Display mode; normal combos such as Ctrl+C are ignored.'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="w",
            wraplength=510,
            justify="left",
        ).pack(fill="x")
        update_shortcut_controls()

        # ── Tab 3: 动态壁纸 ──
        wallpaper_folders = list(state.get("wallpaper_folders") or [])
        wallpaper_disabled_folders = list(
            state.get("wallpaper_disabled_folders") or []
        )
        wallpaper_folder_depths = _normalize_wallpaper_folder_depths(
            state.get("wallpaper_folder_depths"),
            wallpaper_folders,
        )
        wallpaper_image_seconds_var = tk.StringVar(
            value=str(int(state["wallpaper_image_seconds"]))
        )
        wallpaper_audio_var = tk.BooleanVar(
            value=bool(state.get("wallpaper_audio", True))
        )
        wallpaper_random_mode_var = tk.StringVar(
            value=_normalize_wallpaper_random_mode(
                state.get("wallpaper_random_mode")
            )
        )
        settings_media_files: list[Path] = []
        weight_vars: dict[str, tk.StringVar] = {}
        depth_vars: dict[str, tk.StringVar] = {}

        def apply_wallpaper_random_mode() -> None:
            mode = _normalize_wallpaper_random_mode(wallpaper_random_mode_var.get())
            wallpaper_random_mode_var.set(mode)
            changed = mode != state.get("wallpaper_random_mode")
            state["wallpaper_random_mode"] = mode
            cfg["wallpaper_random_mode"] = mode
            save_config(cfg)
            if not changed:
                return
            if state.get("wallpaper_active"):
                set_wallpaper_active(False)
                set_wallpaper_active(True)
            else:
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        dirs_card = make_card(
            directories_pad,
            pady=(0, 8),
            expand=True,
            fill="both",
        )
        dirs_hdr = tk.Frame(dirs_card, bg=S_CARD_BG)
        dirs_hdr.pack(fill="x", pady=(0, 2))
        tk.Label(
            dirs_hdr,
            text=_tr("媒体目录与递归层数", 'Media Folders & Recursion Depth'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(side="left")
        tk.Label(
            dirs_card,
            text=_tr("添加目录后立即生效并在后台慢速建立缓存索引；可为每个目录设置子目录递归层数（用于「随机权重」分组显示）。", 'Folders take effect immediately and are indexed slowly in the background. Set recursion depth per folder to group Random Weights.'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=510,
        ).pack(fill="x", pady=(0, 8))

        folder_list_frame = tk.Frame(
            dirs_card,
            bg=S_COL_BG,
            highlightthickness=0,
            bd=0,
        )
        folder_list_frame.pack(fill="both", expand=True)
        folder_canvas = tk.Canvas(
            folder_list_frame,
            bg=S_COL_BG,
            highlightthickness=0,
            bd=0,
            height=210,
        )
        folder_scrollbar = ttk.Scrollbar(
            folder_list_frame,
            orient="vertical",
            command=folder_canvas.yview,
            style="Vertical.TScrollbar",
        )
        folder_scrollbar.pack(side="right", fill="y")
        folder_canvas.pack(side="left", fill="both", expand=True)
        folder_canvas.configure(yscrollcommand=folder_scrollbar.set)
        folder_rows = tk.Frame(folder_canvas, bg=S_COL_BG, padx=6, pady=6)
        folder_canvas_window = folder_canvas.create_window(
            (0, 0), window=folder_rows, anchor="nw"
        )
        sync_folder_scrollregion, scroll_folder_list = _bind_scrollable_canvas(
            folder_canvas,
            folder_rows,
            folder_canvas_window,
        )

        def bind_folder_list_wheel(widget: Any) -> None:
            widget.bind("<MouseWheel>", scroll_folder_list)
            for child in widget.winfo_children():
                bind_folder_list_wheel(child)

        bind_folder_list_wheel(folder_list_frame)

        folder_button_row = tk.Frame(dirs_card, bg=S_CARD_BG)
        folder_button_row.pack(fill="x", pady=(8, 4))
        folder_status = tk.Label(
            dirs_card,
            text="",
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=510,
        )
        folder_status.pack(fill="x")

        playback_card = make_card(directories_pad, pady=(0, 0), pady_inner=10)
        image_interval_row = tk.Frame(playback_card, bg=S_CARD_BG)
        image_interval_row.pack(fill="x")
        tk.Label(
            image_interval_row,
            text=_tr("图片切换间隔", 'Image Duration'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font,
        ).pack(side="left")

        def apply_wallpaper_image_seconds(
            _event: Any = None,
            *,
            persist: bool = True,
            reprepare: bool = True,
        ) -> None:
            try:
                seconds = int(wallpaper_image_seconds_var.get().strip())
            except (TypeError, ValueError):
                seconds = int(state["wallpaper_image_seconds"])
            seconds = max(2, min(300, seconds))
            wallpaper_image_seconds_var.set(str(seconds))
            changed = seconds != int(state["wallpaper_image_seconds"])
            state["wallpaper_image_seconds"] = seconds
            cfg["wallpaper_image_seconds"] = seconds
            if persist:
                save_config(cfg)
            if changed and reprepare and not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        image_interval_spinbox = tk.Spinbox(
            image_interval_row,
            from_=2,
            to=300,
            increment=1,
            width=5,
            textvariable=wallpaper_image_seconds_var,
            command=apply_wallpaper_image_seconds,
            font=settings_font,
            justify="right",
            bg=S_INPUT_BG,
            fg=S_TEXT,
            insertbackground=S_TEXT,
            buttonbackground=S_BTN_BG,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=S_CARD_BORDER,
        )
        image_interval_spinbox.pack(side="left", padx=(10, 4))
        image_interval_spinbox.bind("<FocusOut>", apply_wallpaper_image_seconds)
        image_interval_spinbox.bind("<Return>", apply_wallpaper_image_seconds)
        tk.Label(
            image_interval_row,
            text=_tr("秒（2–300）", 'seconds (2–300)'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
        ).pack(side="left")

        def apply_wallpaper_audio() -> None:
            enabled = bool(wallpaper_audio_var.get())
            changed = enabled != bool(state.get("wallpaper_audio", True))
            state["wallpaper_audio"] = enabled
            cfg["wallpaper_audio"] = enabled
            save_config(cfg)
            if not changed:
                return
            was_active = bool(state.get("wallpaper_active"))
            if was_active and state.get("wallpaper_web_mode") and browser_wallpaper_player.running:
                browser_wallpaper_player.set_muted(not enabled)
            elif was_active:
                set_wallpaper_active(False)
                set_wallpaper_active(True)
            else:
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        tk.Checkbutton(
            image_interval_row,
            text=_tr("播放视频声音", 'Play Video Audio'),
            variable=wallpaper_audio_var,
            command=apply_wallpaper_audio,
            fg=S_TEXT,
            bg=S_CARD_BG,
            activebackground=S_CARD_BG,
            activeforeground=S_TEXT,
            selectcolor=S_INPUT_BG,
            font=settings_font,
            anchor="w",
            bd=0,
            highlightthickness=0,
        ).pack(side="right")

        # Wallpaper Sub-tab 2: 随机权重
        weights_mode_card = make_card(weights_pad, pady=(0, 8), pady_inner=10)
        tk.Label(
            weights_mode_card,
            text=_tr("随机策略", 'Random Strategy'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        for mode, label in (
            (
                WALLPAPER_RANDOM_MODE_DIRECTORY,
                _tr("单目录随机：按权重抽中某个目录后，本轮只在其中播放", 'Single folder: pick a folder by weight, then play only that folder this session'),
            ),
            (
                WALLPAPER_RANDOM_MODE_GLOBAL,
                _tr("全目录混合随机：每次切换都在所有目录中按权重随机抽取", 'All folders: pick media across all folders by weight at each transition'),
            ),
        ):
            tk.Radiobutton(
                weights_mode_card,
                text=label,
                value=mode,
                variable=wallpaper_random_mode_var,
                command=apply_wallpaper_random_mode,
                fg=S_TEXT,
                bg=S_CARD_BG,
                activebackground=S_CARD_BG,
                activeforeground=S_TEXT,
                selectcolor=S_INPUT_BG,
                font=settings_font_small,
                anchor="w",
                bd=0,
                highlightthickness=0,
            ).pack(fill="x", pady=1)

        weights_list_card = make_card(
            weights_pad,
            pady=(0, 0),
            expand=True,
            fill="both",
        )
        w_hdr = tk.Frame(weights_list_card, bg=S_CARD_BG)
        w_hdr.pack(fill="x", pady=(0, 2))
        tk.Label(
            w_hdr,
            text=_tr("子目录抽中权重（基于缓存索引与递归层数）", 'Subfolder Weights (cached index)'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(side="left")
        tk.Label(
            weights_list_card,
            text=_tr("10 为默认基准，20 约为两倍概率，0 表示禁用首选；直接读取缓存索引，不重复全盘扫描。", '10 is the default; 20 doubles the chance; 0 disables preference. Uses the cached index without scanning again.'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=510,
        ).pack(fill="x", pady=(0, 6))

        weight_list_frame = tk.Frame(
            weights_list_card,
            bg=S_COL_BG,
            highlightthickness=0,
            bd=0,
        )
        weight_list_frame.pack(fill="both", expand=True)
        weight_canvas = tk.Canvas(
            weight_list_frame,
            bg=S_COL_BG,
            highlightthickness=0,
            bd=0,
            height=220,
        )
        weight_scrollbar = ttk.Scrollbar(
            weight_list_frame,
            orient="vertical",
            command=weight_canvas.yview,
            style="Vertical.TScrollbar",
        )
        weight_scrollbar.pack(side="right", fill="y")
        weight_canvas.pack(side="left", fill="both", expand=True)
        weight_canvas.configure(yscrollcommand=weight_scrollbar.set)
        weight_rows = tk.Frame(weight_canvas, bg=S_COL_BG, padx=6, pady=6)
        weight_canvas_window = weight_canvas.create_window(
            (0, 0), window=weight_rows, anchor="nw"
        )
        sync_weight_scrollregion, scroll_weight_list = _bind_scrollable_canvas(
            weight_canvas,
            weight_rows,
            weight_canvas_window,
        )

        def bind_weight_list_wheel(widget: Any) -> None:
            widget.bind("<MouseWheel>", scroll_weight_list)
            for child in widget.winfo_children():
                bind_weight_list_wheel(child)

        bind_weight_list_wheel(weight_list_frame)

        folder_row_records: list[dict[str, Any]] = []
        weight_row_records: list[dict[str, Any]] = []

        def restart_wallpaper_preparation(delay_ms: int = 350) -> None:
            if state.get("wallpaper_active"):
                set_wallpaper_active(False)
                set_wallpaper_active(True)
            else:
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare(delay_ms=delay_ms)

        def persist_wallpaper_folders() -> None:
            """Persist folders, disabled state, and recursion depths immediately (0 disk scan)."""
            normalized = _normalize_wallpaper_folders(wallpaper_folders)
            wallpaper_folders[:] = normalized
            disabled = _normalize_wallpaper_disabled_folders(
                wallpaper_disabled_folders,
                normalized,
            )
            wallpaper_disabled_folders[:] = disabled
            depths = _normalize_wallpaper_folder_depths(
                wallpaper_folder_depths,
                normalized,
            )
            wallpaper_folder_depths.clear()
            wallpaper_folder_depths.update(depths)
            weights = _normalize_wallpaper_weights(
                state.get("wallpaper_subdir_weights")
            )
            state["wallpaper_folders"] = list(normalized)
            state["wallpaper_folder"] = normalized[0] if normalized else None
            state["wallpaper_disabled_folders"] = list(disabled)
            state["wallpaper_folder_depths"] = dict(depths)
            state["wallpaper_subdir_weights"] = weights
            cfg["wallpaper_folders"] = list(normalized)
            cfg["wallpaper_folder"] = normalized[0] if normalized else None
            cfg["wallpaper_disabled_folders"] = list(disabled)
            cfg["wallpaper_folder_depths"] = dict(depths)
            cfg["wallpaper_subdir_weights"] = weights
            save_config(cfg)

        def apply_folder_depth(folder_key: str, variable: tk.StringVar) -> None:
            fallback = wallpaper_folder_depths.get(folder_key, WALLPAPER_DIR_DEPTH_DEFAULT)
            try:
                depth = int(str(variable.get()).strip())
            except (TypeError, ValueError):
                depth = fallback
            depth = max(WALLPAPER_DIR_DEPTH_MIN, min(WALLPAPER_DIR_DEPTH_MAX, depth))
            variable.set(str(depth))
            changed = wallpaper_folder_depths.get(folder_key) != depth
            wallpaper_folder_depths[folder_key] = depth
            state["wallpaper_folder_depths"] = dict(wallpaper_folder_depths)
            cfg["wallpaper_folder_depths"] = dict(wallpaper_folder_depths)
            save_config(cfg)
            request_weight_rows_refresh()
            if changed and not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare(delay_ms=350)

        def apply_directory_weight(key: str, variable: tk.StringVar) -> None:
            weights = _merge_wallpaper_weight_values(
                state.get("wallpaper_subdir_weights"),
                {key: variable.get()},
            )
            weight = weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT)
            variable.set(str(weight))
            state["wallpaper_subdir_weights"] = weights
            cfg["wallpaper_subdir_weights"] = weights
            save_config(cfg)
            if not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare(delay_ms=350)

        def commit_all_directory_weights() -> None:
            for fkey, dvar in list(depth_vars.items()):
                try:
                    d_val = int(str(dvar.get()).strip())
                    wallpaper_folder_depths[fkey] = max(
                        WALLPAPER_DIR_DEPTH_MIN,
                        min(WALLPAPER_DIR_DEPTH_MAX, d_val),
                    )
                except (TypeError, ValueError):
                    pass
            state["wallpaper_folder_depths"] = dict(wallpaper_folder_depths)
            cfg["wallpaper_folder_depths"] = dict(wallpaper_folder_depths)
            previous = _normalize_wallpaper_weights(
                state.get("wallpaper_subdir_weights")
            )
            weights = _merge_wallpaper_weight_values(
                previous,
                {key: variable.get() for key, variable in weight_vars.items()},
            )
            for key, variable in weight_vars.items():
                variable.set(str(weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT)))
            state["wallpaper_subdir_weights"] = weights
            cfg["wallpaper_subdir_weights"] = weights
            if weights != previous and not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare(delay_ms=350)

        def refresh_weight_rows() -> None:
            nonlocal settings_media_files
            wp_subtab_state["weights_dirty"] = False
            enabled_folders = _enabled_wallpaper_folders(
                wallpaper_folders,
                wallpaper_disabled_folders,
            )
            index_cache: dict[str, dict[str, Any]] = state.setdefault(
                "wallpaper_index_cache", {}
            )
            cached_files, _uncached = _cached_wallpaper_media_for_folders(
                enabled_folders,
                index_cache,
            )
            settings_media_files = cached_files
            directories = _wallpaper_media_directories(
                settings_media_files,
                enabled_folders,
                wallpaper_folder_depths,
            )
            scanning_now: set[str] = state.get("wallpaper_scanning_folders") or set()
            weights = dict(state.get("wallpaper_subdir_weights") or {})
            target_keys = [_wallpaper_path_key(directory) for directory, _ in directories]
            existing_keys = [rec.get("key") for rec in weight_row_records]
            if (
                directories
                and target_keys == existing_keys
                and all(rec["row"].winfo_exists() for rec in weight_row_records)
            ):
                for (directory, media_count), rec in zip(directories, weight_row_records):
                    key = rec["key"]
                    dir_text = _wallpaper_directory_label(
                        directory,
                        wallpaper_folders,
                        max_chars=44,
                    )
                    if str(rec["dir_lbl"].cget("text")) != dir_text:
                        rec["dir_lbl"].configure(text=dir_text)
                    cnt_text = _tr(f"  {media_count} 个媒体", f'  {media_count} media files')
                    if str(rec["count_lbl"].cget("text")) != cnt_text:
                        rec["count_lbl"].configure(text=cnt_text)
                    target_val = str(weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT))
                    if key in weight_vars and str(weight_vars[key].get()) != target_val:
                        weight_vars[key].set(target_val)
                return

            for child in weight_rows.winfo_children():
                child.destroy()
            weight_row_records.clear()
            weight_vars.clear()
            if not directories:
                empty_msg = (
                    _tr("正在后台慢速扫描目录索引，请稍候…", 'Indexing folders slowly in the background. Please wait…')
                    if scanning_now
                    else _tr("添加含图片或视频的目录后，这里会按设定的递归层数显示子目录。", 'Add folders with images or videos to show subfolders at the selected depth.')
                )
                tk.Label(
                    weight_rows,
                    text=empty_msg,
                    fg=S_ACCENT_BLUE if scanning_now else S_MUTED,
                    bg=S_COL_BG,
                    font=settings_font_small,
                    anchor="w",
                    justify="left",
                    wraplength=460,
                ).pack(fill="x", pady=12, padx=6)
                sync_weight_scrollregion()
                return
            for directory, media_count in directories:
                key = _wallpaper_path_key(directory)
                row = tk.Frame(
                    weight_rows,
                    bg=S_SURFACE_ALT,
                    highlightthickness=0,
                    bd=0,
                    padx=10,
                    pady=7,
                )
                row.pack(fill="x", pady=2)
                row.grid_columnconfigure(0, weight=1)
                label_frame = tk.Frame(row, bg=S_SURFACE_ALT)
                label_frame.grid(row=0, column=0, sticky="ew")
                dir_lbl = tk.Label(
                    label_frame,
                    text=_wallpaper_directory_label(
                        directory,
                        wallpaper_folders,
                        max_chars=44,
                    ),
                    fg=S_TEXT,
                    bg=S_SURFACE_ALT,
                    font=settings_font_small,
                    anchor="w",
                    justify="left",
                )
                dir_lbl.pack(side="left")
                count_lbl = tk.Label(
                    label_frame,
                    text=_tr(f"  {media_count} 个媒体", f'  {media_count} media files'),
                    fg=S_MUTED,
                    bg=S_SURFACE_ALT,
                    font=settings_font_badge,
                    anchor="w",
                )
                count_lbl.pack(side="left", padx=(6, 0))
                variable = tk.StringVar(
                    value=str(weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT))
                )
                weight_vars[key] = variable
                spinbox = tk.Spinbox(
                    row,
                    from_=WALLPAPER_DIR_WEIGHT_MIN,
                    to=WALLPAPER_DIR_WEIGHT_MAX,
                    width=5,
                    textvariable=variable,
                    command=lambda k=key, v=variable: apply_directory_weight(k, v),
                    font=settings_font,
                    justify="right",
                    bg=S_INPUT_BG,
                    fg=S_TEXT,
                    insertbackground=S_TEXT,
                    buttonbackground=S_BTN_BG,
                    relief="flat",
                    bd=0,
                    highlightthickness=1,
                    highlightbackground=S_CARD_BORDER,
                )
                spinbox.grid(row=0, column=1, sticky="e", padx=(8, 2))
                spinbox.bind(
                    "<FocusOut>",
                    lambda _event, k=key, v=variable: apply_directory_weight(k, v),
                )
                spinbox.bind(
                    "<Return>",
                    lambda _event, k=key, v=variable: apply_directory_weight(k, v),
                )
                weight_row_records.append(
                    {
                        "key": key,
                        "row": row,
                        "dir_lbl": dir_lbl,
                        "count_lbl": count_lbl,
                        "spinbox": spinbox,
                    }
                )
            bind_weight_list_wheel(weight_rows)
            sync_weight_scrollregion()

        wp_subtab_state["refresh_weights"] = refresh_weight_rows

        def request_weight_rows_refresh() -> None:
            wp_subtab_state["weights_dirty"] = True
            if wp_subtab_state.get("active") == "随机权重":
                refresh_weight_rows()

        def update_wallpaper_folder_status(
            uncached_all: list[str] | None = None,
        ) -> None:
            enabled_folders = _enabled_wallpaper_folders(
                wallpaper_folders,
                wallpaper_disabled_folders,
            )
            index_cache: dict[str, dict[str, Any]] = state.setdefault(
                "wallpaper_index_cache", {}
            )
            scanning_set: set[str] = state.setdefault(
                "wallpaper_scanning_folders", set()
            )
            total_cached_media = 0
            for folder in enabled_folders:
                entry = index_cache.get(_wallpaper_path_key(folder))
                if isinstance(entry, dict) and isinstance(entry.get("files"), list):
                    total_cached_media += len(entry["files"])
            executable = _find_mpv_executable(cfg.get("mpv_path"))
            engine_text = _tr("mpv 已就绪", 'mpv Ready') if executable is not None else _tr("mpv 未安装", 'mpv Not Installed')
            scan_note = (
                _tr(" · 后台慢扫描中…", ' · Indexing in background…')
                if (scanning_set or uncached_all)
                else ""
            )
            status_text = (
                _tr(f"已启用 {len(enabled_folders)} / 共 {len(wallpaper_folders)} 个目录"
                f" · 已缓存 {total_cached_media} 个媒体 · {engine_text}{scan_note}", f'Enabled {len(enabled_folders)} / {len(wallpaper_folders)} folders · Cached {total_cached_media} media files · {engine_text}{scan_note}')
            )
            if str(folder_status.cget("text")) != status_text:
                folder_status.configure(text=status_text)

        def _apply_folder_row_visual(rec: dict[str, Any], enabled: bool) -> None:
            is_scanning = bool(rec.get("is_scanning"))
            visual_sig = (bool(enabled), is_scanning)
            if rec.get("_visual_sig") == visual_sig:
                return
            rec["_visual_sig"] = visual_sig
            row_bg = S_SURFACE_ALT if enabled else S_BAR_OFF_BG
            row_fg = S_TEXT if enabled else S_BAR_OFF_FG
            badge_fg = (
                S_ACCENT_BLUE
                if is_scanning
                else (S_ACCENT if enabled else S_BAR_OFF_FG)
            )
            muted_fg = S_MUTED if enabled else S_BAR_OFF_FG
            rec["row"].configure(bg=row_bg)
            rec["chk"].configure(
                text="☑" if enabled else "☐",
                fg=S_ACCENT if enabled else S_BAR_OFF_FG,
                bg=row_bg,
            )
            rec["path"].configure(fg=row_fg, bg=row_bg)
            rec["del_btn"].configure(bg=row_bg)
            rec["layer_lbl"].configure(fg=muted_fg, bg=row_bg)
            rec["spin"].configure(fg=row_fg)
            rec["rec_lbl"].configure(fg=muted_fg, bg=row_bg)
            rec["badge"].configure(fg=badge_fg, bg=row_bg)

        def toggle_single_wallpaper_folder(index: int) -> None:
            if not 0 <= index < len(wallpaper_folders):
                return
            disabled_keys = {
                _wallpaper_path_key(folder)
                for folder in wallpaper_disabled_folders
            }
            folder = wallpaper_folders[index]
            key = _wallpaper_path_key(folder)
            if key in disabled_keys:
                disabled_keys.remove(key)
                enabled_now = True
            else:
                disabled_keys.add(key)
                enabled_now = False
            wallpaper_disabled_folders[:] = [
                f
                for f in wallpaper_folders
                if _wallpaper_path_key(f) in disabled_keys
            ]
            state["wallpaper_disabled_folders"] = list(wallpaper_disabled_folders)
            cfg["wallpaper_disabled_folders"] = list(wallpaper_disabled_folders)
            save_config(cfg)
            if 0 <= index < len(folder_row_records) and folder_row_records[index]["row"].winfo_exists():
                _apply_folder_row_visual(folder_row_records[index], enabled_now)
                update_wallpaper_folder_status()
                request_weight_rows_refresh()
            else:
                refresh_wallpaper_directory_ui()
            restart_wallpaper_preparation(delay_ms=450)

        def remove_single_wallpaper_folder(index: int) -> None:
            if not 0 <= index < len(wallpaper_folders):
                return
            wallpaper_folders.pop(index)
            persist_wallpaper_folders()
            refresh_wallpaper_directory_ui()
            restart_wallpaper_preparation(delay_ms=350)

        def refresh_wallpaper_directory_ui() -> None:
            if not win.winfo_exists():
                return
            disabled_keys = {
                _wallpaper_path_key(folder)
                for folder in wallpaper_disabled_folders
            }
            index_cache: dict[str, dict[str, Any]] = state.setdefault(
                "wallpaper_index_cache", {}
            )
            scanning_set: set[str] = state.setdefault(
                "wallpaper_scanning_folders", set()
            )
            uncached_all: list[str] = []
            current_keys = [_wallpaper_path_key(f) for f in wallpaper_folders]
            existing_keys = [rec.get("key") for rec in folder_row_records]

            if (
                wallpaper_folders
                and current_keys == existing_keys
                and all(rec["row"].winfo_exists() for rec in folder_row_records)
            ):
                for index, folder in enumerate(wallpaper_folders):
                    key = current_keys[index]
                    enabled = key not in disabled_keys
                    entry = index_cache.get(key)
                    is_scanning = key in scanning_set
                    if not is_scanning and (
                        not isinstance(entry, dict)
                        or not isinstance(entry.get("files"), list)
                    ):
                        uncached_all.append(folder)
                        is_scanning = True
                    rec = folder_row_records[index]
                    rec["folder"] = folder
                    rec["is_scanning"] = is_scanning
                    elided = _middle_elide_path(folder, max_chars=34)
                    if str(rec["path"].cget("text")) != elided:
                        rec["path"].configure(text=elided)
                    count_num = (
                        len(entry.get("files") or [])
                        if isinstance(entry, dict)
                        else 0
                    )
                    badge_text = _tr("扫描中…", 'Scanning…') if is_scanning else _tr(f"{count_num} 媒体", f'{count_num} media files')
                    if str(rec["badge"].cget("text")) != badge_text:
                        rec["badge"].configure(text=badge_text)
                    depth_val = wallpaper_folder_depths.get(
                        key, WALLPAPER_DIR_DEPTH_DEFAULT
                    )
                    if key in depth_vars and str(depth_vars[key].get()) != str(depth_val):
                        depth_vars[key].set(str(depth_val))
                    _apply_folder_row_visual(rec, enabled)
                update_wallpaper_folder_status(uncached_all)
                request_weight_rows_refresh()
                if uncached_all:
                    _ensure_wallpaper_folders_indexed_async(
                        uncached_all,
                        notify_start=False,
                    )
                return

            for child in folder_rows.winfo_children():
                child.destroy()
            folder_row_records.clear()
            depth_vars.clear()

            if not wallpaper_folders:
                tk.Label(
                    folder_rows,
                    text=_tr("尚未添加壁纸目录，点击下方「＋ 添加目录」即可立刻添加并后台慢扫描。", 'No wallpaper folders yet. Click “＋ Add Folder” to add and index a folder.'),
                    fg=S_MUTED,
                    bg=S_COL_BG,
                    font=settings_font_small,
                    anchor="w",
                    justify="left",
                    wraplength=460,
                ).pack(fill="x", pady=14, padx=6)

            for index, folder in enumerate(wallpaper_folders):
                key = _wallpaper_path_key(folder)
                enabled = key not in disabled_keys
                entry = index_cache.get(key)
                is_scanning = key in scanning_set
                if not is_scanning and (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("files"), list)
                ):
                    uncached_all.append(folder)
                    is_scanning = True

                row_bg = S_SURFACE_ALT if enabled else S_BAR_OFF_BG
                row_fg = S_TEXT if enabled else S_BAR_OFF_FG
                row = tk.Frame(
                    folder_rows,
                    bg=row_bg,
                    highlightthickness=0,
                    bd=0,
                    padx=10,
                    pady=7,
                )
                row.pack(fill="x", pady=2)

                chk_lbl = tk.Label(
                    row,
                    text="☑" if enabled else "☐",
                    fg=S_ACCENT if enabled else S_BAR_OFF_FG,
                    bg=row_bg,
                    font=settings_font_section,
                    cursor="hand2",
                    padx=2,
                )
                chk_lbl.pack(side="left")
                chk_lbl.bind(
                    "<Button-1>",
                    lambda _e, idx=index: toggle_single_wallpaper_folder(idx),
                )

                path_lbl = tk.Label(
                    row,
                    text=_middle_elide_path(folder, max_chars=34),
                    fg=row_fg,
                    bg=row_bg,
                    font=settings_font_small,
                    anchor="w",
                )
                path_lbl.pack(side="left", fill="x", expand=True, padx=(6, 6))

                del_btn = tk.Label(
                    row,
                    text=_tr("移除", 'Remove'),
                    fg=S_SUBTLE,
                    bg=row_bg,
                    font=settings_font_badge,
                    cursor="hand2",
                    padx=6,
                    pady=2,
                )
                del_btn.pack(side="right", padx=(6, 0))
                del_btn.bind(
                    "<Button-1>",
                    lambda _e, idx=index: remove_single_wallpaper_folder(idx),
                )
                del_btn.bind("<Enter>", lambda _e, w=del_btn: w.configure(fg="#f87171"))
                del_btn.bind("<Leave>", lambda _e, w=del_btn: w.configure(fg=S_SUBTLE))

                depth_val = wallpaper_folder_depths.get(
                    key, WALLPAPER_DIR_DEPTH_DEFAULT
                )
                d_var = tk.StringVar(value=str(depth_val))
                depth_vars[key] = d_var
                layer_lbl = tk.Label(
                    row,
                    text=_tr("层", 'levels'),
                    fg=S_MUTED if enabled else S_BAR_OFF_FG,
                    bg=row_bg,
                    font=settings_font_badge,
                )
                layer_lbl.pack(side="right", padx=(2, 0))
                depth_spin = tk.Spinbox(
                    row,
                    from_=WALLPAPER_DIR_DEPTH_MIN,
                    to=WALLPAPER_DIR_DEPTH_MAX,
                    width=3,
                    textvariable=d_var,
                    command=lambda k=key, v=d_var: apply_folder_depth(k, v),
                    font=settings_font_small,
                    justify="right",
                    bg=S_INPUT_BG,
                    fg=S_TEXT if enabled else S_BAR_OFF_FG,
                    insertbackground=S_TEXT,
                    buttonbackground=S_BTN_BG,
                    relief="flat",
                    bd=0,
                    highlightthickness=1,
                    highlightbackground=S_CARD_BORDER,
                )
                depth_spin.pack(side="right", padx=(3, 0))
                depth_spin.bind(
                    "<FocusOut>",
                    lambda _e, k=key, v=d_var: apply_folder_depth(k, v),
                )
                depth_spin.bind(
                    "<Return>",
                    lambda _e, k=key, v=d_var: apply_folder_depth(k, v),
                )
                rec_lbl = tk.Label(
                    row,
                    text=_tr("递归", 'Depth'),
                    fg=S_MUTED if enabled else S_BAR_OFF_FG,
                    bg=row_bg,
                    font=settings_font_badge,
                )
                rec_lbl.pack(side="right", padx=(8, 0))

                count_num = len(entry.get("files") or []) if isinstance(entry, dict) else 0
                badge_text = _tr("扫描中…", 'Scanning…') if is_scanning else _tr(f"{count_num} 媒体", f'{count_num} media files')
                badge_fg = S_ACCENT_BLUE if is_scanning else (S_ACCENT if enabled else S_BAR_OFF_FG)
                badge_lbl = tk.Label(
                    row,
                    text=badge_text,
                    fg=badge_fg,
                    bg=row_bg,
                    font=settings_font_badge,
                    anchor="e",
                )
                badge_lbl.pack(side="right", padx=(4, 0))

                folder_row_records.append(
                    {
                        "key": key,
                        "folder": folder,
                        "row": row,
                        "chk": chk_lbl,
                        "path": path_lbl,
                        "del_btn": del_btn,
                        "layer_lbl": layer_lbl,
                        "spin": depth_spin,
                        "rec_lbl": rec_lbl,
                        "badge": badge_lbl,
                        "is_scanning": is_scanning,
                        "_visual_sig": (bool(enabled), bool(is_scanning)),
                    }
                )

            bind_folder_list_wheel(folder_rows)
            sync_folder_scrollregion()
            update_wallpaper_folder_status(uncached_all)
            request_weight_rows_refresh()
            if uncached_all:
                _ensure_wallpaper_folders_indexed_async(
                    uncached_all,
                    notify_start=False,
                )

        state["wallpaper_scan_callback"] = refresh_wallpaper_directory_ui

        def add_wallpaper_folder() -> None:
            current = wallpaper_folders[-1] if wallpaper_folders else ""
            initial = current if current and Path(current).is_dir() else str(HOME / "Pictures")
            selected = filedialog.askdirectory(
                parent=win,
                title=_tr("添加动态壁纸目录", 'Add Live Wallpaper Folder'),
                initialdir=initial,
                mustexist=True,
            )
            if not selected:
                return
            norm_sel = _normalize_wallpaper_folders([selected])
            if not norm_sel:
                return
            folder_path = norm_sel[0]
            key = _wallpaper_path_key(folder_path)
            if folder_path not in wallpaper_folders:
                wallpaper_folders.append(folder_path)
            wallpaper_folder_depths.setdefault(key, WALLPAPER_DIR_DEPTH_DEFAULT)
            # Immediately persist and render UI without waiting for disk scan
            persist_wallpaper_folders()
            state.setdefault("wallpaper_scanning_folders", set()).add(key)
            refresh_wallpaper_directory_ui()
            state.setdefault("wallpaper_scanning_folders", set()).discard(key)
            _ensure_wallpaper_folders_indexed_async(
                [folder_path],
                force_rescan=True,
                notify_start=False,
            )

        def rescan_all_wallpaper_folders() -> None:
            if not wallpaper_folders:
                return
            _ensure_wallpaper_folders_indexed_async(
                list(wallpaper_folders),
                force_rescan=True,
                notify_start=False,
            )
            refresh_wallpaper_directory_ui()

        make_button(
            folder_button_row,
            _tr("＋ 添加目录", '＋ Add Folder…'),
            add_wallpaper_folder,
            primary=True,
            small=True,
            padx=12,
            pady=4,
        ).pack(side="left")
        make_button(
            folder_button_row,
            _tr("↻ 重新扫描索引", '↻ Rescan Index'),
            rescan_all_wallpaper_folders,
            small=True,
            padx=10,
            pady=4,
        ).pack(side="left", padx=(8, 0))

        def reset_all_directory_weights() -> None:
            keys = list(weight_vars)
            weights = dict(state.get("wallpaper_subdir_weights") or {})
            for key in keys:
                weights[key] = WALLPAPER_DIR_WEIGHT_DEFAULT
                weight_vars[key].set(str(WALLPAPER_DIR_WEIGHT_DEFAULT))
            state["wallpaper_subdir_weights"] = weights
            cfg["wallpaper_subdir_weights"] = weights
            save_config(cfg)
            if not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        make_button(
            w_hdr,
            _tr("全部恢复为 10", 'Reset All to 10'),
            reset_all_directory_weights,
            small=True,
            padx=10,
            pady=3,
        ).pack(side="right")
        make_button(
            w_hdr,
            _tr("↻ 重新扫描", '↻ Rescan'),
            rescan_all_wallpaper_folders,
            small=True,
            padx=10,
            pady=3,
        ).pack(side="right", padx=(0, 6))
        refresh_wallpaper_directory_ui()

        # ── Tab 4: 悬浮窗 ──
        float_style_card = make_card(floating_pad, pady=(0, 10))
        tk.Label(
            float_style_card,
            text=_tr("外观与不透明度", 'Appearance & Opacity'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        font_hdr = tk.Frame(float_style_card, bg=S_CARD_BG)
        font_hdr.pack(fill="x")
        tk.Label(
            font_hdr,
            text=_tr("悬浮窗字体大小", 'Floating HUD Font Size'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font,
            anchor="w",
        ).pack(side="left")
        size_var = tk.IntVar(value=int(state["font_size"]))
        size_label = tk.Label(
            font_hdr,
            text=f"{size_var.get()} pt",
            fg=S_ACCENT,
            bg=S_CARD_BG,
            font=settings_font_section,
        )
        size_label.pack(side="right")

        def on_font_slide(val: str) -> None:
            try:
                v = int(float(val))
            except Exception:
                return
            try:
                size_label.configure(text=f"{v} pt")
            except Exception:
                pass
            apply_font_size(v, persist=False, relayout=True)

        scale = tk.Scale(
            float_style_card,
            from_=FONT_SIZE_MIN,
            to=FONT_SIZE_MAX,
            orient="horizontal",
            variable=size_var,
            command=on_font_slide,
            showvalue=False,
            length=220,
            bg=S_CARD_BG,
            fg=S_TEXT,
            highlightthickness=0,
            troughcolor=S_INPUT_BG,
            activebackground=S_ACCENT,
            sliderrelief="flat",
            bd=0,
        )
        scale.pack(fill="x", pady=(4, 12))

        bg_hdr = tk.Frame(float_style_card, bg=S_CARD_BG)
        bg_hdr.pack(fill="x")
        tk.Label(
            bg_hdr,
            text=_tr("底板不透明度", 'Background Opacity'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font,
            anchor="w",
        ).pack(side="left")
        bg_op_var = tk.IntVar(value=int(state["bg_opacity"]))
        bg_op_lbl = tk.Label(
            bg_hdr,
            text=f"{bg_op_var.get()}%",
            fg=S_ACCENT,
            bg=S_CARD_BG,
            font=settings_font_section,
        )
        bg_op_lbl.pack(side="right")

        def on_bg_opacity(val: str) -> None:
            try:
                v = int(float(val))
            except Exception:
                return
            v = _clamp_opacity(v, BG_OPACITY_DEFAULT)
            state["bg_opacity"] = v
            bg_op_lbl.configure(text=f"{v}%")
            apply_opacity()

        bg_op_scale = tk.Scale(
            float_style_card,
            from_=OPACITY_MIN,
            to=OPACITY_MAX,
            orient="horizontal",
            variable=bg_op_var,
            command=on_bg_opacity,
            showvalue=False,
            length=220,
            bg=S_CARD_BG,
            fg=S_TEXT,
            highlightthickness=0,
            troughcolor=S_INPUT_BG,
            activebackground=S_ACCENT,
            sliderrelief="flat",
            bd=0,
        )
        bg_op_scale.pack(fill="x", pady=(4, 12))

        tx_hdr = tk.Frame(float_style_card, bg=S_CARD_BG)
        tx_hdr.pack(fill="x")
        tk.Label(
            tx_hdr,
            text=_tr("文字不透明度", 'Text Opacity'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font,
            anchor="w",
        ).pack(side="left")
        tx_op_var = tk.IntVar(value=int(state["text_opacity"]))
        tx_op_lbl = tk.Label(
            tx_hdr,
            text=f"{tx_op_var.get()}%",
            fg=S_ACCENT,
            bg=S_CARD_BG,
            font=settings_font_section,
        )
        tx_op_lbl.pack(side="right")

        def on_text_opacity(val: str) -> None:
            try:
                v = int(float(val))
            except Exception:
                return
            v = _clamp_opacity(v, TEXT_OPACITY_DEFAULT)
            state["text_opacity"] = v
            tx_op_lbl.configure(text=f"{v}%")
            apply_opacity()

        tx_op_scale = tk.Scale(
            float_style_card,
            from_=OPACITY_MIN,
            to=OPACITY_MAX,
            orient="horizontal",
            variable=tx_op_var,
            command=on_text_opacity,
            showvalue=False,
            length=220,
            bg=S_CARD_BG,
            fg=S_TEXT,
            highlightthickness=0,
            troughcolor=S_INPUT_BG,
            activebackground=S_ACCENT,
            sliderrelief="flat",
            bd=0,
        )
        tx_op_scale.pack(fill="x", pady=(4, 8))

        opacity_hint = tk.Label(
            float_style_card,
            text=_tr("拖动滑块即可实时预览。数值越高越不透明，底板与文字互不影响。", 'Drag sliders to preview. Higher values are more opaque; background and text are independent.'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            anchor="w",
            wraplength=510,
            justify="left",
        )
        opacity_hint.pack(fill="x")

        float_size_card = make_card(floating_pad, pady=(0, 0))
        tk.Label(
            float_size_card,
            text=_tr("尺寸与交互", 'Size & Interaction'),
            fg=S_TEXT,
            bg=S_CARD_BG,
            font=settings_font_section,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        resize_var = tk.BooleanVar(value=bool(state["allow_resize"]))

        def on_resize_toggle() -> None:
            set_allow_resize(bool(resize_var.get()), persist=True)

        chk = tk.Checkbutton(
            float_size_card,
            text=_tr("允许拖动悬浮窗右下角调整尺寸", 'Allow resizing from the HUD’s bottom-right corner'),
            variable=resize_var,
            command=on_resize_toggle,
            bg=S_CARD_BG,
            fg=S_TEXT,
            activebackground=S_CARD_BG,
            activeforeground=S_TEXT,
            selectcolor=S_INPUT_BG,
            font=settings_font,
            anchor="w",
            bd=0,
            highlightthickness=0,
        )
        chk.pack(fill="x", pady=(0, 4))
        tk.Label(
            float_size_card,
            text=_tr("开启后，拖动悬浮窗右下角 ◢ 可自由改变宽度和高度；高度不会小于内容所需，避免上下裁切。", 'Drag the bottom-right ◢ to resize width and height. Height stays large enough to show all content.'),
            fg=S_MUTED,
            bg=S_CARD_BG,
            font=settings_font_small,
            wraplength=510,
            justify="left",
            anchor="w",
        ).pack(fill="x")
        update_panel_status()

        def on_close() -> None:
            state["wallpaper_scan_callback"] = None
            _commit_wallpaper_settings_before_close(
                commit_all_directory_weights,
                lambda: apply_wallpaper_image_seconds(
                    persist=False,
                    reprepare=True,
                ),
            )
            final_font_size = int(size_var.get())
            apply_font_size(final_font_size, persist=False, relayout=False)
            cfg["font_size"] = final_font_size
            cfg["card_width"] = int(state["card_width"])
            cfg["card_height"] = state.get("card_height")
            cfg["allow_resize"] = bool(state["allow_resize"])
            cfg["bg_opacity"] = int(state["bg_opacity"])
            cfg["text_opacity"] = int(state["text_opacity"])
            cfg["display_mode"] = str(state["display_mode"])
            cfg["monitor_device"] = state.get("monitor_device")
            cfg["monitor_id"] = state.get("monitor_id")
            cfg["double_ctrl_toggle"] = bool(state.get("shortcut_enabled", True))
            cfg["shortcut_enabled"] = bool(state.get("shortcut_enabled", True))
            cfg["shortcut_mode"] = _normalize_shortcut_mode(state.get("shortcut_mode"))
            cfg["shortcut_key"] = _normalize_shortcut_key(state.get("shortcut_key"))
            cfg["shortcut_repeat_count"] = _normalize_shortcut_repeat_count(
                state.get("shortcut_repeat_count")
            )
            cfg["shortcut_combo"] = _normalize_shortcut_combo(state.get("shortcut_combo"))
            cfg["llm_proxy_timezone"] = _normalize_llm_proxy_timezone(
                state.get("llm_proxy_timezone")
            )
            cfg["wallpaper_folder"] = state.get("wallpaper_folder")
            cfg["wallpaper_folders"] = list(state.get("wallpaper_folders") or [])
            cfg["wallpaper_disabled_folders"] = list(
                state.get("wallpaper_disabled_folders") or []
            )
            cfg["wallpaper_folder_depths"] = dict(
                state.get("wallpaper_folder_depths") or {}
            )
            cfg["wallpaper_subdir_weights"] = dict(
                state.get("wallpaper_subdir_weights") or {}
            )
            cfg["wallpaper_random_mode"] = _normalize_wallpaper_random_mode(
                state.get("wallpaper_random_mode")
            )
            cfg["wallpaper_image_seconds"] = int(state["wallpaper_image_seconds"])
            cfg["wallpaper_audio"] = bool(state.get("wallpaper_audio", True))
            persist_timezone()
            persist_shortcut_from_ui()
            persist_providers(refresh=False)
            if not state.get("panel_active"):
                cfg["x"] = root.winfo_x()
                cfg["y"] = root.winfo_y()
            save_config(cfg)
            state["settings_win"] = None
            win.withdraw()
            win.after_idle(win.destroy)

        btn_row = tk.Frame(shell, bg=S_PAGE_BG)
        btn_row.pack(fill="x", pady=(10, 0))
        close_btn = make_button(
            btn_row,
            _tr("完成", 'Done'),
            on_close,
            primary=True,
            padx=22,
            pady=6,
        )
        close_btn.pack(side="right")

        win.protocol("WM_DELETE_WINDOW", on_close)
        place_settings_window()
        win.deiconify()
        win.lift()
        win.focus_set()

    def open_claude_reset_info(account_id: str = "claude") -> None:
        """Claude's /limit-reset grants, laid out like the Codex card dialog."""
        account_id = account_id if is_claude_provider(account_id) else "claude"
        existing = state.get("claude_reset_win")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    if state.get("claude_reset_account") == account_id:
                        place_settings_on_primary(existing)
                        existing.deiconify()
                        existing.lift()
                        existing.focus_set()
                        return
                    existing.destroy()
            except Exception:
                pass

        win = tk.Toplevel(root)
        # Same reason as the settings dialog: map once, at the final position.
        win.withdraw()
        state["claude_reset_win"] = win
        state["claude_reset_account"] = account_id
        state["claude_reset_busy"] = False
        win.title(_tr(f"{account_id} 用量重置", f'{account_id} Usage Reset'))
        win.configure(bg=BG)
        win.resizable(False, False)

        dialog_font = tkfont.Font(family=font_family, size=10)
        dialog_font_small = tkfont.Font(family=font_family, size=9)
        setattr(win, "_usage_float_fonts", (dialog_font, dialog_font_small))

        shell = tk.Frame(win, bg=BG, padx=14, pady=12)
        shell.pack(fill="both", expand=True)

        tk.Label(
            shell,
            text=_tr(f"选择一次重置，立即重置 {account_id} 的用量窗口。", f'Select a reset to restart {account_id} usage windows immediately.'),
            fg=FG,
            bg=BG,
            font=dialog_font,
            anchor="w",
            justify="left",
        ).pack(fill="x")

        list_frame = tk.Frame(shell, bg=BG, width=360)
        list_frame.pack(fill="both", expand=True, pady=(10, 6))

        status_label = tk.Label(
            shell,
            text="",
            fg=FG_MUTED,
            bg=BG,
            font=dialog_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        status_label.pack(fill="x")

        btn_row = tk.Frame(shell, bg=BG)
        btn_row.pack(fill="x", pady=(10, 0))

        selected_grant = tk.StringVar(value="")
        grants_by_id: dict[str, ClaudeResetGrant] = {}
        pending_note = {"text": ""}

        def alive() -> bool:
            try:
                return bool(win.winfo_exists())
            except Exception:
                return False

        def close() -> None:
            state["claude_reset_win"] = None
            state["claude_reset_account"] = None
            state["claude_reset_busy"] = False
            try:
                win.destroy()
            except Exception:
                pass

        def sync_buttons() -> None:
            if not alive():
                return
            busy = bool(state.get("claude_reset_busy"))
            try:
                use_btn.configure(
                    state="disabled" if busy or not selected_grant.get() else "normal"
                )
                reload_btn.configure(state="disabled" if busy else "normal")
            except Exception:
                pass

        def window_names(windows: tuple[str, ...]) -> str:
            return _tr("、", ", ").join(_ui(CLAUDE_RESET_WINDOW_TEXT.get(w, w)) for w in windows)

        def describe(grant: ClaudeResetGrant, reset: ClaudeResetStatus) -> str:
            parts: list[str] = []
            remaining = format_remaining(grant.expires_at)
            if remaining == "soon":
                parts.append(_tr("即将过期", 'Expires soon'))
            elif remaining:
                parts.append(_tr(f"{remaining} 后过期", f'Expires in {remaining}'))
            elif grant.expires_at is None:
                parts.append(_tr("长期有效", 'No expiry'))
            if grant.resets_left > 1:
                parts.append(_tr(f"{grant.resets_left} 次", f'{grant.resets_left} resets'))
            if grant.clears:
                parts.append(_tr(f"重置{window_names(grant.clears)}", f'Reset {window_names(grant.clears)}'))
            if not reset.claimable(grant):
                if grant.grant_id != reset.next_grant_id:
                    parts.append(_tr("排队中", 'Queued'))
                elif grant.use_requires_limit:
                    parts.append(_tr("到达上限后可用", 'Available at usage limit'))
                else:
                    parts.append(_tr("暂不可用", 'Unavailable'))
            return " · ".join(parts) or grant.grant_id

        def claude_provider() -> ProviderUsage | None:
            for provider in state.get("providers") or []:
                if provider.provider_id == account_id:
                    return provider
            return None

        def render_grants(provider: ProviderUsage | None, error: str | None) -> None:
            if not alive():
                return
            for widget in list(list_frame.winfo_children()):
                try:
                    widget.destroy()
                except Exception:
                    pass
            grants_by_id.clear()
            selected_grant.set("")
            note = pending_note["text"]
            pending_note["text"] = ""

            if error:
                status_label.configure(text=error, fg=RED)
                sync_buttons()
                place_settings_on_primary(win)
                return

            reset = claude_reset_status(provider)
            for index, grant in enumerate(reset.grants, start=1):
                claimable = reset.claimable(grant)
                if claimable:
                    grants_by_id[grant.grant_id] = grant
                tk.Radiobutton(
                    list_frame,
                    text=f"{index}. {describe(grant, reset)}",
                    value=grant.grant_id,
                    variable=selected_grant,
                    command=sync_buttons,
                    state="normal" if claimable else "disabled",
                    fg=FG,
                    bg=BG,
                    activebackground=BG,
                    activeforeground=FG,
                    disabledforeground=FG_MUTED,
                    selectcolor=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                    bd=0,
                    highlightthickness=0,
                ).pack(fill="x", pady=1)

            if grants_by_id:
                selected_grant.set(next(iter(grants_by_id)))
            if reset.grants:
                text = _tr(f"共 {reset.resets_left} 次可用重置。", f'{reset.resets_left} resets available.')
            else:
                tk.Label(
                    list_frame,
                    text=_tr("账号里没有可用的用量重置。", 'No usage resets available on this account.'),
                    fg=FG_MUTED,
                    bg=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                ).pack(fill="x")
                reason = reset.ineligible_reason
                text = _ui(CLAUDE_RESET_REASON_TEXT.get(reason, reason)) if reason else ""
            status_label.configure(
                text=f"{note} {text}".strip() if note else text,
                fg=FG_MUTED,
            )
            sync_buttons()
            place_settings_on_primary(win)

        def load_grants() -> None:
            if not alive() or state.get("claude_reset_busy"):
                return
            state["claude_reset_busy"] = True
            sync_buttons()
            status_label.configure(text=_tr("正在读取重置额度…", 'Loading reset credits…'), fg=FG_MUTED)

            def work() -> None:
                try:
                    fresh: ProviderUsage | None = next(iter(fetch_all([account_id], force=True)), None)
                except Exception:
                    _log_exception("claude reset status")
                    fresh = None

                def deliver() -> None:
                    state["claude_reset_busy"] = False
                    if fresh is None or not fresh.available:
                        render_grants(None, _tr("读取重置额度失败", 'Could not load reset credits'))
                        return
                    # Share the fresh read with the HUD so its 5h number and
                    # reset mark agree with what the dialog lists.
                    state["providers"] = [
                        fresh if p.provider_id == account_id else p
                        for p in state.get("providers") or []
                    ]
                    render(relayout=not state.get("panel_active"))
                    render_grants(fresh, None)

                root.after(0, deliver)

            threading.Thread(target=work, daemon=True).start()

        def finish(claim: ClaudeResetClaim) -> None:
            state["claude_reset_busy"] = False
            if claim.settled:
                state["claude_reset_unsettled"] = None
            if not alive():
                if claim.ok:
                    refresh_async(force=True)
                return
            if claim.ok:
                # Reloading the grants also refreshes the HUD's claude rows.
                pending_note["text"] = claim.message
                load_grants()
            else:
                status_label.configure(text=claim.message, fg=RED)
                sync_buttons()

        def use_selected() -> None:
            if state.get("claude_reset_busy"):
                return
            grant_id = selected_grant.get()
            grant = grants_by_id.get(grant_id)
            if grant is None:
                return
            from tkinter import messagebox

            windows = window_names(grant.clears) or _tr("用量窗口", 'usage windows')
            if not messagebox.askokcancel(
                _tr("使用用量重置", 'Redeem Usage Reset'),
                _tr(f"使用后这次重置立即作废，{account_id} 的{windows}会重新开始计算。\n"
                "确定要使用吗？", f'This reset will be consumed immediately and {account_id} {windows} will restart.\nRedeem this reset?'),
                parent=win,
            ):
                return
            # Resend the id of an attempt whose outcome never arrived, so the
            # server dedupes it rather than spending a second reset.
            unsettled = state.get("claude_reset_unsettled")
            if unsettled and len(unsettled) == 3 and unsettled[0] == account_id and unsettled[1] == grant_id:
                request_id = unsettled[2]
            else:
                request_id = str(uuid.uuid4())
                state["claude_reset_unsettled"] = (account_id, grant_id, request_id)
            state["claude_reset_busy"] = True
            sync_buttons()
            status_label.configure(text=_tr("正在使用重置…", 'Redeeming reset…'), fg=FG_MUTED)

            def work() -> None:
                try:
                    claim = (
                        claim_claude_reset(grant_id, request_id=request_id, provider_id=account_id)
                        if account_id != "claude"
                        else claim_claude_reset(grant_id, request_id=request_id)
                    )
                except Exception:
                    _log_exception("claude reset claim")
                    claim = ClaudeResetClaim(False, _tr("使用失败", 'Redemption failed'), settled=False)
                root.after(0, lambda: finish(claim))

            threading.Thread(target=work, daemon=True).start()

        use_btn = tk.Button(
            btn_row,
            text=_tr("使用", 'Redeem Reset'),
            command=use_selected,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
            state="disabled",
        )
        use_btn.pack(side="right")
        reload_btn = tk.Button(
            btn_row,
            text=_tr("刷新列表", 'Refresh List'),
            command=load_grants,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        )
        reload_btn.pack(side="right", padx=(0, 8))
        tk.Button(
            btn_row,
            text=_tr("关闭", 'Close'),
            command=close,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        ).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: close())
        # The usage read already carries the grants; reuse it instead of
        # another call to an endpoint that answers 429 readily.
        provider = claude_provider()
        cached = provider is not None and provider.reset_block is not None
        if cached:
            render_grants(provider, None)
        place_settings_on_primary(win)
        win.deiconify()
        win.lift()
        win.focus_set()
        if not cached:
            load_grants()

    def open_claude_session_reset(account_id: str = "claude") -> None:
        """Claude's weekly 5h session reset, laid out like the Codex card dialog."""
        account_id = account_id if is_claude_provider(account_id) else "claude"
        existing = state.get("claude_session_win")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    if state.get("claude_session_account") == account_id:
                        place_settings_on_primary(existing)
                        existing.deiconify()
                        existing.lift()
                        existing.focus_set()
                        return
                    existing.destroy()
            except Exception:
                pass

        win = tk.Toplevel(root)
        # Same reason as the settings dialog: map once, at the final position.
        win.withdraw()
        state["claude_session_win"] = win
        state["claude_session_account"] = account_id
        state["claude_session_busy"] = False
        win.title(_tr(f"{account_id} 5 小时重置", f'{account_id} 5-Hour Reset'))
        win.configure(bg=BG)
        win.resizable(False, False)

        dialog_font = tkfont.Font(family=font_family, size=10)
        dialog_font_small = tkfont.Font(family=font_family, size=9)
        setattr(win, "_usage_float_fonts", (dialog_font, dialog_font_small))

        shell = tk.Frame(win, bg=BG, padx=14, pady=12)
        shell.pack(fill="both", expand=True)

        tk.Label(
            shell,
            text=_tr(f"选择一次重置，立即重置 {account_id} 的 5 小时会话。", f'Select a reset to restart {account_id} 5-hour session immediately.'),
            fg=FG,
            bg=BG,
            font=dialog_font,
            anchor="w",
            justify="left",
        ).pack(fill="x")

        list_frame = tk.Frame(shell, bg=BG, width=360)
        list_frame.pack(fill="both", expand=True, pady=(10, 6))

        status_label = tk.Label(
            shell,
            text="",
            fg=FG_MUTED,
            bg=BG,
            font=dialog_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        status_label.pack(fill="x")

        btn_row = tk.Frame(shell, bg=BG)
        btn_row.pack(fill="x", pady=(10, 0))

        selected = tk.StringVar(value="")
        pending_note = {"text": ""}

        def alive() -> bool:
            try:
                return bool(win.winfo_exists())
            except Exception:
                return False

        def close() -> None:
            state["claude_session_win"] = None
            state["claude_session_account"] = None
            state["claude_session_busy"] = False
            try:
                win.destroy()
            except Exception:
                pass

        def sync_buttons() -> None:
            if not alive():
                return
            busy = bool(state.get("claude_session_busy"))
            try:
                use_btn.configure(state="disabled" if busy or not selected.get() else "normal")
                reload_btn.configure(state="disabled" if busy else "normal")
            except Exception:
                pass

        def render_reset(reset: ClaudeSessionReset | None, error: str | None) -> None:
            if not alive():
                return
            for widget in list(list_frame.winfo_children()):
                try:
                    widget.destroy()
                except Exception:
                    pass
            selected.set("")
            note = pending_note["text"]
            pending_note["text"] = ""

            if error or reset is None:
                status_label.configure(text=error or _tr("读取重置额度失败", 'Could not load reset credits'), fg=RED)
                sync_buttons()
                place_settings_on_primary(win)
                return

            if reset.offered:
                parts = [_tr(f"每周 {reset.resets_per_week} 次", f'{reset.resets_per_week} per week')]
                remaining = format_remaining(reset.next_available_at)
                if reset.available:
                    parts.append(_tr("本周可用", 'Available this week'))
                elif remaining:
                    parts.append(_tr(f"{remaining} 后可用", f'Available in {remaining}'))
                else:
                    parts.append(_tr("本周已用", 'Used this week'))
                tk.Radiobutton(
                    list_frame,
                    text=f"1. {' · '.join(parts)}",
                    value="session",
                    variable=selected,
                    command=sync_buttons,
                    state="normal" if reset.claimable else "disabled",
                    fg=FG,
                    bg=BG,
                    activebackground=BG,
                    activeforeground=FG,
                    disabledforeground=FG_MUTED,
                    selectcolor=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                    bd=0,
                    highlightthickness=0,
                ).pack(fill="x", pady=1)
                if reset.claimable:
                    selected.set("session")
            else:
                tk.Label(
                    list_frame,
                    text=_tr("账号里没有可用的 5 小时重置。", 'No 5-hour resets available on this account.'),
                    fg=FG_MUTED,
                    bg=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                ).pack(fill="x")

            if reset.claimable:
                text = _tr("共 1 次可用重置。", '1 reset available.')
            else:
                reason = reset.ineligible_reason
                text = _ui(CLAUDE_SESSION_REASON_TEXT.get(reason, reason)) if reason else ""
            status_label.configure(
                text=f"{note} {text}".strip() if note else text,
                fg=FG_MUTED,
            )
            sync_buttons()
            place_settings_on_primary(win)

        def load_reset() -> None:
            if not alive() or state.get("claude_session_busy"):
                return
            state["claude_session_busy"] = True
            sync_buttons()
            status_label.configure(text=_tr("正在读取重置额度…", 'Loading reset credits…'), fg=FG_MUTED)

            def work() -> None:
                try:
                    reset, error = (
                        fetch_claude_session_reset(account_id)
                        if account_id != "claude"
                        else fetch_claude_session_reset()
                    )
                except Exception:
                    _log_exception("claude session reset status")
                    reset, error = None, _tr("读取重置额度失败", 'Could not load reset credits')

                def deliver() -> None:
                    state["claude_session_busy"] = False
                    render_reset(reset, error)

                root.after(0, deliver)

            threading.Thread(target=work, daemon=True).start()

        def finish(claim: ClaudeResetClaim) -> None:
            state["claude_session_busy"] = False
            if not alive():
                if claim.ok:
                    refresh_async(force=True)
                return
            if claim.ok:
                pending_note["text"] = claim.message
                refresh_async(force=True)
                load_reset()
            else:
                status_label.configure(text=claim.message, fg=RED)
                sync_buttons()

        def use_selected() -> None:
            if state.get("claude_session_busy") or not selected.get():
                return
            from tkinter import messagebox

            if not messagebox.askokcancel(
                _tr("使用 5 小时重置", 'Redeem 5-Hour Reset'),
                _tr(f"使用后本周的 5 小时重置立即作废，{account_id} 的 5 小时会话会重新开始计算。\n"
                "确定要使用吗？", f'This week’s 5-hour reset will be consumed immediately and {account_id} 5-hour session will restart.\nRedeem this reset?'),
                parent=win,
            ):
                return
            state["claude_session_busy"] = True
            sync_buttons()
            status_label.configure(text=_tr("正在使用重置…", 'Redeeming reset…'), fg=FG_MUTED)

            def work() -> None:
                try:
                    claim = (
                        claim_claude_session_reset(account_id)
                        if account_id != "claude"
                        else claim_claude_session_reset()
                    )
                except Exception:
                    _log_exception("claude session reset claim")
                    claim = ClaudeResetClaim(False, _tr("使用失败", 'Redemption failed'), settled=False)
                root.after(0, lambda: finish(claim))

            threading.Thread(target=work, daemon=True).start()

        use_btn = tk.Button(
            btn_row,
            text=_tr("使用", 'Redeem Reset'),
            command=use_selected,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
            state="disabled",
        )
        use_btn.pack(side="right")
        reload_btn = tk.Button(
            btn_row,
            text=_tr("刷新列表", 'Refresh List'),
            command=load_reset,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        )
        reload_btn.pack(side="right", padx=(0, 8))
        tk.Button(
            btn_row,
            text=_tr("关闭", 'Close'),
            command=close,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        ).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: close())
        place_settings_on_primary(win)
        win.deiconify()
        win.lift()
        win.focus_set()
        # The regular usage read doesn't carry this block, so read it now.
        load_reset()

    def open_codex_reset_cards(provider_id: str = "codex") -> None:
        account = CODEX_ACCOUNT_BY_ID.get(provider_id) or CODEX_ACCOUNTS[0]
        existing = state.get("reset_cards_win")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    if state.get("reset_cards_account") == account.provider_id:
                        place_settings_on_primary(existing)
                        existing.deiconify()
                        existing.lift()
                        existing.focus_set()
                        return
                    # Clicking the other account's number must not hand back a
                    # window still listing the first account's cards.
                    existing.destroy()
            except Exception:
                pass

        win = tk.Toplevel(root)
        # Same reason as the settings dialog: map once, at the final position.
        win.withdraw()
        state["reset_cards_win"] = win
        state["reset_cards_account"] = account.provider_id
        state["reset_cards_busy"] = False
        win.title(_tr(f"{account.display_name} 重置卡", f'{account.display_name} Reset Credits'))
        win.configure(bg=BG)
        win.resizable(False, False)

        dialog_font = tkfont.Font(family=font_family, size=10)
        dialog_font_small = tkfont.Font(family=font_family, size=9)
        setattr(win, "_usage_float_fonts", (dialog_font, dialog_font_small))

        shell = tk.Frame(win, bg=BG, padx=14, pady=12)
        shell.pack(fill="both", expand=True)

        tk.Label(
            shell,
            text=_tr(f"选择一张重置卡，立即重置 {account.display_name} 的用量窗口。", f'Select a reset credit to restart {account.display_name} usage windows immediately.'),
            fg=FG,
            bg=BG,
            font=dialog_font,
            anchor="w",
            justify="left",
        ).pack(fill="x")

        list_frame = tk.Frame(shell, bg=BG, width=360)
        list_frame.pack(fill="both", expand=True, pady=(10, 6))

        status_label = tk.Label(
            shell,
            text=_tr("正在读取重置卡…", 'Loading reset credits…'),
            fg=FG_MUTED,
            bg=BG,
            font=dialog_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        status_label.pack(fill="x")

        btn_row = tk.Frame(shell, bg=BG)
        btn_row.pack(fill="x", pady=(10, 0))

        selected_card = tk.StringVar(value="")
        cards_by_id: dict[str, CodexResetCard] = {}
        pending_note = {"text": ""}

        def alive() -> bool:
            try:
                return bool(win.winfo_exists())
            except Exception:
                return False

        def close() -> None:
            state["reset_cards_win"] = None
            state["reset_cards_account"] = None
            state["reset_cards_busy"] = False
            try:
                win.destroy()
            except Exception:
                pass

        def sync_buttons() -> None:
            if not alive():
                return
            busy = bool(state.get("reset_cards_busy"))
            try:
                use_btn.configure(
                    state="disabled" if busy or not selected_card.get() else "normal"
                )
                reload_btn.configure(state="disabled" if busy else "normal")
            except Exception:
                pass

        def describe(card: CodexResetCard) -> str:
            parts: list[str] = []
            remaining = format_remaining(card.expires_at)
            if remaining == "soon":
                parts.append(_tr("即将过期", 'Expires soon'))
            elif remaining:
                parts.append(_tr(f"{remaining} 后过期", f'Expires in {remaining}'))
            elif card.expires_at is None:
                parts.append(_tr("长期有效", 'No expiry'))
            granted = parse_iso(card.granted_at)
            if granted is not None:
                if granted.tzinfo is None:
                    granted = granted.replace(tzinfo=timezone.utc)
                parts.append(_tr(f"获得于 {granted.astimezone():%Y-%m-%d %H:%M}", f'Granted {granted.astimezone():%Y-%m-%d %H:%M}'))
            if not card.usable:
                parts.append(card.status_text)
            return " · ".join(parts) or card.card_id

        def render_cards(cards: list[CodexResetCard], error: str | None) -> None:
            if not alive():
                return
            for widget in list(list_frame.winfo_children()):
                try:
                    widget.destroy()
                except Exception:
                    pass
            cards_by_id.clear()
            selected_card.set("")
            note = pending_note["text"]
            pending_note["text"] = ""

            if error:
                status_label.configure(text=error, fg=RED)
                sync_buttons()
                place_settings_on_primary(win)
                return

            usable = [card for card in cards if card.usable]
            for index, card in enumerate(usable, start=1):
                cards_by_id[card.card_id] = card
                tk.Radiobutton(
                    list_frame,
                    text=f"{index}. {describe(card)}",
                    value=card.card_id,
                    variable=selected_card,
                    command=sync_buttons,
                    fg=FG,
                    bg=BG,
                    activebackground=BG,
                    activeforeground=FG,
                    selectcolor=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                    bd=0,
                    highlightthickness=0,
                ).pack(fill="x", pady=1)

            if usable:
                selected_card.set(usable[0].card_id)
                text = _tr(f"共 {len(usable)} 张可用重置卡。", f'{len(usable)} reset credits available.')
                spent = len(cards) - len(usable)
                if spent:
                    text += _tr(f" 另有 {spent} 张已使用或正在使用。", f' {spent} already redeemed or redeeming.')
            else:
                tk.Label(
                    list_frame,
                    text=_tr("账号里没有可用的重置卡。", 'No reset credits available on this account.'),
                    fg=FG_MUTED,
                    bg=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                ).pack(fill="x")
                text = _tr("OpenAI 发放重置卡后会出现在这里，每张自发放起 30 天内有效。", 'Credits appear here when granted by OpenAI. Each is valid for 30 days from issue.')
            status_label.configure(
                text=f"{note} {text}".strip() if note else text,
                fg=FG_MUTED,
            )
            sync_buttons()
            place_settings_on_primary(win)

        def load_cards() -> None:
            if not alive() or state.get("reset_cards_busy"):
                return
            state["reset_cards_busy"] = True
            sync_buttons()
            status_label.configure(text=_tr("正在读取重置卡…", 'Loading reset credits…'), fg=FG_MUTED)

            def work() -> None:
                try:
                    cards, error = fetch_codex_reset_cards(account.home)
                except Exception:
                    _log_exception("codex reset cards")
                    cards, error = [], _tr("读取重置卡失败", 'Could not load reset credits')

                def deliver() -> None:
                    state["reset_cards_busy"] = False
                    render_cards(cards, error)

                root.after(0, deliver)

            threading.Thread(target=work, daemon=True).start()

        def finish(ok: bool, message: str) -> None:
            state["reset_cards_busy"] = False
            if ok:
                # The window only moves once the backend acknowledges the
                # redeem, so pull fresh numbers instead of guessing locally.
                refresh_async(force=True)
            if not alive():
                return
            if ok:
                pending_note["text"] = message
                load_cards()
            else:
                status_label.configure(text=message, fg=RED)
                sync_buttons()

        def use_selected() -> None:
            if state.get("reset_cards_busy"):
                return
            card_id = selected_card.get()
            if card_id not in cards_by_id:
                return
            from tkinter import messagebox

            if not messagebox.askokcancel(
                _tr("使用重置卡", 'Redeem Reset'),
                _tr(f"使用后这张重置卡立即作废，{account.display_name} 的用量窗口会重新开始计算。\n"
                "确定要使用吗？", f'This credit will be consumed immediately and {account.display_name} usage windows will restart.\nRedeem this reset?'),
                parent=win,
            ):
                return
            state["reset_cards_busy"] = True
            sync_buttons()
            status_label.configure(text=_tr("正在使用重置卡…", 'Redeeming reset credit…'), fg=FG_MUTED)

            def work() -> None:
                try:
                    ok, message = consume_codex_reset_card(card_id, home=account.home)
                except Exception:
                    _log_exception("codex reset consume")
                    ok, message = False, _tr("使用失败", 'Redemption failed')
                root.after(0, lambda: finish(ok, message))

            threading.Thread(target=work, daemon=True).start()

        use_btn = tk.Button(
            btn_row,
            text=_tr("使用", 'Redeem Reset'),
            command=use_selected,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
            state="disabled",
        )
        use_btn.pack(side="right")
        reload_btn = tk.Button(
            btn_row,
            text=_tr("刷新列表", 'Refresh List'),
            command=load_cards,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        )
        reload_btn.pack(side="right", padx=(0, 8))
        tk.Button(
            btn_row,
            text=_tr("关闭", 'Close'),
            command=close,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        ).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: close())
        place_settings_on_primary(win)
        win.deiconify()
        win.lift()
        win.focus_set()
        load_cards()

    def request_open_settings() -> None:
        if state.get("settings_open_job") is not None:
            return

        def open_after_menu_closes() -> None:
            state["settings_open_job"] = None
            open_settings()

        # Let the native popup finish its selection/unpost cycle first. Opening
        # a Toplevel directly from the menu command made the first dialog lose
        # its mapping; the second click merely lifted that hidden instance.
        state["settings_open_job"] = root.after_idle(open_after_menu_closes)

    def toggle_panel_mode() -> None:
        if state.get("display_mode") == DISPLAY_MODE_PANEL:
            set_display_mode(DISPLAY_MODE_FLOAT, persist=True)
            return
        monitor = _choose_panel_monitor(
            _enumerate_monitors(),
            state.get("monitor_device"),
            state.get("monitor_id"),
        )
        if monitor is None:
            monitor = _choose_panel_monitor(_enumerate_monitors(), None)
        set_display_mode(
            DISPLAY_MODE_PANEL,
            monitor.device if monitor is not None else None,
            persist=True,
        )

    def rebuild_menu() -> None:
        menu.delete(0, "end")
        menu.add_command(label=_tr("立即刷新", 'Refresh Now'), command=lambda: refresh_async(force=True))
        menu.add_command(
            label=_tr("粘贴网页/视频链接播放 (Ctrl+V)", 'Paste Web/Video URL to Play (Ctrl+V)'),
            command=paste_wallpaper_from_clipboard,
        )
        menu.add_command(label=_tr("设置…", 'Settings…'), command=request_open_settings)
        menu.add_command(
            label=(_tr("返回用量面板", "Back to Usage Panel")
                   if state.get("wallpaper_active")
                   else _tr("切换动态壁纸", "Toggle Live Wallpaper")),
            command=toggle_wallpaper_mode,
        )
        menu.add_command(
            label=(
                _tr("退出专用副屏（回悬浮窗）", 'Exit Aux Display (Floating HUD)')
                if state.get("display_mode") == DISPLAY_MODE_PANEL
                else _tr("切换到专用副屏", 'Switch to Aux Display')
            ),
            command=toggle_panel_mode,
        )
        menu.add_command(
            label=(_tr("✓ 总在最前", '✓ Always on Top') if state["always_on_top"] else _tr("总在最前", 'Always on Top')),
            command=toggle_topmost,
        )
        menu.add_separator()
        menu.add_command(
            label=(_tr("✓ 开机自启", '✓ Start at Login') if is_autostart_enabled() else _tr("开机自启", 'Start at Login')),
            command=toggle_autostart,
        )
        menu.add_separator()
        menu.add_command(label=_tr("退出", 'Exit'), command=do_quit)

    def toggle_topmost() -> None:
        state["always_on_top"] = not state["always_on_top"]
        root.attributes("-topmost", state["always_on_top"])
        if bg_layer is not None and not state.get("panel_active"):
            try:
                bg_layer.attributes("-topmost", state["always_on_top"])
                stack_text_above_plate()
            except Exception:
                pass
        cfg["always_on_top"] = state["always_on_top"]
        save_config(cfg)

    def toggle_autostart() -> None:
        set_autostart(not is_autostart_enabled())

    def do_quit() -> None:
        if not state.get("panel_active"):
            cfg["x"] = root.winfo_x()
            cfg["y"] = root.winfo_y()
        cfg["font_size"] = int(state["font_size"])
        cfg["card_width"] = int(state["card_width"])
        cfg["card_height"] = state.get("card_height")
        cfg["allow_resize"] = bool(state["allow_resize"])
        cfg["bg_opacity"] = int(state["bg_opacity"])
        cfg["text_opacity"] = int(state["text_opacity"])
        cfg["display_mode"] = str(state["display_mode"])
        cfg["monitor_device"] = state.get("monitor_device")
        cfg["monitor_id"] = state.get("monitor_id")
        cfg["double_ctrl_toggle"] = bool(state.get("shortcut_enabled", True))
        cfg["shortcut_enabled"] = bool(state.get("shortcut_enabled", True))
        cfg["shortcut_mode"] = _normalize_shortcut_mode(state.get("shortcut_mode"))
        cfg["shortcut_key"] = _normalize_shortcut_key(state.get("shortcut_key"))
        cfg["shortcut_repeat_count"] = _normalize_shortcut_repeat_count(
            state.get("shortcut_repeat_count")
        )
        cfg["shortcut_combo"] = _normalize_shortcut_combo(state.get("shortcut_combo"))
        cfg["llm_proxy_timezone"] = _normalize_llm_proxy_timezone(
            state.get("llm_proxy_timezone")
        )
        cfg["wallpaper_folder"] = state.get("wallpaper_folder")
        cfg["wallpaper_folders"] = list(state.get("wallpaper_folders") or [])
        cfg["wallpaper_disabled_folders"] = list(
            state.get("wallpaper_disabled_folders") or []
        )
        cfg["wallpaper_folder_depths"] = dict(
            state.get("wallpaper_folder_depths") or {}
        )
        cfg["wallpaper_subdir_weights"] = dict(
            state.get("wallpaper_subdir_weights") or {}
        )
        cfg["wallpaper_random_mode"] = _normalize_wallpaper_random_mode(
            state.get("wallpaper_random_mode")
        )
        cfg["wallpaper_image_seconds"] = int(state["wallpaper_image_seconds"])
        cfg["wallpaper_audio"] = bool(state.get("wallpaper_audio", True))
        save_config(cfg)
        drop_cleanup = state.get("wallpaper_drop_cleanup")
        if callable(drop_cleanup):
            drop_cleanup()
            state["wallpaper_drop_cleanup"] = None
        bg_drop_cleanup = state.get("wallpaper_bg_drop_cleanup")
        if callable(bg_drop_cleanup):
            bg_drop_cleanup()
            state["wallpaper_bg_drop_cleanup"] = None
        _cancel_wallpaper_prepare_jobs()
        _cancel_wallpaper_check()
        wallpaper_player.stop()
        browser_wallpaper_player.stop()
        double_ctrl_job = state.get("double_ctrl_job")
        if double_ctrl_job is not None:
            try:
                root.after_cancel(double_ctrl_job)
            except Exception:
                pass
        try:
            if bg_layer is not None:
                bg_layer.destroy()
        except Exception:
            pass
        root.destroy()

    def show_menu(event: tk.Event) -> str:
        state["menu_open"] = False
        try:
            stack_text_above_plate()
            rebuild_menu()
            try:
                if sys.platform == "win32":
                    import ctypes

                    text_hwnd = layer_hwnds.get("text") or (
                        ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                    )
                    if text_hwnd:
                        ctypes.windll.user32.SetForegroundWindow(int(text_hwnd))
                root.focus_force()
                root.update_idletasks()
            except Exception:
                pass
            state["menu_open"] = True
            root.after(1, raise_popup_menu_above_hud)
            root.after(25, raise_popup_menu_above_hud)
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            state["menu_open"] = False
            menu.grab_release()
            stack_text_above_plate()
        # Most HUD children have their own interaction binding and Tk then
        # continues through the Toplevel bindtag. Without "break", one physical
        # right-click posts this same menu twice: the first Settings selection
        # merely reveals the second popup, and its lingering grab flashes the
        # panel again when the dialog closes.
        return "break"

    # ── Resize grip: width + height (overrideredirect has no native border) ──
    def start_resize(event: tk.Event) -> None:
        if not state["allow_resize"] or state.get("panel_active"):
            return
        state["resize_active"] = True
        state["resize_start_w"] = int(state["card_width"])
        state["resize_start_h"] = max(
            content_min_height(),
            int(state["card_height"] or root.winfo_height() or content_min_height()),
        )
        state["resize_start_x"] = event.x_root
        state["resize_start_y"] = event.y_root

    def do_resize(event: tk.Event) -> None:
        if not state["resize_active"]:
            return
        dw = event.x_root - int(state["resize_start_x"])
        dh = event.y_root - int(state["resize_start_y"])
        new_w = max(
            CARD_WIDTH_MIN,
            min(CARD_WIDTH_MAX, int(state["resize_start_w"]) + dw),
        )
        auto_h = content_min_height()
        new_h = max(
            auto_h,
            min(CARD_HEIGHT_MAX, int(state["resize_start_h"]) + dh),
        )
        changed = False
        if new_w != state["card_width"]:
            state["card_width"] = new_w
            changed = True
        if state.get("card_height") != new_h:
            state["card_height"] = new_h
            changed = True
        if changed:
            # Width change needs column recompute; height-only can just set geometry
            if new_w != int(state["resize_start_w"]) + 0 and abs(dw) > abs(dh):
                schedule_render(16)
            else:
                apply_geometry()

    def stop_resize(_event: tk.Event) -> None:
        if not state["resize_active"]:
            return
        state["resize_active"] = False
        cfg["card_width"] = int(state["card_width"])
        cfg["card_height"] = state.get("card_height")
        cfg["x"] = root.winfo_x()
        cfg["y"] = root.winfo_y()
        save_config(cfg)
        schedule_render(0)

    grip.bind("<ButtonPress-1>", start_resize)
    grip.bind("<B1-Motion>", do_resize)
    grip.bind("<ButtonRelease-1>", stop_resize)
    grip.bind("<Button-3>", show_menu)

    bind_drag(body)
    bind_drag(root)
    if bg_layer is not None:
        # Transparent pixels in the text window hit the plate underneath.
        # Mirror the normal window interactions there so the HUD never feels
        # frozen when the user clicks or drags an empty area.
        bind_drag(bg_layer)
        bg_layer.bind("<Motion>", lambda event: set_refresh_hover(event_hits_refresh(event)))
        bg_layer.bind("<Leave>", lambda _e: set_refresh_hover(False))
        bind_hover_tracking(bg_layer)
        bg_layer.bind("<Escape>", lambda _e: do_quit())
        bg_layer.bind("<F5>", lambda _e: refresh_async(force=True))
    root.bind("<Button-3>", show_menu)
    root.bind(
        "<FocusIn>",
        lambda event: root.after_idle(stack_text_above_plate)
        if getattr(event, "widget", None) is root
        else None,
    )
    root.bind(
        "<Map>",
        lambda event: root.after_idle(stack_text_above_plate)
        if getattr(event, "widget", None) is root
        else None,
    )
    if bg_layer is not None:
        bg_layer.bind(
            "<FocusIn>",
            lambda event: root.after_idle(stack_text_above_plate)
            if getattr(event, "widget", None) is bg_layer
            else None,
        )
    root.bind("<Escape>", lambda _e: do_quit())
    root.bind("<F5>", lambda _e: refresh_async(force=True))
    root.bind("<Control-v>", paste_wallpaper_from_clipboard)
    root.bind("<Control-V>", paste_wallpaper_from_clipboard)
    if bg_layer is not None:
        bg_layer.bind("<Control-v>", paste_wallpaper_from_clipboard)
        bg_layer.bind("<Control-V>", paste_wallpaper_from_clipboard)

    def reset_position() -> None:
        if state.get("panel_active"):
            apply_geometry()
            return
        mx, my, mw, mh = _screen_bounds()[0]
        w = int(state["card_width"])
        x, y = mx + max(12, mw - w - 24), my + 36
        root.geometry(_tk_position(x, y))
        state["floating_x"], state["floating_y"] = x, y
        cfg["x"], cfg["y"] = x, y
        save_config(cfg)

    root.bind("<Home>", lambda _e: reset_position())

    # Position — width from config
    root.update_idletasks()
    panel = state.get("panel_monitor") if state.get("panel_active") else None
    if panel is not None:
        ww, wh = panel.width, panel.height
        x, y = panel.x, panel.y
    else:
        ww, wh = int(state["card_width"]), 120
        if cfg.get("x") is not None and cfg.get("y") is not None:
            x, y = _clamp_position(int(cfg["x"]), int(cfg["y"]), ww, wh)
        else:
            mx, my, mw, mh = _screen_bounds()[0]
            x, y = mx + max(12, mw - ww - 24), my + 36
        state["floating_x"], state["floating_y"] = x, y
    initial_geo = _tk_geometry(ww, wh, x, y)
    root.geometry(initial_geo)
    if bg_layer is not None and panel is None:
        try:
            bg_layer.geometry(initial_geo)
            stack_text_above_plate()
        except Exception:
            pass
    if panel is None:
        cfg["x"], cfg["y"] = x, y
    cfg["bg_opacity"] = int(state["bg_opacity"])
    cfg["text_opacity"] = int(state["text_opacity"])
    cfg["font_size"] = int(state["font_size"])
    cfg["card_width"] = int(state["card_width"])
    cfg["card_height"] = state.get("card_height")
    cfg["allow_resize"] = bool(state["allow_resize"])
    cfg["display_mode"] = str(state["display_mode"])
    cfg["monitor_device"] = state.get("monitor_device")
    cfg["monitor_id"] = state.get("monitor_id")
    cfg["double_ctrl_toggle"] = bool(state.get("shortcut_enabled", True))
    cfg["shortcut_enabled"] = bool(state.get("shortcut_enabled", True))
    cfg["shortcut_mode"] = _normalize_shortcut_mode(state.get("shortcut_mode"))
    cfg["shortcut_key"] = _normalize_shortcut_key(state.get("shortcut_key"))
    cfg["shortcut_repeat_count"] = _normalize_shortcut_repeat_count(
        state.get("shortcut_repeat_count")
    )
    cfg["shortcut_combo"] = _normalize_shortcut_combo(state.get("shortcut_combo"))
    cfg["llm_proxy_timezone"] = _normalize_llm_proxy_timezone(
        state.get("llm_proxy_timezone")
    )
    cfg["wallpaper_folder"] = state.get("wallpaper_folder")
    cfg["wallpaper_folders"] = list(state.get("wallpaper_folders") or [])
    cfg["wallpaper_disabled_folders"] = list(
        state.get("wallpaper_disabled_folders") or []
    )
    cfg["wallpaper_folder_depths"] = dict(
        state.get("wallpaper_folder_depths") or {}
    )
    cfg["wallpaper_subdir_weights"] = dict(
        state.get("wallpaper_subdir_weights") or {}
    )
    cfg["wallpaper_random_mode"] = _normalize_wallpaper_random_mode(
        state.get("wallpaper_random_mode")
    )
    cfg["wallpaper_image_seconds"] = int(state["wallpaper_image_seconds"])
    cfg["wallpaper_audio"] = bool(state.get("wallpaper_audio", True))
    cfg["refresh_seconds"] = max(REFRESH_SECONDS, int(cfg.get("refresh_seconds") or REFRESH_SECONDS))
    save_config(cfg)

    # Apply initial resize grip visibility + opacity
    set_allow_resize(bool(state["allow_resize"]), persist=False)
    apply_opacity()
    _sync_display_runtime(available_monitors, persist=False)

    def enforce_window_band() -> None:
        if state.get("menu_open"):
            raise_popup_menu_above_hud()
            return
        try:
            root.attributes("-topmost", bool(state["always_on_top"]))
            if bg_layer is not None and not state.get("panel_active"):
                bg_layer.attributes("-topmost", bool(state["always_on_top"]))
            stack_text_above_plate()
        except Exception:
            pass

    # Tk may map/recreate the native wrapper after its first topmost call.
    root.after_idle(enforce_window_band)
    root.after(250, enforce_window_band)
    root.after(1200, enforce_window_band)
    root.after(MONITOR_POLL_MS, poll_monitors)
    state["double_ctrl_job"] = root.after(
        DOUBLE_CTRL_POLL_MS,
        poll_double_ctrl_toggle,
    )
    if not _docs_shot_active():
        root.after(350, _schedule_background_wallpaper_prepare)

    root.after(10, lambda: _hide_from_taskbar(root))
    root.after(100, lambda: _hide_from_taskbar(root))

    interval_ms = int(float(cfg.get("refresh_seconds") or REFRESH_SECONDS) * 1000)

    def tick_refresh() -> None:
        refresh_async(force=False)
        root.after(interval_ms, tick_refresh)

    if _docs_shot_active():
        def _run_docs_shot() -> None:
            log_path = Path(__file__).resolve().parent / "docs" / "screenshots" / "_shot_log.txt"
            try:
                state["display_mode"] = DISPLAY_MODE_PANEL
                state["double_ctrl_toggle"] = True
                state["shortcut_enabled"] = True
                state["panel_active"] = True
                open_settings()
                win = state.get("settings_win")
                if win is None:
                    log_path.write_text("no settings win\n", encoding="utf-8")
                    root.destroy()
                    return
                win.geometry("620x740")
                win.update()
                out_dir = Path(__file__).resolve().parent / "docs" / "screenshots"
                out_dir.mkdir(parents=True, exist_ok=True)
                select_main = getattr(win, "_select_settings_tab", None)
                select_wp = getattr(win, "_select_wallpaper_tab", None)
                log_path.write_text(
                    f"select_main={callable(select_main)} select_wp={callable(select_wp)} title={win.title()!r}\n",
                    encoding="utf-8",
                )

                def capture_main_tab(title: str, path: Path, inner_title: str | None = None) -> None:
                    if callable(select_main):
                        select_main(title)
                    win.update_idletasks()
                    win.update()
                    if inner_title and callable(select_wp):
                        select_wp(inner_title)
                        win.update_idletasks()
                        win.update()
                    time.sleep(0.35)
                    win.update()
                    _capture_widget_png(win, path)

                if callable(select_main):
                    capture_main_tab("用量", out_dir / "settings-usage.png")
                    capture_main_tab("副屏", out_dir / "settings-display.png")
                    capture_main_tab(
                        "动态壁纸",
                        out_dir / "settings-wallpaper.png",
                        "目录",
                    )
                    capture_main_tab(
                        "动态壁纸",
                        out_dir / "settings-wallpaper-weights.png",
                        "随机权重",
                    )
            except Exception:
                log_path.write_text(traceback.format_exc(), encoding="utf-8")
            finally:
                root.destroy()

        root.after(800, _run_docs_shot)
    else:
        # Reopening the panel is not a request to re-query every subscription.
        refresh_async(force=False)
        root.after(interval_ms, tick_refresh)
        root.after(15_000, tick_footer)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        if not _docs_shot_active() and not _acquire_single_instance():
            return 0
        run_ui()
        return 0

    cmd = argv[0]
    if cmd in ("-h", "--help", "help"):
        print(
            """usage-float

  python usage_float.py              Start HUD
  python usage_float.py once         Print usage once
  python usage_float.py autostart on|off|status
"""
        )
        return 0

    if cmd == "once":
        once_cfg = load_config()
        providers = fetch_all(
            active_providers(list(once_cfg.get("providers") or DEFAULT_PROVIDERS), include_discovered=True, disabled=list(once_cfg.get("disabled_providers") or [])),
            force=True,
        )
        for row in iter_display_rows(
            providers, list(once_cfg.get("providers") or DEFAULT_PROVIDERS)
        ):
            if row.failed:
                print(_tr(f"{row.title}  更新失败", f'{row.title}  Update failed'))
                continue
            right = row.summary or ""
            if row.used_pct is not None:
                rem = format_remaining(row.resets_at)
                right = (row.summary or f"{row.used_pct:.0f}%") + (
                    f"  {rem}" if rem else ""
                )
            print(f"{row.title}  {right}".rstrip())
        return 0

    if cmd in ("autostart", "install", "uninstall"):
        if cmd == "install":
            ok, msg = set_autostart(True)
        elif cmd == "uninstall":
            ok, msg = set_autostart(False)
        else:
            sub = argv[1] if len(argv) > 1 else "status"
            if sub == "on":
                ok, msg = set_autostart(True)
            elif sub == "off":
                ok, msg = set_autostart(False)
            else:
                print("enabled" if is_autostart_enabled() else "disabled")
                return 0
        print(msg)
        return 0 if ok else 1

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        _log_exception("fatal")
        traceback.print_exc()
        try:
            import tkinter as tk
            from tkinter import messagebox

            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("UsageFloat", traceback.format_exc()[-800:])
            r.destroy()
        except Exception:
            pass
        raise
