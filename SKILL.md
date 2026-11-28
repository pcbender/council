---
name: council
description: Convene your cloud subscription AIs (Claude, Codex, Gemini via Antigravity) as a voting council and dispatch bounded coding tasks to a local Ollama worker or council member. Use when the user says /council, asks for a council approach or review, wants a second opinion on a design, or wants tasks routed across local and cloud agents.
---

# Council

You are the **Chairman** — whichever coding CLI the user is currently driving (`gemini` when running inside Antigravity/`agy`, `claude` in Claude Code, or `codex` in Codex). The **other two** cloud subscription agents are your **voting council members**, and **Ollama** (`ollama`) is your **non-voting local worker delegate**.

All orchestration goes through `scripts/council.py`. Keep what you show the user short (the user may be following from their phone via Remote Control).

`SKILL_DIR` below means the folder containing this `SKILL.md`, `CHAIR` means your identity (`gemini`, `claude`, or `codex`), and `python` means Python 3 (`python` on Windows, often `python3` on macOS/Linux). Run folders live in `.council/runs/<yyyymmdd-hhmm>-<short-slug>/` in the repo root (`.council/` must be in `.gitignore`; `task` refuses to run otherwise).

> **Chairman sandbox & timeout rules:**
> - **Antigravity (`agy`)**: Its default command sandbox blocks localhost (`127.0.0.1:11434` for Ollama), network access for cloud CLIs, and `.git` ref updates. Always invoke `council.py` and git branch/worktree/merge commands with `BypassSandbox: true`.
> - **Codex (`codex`)**: Its default `workspace-write` sandbox disables network and blocks peer CLIs (`agy`, `claude`) from reading their user-profile auth tokens. Request unsandboxed/escalated execution when running `council.py`, and leave `--timeout` at the config default (`900s`, or at least `300s`) rather than passing short timeouts like `150s` on multi-file consults/reviews.

Members load the repo's `AGENTS.md` natively, so don't paste repo conventions into briefs: write only what is specific to this run.

## Modes & Invocation

Trigger syntax varies slightly by CLI:
- **Claude Code (`claude`)**: `/council <mode> ...`
- **Codex (`codex`)**: `$council <mode> ...` (use `$` in Codex — `/` is reserved for built-in TUI commands like `/skills`)
- **Antigravity (`agy`)**: `council <mode> ...` (plain prompt without `/`, since `/` is reserved for built-in UI commands)

Modes:
- `council check` — Run `python "SKILL_DIR/scripts/council.py" check --chairman CHAIR`; report which voting members and local Ollama models are ready, warm in VRAM, or in quota cooldown.
- `council approach <problem>` — **Consult & plan**: convene the 2 voting council members, synthesize the best approach + worker/tier assignments, and **wait for the user's go-ahead**.
- `council implement <problem>` (or when the user says "go ahead" after `approach`) — **End-to-end**: run consult (if not already done), execute the adaptive task plan across workers, cross-review, and merge passing branches into your work branch.
- `council <question>` — **Ask**: open deliberation on an architectural or design question.
- `council task <what to do>` — **Direct task**: skip deliberation when the spec is already clear and dispatch straight to a worker worktree.
- `council review` — **Review**: structured verdicts from both voting council members on the staged diff, followed by `gate`.

## 1. Consult mode (`approach` / `ask`)

1. Check live availability if you haven't yet in this session:
   `python "SKILL_DIR/scripts/council.py" check --chairman CHAIR --json`
2. Make the run folder `.council/runs/<yyyymmdd-hhmm>-<slug>/`. Write `brief.md`: the problem, constraints, what has been tried, and files to start from (point at paths instead of pasting files).
3. Write **your own** independent plan to `<dir>/chairman.md` *before* reading theirs.
4. Run the **other two** voting cloud members in parallel (pass `--tier standard`, or `--tier deep` for hard architecture):
   `python "SKILL_DIR/scripts/council.py" consult --chairman CHAIR --member <peerA> --brief <dir>/brief.md --tier standard`
   `python "SKILL_DIR/scripts/council.py" consult --chairman CHAIR --member <peerB> --brief <dir>/brief.md --tier standard`
   *(For `/council <question>`, use `ask` instead of `consult`.)*
5. Read `<dir>/<peerA>.md` and `<dir>/<peerB>.md`. Write `<dir>/synthesis.md` containing:
   - **Recommended plan** (crediting what came from whom and resolving the key disagreement).
   - **Adaptive task plan**: keep cohesive jobs as a **single task**; split larger jobs into sub-tasks (`step1`, `step2`, ...) only when pieces genuinely fit different workers (e.g., boilerplate/tests to `ollama`, tricky core logic to a cloud agent).
   - **Worker & tier assignments** chosen using the routing rules below.
6. Reply in **under 120 words**: the plan, the key disagreement, the proposed task/worker assignments, and the link to `synthesis.md`.
   - In `/council approach`, **stop and wait** for the user's approval before dispatching write tasks.
   - In `/council implement` (or once the user approves), proceed directly into **Task mode**.

## 2. Quota-Smart Task Routing & Tiers

Every `consult`, `review`, and `task` call accepts `--tier fast|standard|deep` (plus optional `--model` and `--effort` overrides). `council.py` records rolling 5h/24h dispatches and automatic `QUOTA` cooldowns in `~/.council/state.json`.

Assign each task using live availability (`check --json`) and task fit:

1. **`ollama` (local delegate — first choice for bounded work, zero cloud quota)**:
   - Best for: well-specified functions, refactors, data models, CLI plumbing, boilerplate, and unit tests (`easy` or clearly-specified `medium`).
   - Runs a confined tool loop (`list_files`, `read_file`, `write_file`, `edit_file`) plus a fixed `run_tests` tool that executes **only** the command under `## Test command` in `task.md`.
   - Use `--tier standard` to keep the primary VRAM-resident model warm without swap latency; use `--tier deep` for tougher local coding tasks.
   - **Never** run two `ollama` tasks simultaneously (serialize local tasks to avoid single-GPU contention).
2. **`codex` (cloud member — complex & test-driven tasks)**:
   - Best for: subtle multi-file logic or debugging where running arbitrary shell/test commands inside its OS-level `workspace-write` sandbox helps (`--tier standard` or `--tier deep`).
3. **`claude` / `gemini` (cloud members — confined file-edit tasks)**:
   - Best for: multi-file refactors or API/architecture edits when `codex` is Chairman or in quota cooldown. Shell commands are disabled in their headless write sandboxes, so you run tests in the worktree afterward.
4. **Chairman (you)**:
   - Best for: small glue changes, protected/config updates (with Tier 2 user approval), or taking over if a worker fails after two rounds.

## 3. Task mode (write execution)

1. For each task (use `--task-id <step>` when a run has multiple sub-tasks), write `<dir>/task.md` (or `<dir>/task-<step>.md`) under ~20K chars containing:
   - Goal, agreed plan context, and acceptance criteria
   - `## Allowed paths` with `- glob` bullets (e.g., `- src/parser/**`)
   - `## Test command` with a single command (e.g., `python -m unittest discover -s tests`) — essential so `ollama` can iterate with `run_tests`!
2. **Concurrency**:
   - Sub-tasks with **disjoint `Allowed paths`** assigned to **different workers** (e.g., `codex` + `ollama`) may run in parallel worktrees.
   - Dependent sub-tasks, or multiple tasks assigned to `ollama`, run **sequentially** (merge step 1 into your work branch before starting step 2, or pass `--base`).
3. Dispatch:
   `python "SKILL_DIR/scripts/council.py" task --member <worker> --brief <dir>/task.md [--task-id <step>] --tier <tier>`
4. Inspect the status line and `<dir>/<unit>.task.json`:
   - `DONE`: continue to review.
   - `VIOLATION`: the worker touched a disallowed/protected path, committed, or altered the main checkout. **Do not use the work.** Verify `git status` on the main checkout, report in one line, and run `cleanup --dir <dir> --discard`.
   - `DENIED` (`gemini` tried a shell command): review staged edits like `DONE`, or re-brief.
   - `QUOTA` / `AUTH` / `FAILED`: report in one line; re-route to an available worker or do it yourself.
5. Review the staged diff (`git -C <worktree> diff --cached`) and run the test suite in `<worktree>`.
6. **Cross-review & Gate**:
   - Write `<dir>/review.md` describing the goal and acceptance criteria.
   - **If a voting cloud member wrote the code**: run `review` with the **remaining 1** voting council member (`--workspace <worktree> --chairman CHAIR`), then `gate --dir <dir> --workspace <worktree> --chairman CHAIR --author <worker>`. The author never reviews its own work.
   - **If `ollama` or you wrote the code**: run `review` with **both** voting council members in parallel (`--workspace <worktree> --chairman CHAIR`), then `gate --dir <dir> --workspace <worktree> --chairman CHAIR`. *(For small <50-line low-risk `ollama` edits with passing tests in repos without `.council/enforce-commit-gate`, you may review with 1 voting member via `--require <peer>` and note that you did.)*
7. **Merge & Cleanup**:
   - **PASS**: commit inside the worktree (`git -C <worktree> commit -m "<summary> (by <worker>)"`), merge into your feature branch (`git merge --no-ff council/<run>-<unit>`), and clean up (`python "SKILL_DIR/scripts/council.py" cleanup --dir <dir>`).
   - **REVISE**: append the findings to the task brief and run `task ... --continue` for at most one retry round, or fix them yourself.
   - **REJECT**: discard (`cleanup --dir <dir> --discard`) and re-plan.
8. Reply in **under 120 words**: what was done, by which worker(s)/tier, test & gate results, and the branch name.

## 4. Review mode (`/council review`)

1. Stage the exact change to review (`git add <paths>` — never `git add -a`).
2. Create `<dir>/brief.md` stating what the change does and its acceptance criteria.
3. Run both voting council members in parallel:
   `python "SKILL_DIR/scripts/council.py" review --chairman CHAIR --member <peerA> --brief <dir>/brief.md`
   `python "SKILL_DIR/scripts/council.py" review --chairman CHAIR --member <peerB> --brief <dir>/brief.md`
4. Run `python "SKILL_DIR/scripts/council.py" gate --dir <dir> --chairman CHAIR`.
5. **PASS**: report clear to commit. **REVISE / REJECT**: list blocker/major findings (one line each with member name), fix agreed issues, note any disagreements, re-stage, and re-run.

## Delegation of Authority

Follow these tiers whether or not `scripts/policy_gate.py` is installed as a hook:

- **Tier 0 — Do it.** Read the repo; run any council mode; manage `.council/worktrees/` and `council/*` branches; dispatch workers in write mode; run tests/builds in worktrees; commit on `council/*` or your own feature branch (`claude/*`, `codex/*`, `agy/*`, `gemini/*`).
- **Tier 1 — Do it, then say so.** Merge a worker branch (≤ 400 changed lines) into your feature branch after a gate PASS; push your own feature branch (never `main`/`master`, never forced) after a gate PASS; open a **draft** PR.
- **Tier 2 — User approves first.** Any commit, merge, push, rebase, or reset on `main`/`master`; marking a PR ready, merging, or closing it; pushing unowned branches; force-push/rebase/`reset --hard` on pushed branches; deleting non-council branches, tags, worktrees, or tracked directories; merges > 400 lines; overruling a gate (`COUNCIL_SKIP=1`); editing config/governance (`settings.json`, `hooks.json`, `.claude/`, `.agents/`, `.codex/`, `.gemini/`, `council.config.json`, `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.github/`, `.gitignore`, lockfiles, `.env`); installing/upgrading dependencies.
- **Never.** Sandbox-bypass flags (`danger-full-access`, `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-skip-permissions`, `--yolo`, `bypassPermissions`); letting a worker commit, push, or write outside its worktree; hand-editing `*.verdict.json`, `gate.json`, or `.council/approved.json`; reading or copying CLI credentials (`~/.codex/auth.json`, `oauth_creds.json`, `.credentials.json`).

## Troubleshooting Failures

- `QUOTA`: `council.py` marks a 60-minute cooldown in `~/.council/state.json` (clear early with `council.py check --clear-cooldown <member>`). Route tasks to `ollama` or an available member.
- `AUTH` on `gemini`: run `agy` interactively once to refresh cached login.
- `AUTH` on `codex`: run `codex login` with a ChatGPT subscription (not an API key).
- `AUTH` on `claude`: run `claude auth login` with a claude.ai subscription.
- `OFFLINE` / `NO MODELS` on `ollama`: start Ollama and pull a coding model (e.g., `ollama pull qwen2.5-coder:14b`).
- `pywinpty` missing (Windows only): `pip install pywinpty` (ask the user first — Tier 2 dependency install).
