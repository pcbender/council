"""Regression tests for the release and per-user installation lifecycle."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import council  # noqa: E402
import council_install as installer  # noqa: E402
import policy_gate  # noqa: E402


class DistributionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Council Test ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "User Home [1]"
        self.state = self.home / ".council"
        self.app = self.state / "app"
        self.project = self.root / "Other Project"
        self.project.mkdir()
        self.old_environ = patch.dict(os.environ, {"CODEX_HOME": str(self.home / "Custom Codex")})
        self.old_environ.start()
        self.addCleanup(self.old_environ.stop)

    def bundle(self, version, dest):
        return installer.build_release(council.SKILL_ROOT, self.root / dest, version)

    def cli(self, script, *args):
        return subprocess.run([sys.executable, "-B", str(script), *args, "--home", str(self.home)],
                              cwd=self.project, capture_output=True, text=True, encoding="utf-8")

    def managed(self, cmd, source=None, **options):
        args = argparse.Namespace(cmd=cmd, home=str(self.home), link=False, target=None,
                                  with_hooks=False, force=False)
        vars(args).update(options)
        messages = io.StringIO()
        with contextlib.redirect_stdout(messages), contextlib.redirect_stderr(messages):
            code = installer.run(args, source or council.SKILL_ROOT, council._file_lock,
                                 council._write_utf8_atomic, council.DEFAULT_CONFIG)
        return code, messages.getvalue()

    def test_zip_reproducible_and_installable_from_an_unrelated_project(self):
        first = self.bundle("1.2.3", "one")
        second = self.bundle("1.2.3", "two")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        self.assertEqual(first.with_suffix(".zip.sha256").read_text(), f"{digest}  council-1.2.3.zip\n")
        with tempfile.TemporaryDirectory(dir=self.root, prefix="Extracted Zip ") as extracted:
            payload = Path(extracted)
            installer._unpack_release(first.read_bytes(), payload, "1.2.3")
            self.assertFalse((payload / ".git").exists())
            result = self.cli(payload / "scripts/council.py", "install")
            self.assertEqual(result.returncode, 0, result.stderr)

        for relative in installer.HOST_PATHS.values():
            entry = self.home / relative
            self.assertEqual(entry.resolve(), self.app)
            result = subprocess.run([sys.executable, "-B", str(entry / "scripts/council.py"), "ask", "--help"],
                                    cwd=self.project, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        for relative in installer.HOOK_PATHS.values():
            self.assertFalse((self.home / relative).exists())

        self.assertEqual(self.managed("uninstall", self.app)[0], 0)
        self.assertFalse(self.app.exists())
        self.assertTrue(all(not (self.home / relative).exists() for relative in installer.HOST_PATHS.values()))

    def test_migrate_legacy_links_config_and_own_hooks(self):
        old = self.root / "Former Checkout"
        installer._unpack_release(self.bundle("1.0.0", "old").read_bytes(), old, "1.0.0")
        config = old / "council.config.json"
        overridden = json.loads(config.read_text())
        overridden["members"]["codex"]["enabled"] = False
        config.write_text(json.dumps(overridden), encoding="utf-8")
        legacy = [self.home / ".codex/skills/council", self.home / "Custom Codex/skills/council"]
        for link in legacy:
            installer._make_link(link, old)
        installer._make_link(self.home / installer.HOST_PATHS["claude"], old)
        legacy_command = next(iter(installer._legacy_commands({old})))
        other = {"type": "command", "command": "other-hook"}
        cfg = self.home / installer.HOOK_PATHS["claude"]
        cfg.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": legacy_command}, other]}]}}), encoding="utf-8")
        if os.name != "nt":
            cfg.chmod(0o600)
        code, message = self.managed("install")
        self.assertEqual(code, 0, message)
        self.assertTrue(all(not os.path.lexists(path) for path in legacy))
        self.assertEqual((self.home / installer.HOST_PATHS["codex"]).resolve(), self.app)
        self.assertTrue(old.is_dir())
        self.assertFalse(council.load_config(self.home)["members"]["codex"]["enabled"])
        rows = json.loads(cfg.read_text())["hooks"]["PreToolUse"]
        self.assertEqual(rows[0]["hooks"], [other])
        command = rows[1]["hooks"][0]["command"]
        tool = {"tool_name": "Edit", "tool_input": {"file_path": str(self.home / ".claude/é.txt")}}
        shell = f"cmd.exe /d /v:off /c {command}" if os.name == "nt" else ["/bin/sh", "-c", command]
        hook = subprocess.run(shell, input=json.dumps(tool, ensure_ascii=False), encoding="utf-8",
                              cwd=self.project, capture_output=True)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(json.loads(hook.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(self.managed("uninstall", self.app)[0], 0)
        self.assertEqual(json.loads(cfg.read_text())["hooks"]["PreToolUse"][0]["hooks"], [other])
        if os.name != "nt":
            self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.state / "council.config.json").is_file())

    def test_repairs_owned_dangling_link_and_preserves_unowned_directory(self):
        old = self.root / "Checkout"
        installer._unpack_release(self.bundle("1.0.0", "old").read_bytes(), old, "1.0.0")
        self.assertEqual(self.managed("install", old, link=True, target=["codex"])[0], 0)
        legacy = self.home / ".codex/skills/council"
        installer._make_link(legacy, old)
        old.rename(self.root / "Moved Checkout")
        self.assertTrue(os.path.lexists(legacy))
        blocker = self.home / installer.HOST_PATHS["claude"]
        blocker.mkdir(parents=True)
        marker = blocker / "user.txt"
        marker.write_text("user")
        self.assertEqual(self.managed("install", force=True, target=["claude"])[0], 1)
        self.assertEqual(marker.read_text(), "user")
        self.assertFalse(self.app.exists())
        code, message = self.managed("install", force=True)
        self.assertEqual(code, 0, message)
        self.assertFalse(os.path.lexists(legacy))
        self.assertEqual((self.home / installer.HOST_PATHS["codex"]).resolve(), self.app)

    def test_explicit_repair_of_an_unrecorded_dangling_link(self):
        old = self.root / "Removed Checkout"
        old.mkdir()
        link = self.home / installer.HOST_PATHS["codex"]
        installer._make_link(link, old)
        old.rmdir()
        self.assertIn("Inspect the dangling skill link", self.managed("install", target=["codex"])[1])
        code, message = self.managed("install", target=["codex"], force=True)
        self.assertEqual(code, 0, message)
        self.assertEqual(link.resolve(), self.app)

    def test_custom_codex_home_cannot_retire_the_canonical_link(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".agents")}):
            self.assertEqual(self.managed("install", target=["codex"])[0], 0)
            self.assertEqual(self.managed("install")[0], 0)
        self.assertEqual((self.home / installer.HOST_PATHS["codex"]).resolve(), self.app)

    def test_legacy_codex_parent_alias_cannot_retire_the_canonical_link(self):
        canonical = self.home / installer.HOST_PATHS["codex"]
        canonical.parent.mkdir(parents=True)
        installer._make_link(self.home / ".codex/skills", canonical.parent)
        for _ in range(2):
            code, message = self.managed("install", target=["codex"])
            self.assertEqual(code, 0, message)
            self.assertEqual(canonical.resolve(), self.app)

    def test_failed_first_install_clears_prepared_ownership_record(self):
        blocker = self.home / installer.HOST_PATHS["codex"]
        blocker.mkdir(parents=True)
        self.assertEqual(self.managed("install", target=["codex"])[0], 1)
        self.assertTrue(blocker.is_dir())
        self.assertFalse((self.state / "install.json").exists())
        self.assertFalse(self.app.exists())

    def test_first_install_prepares_config_and_hooks_before_activating(self):
        old = self.root / "Previous Checkout"
        installer._unpack_release(self.bundle("1.0.0", "old-copy").read_bytes(), old, "1.0.0")
        (old / "council.config.json").write_text('{"timeout_seconds": 91}', encoding="utf-8")
        entry = self.home / installer.HOST_PATHS["claude"]
        installer._make_link(entry, old)

        def interrupt(installation, candidate, rollback):
            self.assertTrue(candidate.is_dir())
            self.assertEqual(council.load_config(self.home)["timeout_seconds"], 91)
            record = json.loads((self.state / "install.json").read_text())
            self.assertEqual(set(record["hooks"]), {"claude"})
            raise PermissionError("interrupted before activation")

        with patch.object(installer.Installation, "_change_app", interrupt):
            result = self.managed("install", target=["claude"], with_hooks=True)
            self.assertIn("interrupted before activation", result[1])
        self.assertFalse((self.state / "install.json").exists())
        self.assertFalse((self.state / "council.config.json").exists())
        self.assertEqual(entry.resolve(), old)

    def test_retired_files_do_not_block_install_after_uninstall(self):
        self.assertEqual(self.managed("install", target=["codex"])[0], 0)
        remove = shutil.rmtree

        def retained(path, *args, **kwargs):
            if Path(path).name.startswith(".app-retired-"):
                raise PermissionError("retired files in use")
            return remove(path, *args, **kwargs)

        with patch.object(shutil, "rmtree", retained):
            code, message = self.managed("uninstall", self.app)
        self.assertEqual(code, 0, message)
        self.assertIn("retired files in use", message)
        self.assertFalse((self.state / "app.previous").exists())
        self.assertEqual(self.managed("install", target=["codex"])[0], 0)

    def test_update_checksum_rollback_and_user_state(self):
        initial = self.root / "Initial Release"
        installer._unpack_release(self.bundle("1.0.0", "first").read_bytes(), initial, "1.0.0")
        self.assertEqual(self.managed("install", initial, target=["codex"])[0], 0)
        old_cwd = Path.cwd()
        self.addCleanup(os.chdir, old_cwd)
        (self.state / "state.json").write_text('{"history": []}', encoding="utf-8")
        (self.state / "council.config.json").write_text('{"timeout_seconds": 99}', encoding="utf-8")
        record = self.state / "install.json"
        before = record.read_bytes()
        payload = self.bundle("1.1.0", "update").read_bytes()
        tag = "v1.1.0"
        name = "council-1.1.0.zip"
        url = f"https://github.com/{installer.RELEASE_REPO}/releases/download/{tag}/{name}"
        digest = hashlib.sha256(payload).hexdigest()

        def download(request):
            if request == installer.RELEASE_API:
                return json.dumps({"tag_name": tag, "draft": False, "prerelease": False, "assets": [
                    {"name": name, "browser_download_url": url, "digest": f"sha256:{digest}"}]}).encode()
            self.assertEqual(request, url)
            return payload

        rename = Path.rename

        def blocked(path, dest):
            if path.name == "candidate" and dest == self.app:
                raise PermissionError("simulated Windows file lock")
            return rename(path, dest)

        starting_digest = digest
        digest = "0" * 64
        with patch.object(installer, "_download", side_effect=download):
            self.assertIn("SHA-256 verification failed", self.managed("update", self.app)[1])
        digest = starting_digest
        with patch.object(installer, "_download", side_effect=download), patch.object(Path, "rename", blocked):
            os.chdir(self.app)
            self.assertIn("simulated Windows file lock", self.managed("update", self.app)[1])
        self.assertEqual(Path.cwd(), self.state)
        os.chdir(old_cwd)
        self.assertEqual((self.app / "VERSION").read_text().strip(), "1.0.0")
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse((self.state / "app.previous").exists())
        with patch.object(installer, "_download", side_effect=download) as fetch:
            code, message = self.managed("update", self.app)
            self.assertEqual(code, 0, message)
            self.assertEqual(fetch.call_count, 2)
            fetch.reset_mock()
            self.assertIn("up to date", self.managed("update", self.app)[1])
            fetch.assert_called_once_with(installer.RELEASE_API)
        self.assertEqual((self.app / "VERSION").read_text().strip(), "1.1.0")
        self.assertEqual(council.load_config(self.home)["timeout_seconds"], 99)
        self.assertEqual(self.managed("uninstall", self.app)[0], 0)
        self.assertTrue((self.state / "state.json").is_file())
        self.assertTrue((self.state / "council.config.json").is_file())

    def test_rejects_unexpected_archive_paths(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as bundle:
            bundle.writestr("../user.txt", b"x")
        with self.assertRaisesRegex(ValueError, "expected Council files"):
            installer._unpack_release(data.getvalue(), self.root / "candidate", "1.0.0")
        self.assertFalse((self.root / "user.txt").exists())

    def test_updates_allow_new_python_modules_and_interrupted_swaps_are_repaired(self):
        source = self.root / "Next Bundle"
        installer._unpack_release(self.bundle("2.0.0", "base").read_bytes(), source, "2.0.0")
        module = source / "scripts" / "next_module.py"
        module.write_text("VALUE = 1\n")
        ordinary = installer.build_release(source, self.root / "ordinary", None)
        with zipfile.ZipFile(ordinary) as archive:
            self.assertNotIn("scripts/next_module.py", archive.namelist())
        with patch.object(installer, "RELEASE_FILES", (*installer.RELEASE_FILES, "scripts/next_module.py")):
            archive = installer.build_release(source, self.root / "next", None)
            self.assertEqual(self.managed("install", source, target=["codex"])[0], 0)
        installer._unpack_release(archive.read_bytes(), self.root / "decoded", "2.0.0")
        self.assertTrue((self.root / "decoded/scripts/next_module.py").is_file())

        previous = self.state / "app.previous"
        self.app.rename(previous)
        args = dict(target=["codex"])
        with patch.object(installer, "_copy_release", side_effect=ValueError("stop after recovery")):
            self.assertIn("Restored Council 2.0.0", self.managed("install", source, **args)[1])
        self.assertFalse(previous.exists())
        self.assertTrue((self.app / "scripts/next_module.py").is_file())

    def test_checkout_migration_does_not_pin_unchanged_defaults(self):
        source = self.root / "Linked Checkout"
        archive = self.bundle("1.0.0", "checkout")
        installer._unpack_release(archive.read_bytes(), source, "1.0.0")
        for command in (["init", "-q"], ["add", "council.config.json"],
                        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init"]):
            subprocess.run(["git", *command], cwd=source, check=True, capture_output=True)
        self.assertEqual(self.managed("install", source, target=["codex"], link=True)[0], 0)
        self.assertEqual(self.managed("install", source)[0], 0)
        self.assertFalse((self.state / "council.config.json").exists())

    def test_antigravity_matcher_includes_all_gated_tools(self):
        for tool in ("run_command", *policy_gate.AGY_WRITE_TOOLS, *policy_gate.AGY_READ_TOOLS):
            self.assertIsNotNone(re.fullmatch(installer.MATCHERS["gemini"], tool))

    def test_check_requires_one_ready_peer_and_usable_chair(self):
        config = council.load_config(self.home)
        config["delegates"] = {}
        args = argparse.Namespace(chairman="claude", json=True)
        with patch.object(council, "load_state", return_value={}), contextlib.redirect_stdout(io.StringIO()), \
                patch.object(council.ClaudeAdapter, "executable", return_value="claude"), \
                patch.object(council.ClaudeAdapter, "check_auth", return_value="ready"), \
                patch.object(council.GeminiAgyAdapter, "executable", return_value="agy"), \
                patch.object(council.CodexAdapter, "executable", side_effect=council.MemberUnavailable("missing")):
            self.assertEqual(council.cmd_check(args, config), 0)
            config["members"]["gemini"]["enabled"] = False
            self.assertEqual(council.cmd_check(args, config), 1)
