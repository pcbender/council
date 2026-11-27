# Design history: from Cantor to Council

Council began as a salvage of Cantor, an earlier framework for coordinating several coding
agents. This note records why, what was kept, and the facts that shaped the design. Read it
before making larger changes to the skill.

## Why a skill instead of a framework

Claude Code now covers most of what Cantor was built to coordinate: subagents, worktrees,
hooks, skills, MCP and Remote Control. So Cantor was retired as a framework and its useful
ideas moved into small Claude Code pieces: one skill (`SKILL.md`), one runner
(`scripts/council.py`) and one hook (`scripts/policy_gate.py`).

The goal is a "council of experts" across three consumer subscriptions (Claude, ChatGPT,
Gemini), using subscription logins rather than API billing. Claude Code is the chairman.
Running it in the Desktop app lets you drive a session from your phone with Remote Control
(`/remote-control`). Codex and Gemini are members: read-only when consulted or reviewing,
and confined to a git worktree when given a task.

## Facts that shaped the design

- **The Gemini CLI no longer serves consumer subscriptions.** It stopped serving Google AI
  Pro/Ultra accounts on 2026-06-18. The replacement is the Antigravity CLI (`agy`). Its
  headless mode is `agy -p` / `--print`, using cached credentials from one interactive
  `agy` login.
- **Known agy bug:** run from a pipe or subprocess (no TTY), `agy -p` can exit 0 and print
  nothing. The runner retries under a pseudo-terminal (stdlib `pty` on macOS and Linux,
  `pywinpty` on Windows).
- **Codex** has `codex exec` (headless) and `codex mcp-server`. ChatGPT subscription auth
  lives in `~/.codex/auth.json` with `auth_mode: chatgpt`; the runner refuses any other mode.
- **Windows is a first-class platform.** Cantor's environment allowlist was POSIX-only and
  dropped `USERPROFILE`, `APPDATA`, `SystemRoot` and similar, which broke the CLIs on native
  Windows. Prompts go over stdin or a file because Windows command lines top out at about
  32K characters.

## What was kept from Cantor

| Cantor | Council |
| --- | --- |
| `CliExecutor` + `CodexCliAdapter` / `ClaudeCliAdapter` | `scripts/council.py` adapters: read-only by default, write mode for tasks |
| `GeminiCliAdapter` (`gemini`) | Replaced by `agy` (see above) |
| `assert_auth` subscription checks | `council.py check`, which refuses a member that would bill an API key |
| `cli_env.build_subprocess_env` | `scrubbed_env()`, fixed for Windows and stripping API-key variables |
| `map_quota` | Quota and auth detection, reported as `QUOTA` / `AUTH` |
| `workspace_patch_sha256` acceptance check | Diff fingerprint: a pass only counts for the exact staged change |
| Review gate (`acceptance_ready`, blockers) | `council.py gate` plus the commit gate in `policy_gate.py` |

## New in Council

- **Cross-vendor structured verdict.** Cantor's reviews were human decisions only. Council
  members return pass / revise / reject plus findings with a severity. The gate combines
  them: any reject means REJECT; any revise, unparseable reply or missing member means
  REVISE; a pass with a blocker or major finding is downgraded to revise; reviews of an
  older diff are stale and mean REVISE.
- **Task mode.** A member does the work in its own worktree, the runner checks it stayed in
  bounds, and the other member cross-reviews.
- **Delegation of authority.** A tiered policy for what the chairman may do alone, what it
  must report, and what needs the user's approval, enforced by `policy_gate.py`.

## Open items

- Re-check `codex exec --output-last-message` and the `agy` flags (`agy --help`) when either
  CLI has a major release; adjust `extra_args` or the adapters if they change.
- Per-session allow rules for `agy` (`agy --settings`) did not take effect in testing. If
  they start working, Gemini could run tests in task mode.
