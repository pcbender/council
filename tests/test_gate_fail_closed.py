"""Regression tests for review approval and commit fingerprint failures."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import council
import policy_gate


class GitSnapshot:
    """Supply git outputs without changing the worktree or calling a member CLI."""

    def __init__(self, repo):
        self.repo = repo
        self.head = "a" * 40
        self.staged = "staged diff\nwith trailing whitespace \n"
        self.unstaged = "unstaged diff\n"
        self.fail = None
        self.unborn = False
        self.calls = []

    def run(self, argv, **kwargs):
        command = tuple(argv[3:])
        self.calls.append(command)
        outputs = {
            ("rev-parse", "--show-toplevel"): str(self.repo) + "\n",
            ("rev-parse", "--path-format=absolute", "--git-common-dir"): str(self.repo / ".git") + "\n",
            ("rev-parse", "--verify", "HEAD"): self.head + "\n",
            ("symbolic-ref", "--quiet", "HEAD"): "refs/heads/main\n",
            ("show-ref", "--verify", "--quiet", "refs/heads/main"): "",
            ("diff", "--cached", "--binary"): self.staged,
            ("diff", "--binary"): self.unstaged,
        }
        if command not in outputs:
            raise AssertionError(f"Unexpected git call: {argv}")
        code = 0
        if command == self.fail or (self.unborn and command == ("rev-parse", "--verify", "HEAD")):
            code = 128
        elif self.unborn and command[0] == "show-ref":
            code = 1
        return subprocess.CompletedProcess(argv, code, outputs[command] if code == 0 else "", "git failed" if code else "")


class CombineTest(unittest.TestCase):
    def test_only_exact_passes_pass(self):
        self.assertEqual(council.combine([{"verdict": "pass"}] * 2), "pass")
        self.assertEqual(council.combine([]), "revise")
        for value in ("pending", "bogus", "unparseable", "unavailable", "revise", "PASS", "", None, 1, [], {}):
            with self.subTest(value=value):
                self.assertEqual(council.combine([{"verdict": "pass"}, {"verdict": value}]), "revise")
        self.assertEqual(council.combine([{}]), "revise")

    def test_reject_wins_even_with_invalid_verdicts(self):
        self.assertEqual(council.combine([{}, {"verdict": "reject"}, {"verdict": "pending"}]), "reject")


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.run_dir = self.repo / "reviews"
        self.run_dir.mkdir()
        self.stamp = self.repo / ".council" / "approved.json"
        self.git = GitSnapshot(self.repo)
        self.addCleanup(patch.stopall)
        patch.object(council.subprocess, "run", side_effect=self.git.run).start()
        patch.object(council, "repo_root", return_value=self.repo).start()
        self.config = {"members": {"codex": {"enabled": True}, "gemini": {"enabled": True}}}
        self.fingerprint = council.diff_fingerprint(self.repo)

    def verdict(self, member="codex", **updates):
        return dict({"member": member, "verdict": "pass", "fingerprint": self.fingerprint, "findings": []}, **updates)

    def write(self, name, data):
        (self.run_dir / f"{name}.verdict.json").write_text(json.dumps(data), encoding="utf-8")

    def gate(self, require=None):
        args = argparse.Namespace(dir=str(self.run_dir), workspace=str(self.repo), require=require)
        with contextlib.redirect_stdout(io.StringIO()):
            code = council.cmd_gate(args, self.config)
        return code, json.loads((self.run_dir / "gate.json").read_text(encoding="utf-8"))

    def assert_denied(self, require=None):
        code, gate = self.gate(require)
        self.assertEqual(code, 1)
        self.assertNotEqual(gate["decision"], "pass")
        self.assertFalse(self.stamp.exists())
        return gate

    def test_invalid_files_cannot_approve_or_keep_an_approval(self):
        bad = [[], None, 42, "pass", {}, self.verdict(member=[]), self.verdict(member=None),
               self.verdict(member="stranger"), self.verdict(verdict="bogus"),
               self.verdict(verdict="pending"), self.verdict(verdict=[]),
               self.verdict(verdict=None), self.verdict(fingerprint=None),
               self.verdict(fingerprint=[]), self.verdict(findings="bad"),
               self.verdict(findings=[None]), self.verdict(confidence=float("nan")),
               self.verdict(confidence=float("inf"))]
        for field in ("member", "verdict", "fingerprint"):
            item = self.verdict()
            del item[field]
            bad.append(item)
        for data in bad:
            with self.subTest(data=data):
                self.write("codex", self.verdict())
                self.write("gemini", self.verdict("gemini"))
                self.assertEqual(self.gate()[0], 0)
                self.write("codex", data)
                gate = self.assert_denied()
                self.assertEqual(gate["members"]["codex"], "unparseable")
                self.assert_denied("codex")
        for raw in (b"{broken", b"\xff", b"[" * 1500 + b"]" * 1500, b"9" * 10000):
            with self.subTest(raw=raw[:30]):
                (self.run_dir / "codex.verdict.json").write_bytes(raw)
                self.assertEqual(self.assert_denied()["members"]["codex"], "unparseable")

    def test_unidentifiable_and_unknown_member_files_leave_required_review_missing(self):
        self.write("gemini", self.verdict("gemini"))
        for data in (self.verdict("stranger"), self.verdict(member=[]), [], {}):
            with self.subTest(data=data):
                self.write("unknown", data)
                gate = self.assert_denied()
                self.assertEqual(gate["missing_reviews"], ["codex"])
                self.assertNotIn("stranger", gate["members"])
        (self.run_dir / "unknown.verdict.json").write_text("{broken")
        self.assertEqual(self.assert_denied()["missing_reviews"], ["codex"])
        self.write("unknown", self.verdict("stranger"))
        self.assertEqual(self.assert_denied("stranger")["missing_reviews"], ["stranger"])

    def test_invalid_duplicate_counts_against_a_known_member(self):
        self.write("codex", self.verdict())
        self.write("gemini", self.verdict("gemini"))
        for value in ("bogus", "revise", "reject", None):
            for name in ("aaa", "zzz"):
                with self.subTest(value=value, name=name):
                    self.write(name, self.verdict(verdict=value))
                    gate = self.assert_denied()
                    self.assertEqual(gate["members"]["codex"], "reject" if value == "reject" else "revise")
                    (self.run_dir / f"{name}.verdict.json").unlink()

    def test_all_duplicate_reviews_must_also_be_fresh(self):
        self.write("codex", self.verdict())
        self.write("gemini", self.verdict("gemini"))
        self.write("zzz", self.verdict(fingerprint="old"))
        self.assertEqual(self.assert_denied()["stale_reviews"], ["codex"])

    def test_valid_all_pass_preserves_gate_shape_and_approval(self):
        self.write("codex", self.verdict())
        self.write("gemini", self.verdict("gemini"))
        self.write("duplicate", self.verdict())
        code, gate = self.gate()
        self.assertEqual(code, 0)
        self.assertEqual(gate["decision"], "pass")
        self.assertEqual(gate["members"], {"codex": "pass", "gemini": "pass"})
        self.assertEqual(set(gate), {"decision", "members", "required", "missing_reviews", "stale_reviews",
                                     "staged_fingerprint", "working_fingerprint", "findings", "created_at"})
        approved = json.loads(self.stamp.read_text())
        self.assertEqual(approved["staged_fingerprint"], council.staged_fingerprint(self.repo))

    def test_failed_snapshot_cannot_write_approval(self):
        self.write("codex", self.verdict())
        self.write("gemini", self.verdict("gemini"))
        self.stamp.parent.mkdir(parents=True, exist_ok=True)
        self.stamp.write_text('{"staged_fingerprint": "earlier approval"}')
        self.git.fail = ("diff", "--cached", "--binary")
        with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
            self.gate()
        self.assertFalse(self.stamp.exists())  # an earlier approval is withdrawn too

    def test_gate_reads_each_diff_and_head_once_for_check_and_stamp(self):
        self.write("codex", self.verdict())
        self.write("gemini", self.verdict("gemini"))
        original_staged = council.staged_fingerprint(self.repo)
        self.git.calls.clear()
        run = self.git.run

        def changing_git(argv, **kwargs):
            done = run(argv, **kwargs)
            if tuple(argv[3:]) == ("diff", "--binary"):
                self.git.staged = "different staged diff\n"
            return done

        with patch.object(council.subprocess, "run", side_effect=changing_git):
            code, gate = self.gate()
        self.assertEqual(code, 0)
        self.assertEqual(gate["staged_fingerprint"], original_staged)
        self.assertEqual(json.loads(self.stamp.read_text())["staged_fingerprint"], original_staged)
        self.assertEqual(self.git.calls.count(("rev-parse", "--verify", "HEAD")), 1)
        self.assertEqual(self.git.calls.count(("diff", "--cached", "--binary")), 1)
        self.assertEqual(self.git.calls.count(("diff", "--binary")), 1)


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git = GitSnapshot(self.repo)
        self.addCleanup(patch.stopall)
        patch.object(council.subprocess, "run", side_effect=self.git.run).start()
        (self.repo / ".council").mkdir()
        (self.repo / ".council" / "enforce-commit-gate").touch()

    def approve(self, fingerprint):
        (self.repo / ".council" / "approved.json").write_text(json.dumps({"staged_fingerprint": fingerprint}))

    def commit_gate(self):
        return policy_gate.commit_gate(self.repo, [], [], [], "git commit -m reviewed")

    def test_fingerprints_raise_on_failed_git_reads(self):
        for fn, commands in ((council.diff_fingerprint, [("diff", "--cached", "--binary"), ("diff", "--binary"), ("rev-parse", "--verify", "HEAD")]),
                             (council.staged_fingerprint, [("diff", "--cached", "--binary"), ("rev-parse", "--verify", "HEAD")])):
            for command in commands:
                with self.subTest(function=fn.__name__, command=command):
                    self.git.fail = command
                    with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                        fn(self.repo)

    def test_commit_gate_denies_git_failures_even_with_previous_approval(self):
        self.approve(council.staged_fingerprint(self.repo))
        for command in (("diff", "--cached", "--binary"), ("rev-parse", "--verify", "HEAD"),
                        ("rev-parse", "--show-toplevel"),
                        ("rev-parse", "--path-format=absolute", "--git-common-dir")):
            with self.subTest(command=command):
                self.git.fail = command
                self.assertEqual(self.commit_gate()[0], "deny")
        for error in (OSError("git missing"), subprocess.TimeoutExpired("git", 20)):
            with self.subTest(error=error), patch.object(council.subprocess, "run", side_effect=error):
                self.assertEqual(self.commit_gate()[0], "deny")

    def test_commit_gate_denies_malformed_approval_without_crashing(self):
        stamp = self.repo / ".council" / "approved.json"
        for raw in (b"{broken", b"\xff", b"[]", b"null", b"42", b"[" * 1500 + b"]" * 1500):
            with self.subTest(raw=raw[:30]):
                stamp.write_bytes(raw)
                self.assertEqual(self.commit_gate()[0], "deny")

    def test_git_execution_errors_propagate_from_council_fingerprints(self):
        for fn in (council.diff_fingerprint, council.staged_fingerprint):
            for error in (OSError("git missing"), subprocess.TimeoutExpired("git", 20)):
                with self.subTest(function=fn, error=error), \
                        patch.object(council.subprocess, "run", side_effect=error):
                    with self.assertRaises(type(error)):
                        fn(self.repo)

    def test_policy_and_council_fingerprints_agree(self):
        for unborn in (False, True):
            for staged in ("", "diff\n", "diff with \u2603 and trailing whitespace \n\n"):
                with self.subTest(unborn=unborn, staged=staged):
                    self.git.unborn, self.git.staged = unborn, staged
                    self.approve(council.staged_fingerprint(self.repo))
                    self.assertIsNone(self.commit_gate())
                    self.assertEqual(council.staged_fingerprint(self.repo), policy_gate.staged_fingerprint(self.repo))

    def test_same_diff_on_a_different_head_cannot_reuse_approval(self):
        working = council.diff_fingerprint(self.repo)
        staged = council.staged_fingerprint(self.repo)
        self.approve(staged)
        self.git.head = "b" * 40
        self.assertNotEqual(council.diff_fingerprint(self.repo), working)
        self.assertNotEqual(council.staged_fingerprint(self.repo), staged)
        self.assertEqual(self.commit_gate()[0], "deny")

    def test_unborn_marker_is_stable_and_distinct_from_a_commit(self):
        committed = council.staged_fingerprint(self.repo)
        self.git.unborn = True
        unborn = council.staged_fingerprint(self.repo)
        self.assertNotEqual(unborn, committed)
        self.assertEqual(unborn, council.staged_fingerprint(self.repo))
        self.git.unborn = False
        self.assertNotEqual(unborn, council.staged_fingerprint(self.repo))

    def test_unborn_lookup_errors_are_not_treated_as_an_unborn_head(self):
        self.git.unborn = True
        for command in (("symbolic-ref", "--quiet", "HEAD"),
                        ("show-ref", "--verify", "--quiet", "refs/heads/main")):
            for fn in (council.diff_fingerprint, council.staged_fingerprint, policy_gate.staged_fingerprint):
                with self.subTest(command=command, function=fn):
                    self.git.fail = command
                    with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                        fn(self.repo)


class RepositoryLookupTest(unittest.TestCase):
    def test_commit_gate_denies_failed_lookup_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".council").mkdir()
            (repo / ".council" / "enforce-commit-gate").touch()
            subdir = repo / "src"
            subdir.mkdir()
            failed = subprocess.CompletedProcess(["git"], 128, "", "git failed")
            with patch.object(policy_gate.subprocess, "run", return_value=failed):
                decision = policy_gate.commit_gate(subdir, [], [], [], "git commit -m reviewed")
            self.assertIsNotNone(decision)
            self.assertEqual(decision[0], "deny")

    def test_real_unborn_repository_fingerprints_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
            self.assertEqual(council._fingerprint_head(repo), "unborn HEAD")
            self.assertEqual(council.staged_fingerprint(repo), policy_gate.staged_fingerprint(repo))
            self.assertEqual(council.diff_fingerprint(repo), council.diff_fingerprint(repo))


class MemberAuthTest(unittest.TestCase):
    def test_auth_failure_never_builds_or_launches_member(self):
        adapter = Mock()
        adapter.check_auth.side_effect = council.MemberUnavailable("Login required")
        with patch.dict(council.ADAPTERS, {"codex": Mock(return_value=adapter)}), \
                patch.object(council, "_run_plain") as plain, patch.object(council, "_run_pty") as pty:
            result = council.run_member("codex", Path("brief.md"), Path.cwd(), Path.cwd(), {}, 10)
        self.assertFalse(result.ok)
        self.assertTrue(result.auth_problem)
        self.assertIn("Login required", result.notes)
        adapter.check_auth.assert_called_once_with()
        adapter.build.assert_not_called()
        plain.assert_not_called()
        pty.assert_not_called()

    def test_cmd_run_reports_auth_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "brief.md"
            brief.write_text("review this")
            adapter = Mock()
            adapter.check_auth.side_effect = council.MemberUnavailable("Login required")
            args = argparse.Namespace(member="codex", brief=str(brief), workspace=tmp, timeout=10)
            output = io.StringIO()
            with patch.dict(council.ADAPTERS, {"codex": Mock(return_value=adapter)}), \
                    patch.object(council, "_run_plain") as plain, contextlib.redirect_stdout(output):
                code = council.cmd_run(args, {"members": {"codex": {"enabled": True}}, "timeout_seconds": 10}, "ask")
            self.assertEqual(code, 1)
            self.assertIn("codex: AUTH", output.getvalue())
            self.assertTrue(json.loads((Path(tmp) / "codex.meta.json").read_text())["auth_problem"])
            plain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
