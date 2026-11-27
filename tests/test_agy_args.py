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


if __name__ == "__main__":
    unittest.main()
