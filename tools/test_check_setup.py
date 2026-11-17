"""Synthetic behavior checks: python3 -B -m unittest discover -s tools."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import check_setup


class SetupChecks(unittest.TestCase):
    def test_project_launcher_default_override_and_exit(self):
        launcher = str(check_setup.REPO / "tools" / "with-env")
        env = dict(os.environ)
        env.pop("NATEMU_RAW_ROOT", None)
        command = ["sh", launcher, sys.executable, "-B", "-c",
                   "import os; print(os.environ['NATEMU_RAW_ROOT'])"]
        # The default belongs to the project, not the caller's current directory or where this checkout lives: build a
        # plain checkout and a linked worktree of it in a temporary folder. A plain checkout uses its own sibling
        # natemu-raw; a linked worktree uses the MAIN checkout's sibling; NATEMU_RAW_ROOT wins over both.
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            main = base / "main"
            (main / "tools").mkdir(parents=True)
            (main / "tools" / "with-env").write_bytes((check_setup.REPO / "tools" / "with-env").read_bytes())
            subprocess.run(git + ["init", "-q"], cwd=main, check=True)
            subprocess.run(git + ["add", "."], cwd=main, check=True)
            subprocess.run(git + ["commit", "-q", "-m", "x"], cwd=main, check=True)
            tree = base / "elsewhere" / "tree"
            subprocess.run(git + ["worktree", "add", "-q", "--detach", str(tree)], cwd=main, check=True)

            def default(root, **extra):
                run = subprocess.run(["sh", str(root / "tools" / "with-env"), sys.executable, "-B", "-c",
                                      "import os; print(os.environ['NATEMU_RAW_ROOT'])"],
                                     cwd=directory, env=dict(env, **extra), capture_output=True, text=True, check=True)
                return Path(run.stdout.strip()).resolve()
            self.assertEqual(default(main), base / "natemu-raw")
            self.assertEqual(default(tree), base / "natemu-raw")
            self.assertEqual(default(tree, NATEMU_RAW_ROOT=str(base / "x")), base / "x")
            env["NATEMU_RAW_ROOT"] = directory
            result = subprocess.run(command, env=env, capture_output=True,
                                    text=True, check=True)
            self.assertEqual(result.stdout.strip(), directory)
        result = subprocess.run(["sh", launcher, sys.executable, "-B", "-c",
                                 "raise SystemExit(7)"], env=env)
        self.assertEqual(result.returncode, 7)

    def test_resolved_output_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = base / "repo"
            repo.mkdir()
            (base / "alias").symlink_to(repo, target_is_directory=True)
            for root in (repo, repo / "new", base / "alias" / "new"):
                with self.assertRaises(ValueError):
                    check_setup.external_root(root, repo)
            self.assertEqual(check_setup.external_root(base / "raw", repo), base / "raw")
            self.assertEqual(check_setup.external_root(base / "repo-sibling", repo),
                             base / "repo-sibling")

    def test_target_status_parsing(self):
        self.assertEqual(check_setup.installed_targets(
            "aarch64-apple-darwin\naarch64-unknown-none (installed)\n"
            "x86_64-unknown-linux-gnu (available)\n\n"), set(check_setup.TARGETS))
        self.assertEqual(check_setup.installed_targets(""), set())

    def test_missing_command_and_failed_query(self):
        with patch.object(check_setup.subprocess, "run", side_effect=FileNotFoundError("missing")):
            output, error = check_setup.query(["missing"], time.monotonic() + 1)
        self.assertIsNone(output)
        self.assertIn("missing", error)
        with patch.object(check_setup.subprocess, "run", return_value=
                          subprocess.CompletedProcess(["tool"], 7, "", "unavailable")):
            output, error = check_setup.query(["tool"], time.monotonic() + 1)
        self.assertIsNone(output)
        self.assertIn("exit 7", error)

    def test_timeout_and_total_budget(self):
        output, error = check_setup.query(
            [sys.executable, "-B", "-c", "import time; time.sleep(2)"], time.monotonic() + 0.05)
        self.assertIsNone(output)
        self.assertEqual(error, "query timed out")
        with patch.object(check_setup.subprocess, "run") as run:
            output, error = check_setup.query(["unused"], time.monotonic() - 1)
        run.assert_not_called()
        self.assertIsNone(output)


if __name__ == "__main__":
    unittest.main()
