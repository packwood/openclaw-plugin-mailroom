"""Shared routing-owner policy for the Python cycle and OpenClaw callbacks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_POLICY_PATH = (
    Path.home() / ".openclaw" / "mailroom" / "routing-owner-policy.json"
)


class RoutingOwnerPolicyError(ValueError):
    pass


def _agent_ids(values: Iterable[object]) -> tuple[str, ...]:
    normalized = []
    for raw in values:
        value = str(raw).strip()
        if not value or len(value) > 64:
            continue
        if not value[0].isalnum() or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in value
        ):
            continue
        normalized.append(value)
    return tuple(sorted(set(normalized)))


@dataclass(frozen=True)
class RoutingOwnerPolicy:
    mode: str = "all"
    selected_agent_ids: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RoutingOwnerPolicy":
        configured = path or os.environ.get("MAILROOM_ROUTING_OWNER_POLICY")
        target = Path(configured).expanduser() if configured else DEFAULT_POLICY_PATH
        if not target.exists():
            return cls()
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RoutingOwnerPolicyError("Routing-owner policy is unreadable") from exc
        if value.get("schema_version") != 1 or value.get("mode") not in {
            "all", "selected"
        }:
            raise RoutingOwnerPolicyError("Routing-owner policy is invalid")
        selected = _agent_ids(value.get("selected_agent_ids", []))
        if value["mode"] == "selected" and not selected:
            raise RoutingOwnerPolicyError(
                "Selected routing-owner policy contains no valid agents"
            )
        return cls(mode=value["mode"], selected_agent_ids=selected)

    def resolve(self, available_agent_ids: Iterable[object]) -> tuple[str, ...]:
        available = _agent_ids(available_agent_ids)
        if self.mode == "all":
            return available
        unknown = sorted(set(self.selected_agent_ids) - set(available))
        if unknown:
            raise RoutingOwnerPolicyError(
                "Selected routing owners are not available in the current fleet: "
                + ", ".join(unknown)
            )
        return tuple(agent for agent in available if agent in self.selected_agent_ids)
