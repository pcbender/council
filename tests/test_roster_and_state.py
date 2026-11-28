"""Tests for chairman detection, author-aware gate, global usage ledger, and installer."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import council  # noqa: E402


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class RosterStateAndInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_state = os.environ.get("COUNCIL_STATE_PATH")
        os.environ["COUNCIL_STATE_PATH"] = str(self.root / "state.json")

        self.repo = self.root / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@t")
        sh(self.repo, "git", "config", "user.name", "t")
        (self.repo / "a.txt").write_text("a\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "init")
        self.run_dir = self.repo / ".council" / "runs" / "r1"
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        if self.old_state is None:
            os.environ.pop("COUNCIL_STATE_PATH", None)
        else:
            os.environ["COUNCIL_STATE_PATH"] = self.old_state
        self.tmp.cleanup()

    def test_chairman_detection_and_self_consult_guard(self):
        self.assertEqual(council.resolve_chairman("auto", {"CLAUDECODE": "1"}), "claude")
        self.assertEqual(council.resolve_chairman("auto", {"CODEX_THREAD_ID": "t1"}), "codex")
        self.assertEqual(council.resolve_chairman("auto", {"ANTIGRAVITY_CLI": "1"}), "gemini")
        self.assertEqual(council.resolve_chairman("agy", {}), "gemini")
        self.assertIsNone(council.resolve_chairman("auto", {}))
        with self.assertRaises(ValueError):
            council.resolve_chairman("unknown")

        brief = self.run_dir / "brief.md"
        brief.write_text("question")
        args = argparse.Namespace(member="gemini", brief=str(brief), workspace=str(self.repo),
                                  chairman="agy", tier=None, model=None, effort=None, timeout=10)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(council.cmd_run(args, council.load_config(), "consult"), 2)

    def test_gate_excludes_chairman_and_cloud_task_author(self):
        config = council.load_config()
        fp = council.diff_fingerprint(self.repo)
        # Suppose gemini is Chairman and codex authored the task -> only claude is required
        (self.run_dir / "codex.task.json").write_text(
            json.dumps({"member": "codex", "worktree": str(self.repo)})
        )
        (self.run_dir / "claude.verdict.json").write_text(
            json.dumps({"member": "claude", "verdict": "pass", "fingerprint": fp, "findings": []})
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = council.cmd_gate(argparse.Namespace(dir=str(self.run_dir), workspace=str(self.repo),
                                                       chairman="gemini", author=None, require=None), config)
        self.assertEqual(code, 0)
        gate = json.loads((self.run_dir / "gate.json").read_text())
        self.assertEqual(gate["required"], ["claude"])
        self.assertEqual(gate["decision"], "pass")

        # --require cannot force an excluded author/chairman to approve its own work
        (self.run_dir / "codex.verdict.json").write_text(
            json.dumps({"member": "codex", "verdict": "pass", "fingerprint": fp, "findings": []})
        )
        with contextlib.redirect_stdout(io.StringIO()):
            bad_code = council.cmd_gate(argparse.Namespace(dir=str(self.run_dir), workspace=str(self.repo),
                                                           chairman="gemini", author="codex", require="codex"), config)
        self.assertEqual(bad_code, 1)

        # With enforce-commit-gate enabled, a reduced --require subset cannot mint approved.json
        (self.repo / ".council" / "enforce-commit-gate").write_text("")
        (self.run_dir / "codex.task.json").unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            sub_code = council.cmd_gate(argparse.Namespace(dir=str(self.run_dir), workspace=str(self.repo),
                                                           chairman="gemini", author="ollama", require="claude"), config)
        self.assertEqual(sub_code, 1)
        self.assertFalse((self.repo / ".council" / "approved.json").exists())

    def test_usage_ledger_and_quota_cooldown_lifecycle(self):
        ok_res = council.RunResult(member="codex", ok=True, seconds=3.2)
        quota_res = council.RunResult(member="codex", ok=False, quota_hit=True, seconds=1.1)

        council.record_usage("codex", "consult", ok_res, tier="standard", cooldown_seconds=1800)
        s1 = council.usage_summary("codex")
        self.assertEqual(s1["runs_5h"], 1)
        self.assertTrue(s1["available_quota"])

        council.record_usage("codex", "task", quota_res, tier="deep", cooldown_seconds=1800)
        s2 = council.usage_summary("codex")
        self.assertEqual(s2["runs_5h"], 2)
        self.assertEqual(s2["quota_hits_24h"], 1)
        self.assertFalse(s2["available_quota"])
        self.assertGreater(s2["cooldown_remaining_s"], 0)

        council.clear_cooldown("codex")
        self.assertTrue(council.usage_summary("codex")["available_quota"])

    def test_cross_cli_installer_links_and_hooks(self):
        fake_home = self.root / "home"
        fake_home.mkdir()
        args = argparse.Namespace(with_hooks=True, force=False, home=str(fake_home))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = council.cmd_install(args, council.load_config())
        self.assertEqual(code, 0, out.getvalue())

        for rel in (".gemini/config/skills/council", ".claude/skills/council", ".agents/skills/council"):
            linked = fake_home / rel
            self.assertTrue((linked / "SKILL.md").is_file(), f"Missing SKILL.md under {linked}")
            self.assertEqual(linked.resolve(), council.SKILL_ROOT.resolve())

        claude_cfg = json.loads((fake_home / ".claude" / "settings.json").read_text())
        self.assertTrue(claude_cfg["hooks"]["PreToolUse"])
        agy_hooks = json.loads((fake_home / ".gemini" / "config" / "hooks.json").read_text())
        self.assertIn("council-policy-gate", agy_hooks)
        self.assertTrue(agy_hooks["council-policy-gate"]["enabled"])

        # Re-running install is idempotent
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(council.cmd_install(args, council.load_config()), 0)

        # Malformed settings.json is preserved untouched rather than overwritten
        corrupt = fake_home / ".claude" / "settings.json"
        corrupt.write_text("{not valid json // user comment", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(council.cmd_install(args, council.load_config()), 1)
        self.assertEqual(corrupt.read_text(encoding="utf-8"), "{not valid json // user comment")


if __name__ == "__main__":
    unittest.main()
