import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.public_repo_guard import csv_problem, path_problem


class PublicationGuardTests(unittest.TestCase):
    def test_runtime_and_account_policy(self):
        for name in ("data/events/x.jsonl", "runtime/state/db.sqlite3", "webapp/auth_users.json",
                     "L3_trend/state/db.sqlite3", "spatial/.env.local", "data/v3/experiment.json"):
            self.assertIsNotNone(path_problem(name), name)
        for name in (".env.example", "docs/evaluations/results.json", "agent_v3/tests/fixture.json"):
            self.assertIsNone(path_problem(name), name)
        self.assertIsNotNone(csv_problem(b"title,user_name\ntest,\n"))
        self.assertIsNone(csv_problem(b"title,user_id_hash\ntest,hashed\n"))

    def test_guard_reads_index_even_when_ignored(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "public_repo_guard.py"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-q", folder], check=True)
            (root / ".gitignore").write_text("webapp/auth_users.json\n", encoding="utf-8")
            (root / "webapp").mkdir()
            (root / "webapp" / "auth_users.json").write_text("[]", encoding="utf-8")
            subprocess.run(["git", "add", "-f", "webapp/auth_users.json"], cwd=root, check=True)
            rejected = subprocess.run([sys.executable, str(script)], cwd=root, capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("webapp/auth_users.json", rejected.stdout)
            self.assertNotIn("[]", rejected.stdout)
