"""Tests for the thin stdlib Ollama delegate worker and path confinement."""
from __future__ import annotations

import argparse
import contextlib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import council  # noqa: E402


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class MockOllamaServer:
    def __init__(self, models: list[str], warm: list[str], scripted_turns: list[dict]):
        self.models = models
        self.warm = warm
        self.turns = list(scripted_turns)
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/api/tags":
                    body = {"models": [{"name": m} for m in outer.models]}
                elif self.path == "/api/ps":
                    body = {"models": [{"name": m} for m in outer.warm]}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(payload)
                reply = outer.turns.pop(0) if outer.turns else {"content": "Done"}
                raw = json.dumps({"message": {"role": "assistant", **reply}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class OllamaWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_env = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR", "COUNCIL_STATE_PATH")}
        (root / "elsewhere").mkdir()
        for k in ("TEMP", "TMP", "TMPDIR"):
            os.environ[k] = str(root / "elsewhere")
        os.environ["COUNCIL_STATE_PATH"] = str(root / "state" / "state.json")

        self.repo = root / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@t")
        sh(self.repo, "git", "config", "user.name", "t")
        (self.repo / ".gitignore").write_text(".council/\n__pycache__/\n")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "math_util.py").write_text("def add(a, b):\n    return a - b\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "init")
        sh(self.repo, "git", "checkout", "-q", "-b", "agy/work")

        self.run_dir = self.repo / ".council" / "runs" / "r1"
        self.run_dir.mkdir(parents=True)
        self.cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.cwd)
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        self.tmp.cleanup()

    def test_ollama_task_loop_with_model_fallback_and_fixed_test_runner(self):
        turns = [
            {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "list_files", "arguments": {"directory": "."}}},
                    {"function": {"name": "read_file", "arguments": {"path": "src/math_util.py"}}},
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "edit_file",
                            "arguments": {
                                "path": "src/math_util.py",
                                "old_text": "return a - b",
                                "new_text": "return a + b",
                            },
                        }
                    },
                    {
                        "function": {
                            "name": "write_file",
                            "arguments": {
                                "path": "tests/test_math.py",
                                "content": (
                                    "import unittest\n"
                                    "from src.math_util import add\n"
                                    "class T(unittest.TestCase):\n"
                                    "    def test_add(self):\n"
                                    "        self.assertEqual(add(2, 3), 5)\n"
                                ),
                            },
                        }
                    },
                ],
            },
            {
                "content": "",
                "tool_calls": [{"function": {"name": "run_tests", "arguments": {}}}],
            },
            {
                "content": "## Changed\n- Fixed add and added test\n## Tested\n- unittest OK\n## Unsure\n- nothing"
            },
        ]
        # Configured tier wants qwen2.5-coder:14b, machine only has qwen3.5:9b
        srv = MockOllamaServer(models=["qwen3.5:9b"], warm=["qwen3.5:9b"], scripted_turns=turns)
        self.addCleanup(srv.close)

        brief = self.run_dir / "task.md"
        brief.write_text(
            "# Fix add\n\n"
            "## Allowed paths\n- src/**\n- tests/**\n\n"
            f'## Test command\n`"{sys.executable}" -m unittest discover -s tests`\n',
            encoding="utf-8",
        )
        config = council.load_config()
        config["delegates"]["ollama"]["base_url"] = srv.base_url

        args = argparse.Namespace(member="ollama", brief=str(brief), task_id=None, base=None,
                                  workspace=None, tier="standard", model=None, effort=None, timeout=60)
        with contextlib.redirect_stdout(io.StringIO()):
            code = council.cmd_task(args, config)
        self.assertEqual(code, 0)

        rec = json.loads((self.run_dir / "ollama.task.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["status"], "DONE", rec)
        self.assertEqual(rec["model"], "qwen3.5:9b")
        self.assertEqual(sorted(rec["files"]), ["src/math_util.py", "tests/test_math.py"])
        # Verify run_tests actually executed and passed in the tool message sent back to Ollama
        last_req_msgs = srv.requests[-1]["messages"]
        tool_msgs = [m for m in last_req_msgs if m.get("role") == "tool" and m.get("name") == "run_tests"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("Exit code: 0", tool_msgs[0]["content"])
        # Main checkout remains completely untouched
        self.assertIn("return a - b", (self.repo / "src" / "math_util.py").read_text())

    def test_ollama_pre_write_confinement_blocks_escapes_and_protected_paths(self):
        turns = [
            {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "write_file", "arguments": {"path": "../../../escape.txt", "content": "x"}}},
                    {"function": {"name": "write_file", "arguments": {"path": ".github/workflows/ci.yml", "content": "x"}}},
                    {"function": {"name": "write_file", "arguments": {"path": "CLAUDE.md::$DATA", "content": "x"}}},
                    {"function": {"name": "write_file", "arguments": {"path": ".claude /x", "content": "x"}}},
                    {"function": {"name": "write_file", "arguments": {"path": "docs/out_of_bounds.md", "content": "x"}}},
                    {"function": {"name": "read_file", "arguments": {"path": ".git"}}},
                    {"function": {"name": "read_file", "arguments": {"path": ".env"}}},
                    {"function": {"name": "read_file", "arguments": {"path": "src/math_util.py", "start_line": "bad"}}},
                    {"function": {"name": "write_file", "arguments": "[]"}},
                    {"function": {"name": "write_file", "arguments": {"path": "src/ok.py", "content": "x = 1\n"}}},
                ],
            },
            {"content": "## Changed\n- src/ok.py\n## Tested\n- not run\n## Unsure\n- nothing"},
        ]
        srv = MockOllamaServer(models=["qwen3.5:9b"], warm=[], scripted_turns=turns)
        self.addCleanup(srv.close)

        brief = self.run_dir / "task.md"
        brief.write_text("# Task\n\n## Allowed paths\n- src/**\n", encoding="utf-8")
        config = council.load_config()
        config["delegates"]["ollama"]["base_url"] = srv.base_url

        args = argparse.Namespace(member="ollama", brief=str(brief), task_id=None, base=None,
                                  workspace=None, tier="fast", model=None, effort=None, timeout=60)
        with contextlib.redirect_stdout(io.StringIO()):
            code = council.cmd_task(args, config)
        self.assertEqual(code, 0)

        rec = json.loads((self.run_dir / "ollama.task.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["status"], "DONE")
        self.assertEqual(rec["files"], ["src/ok.py"])
        tool_replies = [m["content"] for m in srv.requests[-1]["messages"] if m.get("role") == "tool"]
        self.assertIn("escapes the worktree", tool_replies[0])
        self.assertIn("protected path", tool_replies[1])
        self.assertIn("disallowed stream/alias syntax", tool_replies[2])
        self.assertIn("disallowed stream/alias syntax", tool_replies[3])
        self.assertIn("outside the brief's Allowed paths", tool_replies[4])
        self.assertIn("accessing .git is forbidden", tool_replies[5])
        self.assertIn("protected path", tool_replies[6])
        self.assertIn("must be integers", tool_replies[7])
        self.assertIn("must be an object", tool_replies[8])
        self.assertIn("Wrote 1 lines to src/ok.py", tool_replies[9])

    def test_ollama_cannot_vote_in_consult_or_gate(self):
        brief = self.run_dir / "brief.md"
        brief.write_text("How should we build X?", encoding="utf-8")
        config = council.load_config()
        args = argparse.Namespace(member="ollama", brief=str(brief), workspace=str(self.repo),
                                  chairman="gemini", tier=None, model=None, effort=None, timeout=10)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(council.cmd_run(args, config, "consult"), 2)
            self.assertEqual(council.cmd_run(args, config, "review"), 2)

        # Even if a bogus ollama.verdict.json exists, gate ignores it and requires voting peers
        fp = council.diff_fingerprint(self.repo)
        (self.run_dir / "ollama.verdict.json").write_text(
            json.dumps({"member": "ollama", "verdict": "pass", "fingerprint": fp, "findings": []})
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = council.cmd_gate(argparse.Namespace(dir=str(self.run_dir), workspace=str(self.repo),
                                                       chairman="gemini", author="ollama", require=None), config)
        self.assertEqual(code, 1)
        gate = json.loads((self.run_dir / "gate.json").read_text())
        self.assertEqual(sorted(gate["required"]), ["claude", "codex"])
        self.assertNotIn("ollama", gate["members"])


if __name__ == "__main__":
    unittest.main()
