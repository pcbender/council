"""agy read mode: a linked worktree also gets the main checkout as a read-only dir."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import council  # noqa: E402


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class AgyReadDirsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = (root / "repo").resolve()
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q")
        sh(self.repo, "git", "config", "user.email", "t@example.com")
        sh(self.repo, "git", "config", "user.name", "t")
        (self.repo / "a.txt").write_text("a\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "init")
        self.wt = (root / "wt").resolve()
        sh(self.repo, "git", "worktree", "add", "-q", str(self.wt))
        self.prompt = root / "run" / "prompt.md"
        self.prompt.parent.mkdir()
        self.prompt.write_text("brief")
        self.adapter = council.GeminiAgyAdapter({"executable": sys.executable})

    def tearDown(self):
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.wt)],
                       capture_output=True)
        self.tmp.cleanup()

    def dirs(self, workspace):
        argv, _ = self.adapter.build(self.prompt, workspace, self.prompt.parent / "out.md")
        self.assertEqual(argv[-2:], ["--mode", "plan"])
        return [Path(argv[i + 1]).resolve() for i, a in enumerate(argv) if a == "--add-dir"]

    def test_worktree_adds_main_checkout(self):
        self.assertEqual(council.main_checkout(self.wt), self.repo)
        self.assertEqual(self.dirs(self.wt), [self.wt, self.prompt.parent.resolve(), self.repo])

    def test_main_checkout_adds_nothing_extra(self):
        self.assertIsNone(council.main_checkout(self.repo))
        self.assertEqual(self.dirs(self.repo), [self.repo, self.prompt.parent.resolve()])

    def test_write_mode_unchanged(self):
        argv, _ = self.adapter.build(self.prompt, self.wt, self.prompt.parent / "out.md", write=True)
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertEqual(dirs, [str(self.wt)])

    def test_tier_model_and_effort_flags_across_adapters(self):
        out = self.prompt.parent / "out.md"
        # Gemini (agy)
        argv, _ = self.adapter.build(self.prompt, self.repo, out, model="gemini-pro", effort="high")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-pro")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

        # Codex
        codex = council.CodexAdapter({"executable": sys.executable})
        c_argv, stdin_text = codex.build(self.prompt, self.repo, out, write=True, model="o3", effort="high")
        self.assertEqual(stdin_text, "brief")
        self.assertEqual(c_argv[c_argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(c_argv[c_argv.index("--model") + 1], "o3")
        self.assertIn('model_reasoning_effort="high"', c_argv)

        # Claude read & confined write mode
        claude = council.ClaudeAdapter({"executable": sys.executable})
        cl_read, _ = claude.build(self.prompt, self.wt, out, write=False, model="sonnet", effort="medium")
        self.assertEqual(cl_read[cl_read.index("--permission-mode") + 1], "plan")
        read_dirs = [Path(cl_read[i + 1]).resolve() for i, a in enumerate(cl_read) if a == "--add-dir"]
        self.assertIn(self.repo, read_dirs)

        cl_write, _ = claude.build(self.prompt, self.wt, out, write=True, model="opus", effort="high")
        self.assertIn("--restricted", cl_write)
        self.assertEqual(cl_write[cl_write.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cl_write[cl_write.index("--permission-prompts") + 1], "none")
        write_dirs = [cl_write[i + 1] for i, a in enumerate(cl_write) if a == "--add-dir"]
        self.assertEqual(write_dirs, [str(self.wt)])
        self.assertEqual(cl_write[cl_write.index("--model") + 1], "opus")
        self.assertEqual(cl_write[cl_write.index("--effort") + 1], "high")

    def test_resolve_tier_precedence(self):
        cfg = {
            "model": "base-model",
            "tiers": {
                "fast": {"model": "fast-model", "effort": "low"},
                "deep": {"model": None, "effort": "high"},
            },
        }
        self.assertEqual(council.resolve_tier(cfg), ("base-model", None))
        self.assertEqual(council.resolve_tier(cfg, tier="fast"), ("fast-model", "low"))
        self.assertEqual(council.resolve_tier(cfg, tier="deep"), ("base-model", "high"))
        self.assertEqual(council.resolve_tier(cfg, tier="fast", model="custom", effort="max"),
                         ("custom", "max"))


if __name__ == "__main__":
    unittest.main()
