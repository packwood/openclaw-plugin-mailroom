from __future__ import annotations

import json
import unittest
from pathlib import Path

from mailroom.scheduler import (
    PROFILE_JOB_DECLARATION_KEY,
    weekly_profile_cron_create_argv,
)


class SchedulerTests(unittest.TestCase):
    def test_weekly_job_is_idempotent_non_delivering_command_payload(self):
        argv = weekly_profile_cron_create_argv(
            shared_root="/tmp/shared",
            profiles_dir="/tmp/profiles",
        )
        self.assertEqual(argv[:3], ("openclaw", "cron", "create"))
        self.assertIn("--cron", argv)
        self.assertIn("--declaration-key", argv)
        self.assertEqual(
            argv[argv.index("--declaration-key") + 1], PROFILE_JOB_DECLARATION_KEY
        )
        self.assertIn("--no-deliver", argv)
        self.assertIn("--command-env", argv)
        command = json.loads(argv[argv.index("--command-argv") + 1])
        self.assertEqual(
            command[:5], ["python3", "-m", "mailroom.cli", "profiles", "generate"]
        )
        self.assertEqual(command[-1], "/tmp/profiles")
        self.assertEqual(
            argv[argv.index("--command-cwd") + 1],
            str(Path("/tmp/shared").resolve().parent),
        )


if __name__ == "__main__":
    unittest.main()
