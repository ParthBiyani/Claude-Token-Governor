# Claude Token Governor

A local, zero-dependency budget enforcer for [Claude Code](https://claude.com/claude-code) on Windows. It gives each session a hard budget against the account's **5-hour rolling rate-limit window** (the real constraint on subscription plans) instead of the context window, which most token-limiter tools mistakenly target. Runs entirely as local Python/PowerShell — no inference calls.

## Why

Claude subscription plans meter usage as a 5-hour rolling **percentage + reset time**, not a fixed token count. There's no local guardrail stopping a long session (especially with subagents) from blowing through that window. This governor tracks cumulative weighted token usage per session, converts it into a budget, and enforces it in two stages before you hit the real limit.

## How it works

- **Hooks** (`hooks/governor_hook.py`) wire into Claude Code's `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolBatch`, `PostCompact`, `SubagentStart/Stop` events and call `governor.py`.
- **`governor.py`** reconciles usage from the session's JSONL transcript plus every subagent transcript, weighting `input × 1.0`, `cache_creation × 1.25`, `cache_read × 0.1`, `output × 5.0`. It also polls Claude's read-only OAuth usage/profile endpoints (cached 60s) for the real 5h utilization %.
- **Plan detection**: auto-detects your rate-limit tier (`pro`/`max5`/`max20`) and maps it to a heuristic token-equivalent cap (`config.json`) — overridable with an explicit cap since Anthropic doesn't publish a raw quota.
- **Enforcement**: at the ship threshold, Claude gets a short reminder to wrap up; at the hard threshold (or if real account usage hits ~99.5%), `PreToolUse` denies further tool calls.
- **Status line**: shows `Model | 5h X% | GOV Y% STATE | ctx Z%` — real account usage next to local governor state.

## Install

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Sets up the governor inside your `~\.claude` config directory, registers the governor hooks, adds a status line if you don't have one, and writes a launcher:

```powershell
& "$HOME\.claude\claude.ps1"
```

## Usage

```
/limit 50 45              # session budget 50% of detected cap, ship mode at 45%
/limit 50 45 250000000    # same, with an explicit token-equivalent cap
/limit status              # live 5h%, plan, session usage, main vs subagent breakdown
/limit on | off | reset    # enable / disable / reset local accounting for this session
```

`/token-limit` is an equivalent alias. Each session tracks its own budget independently by `session_id`, but all sessions still share the account's real 5-hour limit — the account-wide 99.5% backstop protects every session once that's close, regardless of local budgets.

## Configuration

Default caps (`config.example.json` → copied to `.claude\token-governor\config.json`):

```json
{ "planCaps": { "pro": 25000000, "max5": 125000000, "max20": 500000000 } }
```

Or override per-launch: `$env:CLAUDE_5H_TOKEN_CAP = "250000000"`.

## Uninstall

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\uninstall.ps1
```

Reverts hook registration (backs up `settings.json` first) and removes the governor's files.

## Notes and limitations

- The token-equivalent cap is a heuristic, not an Anthropic-published quota — the live 5h utilization % is always authoritative and shown alongside it.
- A local governor can't reserve a slice of a shared server-side quota; the account-wide backstop is the strongest mitigation possible without controlling Anthropic's own scheduler.
- Credentials are read only from the existing `.claude\.credentials.json` OAuth token to call read-only endpoints; never logged or written elsewhere.

## Layout

```
governor.py              Core accounting/enforcement engine (CLI)
hooks/governor_hook.py   Hook entrypoint wiring Claude Code events to governor.py
skill/limit/, skill/token-limit/   Slash command definitions
statusline.ps1            Status line script
install.ps1 / uninstall.ps1
config.example.json       Default plan-tier token-equivalent caps
```

## License

No license file is currently included; all rights reserved by the author unless a license is added.
