"""Declarative scheduler wiring for Mailroom responsibility-profile generation."""

from __future__ import annotations

import json
from pathlib import Path

from .responsibility_profiles import DEFAULT_PROFILE_DIR


PROFILE_JOB_DECLARATION_KEY = "mailroom-agent-responsibility-profiles-v1"
PROFILE_JOB_NAME = "Mailroom Agent Responsibility Profiles"
DEFAULT_WEEKLY_CRON = "30 3 * * 0"
DEFAULT_TIMEZONE = "America/New_York"


def weekly_profile_cron_create_argv(
    *,
    shared_root: str | Path,
    profiles_dir: str | Path = DEFAULT_PROFILE_DIR,
    schedule: str = DEFAULT_WEEKLY_CRON,
    timezone: str = DEFAULT_TIMEZONE,
) -> tuple[str, ...]:
    """Return an idempotent OpenClaw command-job declaration; never execute it."""
    root = Path(shared_root).expanduser().resolve()
    command = [
        "python3",
        "-m",
        "mailroom.cli",
        "profiles",
        "generate",
        "--profiles-dir",
        str(Path(profiles_dir).expanduser()),
    ]
    return (
        "openclaw",
        "cron",
        "create",
        "--cron",
        schedule,
        "--tz",
        timezone,
        "--name",
        PROFILE_JOB_NAME,
        "--description",
        "Weekly complete-fleet Mailroom responsibility-profile generation and atomic publication.",
        "--declaration-key",
        PROFILE_JOB_DECLARATION_KEY,
        "--command-argv",
        json.dumps(command, separators=(",", ":")),
        "--command-cwd",
        str(root.parent),
        "--command-env",
        f"PYTHONPATH={root / 'scripts'}",
        "--timeout-seconds",
        "7200",
        "--no-output-timeout-seconds",
        "1800",
        "--output-max-bytes",
        "65536",
        "--no-deliver",
        "--json",
    )
