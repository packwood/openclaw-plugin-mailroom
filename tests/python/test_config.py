from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailroom.config import (
    MailroomSecretError,
    env_value,
    mailroom_secret_value,
    resolve_connection,
)


class ConfigTests(unittest.TestCase):
    def test_env_file_and_unique_active_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / ".env"
            env.write_text("# ignored\nMATON_API_KEY='secret-value'\n")
            connections = root / "connections.json"
            connections.write_text(json.dumps({"connections": [
                {"app": "outlook", "account": "operator@Example.com", "status": "ACTIVE", "connection_id": "one"},
                {"app": "outlook", "account": "operator@example.com", "status": "INACTIVE", "connection_id": "old"},
            ]}))
            with patch.dict("os.environ", {"MATON_API_KEY": "stale-value"}):
                self.assertEqual(env_value("MATON_API_KEY", env), "secret-value")
            self.assertEqual(resolve_connection("operator@example.com", path=connections), "one")

    def test_ambiguous_connection_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            connections = Path(temp) / "connections.json"
            connections.write_text(json.dumps({"connections": [
                {"app": "outlook", "account": "operator@example.com", "connection_id": "one"},
                {"app": "outlook", "account": "operator@example.com", "connection_id": "two"},
            ]}))
            with self.assertRaises(ValueError):
                resolve_connection("operator@example.com", path=connections)

    def test_mailroom_provider_secret_never_falls_back_to_global_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.env"
            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "global-key"},
                clear=False,
            ):
                self.assertIsNone(
                    mailroom_secret_value("GEMINI_API_KEY", missing)
                )

    def test_mailroom_secret_file_is_owner_private_and_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "secrets.env"
            path.write_text(
                "GEMINI_API_KEY=mailroom-gemini\n"
                "ANTHROPIC_API_KEY=mailroom-anthropic\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            self.assertEqual(
                mailroom_secret_value("GEMINI_API_KEY", path),
                "mailroom-gemini",
            )
            path.chmod(0o640)
            with self.assertRaisesRegex(MailroomSecretError, "group/other"):
                mailroom_secret_value("GEMINI_API_KEY", path)

    def test_mailroom_secret_file_rejects_unknown_or_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "secrets.env"
            path.write_text("OTHER_KEY=value\n", encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(MailroomSecretError, "unsupported key"):
                mailroom_secret_value("GEMINI_API_KEY", path)
            path.write_text(
                "GEMINI_API_KEY=one\nGEMINI_API_KEY=two\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MailroomSecretError, "duplicate key"):
                mailroom_secret_value("GEMINI_API_KEY", path)

if __name__ == "__main__":
    unittest.main()
