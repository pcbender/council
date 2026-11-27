# Council skill for Claude Code

Council turns Claude Code into the **chairman** of a small panel of coding agents. Codex
(ChatGPT subscription) and Gemini (Google subscription, via the Antigravity CLI `agy`) are
**members**: Claude asks them how they would approach a problem, has them review a staged
change, or hands them a task to do in their own git worktree. Members run on your existing
subscription logins, never on metered API keys.

Members are read-only when consulted or reviewing, and confined to their own worktree when
given a task. A policy hook (optional) keeps the chairman inside a delegation-of-authority
policy, which works well when you drive Claude Code from your phone over Remote Control.

## Modes

| Mode | Command | Members can |
| --- | --- | --- |
| Consult | `/council approach <problem>` | read the repo, propose a plan |
| Ask | `/council <question>` | read the repo, answer |
| Review | `/council review` | read the staged diff, return a verdict |
| Task | `/council task <what to do>` | edit files in `.council/worktrees/<run>-<member>` only |
| Check | `/council check` | nothing; reports which members are ready |

Run folders go in `.council/runs/`, worktrees in `.council/worktrees/`. Add `.council/` to
the repo's `.gitignore` (task mode refuses to run without it).

## Requirements

- Claude Code (any surface: terminal, Desktop app, IDE).
- Python 3.9 or newer. The scripts use only the standard library, except for
  `pywinpty` on Windows (see below).
- Git.
- At least one member CLI, logged in with a subscription account:
  - **Codex**: `codex` CLI, logged in with ChatGPT (`codex login`). `council.py check`
    refuses it if it is set up with an API key.
  - **Gemini**: Antigravity CLI `agy`, logged in by running `agy` once interactively.
    (The older Gemini CLI no longer serves Google AI Pro/Ultra subscriptions.)
- Windows only: `pip install pywinpty`. `agy` sometimes prints nothing when its output is
  piped, and the runner retries under a pseudo-terminal. macOS and Linux use the stdlib
  `pty` module, so nothing extra is needed there.

Disable a member you don't have in `council.config.json` (`"enabled": false`).

## Install

Claude Code loads skills from `~/.claude/skills/<name>/` (every repo) or
`<repo>/.claude/skills/<name>/` (one repo). Copy `SKILL.md`, `council.config.json` and
`scripts/` into a `council` folder in either place.

macOS / Linux:

```sh
git clone https://github.com/pcbender/council
mkdir -p ~/.claude/skills/council
cp -R council/SKILL.md council/council.config.json council/scripts ~/.claude/skills/council/
python3 ~/.claude/skills/council/scripts/council.py check
```

Windows (PowerShell):

```powershell
git clone https://github.com/pcbender/council
$dst = "$env:USERPROFILE\.claude\skills\council"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item -Recurse council\SKILL.md, council\council.config.json, council\scripts $dst
pip install pywinpty
python "$dst\scripts\council.py" check
```

`check` lists each member as ready, or says what is missing (not installed, not logged in,
API-key auth). Then add `.council/` to the `.gitignore` of each repo you use it in.

Run the tests from a clone of this repo with `python -m unittest discover -s tests`.

## Configuration

`council.config.json` sits next to `SKILL.md`:

- `timeout_seconds`: per-member time limit.
- `members.<name>.enabled`, `executable`, `model` (`null` = the CLI's default),
  `extra_args`, and `role` (the focus each member is asked to take).
- `claude` is off by default. Turn it on if you chair from another agent instead
  (read-only; it has no write mode).

## How write mode is confined

Tested on Windows with Codex 0.167.2 and agy 1.5.18; the same flags apply on macOS and
Linux.


- **Codex** runs `codex exec --sandbox workspace-write --cd <worktree>`. It can edit files and
  run commands inside the worktree. Writes to the parent folder and the main checkout are
  denied, and so are `git add`/`commit` (a linked worktree's git metadata lives in the main
  repo's `.git/`, outside the sandbox). Network is off. Caveat found in testing: the sandbox
  also allows writes to the system temp folder, so `task` refuses a worktree under
  `TEMP`, `TMP` or `TMPDIR`.
- **Gemini** runs `agy --print <prompt> --add-dir <worktree> --mode accept-edits --sandbox`.
  Headless, it may edit files inside the worktree; edits elsewhere and **every** shell
  command are auto-denied, and a denial ends the run (reported as `DENIED`). So Gemini edits
  and the chairman runs the tests. Per-session allow rules via `agy --settings` did not take
  effect in testing.
- **Afterwards** the runner compares the main checkout (HEAD, branch, status, refs, stash)
  before and after, checks the worktree has no new commits, and checks every changed path
  against the brief's `## Allowed paths` and a protected list (`.git`, `.github/`, `.claude/`,
  `.council/`, `.canto/`, `council.config.json`, `CLAUDE.md`, `.gitignore`, settings,
  lockfiles, `.env`). The main-checkout comparison uses content hashes, renames are checked
  as their new path, ignored files the member created are checked too, and a rewritten
  worktree `.git` pointer is caught before any git command runs there. Any breach is
  `VIOLATION`. Branches under `council/` are left out of the comparison so two tasks can run
  in parallel.
- **Another round** on the same worktree: `task ... --continue` (only for the registered
  worktree on its `council/` branch).
- Not checked: edits to files that were already gitignored inside the worktree, and new
  ignored files outside the allowed paths (test runs create `__pycache__` and the like).
  `cleanup` treats ignored files in a council worktree as disposable build output.

## Shared project context (AGENTS.md)

Keep rules every agent should follow in one `AGENTS.md` at the repo root. Each CLI loads
it differently (checked against each tool's docs and with real runs, November 2026):

| Agent | How it gets `AGENTS.md` |
|---|---|
| Codex (0.167.2) | Natively. Reads `~/.codex/AGENTS.md`, then every `AGENTS.md` from the git root down to `--cd`. 32 KiB cap (`project_doc_max_bytes`). |
| Gemini via agy (1.5.18) | Natively. Reads `AGENTS.md` or `GEMINI.md` (and `.agents/rules/*.md`) in the workspace and each parent up to the repo root, plus `~/.gemini/AGENTS.md` and `~/.gemini/config/...` globals. Works in `--print` mode. |
| Claude Code (2.1.325) | Reads `AGENTS.md` only when no `CLAUDE.md` exists. If the repo has a `CLAUDE.md`, that file must start with `@AGENTS.md`. |

Council runs inherit this for free: `council.py` starts Codex with `--cd <workspace>` and
agy with the workspace as its folder, and a task worktree is a full checkout, so both
members see the repo's `AGENTS.md` without it being pasted into the brief. A consult
probe confirmed both members quoted the repo's `AGENTS.md` headings without reading any
file.

Notes:
- Put shared rules in `AGENTS.md`, Claude-only notes under the import in `CLAUDE.md`.
  Don't keep a `GEMINI.md` beside it: agy loads both, so rules would be duplicated.
- Everything in `AGENTS.md` reaches every member on every run, so keep it short and
  current. A stale section misleads all three agents at once.
- `AGENTS.md` and `CLAUDE.md` are on the protected list for write mode (see above), so
  members can't change the shared rules.

## Delegation-of-authority hook (optional)

`scripts/policy_gate.py` enforces the policy in `SKILL.md`. Tier 0 and Tier 1 actions pass
silently. Tier 2 actions return `ask`, so Claude Code prompts you (on your phone, if you
are using Remote Control) and only you can approve. "Never" actions are denied. It also
contains the commit gate.

Add it to `~/.claude/settings.json` (or a repo's `.claude/settings.json`). Use the absolute
path to where you installed the skill; on Windows, use forward slashes and `python`
instead of `python3`.

```json
{
  "permissions": {
    "allow": [
      "Bash(python3 /home/you/.claude/skills/council/scripts/council.py:*)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|Read",
        "hooks": [
          { "type": "command",
            "command": "python3 /home/you/.claude/skills/council/scripts/policy_gate.py" }
        ]
      }
    ]
  }
}
```

The allow rule is a prefix match on the command Claude runs, so it only saves prompts if
the interpreter and path match what Claude actually types. Replace `/home/you/...` with
your real path (for example `/Users/you/...` on macOS or `C:/Users/you/...` on Windows).

What it checks, in short:

- **ask**: push to `main`/`master` or to a branch not named `council/*` or `claude/*`; force,
  delete, mirror or `--tags` pushes; commit, merge, rebase or reset while on `main`; merges
  over 400 changed lines; rebase or `reset --hard` of a pushed branch; deleting non-council
  branches, tags or worktrees; `git clean -f` and `checkout -- .` in the main checkout; global
  git config; `gh pr merge/ready/close`, non-draft `gh pr create`, `gh repo/release/secret/...`
  and writing `gh api` calls; dependency installs; recursive deletes outside `.council/`;
  edits to config files; `COUNCIL_SKIP=1`.
- **deny**: sandbox-bypass flags, reading CLI credentials, and writing verdict, gate or
  approval files by hand; `git commit -a` and commits whose staged diff the council has not
  passed, in repos where the commit gate is on.

It splits chained commands (`&&`, `;`, `|`, `&`, newlines, subshells), looks inside
`bash -c` / `pwsh -Command`, expands git aliases, tracks a `git checkout` earlier in the
same command, normalises paths (including `..`, `~` and NTFS `::$DATA` suffixes), and checks
shell writes (`>`, `Set-Content`, `cp`, `sed -i`...) against the same config and forgery rules
as the Edit tool.

Limits: it sees only the chairman's own tool calls, not what a member runs inside its
sandbox (the sandboxes and the after-run check cover that). It classifies commands with
simple parsing, so it is a guardrail against mistakes, not a sandbox: an agent set on
evading it could write a script and run that. Branch ownership is by name (`council/*`,
`claude/*`), so another session's `claude/*` branch counts as delegated too.

## Commit gate (optional, per repo)

Create `.council/enforce-commit-gate` in a repo to require a passing council review of the
exact staged diff before Claude can `git commit` there. Restaging anything after approval
blocks it again. Run folders alone don't turn it on.

## Gate policy

- Any member says `reject` → **REJECT**
- Any `revise`, unparseable reply, member that failed to run, or required reviewer with no
  verdict (`--require`, default every enabled member) → **REVISE**
- Any non-pass removes an earlier `.council/approved.json`
- A `pass` with a blocker or major finding is downgraded to `revise`
- Reviews of an older diff are marked stale → **REVISE**
- Otherwise → **PASS**

## Background

The design started as a salvage of an earlier multi-agent framework (Cantor). See
[docs/history.md](docs/history.md) for what was kept, what changed, and why.
