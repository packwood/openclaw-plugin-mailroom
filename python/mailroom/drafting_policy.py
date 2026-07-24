"""Workflow-neutral deterministic checks for Mailroom draft proposals.

The responsible OpenClaw agent owns email workflow selection. This module must
not load or depend on any named skill: an operator can replace or reorganize the
agent's email workflow without requiring a Mailroom change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class DraftPolicyError(ValueError):
    """A generated proposal violates a workflow-neutral Mailroom safety check."""


@dataclass(frozen=True)
class DraftingPolicy:
    """Backward-compatible name for Mailroom's internal draft quality gate."""

    name: str = "mailroom-draft-quality"
    version: int = 2
    validators: frozenset[str] = frozenset({
        "opening-em-dash",
        "recipient-first-name",
    })

    @classmethod
    def load(cls) -> "DraftingPolicy":
        return cls()

    def violations(self, proposal: dict[str, Any], *, sender_name: str | None) -> list[str]:
        if proposal.get("decision") == "no_reply":
            return []
        reply = proposal.get("reply_text")
        if not isinstance(reply, str) or not reply.strip():
            return ["missing-reply-text: reply_text must be a non-empty string"]
        violations: list[str] = []
        if _is_placeholder_reply(reply):
            violations.append(
                "placeholder-reply-text: reply_text must be a substantive email response, not placeholder text"
            )
        opening = next((line.strip() for line in reply.splitlines() if line.strip()), "")
        if "opening-em-dash" in self.validators and "—" in opening:
            violations.append("opening-em-dash: the first non-empty line must not contain an em dash")
        if "recipient-first-name" in self.validators:
            candidate = _opening_name(opening)
            if candidate is not None and not _name_is_supported(candidate, sender_name):
                violations.append(
                    "recipient-first-name: opening name "
                    f"{candidate!r} is not supported by sender display name {sender_name!r}"
                )
        return violations

    def audit_metadata(self, *, attempts: int, corrected_violations: list[str]) -> dict[str, Any]:
        return {
            "name": self.name,
            "quality_gate_version": self.version,
            "validators": sorted(self.validators),
            "attempts": attempts,
            "corrected_violations": corrected_violations,
        }

    def stored_proposal_violations(
        self, proposal: dict[str, Any], *, sender_name: str | None,
    ) -> list[str]:
        """Revalidate current proposal content without coupling to agent skills."""
        return self.violations(proposal, sender_name=sender_name)


_NON_NAME_OPENERS = frozenset({
    "agreed", "all", "everyone", "folks", "good", "great", "no", "perfect",
    "sounds", "team", "thank", "thanks", "understood", "yes",
})

_PLACEHOLDER_REPLIES = frozenset({
    "draft", "insert reply", "insert response", "n/a", "na", "placeholder",
    "response", "tbd", "todo",
})


def _is_placeholder_reply(reply: str) -> bool:
    normalized = " ".join(reply.casefold().split()).strip(" .…")
    if normalized in _PLACEHOLDER_REPLIES:
        return True
    return re.fullmatch(r"[\s.…·•*_\-–—]+", reply) is not None


def _opening_name(opening: str) -> str | None:
    name = r"([^\s,!—:;]+)"
    suffix = r"(?:\s+and\s+[^,!—:;]+)?\s*[,!;:—-]"
    patterns = (
        rf"^(?:hi|hello|dear|hey)\s+{name}{suffix}",
        rf"^(?:(?:good\s+)?(?:morning|afternoon|evening))[,!]?\s+{name}{suffix}",
        rf"^{name}{suffix}",
    )
    match = next(
        (match for pattern in patterns if (match := re.match(pattern, opening, re.IGNORECASE))),
        None,
    )
    if match is None:
        return None
    candidate = match.group(1).strip(". ")
    if not candidate or candidate.casefold() in _NON_NAME_OPENERS:
        return None
    return candidate


def _name_is_supported(candidate: str, sender_name: str | None) -> bool:
    if not sender_name:
        return False
    sender_tokens = {
        token.casefold()
        for token in re.findall(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", sender_name, re.UNICODE)
    }
    return candidate.casefold() in sender_tokens
