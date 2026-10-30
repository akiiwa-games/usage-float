---
name: check-quota
description: Inspect live multi-account AI subscription quotas, 5-hour/7-day rate-limit windows, and reset timers via usage-float CLI or local JSON cache before dispatching coding-agent tasks.
---

# Check Quota

Read the live remaining quota of every subscription you have signed in to (Claude Code, Codex / ChatGPT, Grok, Antigravity, LiteLLM proxy) before starting a sub-agent, worker or long-running job, so work lands on an account that still has headroom and does not stall on a rate-limit wall.

## What it does

[usage-float](https://github.com/akiiwa-games/usage-float) is a headless quota collector plus an optional Windows HUD. For each account it records one or more rate-limit windows (for example `5h`, `7d`) with a used percentage and a reset timestamp, and writes them to a local JSON cache. Hosts such as Claude Code, Codex, Antigravity and Paseo, and routers such as [AgentCompass](https://github.com/akiiwa-games/AgentCompass) (`route-agent-work`), use it to decide which account and model to dispatch to.

## Installation & prerequisites

- Windows with Python 3.10+ (run from the cloned repository), or the `UsageFloat.exe` release from the Releases page (`UsageFloat.exe once`).
- Clone or download <https://github.com/akiiwa-games/usage-float>.
- Be signed in to the providers you want measured (`claude`, `codex`, `grok`, `agy`); usage-float discovers them from their normal local login state.

## Usage for agents

1. **Read the cache** (fast, no network): parse `~/.usage-float/usage-cache.json`.

   | Field | Meaning |
   |---|---|
   | `providers.<id>` | One entry per account: `claude`, `claude-2`, `codex`, `codex-2`, `grok`, `antigravity`, `llmproxy`, ... |
   | `.windows[]` | Rate-limit windows: `id` (for example `seven_day`, `five_hour`, `weekly_fable`, `gemini-weekly`), `label` (for example `7d`, `5h`), `used_pct` (0-100), `resets_at` (ISO-8601 reset time) |
   | `.plan_label` | Subscription tier when known (for example `max`, `pro`) |
   | `.fetched_at` | When this reading was taken |
   | `.stale`, `.error` | `stale: true` or a non-null `error` means the last refresh failed and the numbers are an older reading |
   | `.summary` | Free-text figure for budget-style rows (for example `$0/200`) |

   Remaining quota is `100 - used_pct`. Check `fetched_at` and `stale` before trusting a number.

2. **Force a fresh headless reading** when the cache is old or missing:

   ```powershell
   python usage_float.py once
   ```

   It prints one line per window (title, usage, time until reset) and exits. No UI is opened.

3. **Decide**: prefer accounts whose relevant window (`5h` for short jobs, `7d` for long ones) has headroom, and avoid starting large work on a window that resets later than the job would take.

## Permissions & network disclosure

- Read-only: usage-float reads the local login state of the providers you already use (`~/.claude*`, `~/.codex*`, `~/.grok`, the `agy` CLI). It never rotates, writes or exports tokens or API keys.
- Network: it sends read-only quota queries only to each provider's own official usage endpoint (and your own LiteLLM/Magpie endpoint if configured). Nothing is sent to any third-party server.
- Local writes: its own files under `~/.usage-float/` (`config.json`, `usage-cache.json`). The JSON cache contains one-way credential fingerprints, not credentials.
- Reading the cache file needs no network and no credentials.
