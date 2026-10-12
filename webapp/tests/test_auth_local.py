"""Fresh clones do not depend on a checked-in account file."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webapp.auth import initialize_user, verify_login


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "users.json"
        env = patch.dict(os.environ, {"TAPTAP_AUTH_USERS_FILE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def test_missing_accounts_fail_closed(self):
        self.assertIsNone(verify_login("owner", "any password"))
        self.assertFalse(self.path.exists())

    def test_initialized_password_is_salted_and_verified(self):
        initialize_user("owner", "operator", "test-only-password-2026")
        first = self.path.read_bytes()
        self.assertNotIn(b"test-only-password-2026", first)
        self.assertNotIn(b'"sha256"', first)
        self.assertEqual(verify_login("owner", "test-only-password-2026"),
                         {"actor": "owner", "role": "operator"})
        self.assertIsNone(verify_login("owner", "wrong"))
        with self.assertRaises(FileExistsError):
            initialize_user("other", "admin", "a-different-test-password")
        self.assertEqual(first, self.path.read_bytes())

    def test_invalid_configuration_cannot_authenticate(self):
        for encoded in ("pbkdf2_sha256$9999999999$aa$bb", "broken"):
            self.path.write_text(json.dumps([{"actor": "owner", "role": "admin",
                                              "password_hash": encoded}]), encoding="utf-8")
            self.assertIsNone(verify_login("owner", "password"))
        with self.assertRaises(ValueError):
            initialize_user("bad|name", "operator", "test-only-password-2026")
        with self.assertRaises(ValueError):
            initialize_user("owner", "operator", "short")
