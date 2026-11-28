# Council — Multi-Agent Subscription Council + Local Ollama Worker

Council turns **whichever coding CLI you open** — **Antigravity (`agy`)**, **Claude Code (`claude`)**, or **Codex (`codex`)** — into the **Chairman** of a three-vendor agent council, backed by a **local Ollama worker delegate**:

- **Voting Council (Cloud Subscriptions)**: The active CLI acts as Chairman and convenes the **other two** headless (`codex`, `claude`, `gemini` via `agy`) to deliberate on approaches (`consult` / `ask`) and verify changes (`review` / `gate`). Everything runs on your existing consumer subscriptions — never metered API keys.
- **Worker Delegate (Local Ollama)**: Once the council converges on an approach, the Chairman routes well-scoped implementation tasks to a confined local **Ollama** worker (`ollama`) at zero quota cost, saving cloud dispatches for complex tasks and code review.

## Modes

| Mode | Command | What happens |
| --- | --- | --- |
| **Consult** | `/council approach <problem>` | Voting council proposes plans in parallel; Chairman synthesizes an adaptive task plan + worker assignments and waits for your go-ahead |
| **Implement** | `/council implement <task>` | Runs deliberation straight into worktree task execution, cross-review, gate check, and branch merge |
| **Ask** | `/council <question>` | Open design/architecture deliberation across the council |
| **Task** | `/council task <what to do>` | Dispatches a worker (`ollama`, `codex`, `claude`, or `gemini`) inside an isolated `.council/worktrees/` branch |
| **Review** | `/council review` | Voting council reviews the staged diff and runs the fail-closed `gate` |
| **Check** | `/council check` | Reports member auth, rolling 5h/24h usage, quota cooldowns, and installed/warm Ollama models |

Run artifacts live in `.council/runs/` and isolated task worktrees in `.council/worktrees/`. Add `.council/` to each repo's `.gitignore` (`task` mode refuses to run without it).

## Requirements

- Python 3.9+ (stdlib only; `pip install pywinpty` recommended on Windows for `agy`'s non-TTY fallback).
- Git.
- Consumer subscription CLIs (enable whichever ones you use in `council.config.json`):
  - **Gemini**: Antigravity CLI (`agy`), logged in once interactively.
  - **Claude**: Claude Code CLI (`claude`), logged in via `claude.ai` subscription (`claude auth status`).
  - **Codex**: Codex CLI (`codex`), logged in with ChatGPT (`codex login`; API keys are refused).
- **Ollama** (optional, enabled by default as the local `ollama` worker delegate):
  - Running locally at `http://127.0.0.1:11434` with at least one pulled coding model.
  - **Recommended 3-tier lineup for 16 GB VRAM + high system RAM (e.g. RTX 5080 + 64–96 GB DDR5)**:
    - `fast` (100% VRAM, quickest turnaround): `ollama pull qwen3.5:9b` (~6.6 GB).
    - `standard` (100% VRAM coding workhorse): `ollama pull qwen2.5-coder:14b` (~9.0 GB).
    - `deep` (30B-A3B MoE coder leveraging fast DDR5 spillover): `ollama pull qwen3-coder:30b` (~18 GB).
    - If a configured tag isn't pulled yet, `council.py` automatically falls back to whatever coding model is installed in `ollama list`.

## Install Across All Three CLIs

Link this repository once as a single source of truth into Antigravity (`~/.gemini/config/skills/council`), Claude Code (`~/.claude/skills/council`), and Codex (`~/.agents/skills/council`). On Windows, `install` uses directory junctions (`mklink /J`), which require **no Administrator privileges**:

```powershell
git clone https://github.com/pcbender/council
cd council
pip install pywinpty   # Windows only
python scripts/council.py install
python scripts/council.py check --chairman auto
```

Pass `--with-hooks` to `install` if you also want the optional `policy_gate.py` `PreToolUse` hook wired into `~/.claude/settings.json` and `~/.gemini/config/hooks.json` (`--force` replaces any earlier copied skill folders).

Run the test suite with:

```powershell
python -m unittest discover -s tests -v
```

## Smart Routing, Tiers & Global Usage Ledger

`council.config.json` defines semantic tiers (`fast`, `standard`, `deep`) mapped to each CLI's `--model` and reasoning `--effort` flags, while `delegates.ollama` configures local worker models, context size (`num_ctx`), and `keep_alive`.

Because subscription rate limits are account-wide across repositories, `council.py` maintains a lightweight rolling ledger in `~/.council/state.json`:
- Tracks 5-hour and 24-hour dispatch counts per agent.
- Automatically places a member into a 60-minute cooldown if a run hits `QUOTA` (clearable early with `python scripts/council.py check --clear-cooldown <member|all>`).
- Queries Ollama's `/api/tags` and `/api/ps` so the Chairman can see which local model is already warm in VRAM.

## How Write Mode Is Confined

Every `task` runs on a dedicated branch `council/<run>-[<step>-]<worker>` inside `.council/worktrees/<run>-[<step>-]<worker>`:

- **Ollama (`ollama`)**: Driven by a thin stdlib HTTP tool loop inside `scripts/council.py` (~500-token system prompt). Python validates every `list_files`, `read_file`, `write_file`, and `edit_file` path *before* touching disk — rejecting worktree escapes, NTFS alternate data streams/aliases, symlinks/junctions, hardlinks, `.git`, `PROTECTED_GLOBS`, and paths outside the brief's `## Allowed paths`. Its zero-argument `run_tests` tool executes **only** the Chairman's `## Test command` from `task.md` (`shell=False`, killing the full process tree on timeout). Note that `run_tests` runs as a local user process without an OS kernel sandbox, so `snapshot()` also hashes `.council` approval/verdict/gate files alongside the main checkout to detect any out-of-bounds tampering. A GPU lock (`~/.council/ollama.lock`) prevents overlapping local runs from thrashing VRAM.
- **Codex (`codex`)**: Runs `codex exec --sandbox workspace-write --cd <worktree>`. Can edit files and run test commands inside the worktree; network and writes outside the worktree (including `.git/` commits) are blocked.
- **Claude (`claude`)**: Runs `claude --print --restricted --permission-mode acceptEdits --permission-prompts none --add-dir <worktree>`. Bash/PowerShell/WebFetch are removed and file edits are confined to the worktree; the Chairman runs tests afterward.
- **Gemini (`gemini` via `agy`)**: Runs `agy --print <prompt> --add-dir <worktree> --mode accept-edits --sandbox`. File edits inside the worktree are allowed; edits elsewhere and all shell commands are auto-denied.
- **Post-run verification**: Regardless of worker, `council.py` snapshots the main checkout (HEAD, branch, staged/unstaged hashes, untracked hashes, ignored mtimes, git config/hooks, refs, stash) before and after, checks that no commits were made in the worktree, and validates all changed paths against `Allowed paths` and `PROTECTED_GLOBS`. Any breach is flagged `VIOLATION`.

## Review & Fail-Closed Gate Policy

- `ollama` is a **non-voting delegate** and never participates in `gate` verdicts.
- The active `--chairman` and any cloud `--author` of a task worktree are excluded from headless self-review:
  - **Cloud-authored worktree**: cross-reviewed by the remaining voting council member + Chairman.
  - **Ollama or Chairman work**: reviewed by both voting council members (with `--require` available for 1-reviewer sign-off on small <50-line Ollama tasks when `.council/enforce-commit-gate` is off).
- Any `reject` → **REJECT**; any `revise`, `unparseable`, `unavailable`, stale diff fingerprint, or missing required reviewer → **REVISE** (and withdraws any prior `.council/approved.json`); `pass` with a `blocker` or `major` finding is downgraded to `revise`.

## Shared Project Context (`AGENTS.md`) & Policy Hook

- Keep shared repository rules in `AGENTS.md` at the repo root (`codex` and `agy` load it natively; if a repo also has `CLAUDE.md`, start `CLAUDE.md` with `@AGENTS.md`). Both `AGENTS.md` and `CLAUDE.md` are protected in write mode.
- `scripts/policy_gate.py` enforces the Chairman's Tier 0 / Tier 1 / Tier 2 / Never policy and optional `.council/enforce-commit-gate` commit gate natively in **both** Claude Code (`settings.json`) and Antigravity (`hooks.json`), recognizing `council/*`, `claude/*`, `codex/*`, `agy/*`, and `gemini/*` feature branches.

## Background

Council evolved from an earlier multi-agent framework (Cantor). See [docs/history.md](docs/history.md) for the original design notes.
