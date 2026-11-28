#!/usr/bin/env python3
"""Council runner — multi-chairman subscription council + local Ollama delegate.

Runs voting cloud council members (codex, gemini via Antigravity `agy`, claude)
headless on your subscription logins, and dispatches bounded write tasks to
either a cloud member or a non-voting local Ollama worker inside an isolated
git worktree.

Stdlib only (plus optional `pywinpty` on Windows for agy's non-TTY fallback).

Usage:
  python council.py check   [--chairman auto|claude|codex|gemini] [--json]
  python council.py consult --member codex  --brief .council/runs/<id>/brief.md [--tier standard]
  python council.py ask     --member claude --brief .council/runs/<id>/brief.md
  python council.py review  --member gemini --brief .council/runs/<id>/brief.md
  python council.py gate    --dir .council/runs/<id> [--chairman auto] [--author ollama]
  python council.py task    --member ollama --brief .council/runs/<id>/task.md [--task-id step1] [--tier fast]
  python council.py cleanup --dir .council/runs/<id> [--member ollama] [--task-id step1]
  python council.py install [--with-hooks] [--force]
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import council_install

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
CONFIG_PATH = SKILL_ROOT / "council.config.json"
STATE_PATH = Path(os.environ.get("COUNCIL_STATE_PATH") or (Path.home() / ".council" / "state.json"))
HISTORY_RETENTION_SECONDS = 7 * 86400

# ---------------------------------------------------------------------------
# Environment scrubbing (from Cantor cli_env.py)
# ---------------------------------------------------------------------------
SAFE_ENV_KEYS = {
    "HOME", "LANG", "LC_ALL", "PATH", "TERM", "TZ", "TMPDIR",
    # Windows essentials
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMROOT", "SYSTEMDRIVE",
    "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERNAME", "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
}
# Never leak API keys into a subscription worker: if one is present the CLI may
# silently switch to metered API billing.
BLOCKED_ENV_KEYS = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CODEX_API_KEY", "CODEX_BASE_URL",
    "GEMINI_API_KEY", "GEMINI_BASE_URL", "GOOGLE_API_KEY", "GOOGLE_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
}


def scrubbed_env() -> dict[str, str]:
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in SAFE_ENV_KEYS and upper not in BLOCKED_ENV_KEYS:
            env[key] = value
    env.setdefault("NO_COLOR", "1")
    return env


# ---------------------------------------------------------------------------
# Config & Chairman / Tier resolution
# ---------------------------------------------------------------------------
CLOUD_MEMBERS = ("codex", "gemini", "claude")
TIERS = ("fast", "standard", "deep")

DEFAULT_CONFIG = {
    "timeout_seconds": 900,
    "quota_cooldown_seconds": 3600,
    "members": {
        "codex": {
            "enabled": True, "executable": "codex", "model": None, "extra_args": [],
            "tiers": {
                "fast": {"model": None, "effort": "low"},
                "standard": {"model": None, "effort": "medium"},
                "deep": {"model": None, "effort": "high"},
            },
            "role": "Adversarial code reviewer: correctness, edge cases, concurrency, bugs, security.",
        },
        "gemini": {
            "enabled": True, "executable": "agy", "model": None, "extra_args": [],
            "tiers": {
                "fast": {"model": None, "effort": "low"},
                "standard": {"model": None, "effort": "medium"},
                "deep": {"model": None, "effort": "high"},
            },
            "role": "Whole-repo critic: architecture, consistency with the rest of the codebase, what was missed.",
        },
        "claude": {
            "enabled": True, "executable": "claude", "model": None, "extra_args": [],
            "tiers": {
                "fast": {"model": "sonnet", "effort": "low"},
                "standard": {"model": "sonnet", "effort": "medium"},
                "deep": {"model": "opus", "effort": "high"},
            },
            "role": "Pragmatic implementer & API reviewer: design clarity, maintainability, failure handling.",
        },
    },
    "delegates": {
        "ollama": {
            "enabled": True,
            "base_url": "http://127.0.0.1:11434",
            "keep_alive": "30m",
            "num_ctx": 16384,
            "max_steps": 25,
            "test_timeout_seconds": 120,
            "tiers": {
                "fast": {"model": "qwen3.5:9b"},
                "standard": {"model": "qwen2.5-coder:14b"},
                "deep": {"model": "qwen3-coder:30b"},
            },
            "role": "Local implementation worker: execute bounded file edits and iterate against tests.",
        }
    },
}


def _merge_actor_group(dst: dict, src: dict) -> None:
    for name, values in (src or {}).items():
        if not isinstance(values, dict):
            continue
        entry = dst.setdefault(name, {})
        tiers = values.get("tiers")
        for k, v in values.items():
            if k != "tiers":
                entry[k] = v
        if isinstance(tiers, dict):
            dst_tiers = entry.setdefault("tiers", {})
            for t_name, t_vals in tiers.items():
                if isinstance(t_vals, dict):
                    dst_tiers.setdefault(t_name, {}).update(t_vals)


def load_config(home: Path | None = None) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in (CONFIG_PATH, (home or Path.home()) / ".council/council.config.json"):
        if not path.is_file():
            continue
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(user, dict) or any(
                user.get(name) is not None and not isinstance(user[name], dict) for name in ("members", "delegates")
            ):
                raise ValueError("configuration, members and delegates must be objects")
        except ValueError as exc:
            raise ValueError(f"Invalid Council configuration in {path}: {exc}") from exc
        for scalar in ("timeout_seconds", "quota_cooldown_seconds"):
            if scalar in user:
                config[scalar] = user[scalar]
        _merge_actor_group(config["members"], user.get("members") or {})
        _merge_actor_group(config["delegates"], user.get("delegates") or {})
    return config


CHAIRMAN_ALIASES = {"agy": "gemini", "antigravity": "gemini"}


def detect_chairman(env: dict[str, str] | None = None) -> str | None:
    environ = os.environ if env is None else env
    explicit = environ.get("COUNCIL_CHAIRMAN", "").strip().lower()
    if explicit:
        explicit = CHAIRMAN_ALIASES.get(explicit, explicit)
        if explicit in CLOUD_MEMBERS:
            return explicit
    if environ.get("CLAUDECODE") == "1" or "CLAUDE_CODE_ENTRYPOINT" in environ:
        return "claude"
    if any(k in environ for k in ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_CI")):
        return "codex"
    if any(k.startswith("ANTIGRAVITY_") for k in environ):
        return "gemini"
    return None


def resolve_chairman(value: str | None, env: dict[str, str] | None = None) -> str | None:
    if not value:
        return None
    norm = CHAIRMAN_ALIASES.get(value.strip().lower(), value.strip().lower())
    if norm == "auto":
        return detect_chairman(env)
    if norm not in CLOUD_MEMBERS:
        raise ValueError(f"Unknown chairman '{value}' (expected claude, codex, gemini, or auto).")
    return norm


def resolve_tier(cfg: dict, tier: str | None = None, model: str | None = None,
                 effort: str | None = None) -> tuple[str | None, str | None]:
    tier_cfg = (cfg.get("tiers") or {}).get(tier or "") or {}
    resolved_model = model if model is not None else (
        tier_cfg.get("model") if tier and tier_cfg.get("model") is not None else cfg.get("model")
    )
    resolved_effort = effort if effort is not None else tier_cfg.get("effort")
    return resolved_model, resolved_effort


# ---------------------------------------------------------------------------
# Global usage & quota ledger (~/.council/state.json)
# ---------------------------------------------------------------------------
def _state_file() -> Path:
    override = os.environ.get("COUNCIL_STATE_PATH")
    return Path(override) if override else STATE_PATH


def _safe_float(val, default: float = 0.0) -> float:
    try:
        out = float(val)
        return out if out == out and out not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


@contextlib.contextmanager
def _file_lock(lock_path: Path, timeout: float = 10.0, *, required: bool = False):
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+b")
    except OSError:
        if required:
            raise
        yield
        return
    start = time.time()
    locked = False
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() - start > timeout:
                    raise TimeoutError(f"Timed out waiting for lock {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _read_state_unlocked(path: Path) -> dict:
    if not path.is_file():
        return {"history": [], "cooldowns": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "history": data.get("history") if isinstance(data.get("history"), list) else [],
                "cooldowns": data.get("cooldowns") if isinstance(data.get("cooldowns"), dict) else {},
            }
    except (OSError, ValueError):
        pass
    return {"history": [], "cooldowns": {}}


def load_state() -> dict:
    return _read_state_unlocked(_state_file())


def _write_state_unlocked(path: Path, state: dict) -> None:
    now = time.time()
    cutoff = now - HISTORY_RETENTION_SECONDS
    state["history"] = [h for h in state.get("history", [])
                        if isinstance(h, dict) and _safe_float(h.get("ts")) >= cutoff][-500:]
    state["cooldowns"] = {
        k: v for k, v in state.get("cooldowns", {}).items()
        if isinstance(v, dict) and _safe_float(v.get("until")) > now
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def save_state(state: dict) -> None:
    path = _state_file()
    with contextlib.suppress(TimeoutError), _file_lock(path.with_suffix(".lock")):
        _write_state_unlocked(path, state)


def record_usage(target: str, kind: str, result: "RunResult", tier: str | None = None,
                 model: str | None = None, cooldown_seconds: int = 3600) -> None:
    path = _state_file()
    with contextlib.suppress(OSError, TimeoutError), _file_lock(path.with_suffix(".lock")):
        now = time.time()
        state = _read_state_unlocked(path)
        state["history"].append({
            "ts": round(now, 2),
            "target": target,
            "kind": kind,
            "tier": tier,
            "model": model,
            "ok": result.ok,
            "quota_hit": result.quota_hit,
            "seconds": result.seconds,
        })
        if result.quota_hit and cooldown_seconds > 0:
            state["cooldowns"][target] = {
                "until": round(now + cooldown_seconds, 2),
                "reason": f"QUOTA hit during {kind}",
            }
        elif result.ok and target in state["cooldowns"]:
            del state["cooldowns"][target]
        _write_state_unlocked(path, state)


def usage_summary(target: str, state: dict | None = None, now: float | None = None) -> dict:
    st = load_state() if state is None else state
    ts_now = time.time() if now is None else now
    hist = [h for h in st.get("history", []) if isinstance(h, dict) and h.get("target") == target]
    runs_5h = sum(1 for h in hist if ts_now - _safe_float(h.get("ts")) <= 5 * 3600)
    runs_24h = sum(1 for h in hist if ts_now - _safe_float(h.get("ts")) <= 86400)
    quota_24h = sum(1 for h in hist if h.get("quota_hit") and ts_now - _safe_float(h.get("ts")) <= 86400)
    cd = st.get("cooldowns", {}).get(target) or {}
    rem = max(0, int(round(_safe_float(cd.get("until")) - ts_now))) if isinstance(cd, dict) else 0
    return {
        "runs_5h": runs_5h,
        "runs_24h": runs_24h,
        "quota_hits_24h": quota_24h,
        "cooldown_remaining_s": rem,
        "available_quota": rem == 0,
    }


def clear_cooldown(target: str) -> None:
    path = _state_file()
    with contextlib.suppress(OSError, TimeoutError), _file_lock(path.with_suffix(".lock")):
        state = _read_state_unlocked(path)
        if target == "all":
            state["cooldowns"] = {}
        else:
            state["cooldowns"].pop(target, None)
        _write_state_unlocked(path, state)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    member: str
    ok: bool
    text: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    quota_hit: bool = False
    auth_problem: bool = False
    used_pty: bool = False
    denied: str = ""
    model_used: str | None = None
    effort_used: str | None = None
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


class MemberUnavailable(RuntimeError):
    pass


def looks_like_quota(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in ("rate limit", "quota", "usage limit", "resource_exhausted"))


def looks_like_auth(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in ("authentication required", "not logged in", "please log in",
                                "login required", "unauthorized"))


# ---------------------------------------------------------------------------
# Cloud Adapters (read-only by default; write=True confines edits to worktree)
# ---------------------------------------------------------------------------
SANDBOX_BYPASS = ("danger-full-access", "--dangerously-bypass-approvals-and-sandbox",
                  "--dangerously-skip-permissions", "--yolo", "bypasspermissions")


class Adapter:
    name = ""
    label = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def executable(self) -> str:
        for arg in self.cfg.get("extra_args", []):
            if any(flag in str(arg).lower() for flag in SANDBOX_BYPASS):
                raise MemberUnavailable(f"extra_args contains a sandbox bypass ({arg}); refused.")
        exe = self.cfg.get("executable") or self.name
        resolved = shutil.which(exe)
        if not resolved and os.name == "nt" and exe.lower().endswith(".ps1"):
            alt = exe[:-4] + ".cmd"
            resolved = shutil.which(alt) or (alt if Path(alt).is_file() else None)
        if not resolved and Path(exe).is_file():
            resolved = str(Path(exe).resolve())
        if not resolved:
            raise MemberUnavailable(f"{self.label} not found on PATH: {exe}")
        if os.name == "nt" and resolved.lower().endswith(".ps1"):
            cmd_sibling = Path(resolved).with_suffix(".cmd")
            if cmd_sibling.is_file():
                return str(cmd_sibling)
        return resolved

    def check_auth(self) -> str:
        return "not checked"

    def build(self, prompt_file: Path, workspace: Path, out_file: Path,
              write: bool = False, model: str | None = None,
              effort: str | None = None) -> tuple[list[str], str | None]:
        """Return (argv, stdin_text)."""
        raise NotImplementedError

    def read_answer(self, stdout: str, out_file: Path) -> str:
        return stdout


class CodexAdapter(Adapter):
    name, label = "codex", "Codex CLI"

    def check_auth(self) -> str:
        auth = Path.home() / ".codex" / "auth.json"
        if not auth.is_file():
            raise MemberUnavailable("Codex is not logged in (run `codex login`).")
        try:
            data = json.loads(auth.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemberUnavailable(f"Cannot read Codex auth state: {exc}")
        if data.get("auth_mode") != "chatgpt":
            raise MemberUnavailable("Codex is not using ChatGPT subscription auth.")
        if data.get("OPENAI_API_KEY"):
            raise MemberUnavailable("Codex auth contains an API key (would bill the API).")
        return "ChatGPT subscription"

    def build(self, prompt_file, workspace, out_file, write=False, model=None, effort=None):
        sandbox = "workspace-write" if write else "read-only"
        argv = [self.executable(), "exec", "--sandbox", sandbox,
                "--skip-git-repo-check", "--cd", str(workspace),
                "--output-last-message", str(out_file)]
        chosen_model = model if model is not None else self.cfg.get("model")
        if chosen_model:
            argv += ["--model", chosen_model]
        if effort:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        argv += list(self.cfg.get("extra_args", []))
        argv.append("-")  # prompt on stdin: no argv length limit on Windows
        return argv, prompt_file.read_text(encoding="utf-8")

    def read_answer(self, stdout, out_file):
        if out_file.is_file() and out_file.read_text(encoding="utf-8").strip():
            return out_file.read_text(encoding="utf-8")
        return stdout


class ClaudeAdapter(Adapter):
    name, label = "claude", "Claude Code CLI"

    def check_auth(self) -> str:
        try:
            done = subprocess.run([self.executable(), "auth", "status"], text=True,
                                  capture_output=True, timeout=15, env=scrubbed_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MemberUnavailable(f"Cannot inspect Claude auth: {exc}")
        text = (done.stdout + done.stderr).lower()
        if done.returncode:
            raise MemberUnavailable("Claude CLI is not logged in.")
        if "api key" in text or "apikey" in text:
            raise MemberUnavailable("Claude CLI appears to be on API-key auth.")
        return "claude.ai subscription"

    def build(self, prompt_file, workspace, out_file, write=False, model=None, effort=None):
        exe = self.executable()
        if write:
            # --restricted removes Bash/PowerShell/WebFetch and confines file edits to
            # working directories (--add-dir); --permission-prompts none auto-denies
            # anything outside acceptEdits (such as settings/git config writes).
            argv = [exe, "--print", "--input-format", "text", "--output-format", "text",
                    "--restricted", "--permission-mode", "acceptEdits",
                    "--permission-prompts", "none", "--no-session-persistence",
                    "--add-dir", str(workspace)]
        else:
            argv = [exe, "--print", "--input-format", "text", "--output-format", "text",
                    "--permission-mode", "plan", "--permission-prompts", "none",
                    "--no-session-persistence", "--add-dir", str(workspace),
                    "--add-dir", str(prompt_file.parent)]
            main = main_checkout(workspace)
            if main is not None:
                argv += ["--add-dir", str(main)]
        chosen_model = model if model is not None else self.cfg.get("model")
        if chosen_model:
            argv += ["--model", chosen_model]
        if effort:
            argv += ["--effort", effort]
        argv += list(self.cfg.get("extra_args", []))
        return argv, prompt_file.read_text(encoding="utf-8")


class GeminiAgyAdapter(Adapter):
    """Gemini via Antigravity CLI (`agy`)."""
    name, label = "gemini", "Antigravity CLI (agy)"

    def check_auth(self) -> str:
        return "cached agy login (run `agy` once interactively if this fails)"

    def build(self, prompt_file, workspace, out_file, write=False, model=None, effort=None):
        if write:
            prompt = prompt_file.read_text(encoding="utf-8")
            if self.executable().lower().endswith((".cmd", ".bat")):
                raise MemberUnavailable("agy resolves to a .cmd/.bat wrapper, which would mangle the "
                                        "inline prompt; point `executable` at agy.exe.")
            if len(prompt) > MAX_INLINE_PROMPT:
                raise MemberUnavailable(
                    f"Task prompt is {len(prompt)} chars; agy write mode passes it inline "
                    f"(limit {MAX_INLINE_PROMPT}). Shorten the brief and point at files.")
            argv = [self.executable(), "--print", prompt, "--add-dir", str(workspace),
                    "--mode", "accept-edits", "--sandbox", "--disable-slash-commands"]
        else:
            # Note: Do NOT pass `--mode plan` on agy 1.5.19+: plan mode triggers the
            # interactive /plan workflow and writes an implementation_plan.md artifact
            # instead of replying on stdout. Default headless `--print` with `--sandbox`
            # auto-denies all file writes and shell commands while answering on stdout.
            instruction = (
                f"Read the file at {prompt_file} and do exactly what it asks. "
                "Do not modify any files and do not create artifact/plan files — write your "
                "complete response directly in your reply text only. Do not run shell commands: "
                "in this mode they are denied and end your run, so use your file-reading tools only."
            )
            argv = [self.executable(), "--print", instruction, "--add-dir", str(workspace),
                    "--add-dir", str(prompt_file.parent)]
            main = main_checkout(workspace)
            if main is not None:
                argv += ["--add-dir", str(main)]
            argv += ["--sandbox", "--disable-slash-commands"]
        chosen_model = model if model is not None else self.cfg.get("model")
        if chosen_model:
            argv += ["--model", chosen_model]
        if effort:
            argv += ["--effort", effort]
        argv += list(self.cfg.get("extra_args", []))
        return argv, None


ADAPTERS = {"codex": CodexAdapter, "gemini": GeminiAgyAdapter, "claude": ClaudeAdapter}
WRITE_MEMBERS = ("codex", "gemini", "claude", "ollama")
MAX_INLINE_PROMPT = 24000
AGY_DENIED = re.compile(r"no output produced.*?required the \"(\w+)\" permission", re.S)


# ---------------------------------------------------------------------------
# Cloud CLI execution, with the agy non-TTY workaround
# ---------------------------------------------------------------------------
def _run_plain(argv, stdin_text, cwd, timeout):
    io_kw = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(argv, **io_kw, cwd=cwd, text=True, encoding="utf-8",
                          errors="replace", capture_output=True, timeout=timeout,
                          env=scrubbed_env(), check=False)


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def _run_pty(argv, cwd, timeout) -> tuple[str, int]:
    """Run under a pseudo-terminal. Windows: pywinpty (`pip install pywinpty`).
    POSIX: stdlib pty."""
    if os.name == "nt":
        try:
            from winpty import PtyProcess  # type: ignore
        except ImportError as exc:
            raise MemberUnavailable(
                "agy printed nothing to a pipe (known non-TTY bug) and pywinpty is not "
                "installed. Fix: `pip install pywinpty`.") from exc
        proc = PtyProcess.spawn(subprocess.list2cmdline(argv), cwd=str(cwd),
                                env=scrubbed_env(), dimensions=(50, 400))
        chunks, start = [], time.time()
        while True:
            try:
                data = proc.read(4096)
            except EOFError:
                break
            if data:
                chunks.append(data)
            elif not proc.isalive():
                break
            if time.time() - start > timeout:
                proc.terminate(force=True)
                raise subprocess.TimeoutExpired(argv, timeout)
        code = proc.exitstatus if proc.exitstatus is not None else 0
        return ANSI.sub("", "".join(chunks)).replace("\r", ""), code
    import pty
    import select
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(argv[0], argv, scrubbed_env())
    chunks, start = [], time.time()
    while True:
        if time.time() - start > timeout:
            os.kill(pid, 9)
            raise subprocess.TimeoutExpired(argv, timeout)
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", "replace"))
    _, status = os.waitpid(pid, 0)
    return ANSI.sub("", "".join(chunks)).replace("\r", ""), os.waitstatus_to_exitcode(status)


def run_member(name: str, prompt_file: Path, workspace: Path, out_dir: Path, cfg: dict,
               timeout: int, write: bool = False, model: str | None = None,
               effort: str | None = None, unit: str | None = None) -> RunResult:
    adapter = ADAPTERS[name](cfg)
    out_file = out_dir / f"{unit or name}.raw.md"
    with contextlib.suppress(OSError):
        out_file.unlink()
    result = RunResult(member=name, ok=False, model_used=model or cfg.get("model"), effort_used=effort)
    start = time.time()
    try:
        adapter.executable()
    except MemberUnavailable as exc:
        result.notes.append(str(exc))
        result.seconds = round(time.time() - start, 1)
        return result
    try:
        adapter.check_auth()
    except MemberUnavailable as exc:
        result.auth_problem = True
        result.notes.append(str(exc))
        result.seconds = round(time.time() - start, 1)
        return result
    gitfile = workspace / ".git"
    pointer = gitfile.read_bytes() if write and gitfile.is_file() else b""

    def tree_state() -> str:
        if (gitfile.read_bytes() if gitfile.is_file() else b"missing") != pointer:
            return "\0pointer changed"
        return "".join(subprocess.run(["git", "-C", str(workspace), *cmd], capture_output=True,
                                      text=True, encoding="utf-8", errors="replace").stdout
                       for cmd in (["status", "--porcelain"], ["diff", "HEAD"]))
    before_state = tree_state() if write else ""
    try:
        argv, stdin_text = adapter.build(prompt_file, workspace, out_file,
                                         write=write, model=model, effort=effort)
        try:
            done = _run_plain(argv, stdin_text, workspace, timeout)
            result.exit_code, result.stderr = done.returncode, done.stderr
            text = adapter.read_answer(done.stdout, out_file)
            if name == "gemini" and done.returncode == 0 and not text.strip():
                changed = write and tree_state() != before_state
                if changed:
                    result.notes.append("Empty report on pipe; edits were made, so not re-run.")
                    text = "(agy made edits but its report was lost to the non-TTY bug)"
                else:
                    result.notes.append("Empty stdout on pipe; retried under a pseudo-terminal.")
                    text, result.exit_code = _run_pty(argv, workspace, timeout)
                    result.used_pty = True
        except subprocess.TimeoutExpired:
            result.timed_out = True
            text = ""
        result.text = text.strip()
        blob = f"{result.text}\n{result.stderr}"
        result.quota_hit = looks_like_quota(blob) and result.exit_code not in (0, None)
        result.auth_problem = looks_like_auth(blob) and result.exit_code not in (0, None)
        result.ok = bool(result.text) and result.exit_code == 0 and not result.timed_out
        denied = AGY_DENIED.search(result.text) if name == "gemini" else None
        if denied:
            result.ok = False
            result.denied = denied.group(1)
            result.notes.append(f"agy tried an action needing the '{denied.group(1)}' "
                                "permission; it was auto-denied and the run stopped.")
        if result.timed_out:
            result.notes.append(f"Timed out after {timeout}s.")
        elif not result.ok and not result.text:
            result.notes.append("No answer produced.")
    except MemberUnavailable as exc:
        result.notes.append(str(exc))
    result.seconds = round(time.time() - start, 1)
    return result


# ---------------------------------------------------------------------------
# Local Ollama Worker Delegate (stdlib HTTP + Python-enforced path confinement)
# ---------------------------------------------------------------------------
OLLAMA_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OLLAMA_SYSTEM_PROMPT = """You are a focused local coding worker executing a bounded task inside a git worktree.
Follow the task brief carefully:
1. Read relevant files before editing so line contents match accurately.
2. Stay strictly within the allowed paths and never touch git or config files.
3. Prefer `edit_file` for targeted changes and `write_file` for new or full-file rewrites.
4. If `run_tests` is available, run it after making changes and fix any failures.
5. Once complete, stop calling tools and reply with:
## Changed
- <files and summary>
## Tested
- <test result or "not run">
## Unsure
- <anything left uncertain or "nothing">
"""


def _ollama_http(base_url: str, endpoint: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
    url = base_url.rstrip("/") + endpoint
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with OLLAMA_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ollama_status(base_url: str, timeout: float = 4.0) -> tuple[bool, list[str], list[str], str]:
    """Return (reachable, installed_models, warm_models, error_detail)."""
    try:
        tags_data = _ollama_http(base_url, "/api/tags", timeout=timeout)
        installed = [m["name"] for m in tags_data.get("models", [])
                     if isinstance(m, dict) and isinstance(m.get("name"), str)]
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return False, [], [], f"Ollama unreachable at {base_url}: {exc}"
    warm = []
    try:
        ps_data = _ollama_http(base_url, "/api/ps", timeout=timeout)
        warm = [m["name"] for m in ps_data.get("models", [])
                if isinstance(m, dict) and isinstance(m.get("name"), str)]
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return True, installed, warm, ""


def select_ollama_model(desired: str | None, installed: list[str],
                        warm: list[str]) -> tuple[str, str | None]:
    if not installed:
        raise MemberUnavailable("No Ollama models are installed (run `ollama pull <model>`).")
    if desired:
        for candidate in (desired, f"{desired}:latest"):
            if candidate in installed:
                return candidate, None
    coding_hints = ("coder", "codestral", "devstral", "qwen", "deepseek", "llama", "gemma")
    for pool in (warm, installed):
        for hint in coding_hints:
            for name in pool:
                if hint in name.lower():
                    note = (f"Configured model '{desired}' not installed; fell back to '{name}'."
                            if desired else None)
                    return name, note
    chosen = warm[0] if warm else installed[0]
    note = f"Configured model '{desired}' not installed; fell back to '{chosen}'." if desired else None
    return chosen, note


def parse_test_command(brief_text: str) -> str | None:
    m = re.search(r"^#+\s*Test command\s*$(.*?)(?=^#|\Z)", brief_text, re.M | re.S | re.I)
    if not m:
        return None
    body = m.group(1).strip()
    fenced = re.search(r"```(?:[A-Za-z0-9_-]+)?\s*\n(.*?)\n```", body, re.S)
    if fenced:
        lines = [ln.strip() for ln in fenced.group(1).splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        return lines[0] if lines else None
    for raw in body.splitlines():
        line = re.sub(r"^\s*[-*]\s+", "", raw).strip().strip("`").strip()
        if line and not line.startswith("#"):
            return line
    return None


@contextlib.contextmanager
def _ollama_gpu_lock(timeout: float = 600.0):
    with _file_lock(_state_file().parent / "ollama.lock", timeout=timeout):
        yield


DOS_SHORT_RE = re.compile(r"~\d+(?:\.|$)")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    if hasattr(path, "is_junction") and path.is_junction():
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _safe_worktree_target(wt: Path, raw_path: str, allowed: list[str],
                          for_write: bool) -> tuple[Path | None, str | None, str | None]:
    """Validate `raw_path` inside `wt`. Returns (resolved_path, posix_rel, error_msg)."""
    if not raw_path or not isinstance(raw_path, str):
        return None, None, "Error: path must be a non-empty string."
    if "\0" in raw_path:
        return None, None, "Error: invalid NUL byte in path."
    wt_root = wt.resolve()
    cleaned = raw_path.strip().replace("\\", "/")
    candidate = Path(cleaned)
    if candidate.is_absolute():
        try:
            rel_from_wt = candidate.resolve().relative_to(wt_root).as_posix()
        except (OSError, ValueError):
            return None, None, f"Error: path '{raw_path}' escapes the worktree."
        cleaned = rel_from_wt or "."

    raw_parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    for part in raw_parts:
        if part == "..":
            return None, None, f"Error: path '{raw_path}' escapes the worktree."
        if ":" in part or part.endswith((".", " ")) or DOS_SHORT_RE.search(part):
            return None, None, f"Error: path component '{part}' uses disallowed stream/alias syntax."

    cur = wt_root
    for part in raw_parts:
        cur = cur / part
        if _is_link_or_reparse(cur):
            return None, None, f"Error: symlink or junction path '{raw_path}' is not permitted."
    try:
        if cur.exists() and not cur.is_dir():
            st = os.lstat(cur)
            if getattr(st, "st_nlink", 1) > 1:
                return None, None, f"Error: hard-linked file '{raw_path}' is not permitted."
        if not cur.resolve().is_relative_to(wt_root):
            return None, None, f"Error: path '{raw_path}' escapes the worktree."
    except (OSError, ValueError):
        return None, None, f"Error: invalid path '{raw_path}'."

    rel = "/".join(raw_parts)
    if rel == ".git" or rel.startswith(".git/") or any(p.lower() == ".git" for p in raw_parts):
        return None, None, "Error: accessing .git is forbidden."
    if rel and any(glob_match(rel, g) for g in PROTECTED_GLOBS):
        return None, None, f"Error: '{rel}' is a protected path."
    if for_write and rel and allowed and not any(glob_match(rel, g) for g in allowed):
        return None, None, (f"Error: '{rel}' is outside the brief's Allowed paths "
                            f"({', '.join(allowed)}).")
    return cur, rel, None


def _ollama_tools(has_test_cmd: bool) -> list[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files inside a directory of the worktree.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string", "description": "Relative directory path (default '.')"}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read lines from a file in the worktree (with line numbers).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the file"},
                        "start_line": {"type": "integer", "description": "1-based start line (default 1)"},
                        "end_line": {"type": "integer", "description": "1-based end line (default 300)"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or overwrite a file in the worktree with full contents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to write"},
                        "content": {"type": "string", "description": "Complete file content"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace an exact unique block of text in an existing file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to modify"},
                        "old_text": {"type": "string", "description": "Exact existing text to replace"},
                        "new_text": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
    ]
    if has_test_cmd:
        tools.append({
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "Run the task's configured test command inside the worktree.",
                "parameters": {"type": "object", "properties": {}},
            },
        })
    return tools


TOOL_CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}
SKIP_WALK_DIRS = {".git", "node_modules", ".venv", "venv", ".council", ".claude", ".agents",
                  ".agent", ".codex", ".gemini", ".github", ".canto", *TOOL_CACHE_DIRS}


def _split_test_argv(cmd: str) -> list[str]:
    import shlex
    parts = shlex.split(cmd, posix=(os.name != "nt"))
    if os.name == "nt":
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"') else p
                 for p in parts]
    if not parts:
        raise ValueError("Empty test command.")
    return parts


def _run_test_process(test_cmd: str, wt: Path, timeout_s: float) -> tuple[int, str, bool]:
    """Run `test_cmd` without a shell, killing its entire process tree on timeout."""
    import signal
    argv = _split_test_argv(test_cmd)
    env = scrubbed_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, cwd=str(wt), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            env=env, **kwargs)
    try:
        out, err = proc.communicate(timeout=max(1.0, timeout_s))
        combined = (out + ("\n" + err if err else "")).strip()
        return proc.returncode or 0, combined, False
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=2)
        return -1, "", True


def _write_utf8_atomic(target: Path, text: str, *, preserve_mode: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if preserve_mode and os.name != "nt":
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            tmp.chmod(mode)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, target)


def _exec_ollama_tool(name: str, raw_args, wt: Path, allowed: list[str],
                      test_cmd: str | None, test_timeout: float) -> str:
    try:
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                return f"Error: invalid JSON tool arguments: {raw_args}"
            if not isinstance(parsed, dict):
                return "Error: tool arguments JSON must be an object."
            args = parsed
        elif isinstance(raw_args, dict):
            args = raw_args
        elif raw_args is None:
            args = {}
        else:
            return "Error: tool arguments must be a JSON object."

        if name == "list_files":
            sub = str(args.get("directory") or ".")
            target, _, err = _safe_worktree_target(wt, sub, allowed, for_write=False)
            if err or target is None:
                return err or "Error: invalid directory."
            if not target.is_dir():
                return f"Error: '{sub}' is not a directory."
            entries = []
            wt_root = wt.resolve()
            for root, dirs, files in os.walk(target, followlinks=False):
                kept_dirs = []
                for d in sorted(dirs):
                    if d.lower() in {s.lower() for s in SKIP_WALK_DIRS}:
                        continue
                    d_path = Path(root) / d
                    if _is_link_or_reparse(d_path):
                        continue
                    kept_dirs.append(d)
                dirs[:] = kept_dirs
                for fname in sorted(files):
                    fpath = Path(root) / fname
                    if _is_link_or_reparse(fpath):
                        continue
                    rel = fpath.resolve().relative_to(wt_root).as_posix()
                    if rel == ".git" or any(glob_match(rel, g) for g in PROTECTED_GLOBS):
                        continue
                    entries.append(rel)
                    if len(entries) >= 200:
                        return "\n".join(entries) + "\n... (truncated at 200 files)"
            return "\n".join(entries) if entries else "(empty directory)"

        if name == "read_file":
            target, rel, err = _safe_worktree_target(wt, str(args.get("path") or ""), allowed, for_write=False)
            if err or target is None or not rel:
                return err or "Error: file path is required."
            if not target.is_file():
                return f"Error: file '{rel}' does not exist."
            try:
                raw_bytes = target.read_bytes()
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return f"Error: '{rel}' is binary or not valid UTF-8."
            except OSError as exc:
                return f"Error reading '{rel}': {exc}"
            lines = text.splitlines()
            try:
                start_ln = max(1, int(args.get("start_line") or 1))
                end_ln = min(len(lines), max(start_ln, int(args.get("end_line") or (start_ln + 299))))
            except (TypeError, ValueError):
                return "Error: 'start_line' and 'end_line' must be integers."
            if not lines:
                return f"File: {rel} (empty file)"
            numbered = [f"{idx}: {lines[idx - 1]}" for idx in range(start_ln, end_ln + 1)]
            header = f"File: {rel} (lines {start_ln}-{end_ln} of {len(lines)})"
            return header + "\n" + "\n".join(numbered)

        if name == "write_file":
            target, rel, err = _safe_worktree_target(wt, str(args.get("path") or ""), allowed, for_write=True)
            if err or target is None or not rel:
                return err or "Error: valid file path is required."
            content = args.get("content")
            if not isinstance(content, str):
                return "Error: 'content' must be a string."
            if target.is_file():
                try:
                    existing = target.read_bytes().decode("utf-8")
                    if "\r\n" in existing and "\r\n" not in content:
                        content = content.replace("\n", "\r\n")
                except (OSError, UnicodeDecodeError):
                    pass
            _write_utf8_atomic(target, content)
            return f"Wrote {len(content.splitlines())} lines to {rel}."

        if name == "edit_file":
            target, rel, err = _safe_worktree_target(wt, str(args.get("path") or ""), allowed, for_write=True)
            if err or target is None or not rel:
                return err or "Error: valid file path is required."
            if not target.is_file():
                return f"Error: file '{rel}' does not exist (use write_file to create it)."
            old_text = args.get("old_text")
            new_text = args.get("new_text")
            if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
                return "Error: 'old_text' (non-empty) and 'new_text' strings are required."
            try:
                raw_current = target.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                return f"Error: '{rel}' is binary or not valid UTF-8."
            eol = "\r\n" if "\r\n" in raw_current else "\n"
            norm_current = raw_current.replace("\r\n", "\n")
            norm_old = old_text.replace("\r\n", "\n")
            norm_new = new_text.replace("\r\n", "\n")
            count = norm_current.count(norm_old)
            if count == 0:
                return f"Error: 'old_text' was not found in {rel}. Call read_file to inspect exact lines."
            if count > 1:
                return f"Error: 'old_text' matched {count} times in {rel}; include more surrounding context."
            updated = norm_current.replace(norm_old, norm_new, 1)
            if eol == "\r\n":
                updated = updated.replace("\n", "\r\n")
            _write_utf8_atomic(target, updated)
            return f"Edited {rel} successfully."

        if name == "run_tests":
            if not test_cmd:
                return "Error: no test command is configured for this task."
            code, combined, timed_out = _run_test_process(test_cmd, wt, test_timeout)
            if timed_out:
                return f"Tests timed out after {int(test_timeout)}s."
            if len(combined) > 6000:
                combined = "...(truncated)...\n" + combined[-6000:]
            return f"Exit code: {code}\n{combined}"

        return f"Error: unknown tool '{name}'."
    except Exception as exc:
        return f"Error executing {name}: {exc}"


KNOWN_OLLAMA_TOOLS = {"list_files", "read_file", "write_file", "edit_file", "run_tests"}


def _extract_inline_tool_calls(content: str) -> list[dict]:
    """Fallback when a local model emits a JSON tool call inside message.content."""
    if not content:
        return []
    candidates = JSON_BLOCK.findall(content)
    if not candidates and content.lstrip().startswith("{") and content.rstrip().endswith("}"):
        candidates = [content.strip()]
    calls = []
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
            name = fn.get("name")
            if isinstance(name, str) and name in KNOWN_OLLAMA_TOOLS:
                calls.append({"function": {"name": name, "arguments": fn.get("arguments", fn.get("parameters", {}))}})
    return calls


def run_ollama_task(brief_text: str, wt: Path, allowed: list[str], cfg: dict,
                    timeout: int, desired_model: str | None = None) -> RunResult:
    base_url = cfg.get("base_url") or "http://127.0.0.1:11434"
    result = RunResult(member="ollama", ok=False)
    start = time.time()
    ok_conn, installed, warm, err_msg = ollama_status(base_url)
    if not ok_conn:
        result.notes.append(err_msg)
        result.seconds = round(time.time() - start, 1)
        return result
    try:
        model, fallback_note = select_ollama_model(desired_model, installed, warm)
    except MemberUnavailable as exc:
        result.notes.append(str(exc))
        result.seconds = round(time.time() - start, 1)
        return result
    result.model_used = model
    if fallback_note:
        result.notes.append(fallback_note)

    test_cmd = parse_test_command(brief_text)
    tools = _ollama_tools(has_test_cmd=bool(test_cmd))
    max_steps = int(cfg.get("max_steps") or 25)
    test_timeout = float(cfg.get("test_timeout_seconds") or 120)
    num_ctx = int(cfg.get("num_ctx") or 16384)
    keep_alive = cfg.get("keep_alive", "30m")

    user_msg = brief_text.strip()
    if allowed:
        user_msg += "\n\nAllowed paths: " + ", ".join(allowed)
    if test_cmd:
        user_msg += f"\nConfigured test command (call `run_tests` to run): `{test_cmd}`"
    messages: list[dict] = [
        {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    made_tool_call = False
    nudges = 0
    try:
        with _ollama_gpu_lock(timeout=timeout):
            for _ in range(max_steps):
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    result.timed_out = True
                    result.notes.append(f"Timed out after {timeout}s.")
                    break
                payload = {
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "stream": False,
                    "keep_alive": keep_alive,
                    "options": {"num_ctx": num_ctx, "temperature": 0.1},
                }
                resp = _ollama_http(base_url, "/api/chat", payload=payload, timeout=remaining)
                msg = resp.get("message") or {}
                content = (msg.get("content") or "").strip()
                tool_calls = msg.get("tool_calls") or _extract_inline_tool_calls(content)
                messages.append(msg)
                if not tool_calls:
                    if not content and not made_tool_call:
                        result.notes.append("Ollama returned an empty response without making edits.")
                        result.exit_code = 1
                        break
                    if "## changed" not in content.lower() and nudges < 2:
                        nudges += 1
                        messages.append({
                            "role": "user",
                            "content": (
                                "You did not emit a tool call or final report. If more edits or `run_tests` "
                                "are needed, emit the tool call now; if everything is complete and tested, "
                                "reply with `## Changed`, `## Tested`, and `## Unsure`."
                            ),
                        })
                        continue
                    result.text = content or "(completed without summary)"
                    result.exit_code = 0
                    result.ok = True
                    break
                made_tool_call = True
                for call in tool_calls:
                    fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                    t_name = str(fn.get("name") or "")
                    t_args = fn.get("arguments")
                    step_rem = max(1.0, min(test_timeout, timeout - (time.time() - start)))
                    out = _exec_ollama_tool(t_name, t_args, wt, allowed, test_cmd, step_rem)
                    messages.append({"role": "tool", "name": t_name, "content": out})
            else:
                result.notes.append(f"Reached max_steps ({max_steps}) before finishing.")
                result.text = "(stopped at max_steps)"
                result.exit_code = 1
    except TimeoutError as exc:
        result.timed_out = True
        result.notes.append(str(exc))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
            result.timed_out = True
            result.notes.append(f"Timed out after {timeout}s.")
        else:
            result.stderr = str(exc)
            result.notes.append(f"Ollama chat failed: {exc}")
    result.seconds = round(time.time() - start, 1)
    return result


# ---------------------------------------------------------------------------
# Verdicts & Instructions
# ---------------------------------------------------------------------------
VERDICTS = ("pass", "revise", "reject")
SEVERITIES = ("blocker", "major", "minor", "nit")

REVIEW_INSTRUCTIONS = """
---
END YOUR REPLY with a single fenced ```json block, and nothing after it, matching:
{
  "verdict": "pass" | "revise" | "reject",
  "confidence": 0.0-1.0,
  "summary": "one or two sentences",
  "findings": [
    {"severity": "blocker"|"major"|"minor"|"nit",
     "file": "path or null", "line": number or null,
     "issue": "what is wrong", "fix": "concrete suggested fix"}
  ]
}
Use "reject" only if the approach itself is wrong. Use "revise" if any blocker or
major finding exists. Use "pass" otherwise. Do not invent findings to seem thorough.
"""

CONSULT_INSTRUCTIONS = """
---
You are being consulted BEFORE any work starts. Read the code you need to; do not edit
anything. Answer with these headings:
## Approach - the plan you would follow, as numbered steps
## Files - the files you would read or change, and why
## Risks - what could go wrong or is easy to miss
## Alternatives - other approaches worth considering and why you did not pick them
## Open questions - what you would ask the person before starting
## Difficulty - easy / medium / hard, and which parts fit a local worker vs a frontier agent
Be concrete and point at real code. Disagree with the brief if it is heading the wrong way.
"""

WRITE_INSTRUCTIONS = """
---
WRITE MODE. You are working in a git worktree made for this task: {worktree}
Rules:
- Edit files only inside that folder. Writes anywhere else are blocked.
- Do NOT run git add, commit, push, merge, checkout or branch. The chairman reviews and
  commits your work.
- Do not touch these protected paths: {protected}
- Stay within the allowed paths if the brief lists any.
- {shell_note}
When you are done, reply with a report using these headings:
## Changed - each file and what you changed
## Tested - what you ran or checked, and the result (or "not run")
## Unsure - anything you are not confident about or left undone
"""

SHELL_NOTE = {
    "codex": "You may run commands (tests, linters) inside the worktree; network is off.",
    "gemini": "Do not run any shell commands: they are denied in this mode and end your run. "
              "Use your file tools only; the chairman will run the tests.",
    "claude": "Do not run any shell commands: Bash/PowerShell are disabled in restricted mode. "
              "Use your file tools only; the chairman will run the tests.",
}

PROTECTED_GLOBS = (
    ".git", ".git/**", "**/.git", ".github/**", ".claude/**", ".agents/**", ".agent/**",
    ".codex/**", ".gemini/**", ".council/**", ".canto/**",
    "**/council.config.json", "**/.gitignore", "**/.gitattributes", "**/.gitmodules",
    "**/CLAUDE.md", "**/AGENTS.md", "**/GEMINI.md", ".gitignore", "**/settings.json",
    "**/settings.local.json", "**/hooks.json", "**/package-lock.json", "**/yarn.lock",
    "**/pnpm-lock.yaml", "**/uv.lock", "**/poetry.lock", "**/Cargo.lock", "**/.env", "**/.env.*",
)

JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_verdict(member: str, text: str) -> dict:
    blocks = JSON_BLOCK.findall(text)
    candidate = blocks[-1] if blocks else None
    if candidate is None:
        start, end = text.rfind("{"), text.rfind("}")
        candidate = text[start:end + 1] if 0 <= start < end else None
    try:
        data = json.loads(candidate) if candidate else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return {"member": member, "verdict": "unparseable", "confidence": 0.0,
                "summary": "Reviewer did not return a valid verdict block.", "findings": []}
    findings = []
    for f in data.get("findings") or []:
        if isinstance(f, dict):
            sev = f.get("severity") if f.get("severity") in SEVERITIES else "minor"
            findings.append({"severity": sev, "file": f.get("file"), "line": f.get("line"),
                             "issue": str(f.get("issue", "")), "fix": str(f.get("fix", ""))})
    verdict = data["verdict"]
    if verdict == "pass" and any(f["severity"] in ("blocker", "major") for f in findings):
        verdict = "revise"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    return {"member": member, "verdict": verdict, "confidence": confidence,
            "summary": str(data.get("summary", "")), "findings": findings}


def combine(verdicts: list[dict]) -> str:
    """Any reject -> reject; only a nonempty list of exact passes -> pass."""
    kinds = [v.get("verdict") for v in verdicts]
    if "reject" in kinds:
        return "reject"
    return "pass" if kinds and all(k == "pass" for k in kinds) else "revise"


# ---------------------------------------------------------------------------
# Diff stamp — keep identical to policy_gate.py
# ---------------------------------------------------------------------------
def _fingerprint_git(repo: Path, *args) -> str:
    argv = ["git", "-C", str(repo), *args]
    done = subprocess.run(argv, text=True, encoding="utf-8", errors="replace",
                          capture_output=True, check=False, timeout=20)
    if done.returncode:
        raise subprocess.CalledProcessError(done.returncode, argv, done.stdout, done.stderr)
    return done.stdout


def _fingerprint_head(repo: Path) -> str:
    try:
        return _fingerprint_git(repo, "rev-parse", "--verify", "HEAD").strip()
    except subprocess.CalledProcessError as exc:
        ref = _fingerprint_git(repo, "symbolic-ref", "--quiet", "HEAD").strip()
        if not ref.startswith("refs/heads/"):
            raise exc
        try:
            _fingerprint_git(repo, "show-ref", "--verify", "--quiet", ref)
        except subprocess.CalledProcessError as missing:
            if missing.returncode == 1:
                return "unborn HEAD"
            raise
        raise exc


def _staged_fingerprint(head: str, staged: str) -> str:
    return hashlib.sha256((head + "\0" + staged).encode("utf-8")).hexdigest()


def staged_fingerprint(repo: Path) -> str:
    head = _fingerprint_head(repo)
    staged = _fingerprint_git(repo, "diff", "--cached", "--binary")
    return _staged_fingerprint(head, staged)


def _diff_fingerprints(repo: Path) -> tuple[str, str]:
    """Working and staged fingerprints derived from one set of git reads."""
    head = _fingerprint_head(repo)
    staged = _fingerprint_git(repo, "diff", "--cached", "--binary")
    unstaged = _fingerprint_git(repo, "diff", "--binary")
    working = hashlib.sha256((head + "\0" + staged + "\0" + unstaged).encode("utf-8")).hexdigest()
    return working, _staged_fingerprint(head, staged)


def diff_fingerprint(repo: Path) -> str:
    return _diff_fingerprints(repo)[0]


# ---------------------------------------------------------------------------
# CLI commands: check, consult/ask/review, gate, task, cleanup, install
# ---------------------------------------------------------------------------
def cmd_check(args, config):
    clear_target = getattr(args, "clear_cooldown", None)
    if clear_target:
        clear_cooldown(clear_target)
    try:
        chairman = resolve_chairman(getattr(args, "chairman", None))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    state = load_state()
    now = time.time()
    member_rows = []
    for name, cfg in config["members"].items():
        usage = usage_summary(name, state, now)
        if not cfg.get("enabled"):
            member_rows.append({"name": name, "role_kind": "chairman" if name == chairman else "member",
                                "status": "disabled", "detail": "", "usage": usage})
            continue
        role_kind = "chairman" if name == chairman else "voting-member"
        adapter = ADAPTERS[name](cfg)
        try:
            exe = adapter.executable()
            auth = adapter.check_auth()
            status = "cooldown" if usage["cooldown_remaining_s"] > 0 else "ready"
            detail = f"{exe} — {auth}"
            if usage["cooldown_remaining_s"] > 0:
                detail += f" (cooldown {usage['cooldown_remaining_s']}s)"
            member_rows.append({"name": name, "role_kind": role_kind, "status": status,
                                "detail": detail, "usage": usage})
        except MemberUnavailable as exc:
            member_rows.append({"name": name, "role_kind": role_kind, "status": "NOT READY",
                                "detail": str(exc), "usage": usage})

    delegate_rows = []
    ollama_cfg = (config.get("delegates") or {}).get("ollama") or {}
    if ollama_cfg:
        usage = usage_summary("ollama", state, now)
        if not ollama_cfg.get("enabled"):
            delegate_rows.append({"name": "ollama", "role_kind": "delegate", "status": "disabled",
                                  "detail": "", "installed_models": [], "warm_models": [], "usage": usage})
        else:
            base_url = ollama_cfg.get("base_url") or "http://127.0.0.1:11434"
            ok_conn, installed, warm, err_msg = ollama_status(base_url)
            if not ok_conn:
                delegate_rows.append({"name": "ollama", "role_kind": "delegate", "status": "OFFLINE",
                                      "detail": err_msg, "installed_models": [], "warm_models": [],
                                      "usage": usage})
            elif not installed:
                delegate_rows.append({"name": "ollama", "role_kind": "delegate", "status": "NO MODELS",
                                      "detail": f"{base_url} reachable, 0 models pulled",
                                      "installed_models": [], "warm_models": [], "usage": usage})
            else:
                std_model, _ = resolve_tier(ollama_cfg, "standard")
                chosen, note = select_ollama_model(std_model, installed, warm)
                warm_str = f", warm={warm}" if warm else ""
                fallback_str = f" ({note})" if note else ""
                detail = f"{base_url} — active={chosen}, installed={installed}{warm_str}{fallback_str}"
                delegate_rows.append({"name": "ollama", "role_kind": "delegate", "status": "ready",
                                      "detail": detail, "active_model": chosen,
                                      "installed_models": installed, "warm_models": warm, "usage": usage})

    pywinpty_ok = True
    if os.name == "nt":
        try:
            from winpty import PtyProcess  # noqa: F401  # type: ignore
        except ImportError:
            pywinpty_ok = False

    voting_peers = [r["name"] for r in member_rows
                    if r["role_kind"] == "voting-member" and r["status"] in ("ready", "cooldown")]

    if getattr(args, "json", False):
        payload = {
            "chairman": chairman,
            "voting_members": voting_peers,
            "members": member_rows,
            "delegates": delegate_rows,
            "pywinpty": pywinpty_ok if os.name == "nt" else None,
        }
        print(json.dumps(payload, indent=2))
    else:
        if chairman:
            print(f"chairman  {chairman} (voting council: {', '.join(voting_peers) or 'none'})")
        for r in member_rows:
            u = r["usage"]
            tag = " [chairman]" if r["role_kind"] == "chairman" else ""
            stats = f" [5h:{u['runs_5h']} 24h:{u['runs_24h']}]" if r["status"] != "disabled" else ""
            print(f"{r['name']:8} {r['status']:10} {r['detail']}{stats}{tag}")
        for r in delegate_rows:
            u = r["usage"]
            stats = f" [5h:{u['runs_5h']} 24h:{u['runs_24h']}]" if r["status"] != "disabled" else ""
            print(f"{r['name']:8} {r['status']:10} [delegate] {r['detail']}{stats}")
        if os.name == "nt":
            print("pywinpty  installed" if pywinpty_ok else
                  "pywinpty  MISSING  (pip install pywinpty; agy may print nothing without it)")

    ready_peers = [r for r in member_rows if r["role_kind"] == "voting-member" and r["status"] == "ready"]
    if chairman is None:
        if len(ready_peers) < 2 and not getattr(args, "json", False):
            print("Council needs two ready CLIs. Use --chairman to check a specific host.")
        return 0 if len(ready_peers) >= 2 else 1
    chair_ready = any(r["role_kind"] == "chairman" and r["status"] in ("ready", "cooldown") for r in member_rows)
    if not getattr(args, "json", False):
        if not chair_ready:
            print(f"Chairman {chairman} is unavailable.")
        if not ready_peers:
            print("No voting peer is ready.")
    return 0 if chair_ready and ready_peers else 1


def cmd_run(args, config, kind: str):
    review = kind == "review"
    brief = Path(args.brief).resolve()
    out_dir = brief.parent
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    try:
        chairman = resolve_chairman(getattr(args, "chairman", None))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if chairman and args.member == chairman:
        print(f"Member {args.member} is the active chairman; consult/review the other council members.",
              file=sys.stderr)
        return 2
    if args.member in (config.get("delegates") or {}):
        print(f"'{args.member}' is a non-voting delegate (worker only); it cannot {kind}.",
              file=sys.stderr)
        return 2
    cfg = config["members"].get(args.member)
    if cfg is None or args.member not in ADAPTERS:
        print(f"Unknown member: {args.member}", file=sys.stderr)
        return 2
    if not cfg.get("enabled"):
        print(f"Member {args.member} is disabled in council.config.json.", file=sys.stderr)
        return 2

    tier = getattr(args, "tier", None)
    model, effort = resolve_tier(cfg, tier=tier, model=getattr(args, "model", None),
                                 effort=getattr(args, "effort", None))
    input_fingerprint = diff_fingerprint(workspace) if review else None
    role = cfg.get("role", "")
    prompt = f"You are a council member. Your role: {role}\n\n" + brief.read_text(encoding="utf-8")
    if review:
        diff_file = out_dir / f"{args.member}.staged.diff"
        diff_file.write_text(git(workspace, "diff", "--cached", "--binary"), encoding="utf-8")
        prompt += (f"\n\nThe staged diff under review is saved at {diff_file}. Read it there; "
                   "you can also read any file in the repository for context.\n")
        prompt += "\n" + REVIEW_INSTRUCTIONS
    elif kind == "consult":
        prompt += "\n" + CONSULT_INSTRUCTIONS
    prompt_file = out_dir / f"{args.member}.prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    result = run_member(args.member, prompt_file, workspace, out_dir, cfg,
                        args.timeout or config["timeout_seconds"],
                        model=model, effort=effort)
    record_usage(args.member, kind, result, tier=tier, model=result.model_used,
                 cooldown_seconds=int(config.get("quota_cooldown_seconds", 3600)))

    answer_path = out_dir / f"{args.member}.md"
    answer_path.write_text(result.text or "(no answer)\n" + "\n".join(result.notes), encoding="utf-8")
    meta = {k: v for k, v in result.__dict__.items() if k != "text"}
    meta["stderr"] = meta["stderr"][-2000:]
    if review:
        verdict = parse_verdict(args.member, result.text) if result.ok else {
            "member": args.member, "verdict": "unavailable", "confidence": 0.0,
            "summary": "; ".join(result.notes) or "Member failed.", "findings": []}
        verdict["fingerprint"] = input_fingerprint
        if diff_fingerprint(workspace) != input_fingerprint:
            result.notes.append("The change moved during the review; this verdict will be stale.")
        (out_dir / f"{args.member}.verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        meta["verdict"] = verdict["verdict"]
    (out_dir / f"{args.member}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    status = "ok" if result.ok else ("QUOTA" if result.quota_hit else
                                     "AUTH" if result.auth_problem else "FAILED")
    line = f"{args.member}: {status} in {result.seconds}s -> {answer_path}"
    if review:
        line += f" | verdict={meta['verdict']}"
    if result.notes:
        line += " | " + " ".join(result.notes)
    print(line)
    return 0 if result.ok else 1


def _infer_task_authors(out_dir: Path, workspace: Path | None) -> set[str]:
    records = []
    for p in sorted(out_dir.glob("*.task.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("member"), str):
            records.append(rec)
    if not records:
        return set()
    if workspace is not None:
        ws_norm = os.path.normcase(str(workspace.resolve()))
        matched = {r["member"] for r in records
                   if isinstance(r.get("worktree"), str)
                   and os.path.normcase(str(Path(r["worktree"]).resolve())) == ws_norm}
        if matched:
            return matched
    # If reviewing the merged branch in the main checkout (or no workspace given),
    # exclude every cloud member that authored a subtask in this run directory.
    return {r["member"] for r in records}


def _commit_gate_enforced(repo: Path) -> bool:
    if (repo / ".council" / "enforce-commit-gate").is_file():
        return True
    main = main_checkout(repo)
    return bool(main and (main / ".council" / "enforce-commit-gate").is_file())


def cmd_gate(args, config):
    out_dir = Path(args.dir).resolve()
    ws_arg = Path(args.workspace).resolve() if getattr(args, "workspace", None) else None
    repo = repo_root(ws_arg if ws_arg else Path.cwd())
    try:
        chairman = resolve_chairman(getattr(args, "chairman", None))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    raw_author = getattr(args, "author", None)
    authors = ({a.strip() for a in raw_author.split(",") if a.strip()} if raw_author else
               (_infer_task_authors(out_dir, ws_arg) if chairman is not None else set()))
    excluded = ({chairman} if chairman else set()) | (authors & set(config["members"]))
    eligible = [n for n, c in config["members"].items() if c.get("enabled") and n not in excluded]

    req_arg = getattr(args, "require", None)
    ineligible_requested: list[str] = []
    if req_arg:
        requested = [m.strip() for m in req_arg.split(",") if m.strip()]
        ineligible_requested = [m for m in requested if m in excluded]
        required = requested
    else:
        required = list(eligible)

    verdicts = []
    for path in sorted(out_dir.glob("*.verdict.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"), parse_constant=int)
        except (OSError, ValueError, RecursionError):
            data = None
        member = data.get("member") if isinstance(data, dict) else None
        known_member = isinstance(member, str) and member in config["members"]
        if not known_member:
            member = path.name.removesuffix(".verdict.json")
            if member not in config["members"]:
                continue
        if member in excluded:
            continue  # excluded chairman or task author can never satisfy the gate
        if (not known_member or data.get("verdict") not in (*VERDICTS, "unparseable", "unavailable")
                or not isinstance(data.get("fingerprint"), str)
                or not isinstance(data.get("findings", []), list)
                or any(not isinstance(f, dict) for f in data.get("findings", []))):
            data = {"member": member, "verdict": "unparseable", "findings": []}
        verdicts.append(data)
    if req_arg or excluded:
        verdicts = [v for v in verdicts if v["member"] in required]
    by_member = {}
    for verdict in verdicts:
        by_member.setdefault(verdict["member"], []).append(verdict)
    missing = [m for m in required if m not in by_member or m in ineligible_requested]
    stamp = repo / ".council" / "approved.json"
    try:
        current, staged = _diff_fingerprints(repo)
    except BaseException:
        if stamp.is_file():
            stamp.unlink()
        raise
    stale = [v["member"] for v in verdicts if v.get("fingerprint") != current]
    decision = combine(verdicts)
    if stale or missing or ineligible_requested:
        decision = "revise" if decision != "reject" else decision
    # When commit-gate enforcement is active, a reduced --require subset cannot mint
    # a commit approval stamp unless every eligible voting member passed.
    enforced = _commit_gate_enforced(repo)
    if decision == "pass" and enforced and (not eligible or any(m not in by_member for m in eligible)):
        decision = "revise"
        for m in eligible:
            if m not in missing and m not in by_member:
                missing.append(m)
    gate = {
        "decision": decision,
        "members": {m: vs[0]["verdict"] if len(vs) == 1 else combine(vs)
                    for m, vs in by_member.items()},
        "required": required,
        "missing_reviews": missing,
        "stale_reviews": stale,
        "staged_fingerprint": staged,
        "working_fingerprint": current,
        "findings": [dict(f, member=v["member"]) for v in verdicts for f in v.get("findings", [])],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (out_dir / "gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
    if decision == "pass":
        stamp.parent.mkdir(exist_ok=True)
        stamp.write_text(json.dumps({"staged_fingerprint": gate["staged_fingerprint"],
                                     "source": str(out_dir / "gate.json")}, indent=2), encoding="utf-8")
    elif stamp.is_file():
        stamp.unlink()
    counts = {s: sum(1 for f in gate["findings"] if f.get("severity") == s) for s in SEVERITIES}
    print(f"GATE: {decision.upper()} | " + ", ".join(f"{m}={v}" for m, v in gate["members"].items())
          + f" | findings: {counts}" + (f" | stale: {stale}" if stale else "")
          + (f" | missing: {missing}" if missing else ""))
    return 0 if decision == "pass" else 1


# ---------------------------------------------------------------------------
# Write mode: one worker (cloud member or local Ollama), one worktree
# ---------------------------------------------------------------------------
def git(repo: Path, *args, check: bool = False) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], text=True, encoding="utf-8",
                          errors="replace", capture_output=True, check=False)
    if check and done.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def git_ok(repo: Path, *args) -> tuple[bool, str]:
    done = subprocess.run(["git", "-C", str(repo), *args], text=True, encoding="utf-8",
                          errors="replace", capture_output=True, check=False)
    return done.returncode == 0, done.stdout


def repo_root(start: Path) -> Path:
    top = git(start, "rev-parse", "--show-toplevel").strip()
    if not top:
        raise RuntimeError(f"Not inside a git repository: {start}")
    return Path(top).resolve()


def main_checkout(workspace: Path) -> Path | None:
    """The main checkout when `workspace` is a linked worktree, else None."""
    ok, top = git_ok(workspace, "rev-parse", "--show-toplevel")
    ok2, common = git_ok(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not (ok and ok2):
        return None
    common_dir = Path(common.strip()).resolve()
    if common_dir.name != ".git":
        return None
    main = common_dir.parent
    return None if main == Path(top.strip()).resolve() else main


def clean_slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "task"


def run_slug(run_dir: Path) -> str:
    return clean_slug(run_dir.name)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(repo: Path) -> dict:
    """State of the main checkout that a confined worker must not be able to change."""
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    hashes = []
    for rel in filter(None, untracked):
        try:
            hashes.append(f"{rel}:{_sha((repo / rel).read_bytes())}")
        except OSError:
            hashes.append(f"{rel}:unreadable")
    ignored = []
    for rel in filter(None, git(repo, "ls-files", "--others", "--ignored", "--exclude-standard",
                                "--directory", "-z").split("\0")):
        if rel.replace("\\", "/").startswith(".council"):
            continue
        try:
            st = (repo / rel).stat()
            ignored.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            ignored.append(f"{rel}:gone")
    gitdir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    meta = []
    for f in [gitdir / "config", *sorted((gitdir / "hooks").glob("*"))]:
        try:
            meta.append(f"{f.name}:{_sha(f.read_bytes())}")
        except OSError:
            pass
    # Hash council governance files inside .council/ so a local test process can't forge approvals.
    gov = []
    council_dir = repo / ".council"
    if council_dir.is_dir():
        gov_files = [council_dir / "approved.json", council_dir / "enforce-commit-gate"]
        runs_dir = council_dir / "runs"
        if runs_dir.is_dir():
            gov_files.extend(sorted(runs_dir.rglob("gate.json")))
            gov_files.extend(sorted(runs_dir.rglob("*.verdict.json")))
        for gf in gov_files:
            if gf.exists():
                try:
                    gov.append(f"{gf.relative_to(repo).as_posix()}:{_sha(gf.read_bytes())}")
                except OSError:
                    gov.append(f"{gf.name}:unreadable")
    refs = dict(r.split(" ", 1) for r in
                git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines() if " " in r)
    return {
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "index": _sha(git(repo, "diff", "--cached", "--binary").encode("utf-8", "replace")),
        "worktree": _sha(git(repo, "diff", "--binary").encode("utf-8", "replace")),
        "untracked": _sha("\n".join(sorted(hashes)).encode("utf-8")),
        "ignored": _sha("\n".join(sorted(ignored)).encode("utf-8")),
        "git_config_hooks": _sha("\n".join(meta).encode("utf-8")),
        "council_governance": _sha("\n".join(sorted(gov)).encode("utf-8")),
        "refs": refs,
        "stash": git(repo, "stash", "list"),
    }


def glob_match(path: str, pattern: str) -> bool:
    path = path.replace("\\", "/")
    regex, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            regex, i = regex + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            regex, i = regex + ".*", i + 2
        elif c == "*":
            regex, i = regex + "[^/]*", i + 1
        elif c == "?":
            regex, i = regex + "[^/]", i + 1
        else:
            regex, i = regex + re.escape(c), i + 1
    flags = re.I if os.name == "nt" else 0
    return re.fullmatch(regex, path, flags) is not None


def allowed_paths(brief_text: str) -> list[str]:
    """Bullets under an '## Allowed paths' heading in the brief. Empty = anywhere not protected."""
    m = re.search(r"^#+\s*Allowed paths\s*$(.*?)(?=^#|\Z)", brief_text, re.M | re.S | re.I)
    if not m:
        return []
    return [g.strip().strip("`") for g in re.findall(r"^\s*[-*]\s+(.+)$", m.group(1), re.M)]


def _is_tool_cache_path(rel_path: str) -> bool:
    parts = {p.lower() for p in rel_path.replace("\\", "/").split("/") if p}
    return bool(parts & {d.lower() for d in TOOL_CACHE_DIRS})


def _prune_untracked_tool_caches(wt: Path) -> None:
    untracked = git(wt, "ls-files", "--others", "-z").split("\0")
    for rel in filter(None, untracked):
        if _is_tool_cache_path(rel):
            p = wt / rel
            with contextlib.suppress(OSError):
                if p.is_file() or p.is_symlink():
                    p.unlink()
    for root, dirs, _ in os.walk(wt, topdown=False):
        if ".git" in Path(root).parts:
            continue
        for d in dirs:
            if d.lower() in {c.lower() for c in TOOL_CACHE_DIRS}:
                dp = Path(root) / d
                with contextlib.suppress(OSError):
                    shutil.rmtree(dp)


def worktree_changes(wt: Path) -> tuple[list[str], int]:
    _prune_untracked_tool_caches(wt)
    git(wt, "add", "-A", check=True)
    files = [f for f in git(wt, "diff", "--cached", "--no-renames", "--name-only", "-z").split("\0") if f]
    lines = 0
    for row in git(wt, "diff", "--cached", "--no-renames", "--numstat").splitlines():
        added, deleted = row.split("\t")[:2]
        lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    return files, lines


def ignored_files(wt: Path) -> list[str]:
    out = git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    return [f for f in out.split("\0") if f and not _is_tool_cache_path(f)]


def under(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def _task_actor_cfg(config: dict, member: str) -> tuple[dict | None, bool]:
    """Return (cfg, is_delegate)."""
    if member in (config.get("delegates") or {}):
        return config["delegates"][member], True
    return (config.get("members") or {}).get(member), False


def cmd_task(args, config):
    if args.member not in WRITE_MEMBERS:
        print(f"Write mode supports {', '.join(WRITE_MEMBERS)}, not {args.member}.", file=sys.stderr)
        return 2
    cfg, is_delegate = _task_actor_cfg(config, args.member)
    if not cfg or not cfg.get("enabled"):
        print(f"Worker {args.member} is disabled in council.config.json.", file=sys.stderr)
        return 2
    brief = Path(args.brief).resolve()
    run_dir = brief.parent
    repo = repo_root(Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd())
    slug = run_slug(run_dir)
    raw_task_id = getattr(args, "task_id", None)
    task_id = clean_slug(raw_task_id) if raw_task_id else None
    unit = f"{task_id}-{args.member}" if task_id else args.member
    branch = f"council/{slug}-{unit}"
    wt = repo / ".council" / "worktrees" / f"{slug}-{unit}"
    temps = [os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR")]
    if any(t and under(wt, Path(t)) for t in temps):
        print("Refusing to run write mode inside a temp folder: the sandbox can't confine it.",
              file=sys.stderr)
        return 2
    if subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", ".council/x"],
                      capture_output=True).returncode != 0:
        print("Add `.council/` to .gitignore first, so worktrees and run files never get "
              "committed to the main checkout.", file=sys.stderr)
        return 2
    exists = wt.exists() or bool(git(repo, "branch", "--list", branch).strip())
    if getattr(args, "resume", False):
        listing = git(repo, "worktree", "list", "--porcelain")
        entry = next((block for block in listing.split("\n\n")
                      if block.startswith("worktree ") and Path(block.splitlines()[0][9:]).resolve() == wt.resolve()), "")
        if f"branch refs/heads/{branch}" not in entry:
            print(f"--continue needs the registered worktree {wt} on {branch}.", file=sys.stderr)
            return 2
        base = git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()
    elif exists:
        print(f"{branch} or {wt} already exists. Pass --continue for another round, run "
              f"`council.py cleanup --dir {run_dir}`, or pass --task-id <step>.", file=sys.stderr)
        return 2
    else:
        base = getattr(args, "base", None) or git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        base_sha = git(repo, "rev-parse", base, check=True).strip()
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "-b", branch, str(wt), base_sha, check=True)
    start_sha = git(wt, "rev-parse", "HEAD").strip()
    gitfile = (wt / ".git").read_bytes() if (wt / ".git").is_file() else b""

    brief_text = brief.read_text(encoding="utf-8")
    allowed = allowed_paths(brief_text)
    tier = getattr(args, "tier", None)
    model, effort = resolve_tier(cfg, tier=tier, model=getattr(args, "model", None),
                                 effort=getattr(args, "effort", None))
    timeout = getattr(args, "timeout", None) or config["timeout_seconds"]

    ignored_before = set(ignored_files(wt))
    before = snapshot(repo)
    if is_delegate and args.member == "ollama":
        result = run_ollama_task(brief_text, wt, allowed, cfg, timeout=timeout, desired_model=model)
    else:
        prompt = (f"You are a council member doing a task. Your role: {cfg.get('role', '')}\n\n"
                  + brief_text + "\n" + WRITE_INSTRUCTIONS.format(
                      worktree=wt, protected=", ".join(PROTECTED_GLOBS),
                      shell_note=SHELL_NOTE[args.member]))
        prompt_file = run_dir / f"{unit}.task-prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        result = run_member(args.member, prompt_file, wt, run_dir, cfg, timeout,
                            write=True, model=model, effort=effort, unit=unit)
    after = snapshot(repo)
    record_usage(args.member, "task", result, tier=tier, model=result.model_used,
                 cooldown_seconds=int(config.get("quota_cooldown_seconds", 3600)))

    violations = [f"main checkout {key} changed during the run"
                  for key in ("head", "branch", "index", "worktree", "untracked", "ignored",
                              "git_config_hooks", "council_governance", "stash")
                  if before[key] != after[key]]
    for ref in set(before["refs"]) | set(after["refs"]):
        if before["refs"].get(ref) != after["refs"].get(ref) and not (
                ref not in before["refs"] and ref.startswith("refs/heads/council/")):
            violations.append(f"ref {ref} changed during the run")
    now_gitfile = (wt / ".git").read_bytes() if (wt / ".git").is_file() else b"missing"
    files, lines, new_ignored = [], 0, []
    if now_gitfile != gitfile:
        violations.append("the worktree's .git file was changed")
    else:
        if git(wt, "rev-parse", "HEAD").strip() != start_sha:
            violations.append("the worktree has new commits (members must not commit)")
        files, lines = worktree_changes(wt)
        new_ignored = sorted(set(ignored_files(wt)) - ignored_before)
        for f in files:
            if any(glob_match(f, g) for g in PROTECTED_GLOBS):
                violations.append(f"protected path changed: {f}")
            elif allowed and not any(glob_match(f, g) for g in allowed):
                violations.append(f"outside allowed paths: {f}")
        for row in git(wt, "diff", "--cached", "--no-renames", "--raw", "-z").split("\0:"):
            mode = row.lstrip(":").split(" ")[1:2]
            if mode and mode[0] in ("120000", "160000"):
                violations.append("a symlink or submodule was staged: " + row.split("\0")[-1])
        for f in new_ignored:
            if any(glob_match(f, g) for g in PROTECTED_GLOBS):
                violations.append(f"protected ignored file created: {f}")

    if violations:
        status = "VIOLATION"
    elif result.ok:
        status = "DONE"
    elif result.denied:
        status = "DENIED"
    else:
        status = "QUOTA" if result.quota_hit else "AUTH" if result.auth_problem else "FAILED"
    report = run_dir / f"{unit}.md"
    report.write_text(result.text or "(no report)\n" + "\n".join(result.notes), encoding="utf-8")
    record = {
        "member": args.member, "task_id": task_id, "status": status, "branch": branch,
        "worktree": str(wt), "base": base, "start_sha": start_sha, "files": files,
        "lines_changed": lines, "new_ignored_files": new_ignored, "allowed_paths": allowed,
        "violations": violations,
        "staged_fingerprint": staged_fingerprint(wt) if now_gitfile == gitfile else None,
        "tier": tier, "model": result.model_used, "effort": result.effort_used,
        "seconds": result.seconds, "notes": result.notes, "stderr": result.stderr[-2000:],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (run_dir / f"{unit}.task.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    line = (f"{unit}: {status} in {result.seconds}s | {len(files)} files, {lines} lines "
            f"| worktree {wt} | branch {branch} | report {report}")
    if violations:
        line += " | VIOLATIONS: " + "; ".join(violations)
    if result.notes:
        line += " | " + " ".join(result.notes)
    print(line)
    return 0 if status == "DONE" else 1


def cmd_cleanup(args, config):
    """Remove worktrees and council/* branches a task run created."""
    run_dir = Path(args.dir).resolve()
    repo = repo_root(Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd())
    slug = run_slug(run_dir)
    root = repo / ".council" / "worktrees"
    raw_task_id = getattr(args, "task_id", None)
    task_id = clean_slug(raw_task_id) if raw_task_id else None
    member_arg = getattr(args, "member", None)

    units: list[str] = []
    if member_arg and task_id:
        units.append(f"{task_id}-{member_arg}")
    elif member_arg:
        units.append(member_arg)
        for p in sorted(run_dir.glob(f"*-{member_arg}.task.json")):
            u = p.name.removesuffix(".task.json")
            if u not in units:
                units.append(u)
    elif task_id:
        for m in WRITE_MEMBERS:
            units.append(f"{task_id}-{m}")
    else:
        units.extend(WRITE_MEMBERS)
        for p in sorted(run_dir.glob("*.task.json")):
            u = p.name.removesuffix(".task.json")
            if u not in units:
                units.append(u)

    for unit in units:
        wt = root / f"{slug}-{unit}"
        branch = f"council/{slug}-{unit}"
        if not under(wt, root):
            print(f"refusing: {wt} is not under {root}", file=sys.stderr)
            return 2
        if wt.exists():
            ok, status = git_ok(wt, "status", "--porcelain")
            head_ok, head = git_ok(wt, "rev-parse", "HEAD")
            tip_ok, tip = git_ok(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
            protected = tip_ok and tip.strip() == head.strip() or (
                head_ok and git_ok(repo, "merge-base", "--is-ancestor", head.strip(), "HEAD")[0])
            reason = ("git status failed" if not ok else
                      "it has uncommitted work" if status.strip() else
                      "it has commits no branch holds" if not protected else "")
            if reason and not args.discard:
                print(f"kept {wt}: {reason} (pass --discard to remove it anyway)")
                continue
            git(repo, "worktree", "remove", "--force", str(wt), check=True)
            print(f"removed worktree {wt}")
        if git(repo, "branch", "--list", branch).strip():
            merged = git_ok(repo, "merge-base", "--is-ancestor", branch, "HEAD")[0]
            if merged or args.discard:
                git(repo, "branch", "-D", branch, check=True)
                print(f"deleted branch {branch}" + ("" if merged else " (unmerged, discarded)"))
            else:
                print(f"kept {branch}: not merged into HEAD (pass --discard to delete it)")
    git(repo, "worktree", "prune")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Council member & local worker runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    chk = sub.add_parser("check")
    chk.add_argument("--chairman", help="active chairman CLI (claude, codex, gemini/agy, or auto)")
    chk.add_argument("--json", action="store_true", help="output machine-readable status & usage JSON")
    chk.add_argument("--clear-cooldown", help="clear quota cooldown for a member or 'all'")

    for name in ("ask", "consult", "review"):
        p = sub.add_parser(name)
        p.add_argument("--member", required=True)
        p.add_argument("--brief", required=True)
        p.add_argument("--workspace")
        p.add_argument("--chairman", help="active chairman CLI (prevents self-consulting)")
        p.add_argument("--tier", choices=TIERS, help="semantic model/effort preset")
        p.add_argument("--model", help="explicit model override")
        p.add_argument("--effort", help="explicit reasoning effort override")
        p.add_argument("--timeout", type=int)

    g = sub.add_parser("gate")
    g.add_argument("--dir", required=True)
    g.add_argument("--workspace")
    g.add_argument("--chairman", help="exclude active chairman from required headless reviewers")
    g.add_argument("--author", help="exclude task author from reviewing its own work")
    g.add_argument("--require", help="comma-separated reviewers that must have a verdict "
                                     "(default: enabled voting members minus chairman & author)")

    t = sub.add_parser("task")
    t.add_argument("--member", required=True, help="worker to run (ollama, codex, claude, or gemini)")
    t.add_argument("--brief", required=True)
    t.add_argument("--task-id", help="optional sub-task slug for multi-task runs in one run folder")
    t.add_argument("--base", help="branch or commit to start from (default: current branch)")
    t.add_argument("--workspace", help="repo to work in (default: current directory)")
    t.add_argument("--tier", choices=TIERS, help="semantic model/effort preset")
    t.add_argument("--model", help="explicit model override")
    t.add_argument("--effort", help="explicit reasoning effort override")
    t.add_argument("--timeout", type=int)
    t.add_argument("--continue", dest="resume", action="store_true",
                   help="another round in the existing worktree (e.g. with review findings)")

    c = sub.add_parser("cleanup")
    c.add_argument("--dir", required=True)
    c.add_argument("--member", choices=WRITE_MEMBERS)
    c.add_argument("--task-id", help="optional sub-task slug")
    c.add_argument("--workspace")
    c.add_argument("--discard", action="store_true", help="also delete unmerged council branches")

    council_install.add_commands(sub)

    args = parser.parse_args()
    if args.cmd in ("install", "update", "uninstall", "build-release"):
        return council_install.run(args, SKILL_ROOT, _file_lock, _write_utf8_atomic, DEFAULT_CONFIG)
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.cmd == "check":
        return cmd_check(args, config)
    if args.cmd in ("ask", "consult", "review"):
        return cmd_run(args, config, kind=args.cmd)
    if args.cmd == "task":
        return cmd_task(args, config)
    if args.cmd == "cleanup":
        return cmd_cleanup(args, config)
    return cmd_gate(args, config)


if __name__ == "__main__":
    sys.exit(main())
