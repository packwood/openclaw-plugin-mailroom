"""Local secret and Maton connection resolution without persisting credentials."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path


DEFAULT_ENV = Path.home() / ".openclaw" / ".env"
DEFAULT_MAILROOM_SECRETS = Path.home() / ".openclaw" / "mailroom" / "secrets.env"
DEFAULT_CONNECTIONS = Path.home() / ".openclaw" / "shared" / "connections.json"
MAILROOM_PROVIDER_SECRETS = frozenset({"ANTHROPIC_API_KEY", "GEMINI_API_KEY"})


class MailroomSecretError(RuntimeError):
    """Raised when the isolated Mailroom secret store is unsafe or malformed."""


def env_value(name: str, env_path: str | Path = DEFAULT_ENV) -> str | None:
    path = Path(env_path).expanduser()
    if path.exists():
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            if key.strip() == name:
                return raw_value.strip().strip('"').strip("'") or None
    # ~/.openclaw/.env is authoritative for this install. Environment is only
    # a fallback because a stale launchd value can otherwise shadow the file.
    return os.environ.get(name) or None


def mailroom_secret_value(
    name: str,
    secret_path: str | Path = DEFAULT_MAILROOM_SECRETS,
) -> str | None:
    """Read one provider key only from Mailroom's owner-private secret file."""
    if name not in MAILROOM_PROVIDER_SECRETS:
        raise MailroomSecretError(f"Unsupported Mailroom secret name: {name}")
    path = Path(secret_path).expanduser()
    if not path.exists():
        return None
    if path.is_symlink():
        raise MailroomSecretError("Mailroom secret file must not be a symlink")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise MailroomSecretError("Mailroom secret path must be a regular file")
    if metadata.st_uid != os.getuid():
        raise MailroomSecretError("Mailroom secret file must be owned by this user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise MailroomSecretError("Mailroom secret file must not grant group/other access")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise MailroomSecretError("Mailroom secret file contains an invalid line")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in MAILROOM_PROVIDER_SECRETS:
            raise MailroomSecretError(
                f"Mailroom secret file contains unsupported key: {key}"
            )
        if key in values:
            raise MailroomSecretError(
                f"Mailroom secret file contains duplicate key: {key}"
            )
        value = raw_value.strip().strip('"').strip("'")
        if not value:
            raise MailroomSecretError(f"Mailroom secret is empty: {key}")
        values[key] = value
    return values.get(name)


def resolve_connection(
    mailbox: str,
    *,
    app: str = "outlook",
    path: str | Path = DEFAULT_CONNECTIONS,
) -> str:
    data = json.loads(Path(path).expanduser().read_text())
    connections = data.get("connections", data if isinstance(data, list) else [])
    matches = [
        item for item in connections
        if item.get("app") == app
        and (item.get("account") or "").lower() == mailbox.lower()
        and item.get("status", "ACTIVE") == "ACTIVE"
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one active {app} connection for {mailbox}; found {len(matches)}"
        )
    return matches[0]["connection_id"]
