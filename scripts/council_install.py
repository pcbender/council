"""Build and manage a per-user installation of the Council skill."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path

HOST_PATHS = {
    "claude": ".claude/skills/council",
    "codex": ".agents/skills/council",
    "gemini": ".gemini/config/skills/council",
}
HOOK_PATHS = {"claude": ".claude/settings.json", "gemini": ".gemini/config/hooks.json"}
MATCHERS = {
    "claude": "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|Read",
    "gemini": ("run_command|edit_file|write_file|multi_edit|write_to_file|replace_file_content|"
               "multi_replace_file_content|read_file|view_file"),
}
HOOK_KEY = "council-policy-gate"
RELEASE_REPO = "pcbender/council"
RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", re.ASCII)
RELEASE_FILES = ("README.md", "SKILL.md", "VERSION", "council.config.json",
                 "scripts/council.py", "scripts/council_install.py", "scripts/policy_gate.py")
MAX_RELEASE_SIZE = 4 * 1024 * 1024


def add_commands(sub) -> None:
    install = sub.add_parser("install", help="install Council for this user")
    install.add_argument("--link", action="store_true", help="link to this checkout for development")
    install.add_argument("--target", choices=HOST_PATHS, action="append", help="host to register (repeatable)")
    install.add_argument("--with-hooks", action="store_true", help="enable Council's policy hooks for selected hosts")
    install.add_argument("--force", action="store_true", help="replace dangling links at selected Council skill paths")
    update = sub.add_parser("update", help="update a managed install from GitHub Releases")
    uninstall = sub.add_parser("uninstall", help="remove Council-owned files and registrations")
    for command in (install, update, uninstall):
        command.add_argument("--home", help=argparse.SUPPRESS)
    release = sub.add_parser("build-release", help="build a reproducible release ZIP and SHA-256 file")
    release.add_argument("--version", help="release version (defaults to VERSION)")
    release.add_argument("--output", type=Path, default=Path("dist"), help="output directory (default: dist)")


def _version(text: str) -> tuple[int, ...]:
    if not VERSION.fullmatch(text):
        raise ValueError(f"Invalid release version: {text!r}")
    return tuple(map(int, text.split(".")))


def _release_name(version: str) -> str:
    _version(version)
    return f"council-{version}.zip"


def _read_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def _script_name(name: str) -> bool:
    if not re.fullmatch(r"scripts/(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py", name, re.ASCII):
        return False
    return all(not re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", part.split('.')[0], re.I)
               for part in name.split("/"))


def _release_files(source: Path) -> set[str]:
    files = set(RELEASE_FILES)
    if (source / "LICENSE").is_file():
        files.add("LICENSE")
    for script in (source / "scripts").rglob("*.py"):
        name = script.relative_to(source).as_posix()
        if _script_name(name) and not _is_reparse(script):
            files.add(name)
    return files


def _copy_release(source: Path, dest: Path) -> str:
    for name in sorted(_release_files(source)):
        src = source / name
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    version = (dest / "VERSION").read_text(encoding="utf-8").strip()
    _version(version)
    _read_object(dest / "council.config.json")
    return version


def build_release(source: Path, output: Path, version: str | None) -> Path:
    version = version or (source / "VERSION").read_text(encoding="utf-8").strip()
    name = _release_name(version)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        stage = Path(temporary)
        archive = stage / name
        with zipfile.ZipFile(archive, "w") as bundle:
            for entry in sorted(_release_files(source)):
                data = (version + "\n").encode() if entry == "VERSION" else (source / entry).read_bytes()
                # Git checkouts can use CRLF. Normalize this text-only payload for reproducible builds.
                info = zipfile.ZipInfo(entry)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, data.replace(b"\r\n", b"\n"), compress_type=zipfile.ZIP_STORED)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = stage / (name + ".sha256")
        checksum.write_bytes(f"{digest}  {name}\n".encode())
        archive.replace(output / name)
        checksum.replace(output / checksum.name)
    return output / name


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Council-Updater"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(MAX_RELEASE_SIZE + 1)
    if len(data) > MAX_RELEASE_SIZE:
        raise ValueError("GitHub response exceeds the Council release size limit")
    return data


def _latest_release() -> tuple[str, str, str]:
    release = json.loads(_download(RELEASE_API))
    tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not tag.startswith("v"):
        raise ValueError("Expected a stable GitHub release tagged vMAJOR.MINOR.PATCH")
    version = tag[1:]
    name = _release_name(version)
    url = f"https://github.com/{RELEASE_REPO}/releases/download/{tag}/{name}"
    for asset in release["assets"]:
        digest = asset.get("digest") or ""
        if (asset["name"] == name and asset["browser_download_url"] == url
                and re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
            return version, url, digest[7:]
    raise ValueError(f"GitHub Release {tag} has no {name} asset with a SHA-256 digest")


def _unpack_release(data: bytes, destination: Path, version: str) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        names = bundle.namelist()
        if (not set(RELEASE_FILES) <= set(names) or len({name.lower() for name in names}) != len(names)
                or any(name not in RELEASE_FILES and name != "LICENSE" and not _script_name(name) for name in names)):
            raise ValueError("Release ZIP does not contain the expected Council files")
        if sum(info.file_size for info in bundle.infolist()) > MAX_RELEASE_SIZE:
            raise ValueError("Unpacked release exceeds the Council release size limit")
        for name in names:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(name))
    if (destination / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("Release ZIP version does not match the GitHub tag")
    _read_object(destination / "council.config.json")


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _link_target(path: Path) -> Path | None:
    try:
        target = os.readlink(path)
        if os.name == "nt":
            if target.startswith("\\\\?\\UNC\\"):
                target = "\\\\" + target[8:]
            else:
                target = target.removeprefix("\\\\?\\")
        return (path.parent / target).resolve()
    except (OSError, RuntimeError):
        return None


def _make_link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.symlink_to(target, target_is_directory=True)
        return
    env = {**os.environ, "COUNCIL_LINK_PATH": str(path), "COUNCIL_LINK_TARGET": str(target)}
    # A fixed command with quoted environment expansion passes paths with spaces and metacharacters.
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/cmd.exe"
    subprocess.run(r'cmd.exe /d /v:off /c mklink /J "%COUNCIL_LINK_PATH%" "%COUNCIL_LINK_TARGET%"',
                   executable=shell, env=env, check=True, capture_output=True, text=True)


def _remove_link(path: Path) -> None:
    if os.name == "nt" and not path.is_symlink():
        path.rmdir()
    else:
        path.unlink()


def _council_checkout(target: Path) -> bool:
    skill = target / "SKILL.md"
    return (skill.is_file() and (target / "scripts/council.py").is_file()
            and bool(re.match(r"^---\n(?:[^\n]+\n)*?name: council\s*\n",
                              skill.read_text(encoding="utf-8-sig"))))


def _config_overrides(old: dict, defaults: dict) -> dict:
    overrides = {}
    for key, value in old.items():
        baseline = defaults.get(key)
        if isinstance(value, dict) and isinstance(baseline, dict):
            value = _config_overrides(value, baseline)
            if value:
                overrides[key] = value
        elif key not in defaults or value != baseline:
            overrides[key] = value
    return overrides


def _checkout_defaults(root: Path) -> dict | None:
    if not (root / ".git").exists():
        return None
    try:
        committed = subprocess.run(["git", "show", "HEAD:council.config.json"], cwd=root, check=True,
                                   capture_output=True, encoding="utf-8")
        data = json.loads(committed.stdout)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def _legacy_commands(roots: set[Path]) -> set[str]:
    python = "python" if os.name == "nt" else "python3"
    return {f'{python} "{(root / "scripts/policy_gate.py").as_posix()}"' for root in roots}


def _hook_command(target: Path) -> str:
    executable = Path(sys.executable).absolute()
    if not executable.is_file():
        raise OSError(f"Cannot validate the Python executable: {executable}")
    script = target / "scripts/policy_gate.py"
    if os.name != "nt":
        return shlex.join([executable.as_posix(), "-X", "utf8", script.as_posix()])
    # Both Windows hosts can launch this without asking their outer shell to parse the paths.
    python_path = str(executable).replace("'", "''")
    script_path = str(script).replace("'", "''")
    launch = ("$ProgressPreference = 'SilentlyContinue'\n"
              "$ErrorActionPreference = 'Stop'\n"
              "[Console]::InputEncoding = $OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
              f"[Console]::In.ReadToEnd() | & '{python_path}' -X utf8 '{script_path}'\n"
              "exit $LASTEXITCODE\n")
    encoded = base64.b64encode(launch.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


def _rewrite_hooks(host: str, data: dict, owned: set[str], command: str | None, enable: bool = False) -> bool:
    def owns(action) -> bool:
        return (isinstance(action, dict) and action.get("type") == "command"
                and action.get("command") in owned)

    key = HOOK_KEY if host == "gemini" else "hooks"
    hooks = data.get(key, {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get("PreToolUse", []), list):
        raise ValueError(f"Invalid {host} hook settings")
    rows = []
    found = False
    for row in hooks.get("PreToolUse", []):
        actions = row.get("hooks") if isinstance(row, dict) else None
        if not isinstance(actions, list) or not any(map(owns, actions)):
            rows.append(row)
            continue
        found = True
        rest = [action for action in actions if not owns(action)]
        if rest:
            rows.append({**row, "hooks": rest})
    if host == "gemini" and key in data and not found and enable:
        raise ValueError(f"Existing Antigravity hook {HOOK_KEY} is not owned by Council")
    if command and (found or enable):
        action = {"type": "command", "command": command}
        if host == "gemini":
            action["timeout"] = 20
            if enable or not hooks:
                hooks = {**hooks, "enabled": True}
        rows.append({"matcher": MATCHERS[host], "hooks": [action]})
    if found and not rows and host == "gemini" and set(hooks) <= {"enabled", "PreToolUse"}:
        del data[key]
    elif found or enable:
        data[key] = {**hooks, "PreToolUse": rows}
    return found or enable


@contextlib.contextmanager
def _transaction():
    with contextlib.ExitStack() as rollback:
        yield rollback
        rollback.pop_all()


def _write_json(path: Path, data: dict, write, rollback) -> None:
    if _is_reparse(path):
        raise ValueError(f"Preserving a linked settings or ownership file at {path}")
    previous = path.read_text(encoding="utf-8") if _exists(path) else None
    write(path, json.dumps(data, indent=2) + "\n", preserve_mode=True)
    if previous is None:
        rollback.callback(path.unlink)
    else:
        rollback.callback(write, path, previous, preserve_mode=True)


class Installation:
    def __init__(self, home: Path, write, defaults: dict):
        self.home = home
        self.state = home / ".council"
        self.app = self.state / "app"
        self.previous = self.state / "app.previous"
        self.record = self.state / "install.json"
        self.overrides = self.state / "council.config.json"
        self.write = write
        self.defaults = defaults
        if _is_reparse(self.record):
            raise ValueError(f"Preserving a linked ownership file at {self.record}")
        self.data = self._read_record()
        self.target = Path(self.data["target"]) if self.data else None
        self.hosts = set(self.data.get("hosts", []))
        self.hooks = self.data.get("hooks", {})
        self.managed = self.data.get("mode") == "managed"
        self._recover_previous()

    def _read_record(self) -> dict:
        if not _exists(self.record):
            return {}
        data = _read_object(self.record)
        target, hosts, hooks = data.get("target"), data.get("hosts"), data.get("hooks")
        mode, version = data.get("mode"), data.get("version")
        if (data.get("schema") != 1 or not isinstance(target, str) or not Path(target).is_absolute()
                or mode not in ("managed", "link") or (mode == "managed") != (Path(target) == self.app)
                or not isinstance(version, str) or not VERSION.fullmatch(version)
                or not isinstance(hosts, list) or not all(isinstance(h, str) and h in HOST_PATHS for h in hosts)
                or not isinstance(hooks, dict) or not hooks.keys() <= HOOK_PATHS.keys()
                or not all(isinstance(c, str) for c in hooks.values())):
            raise ValueError(f"Unrecognized Council ownership record at {self.record}")
        return data

    def _legacy_paths(self) -> set[Path]:
        codex_home = Path(os.environ.get("CODEX_HOME") or self.home / ".codex").expanduser().resolve()
        return {self.home / ".codex/skills/council", codex_home / "skills/council"}

    def _leave_app(self) -> None:
        # Windows prevents renaming a directory which is the current working directory.
        if Path.cwd().resolve().is_relative_to(self.app):
            os.chdir(self.state)

    def _change_app(self, candidate: Path | None, rollback) -> None:
        self._leave_app()
        if _exists(self.app):
            if not self.managed or _is_reparse(self.app) or not self.app.is_dir():
                raise ValueError(f"Preserving an unowned installation path: {self.app}")
            self.app.rename(self.previous)
            rollback.callback(self.previous.rename, self.app)
        if candidate is not None:
            candidate.rename(self.app)
            rollback.callback(self.app.rename, candidate)

    def _managed_version(self, payload: Path) -> str | None:
        if _is_reparse(payload) or not _council_checkout(payload):
            return None
        version = payload / "VERSION"
        return version.read_text(encoding="utf-8").strip() if version.is_file() else None

    def _recover_previous(self) -> None:
        if not _exists(self.previous):
            return
        active = self._managed_version(self.app)
        previous = self._managed_version(self.previous)
        if self.managed and previous is not None:
            recorded = self.data["version"]
            if active == recorded:
                self._retire(self.previous)
                return
            if previous == recorded and (active is not None or not _exists(self.app)):
                with _transaction() as rollback:
                    misplaced = self.state / f".app-retired-{uuid.uuid4().hex}"
                    if active is not None:
                        self._leave_app()
                        self.app.rename(misplaced)
                        rollback.callback(misplaced.rename, self.app)
                    self.previous.rename(self.app)
                self._remove_retired(misplaced)
                print(f"Restored Council {previous} after an interrupted installation.")
                return
        raise ValueError(f"Cannot identify the previous installation at {self.previous}. "
                         f"Preserve it and inspect {self.record} before retrying.")

    def _retire(self, payload: Path) -> None:
        retired = self.state / f".app-retired-{uuid.uuid4().hex}"
        payload.rename(retired)
        self._remove_retired(retired)

    def _remove_retired(self, retired: Path) -> None:
        if retired.is_dir():
            try:
                shutil.rmtree(retired)
            except OSError as exc:
                print(f"Could not remove the retired Council files at {retired}: {exc}", file=sys.stderr)

    def _finish_app(self) -> None:
        if not _exists(self.previous):
            return
        try:
            self._retire(self.previous)
        except OSError as exc:
            print(f"Could not retire the previous Council files at {self.previous}: {exc}", file=sys.stderr)

    def install(self, source: Path, candidate: Path | None, version: str,
                hosts: set[str], with_hooks: bool, force: bool) -> None:
        target = self.app if candidate is not None else source
        if candidate is None and self.app == target:
            raise ValueError("Run install --link from the contributor checkout")
        selected = self.hosts | (hosts or self.hosts or set(HOST_PATHS))
        destinations = {host: self.home / HOST_PATHS[host] for host in sorted(selected)}
        legacy = self._legacy_paths() - set(destinations.values()) if "codex" in selected else set()
        roots = {source, target}
        if self.target is not None:
            roots.add(self.target)
        old_links = {path: _link_target(path) for path in (*destinations.values(), *sorted(legacy))}
        previous_roots = {old for old in old_links.values() if old and old != self.app and _council_checkout(old)}
        roots.update(previous_roots)
        with _transaction() as rollback:
            if candidate is not None:
                self._change_app(candidate, rollback)
            self._migrate_config(candidate is not None, sorted(previous_roots), source, target, rollback)
            for path in destinations.values():
                old = old_links[path]
                if _exists(path) and old not in roots:
                    if old is None or old.exists():
                        raise ValueError(f"Preserving an unowned skill path: {path}")
                    if not force:
                        raise ValueError(f"Inspect the dangling skill link {path} -> {old}. "
                                         "Use install --force to replace that link.")
                if old == target:
                    continue
                if old is not None:
                    _remove_link(path)
                    rollback.callback(_make_link, path, old)
                _make_link(path, target)
                rollback.callback(_remove_link, path)
            for path in sorted(legacy):
                old = old_links[path]
                if old is not None and (old in roots or (force and not old.exists())):
                    _remove_link(path)
                    rollback.callback(_make_link, path, old)
                elif _exists(path):
                    print(f"Preserving legacy Codex skill path {path}; inspect it if Council appears twice.", file=sys.stderr)
            hooks = self._install_hooks(target, roots, selected, hosts, with_hooks, rollback)
            if candidate is None and self.managed:
                self._change_app(None, rollback)
            _write_json(self.record, {"schema": 1, "mode": "managed" if candidate else "link",
                        "target": str(target), "version": version, "hosts": sorted(selected), "hooks": hooks},
                        self.write, rollback)
        self._finish_app()
        for host, path in destinations.items():
            print(f"{host}: {path} -> {target}")

    def _migrate_config(self, managed: bool, roots: list[Path], source: Path, target: Path, rollback) -> None:
        if not managed or self.managed or _exists(self.overrides):
            return
        for root in roots:
            old = root / "council.config.json"
            if old.is_file():
                values = _read_object(old)
                baseline = _checkout_defaults(root)
                if baseline is not None:
                    overrides = _config_overrides(values, baseline)
                else:
                    overrides = _config_overrides(values, self.defaults)
                    if root != source:
                        overrides = _config_overrides(overrides, _read_object(target / "council.config.json"))
                if overrides:
                    _write_json(self.overrides, overrides, self.write, rollback)
                    print(f"Migrated settings from {old} to {self.overrides}: {', '.join(sorted(overrides))}")
                return

    def _install_hooks(self, target: Path, roots: set[Path], selected: set[str], hosts: set[str],
                       with_hooks: bool, rollback) -> dict:
        hooks = {}
        legacy = _legacy_commands(roots)
        command = _hook_command(target)
        for host, relative in HOOK_PATHS.items():
            if host not in selected:
                continue
            path = self.home / relative
            owned = legacy | {command}
            if host in self.hooks:
                owned.add(self.hooks[host])
            enable = with_hooks and (not hosts or host in hosts)
            try:
                data = _read_object(path) if _exists(path) else {}
                previous = json.dumps(data)
                changed = _rewrite_hooks(host, data, owned, command, enable)
            except ValueError:
                if enable or host in self.hooks:
                    raise
                continue
            if changed:
                if json.dumps(data) != previous:
                    _write_json(path, data, self.write, rollback)
                hooks[host] = command
        return hooks

    def uninstall(self) -> None:
        if not self.data:
            print("Council has no recorded installation.")
            return
        with _transaction() as rollback:
            for host, command in self.hooks.items():
                path = self.home / HOOK_PATHS[host]
                if _exists(path):
                    data = _read_object(path)
                    if _rewrite_hooks(host, data, {command}, None):
                        _write_json(path, data, self.write, rollback)
            for host in sorted(self.hosts):
                path = self.home / HOST_PATHS[host]
                if _link_target(path) == self.target:
                    _remove_link(path)
                    rollback.callback(_make_link, path, self.target)
                elif _exists(path):
                    print(f"Preserving skill path that no longer points to Council: {path}", file=sys.stderr)
            if self.managed:
                self._change_app(None, rollback)
            self.record.unlink()
        self._finish_app()
        print("Council registrations removed. User settings and history are preserved.")


def run(args, source: Path, lock, write, defaults: dict) -> int:
    try:
        if args.cmd == "build-release":
            print(build_release(source, args.output, args.version))
            return 0
        home = Path(getattr(args, "home", None) or Path.home()).resolve()
        if _is_reparse(home / ".council") or _is_reparse(home / ".council/install.lock"):
            raise ValueError("Preserving a linked Council installation directory or lock")
        with lock(home / ".council/install.lock", required=True):
            installation = Installation(home, write, defaults)
            if args.cmd == "uninstall":
                installation.uninstall()
            elif args.cmd == "update":
                if not installation.managed:
                    raise ValueError("Update requires a managed Council install. Run install from a release ZIP first.")
                version, url, digest = _latest_release()
                current = installation.data["version"]
                if _version(version) <= _version(current):
                    print(f"Council {current} is up to date.")
                    return 0
                data = _download(url)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError("SHA-256 verification failed for the Council release ZIP")
                with tempfile.TemporaryDirectory(dir=installation.state) as stage:
                    candidate = Path(stage) / "candidate"
                    _unpack_release(data, candidate, version)
                    installation.install(source, candidate, version, set(), False, False)
                print(f"Updated Council from {current} to {version}.")
            else:
                hosts = set(getattr(args, "target", None) or [])
                if getattr(args, "link", False):
                    version = (source / "VERSION").read_text(encoding="utf-8").strip()
                    _version(version)
                    installation.install(source, None, version, hosts, args.with_hooks, args.force)
                else:
                    with tempfile.TemporaryDirectory(dir=installation.state) as stage:
                        candidate = Path(stage) / "candidate"
                        version = _copy_release(source, candidate)
                        installation.install(source, candidate, version, hosts, args.with_hooks, args.force)
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.CalledProcessError,
            zipfile.BadZipFile) as exc:
        print(f"Council {args.cmd}: {getattr(exc, 'stderr', None) or exc}", file=sys.stderr)
        return 1
