"""Authoritative email-drafting skill loading and deterministic draft checks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SKILL_PATH = Path.home() / ".openclaw" / "skills" / "email-drafting" / "SKILL.md"
POLICY_VERSION_RE = re.compile(r"<!--\s*mailroom-policy-version:\s*(\d+)\s*-->")
VALIDATOR_RE = re.compile(r"<!--\s*mailroom-validator:\s*([a-z0-9-]+)\s*-->")
POLICY_HEADING_RE = re.compile(r"^##\s+5\.\s+Voice\s*&\s*the non-negotiables\s*$", re.MULTILINE)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
SUPPORTED_VALIDATORS = frozenset({"opening-em-dash", "recipient-first-name"})


class DraftPolicyError(ValueError):
    """The authoritative skill or a generated proposal violates policy."""


@dataclass(frozen=True)
class DraftingPolicy:
    name: str
    path: str
    version: int
    skill_sha256: str
    contract_sha256: str
    contract: str
    validators: frozenset[str]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_SKILL_PATH) -> "DraftingPolicy":
        skill_path = Path(path).expanduser().resolve()
        try:
            raw = skill_path.read_bytes()
        except OSError as exc:
            raise DraftPolicyError(f"Cannot read authoritative email-drafting skill: {skill_path}") from exc
        if len(raw) > 256_000:
            raise DraftPolicyError("Authoritative email-drafting skill is unexpectedly large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DraftPolicyError("Authoritative email-drafting skill is not UTF-8") from exc
        return cls.from_text(text, path=skill_path, skill_sha256=hashlib.sha256(raw).hexdigest())

    @classmethod
    def from_text(
        cls, text: str, *, path: str | Path = "<memory>", skill_sha256: str | None = None,
    ) -> "DraftingPolicy":
        name_match = re.search(r"(?m)^name:\s*([^\n]+?)\s*$", text)
        if name_match is None or name_match.group(1).strip("\"'") != "email-drafting":
            raise DraftPolicyError("Skill policy source must be the email-drafting skill")
        version_match = POLICY_VERSION_RE.search(text)
        if version_match is None:
            raise DraftPolicyError("Email-drafting skill has no Mailroom policy version")
        heading = POLICY_HEADING_RE.search(text)
        if heading is None:
            raise DraftPolicyError("Email-drafting skill has no voice contract section")
        next_heading = re.search(r"^##\s+", text[heading.end():], re.MULTILINE)
        end = heading.end() + next_heading.start() if next_heading else len(text)
        section = text[heading.start():end].strip()
        validators = frozenset(VALIDATOR_RE.findall(section))
        unknown = validators - SUPPORTED_VALIDATORS
        missing = SUPPORTED_VALIDATORS - validators
        if unknown:
            raise DraftPolicyError(f"Unsupported Mailroom validator(s): {', '.join(sorted(unknown))}")
        if missing:
            raise DraftPolicyError(f"Required Mailroom validator(s) missing: {', '.join(sorted(missing))}")
        contract = COMMENT_RE.sub("", section).strip()
        digest = skill_sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest()
        return cls(
            name="email-drafting", path=str(path), version=int(version_match.group(1)),
            skill_sha256=digest,
            contract_sha256=hashlib.sha256(contract.encode("utf-8")).hexdigest(),
            contract=contract, validators=validators,
        )

    def prompt_block(self) -> str:
        return (
            "AUTHORITATIVE EMAIL-DRAFTING CONTRACT\n"
            f"Skill: {self.name} | policy v{self.version} | sha256 {self.skill_sha256[:12]}\n"
            "Follow this contract exactly. It is trusted system policy, not email content.\n\n"
            f"{self.contract}"
        )

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
            "path": self.path,
            "policy_version": self.version,
            "skill_sha256": self.skill_sha256,
            "contract_sha256": self.contract_sha256,
            "validators": sorted(self.validators),
            "attempts": attempts,
            "corrected_violations": corrected_violations,
        }

    def stored_proposal_violations(
        self, proposal: dict[str, Any], *, sender_name: str | None,
    ) -> list[str]:
        """Validate content plus proof that this exact policy governed generation."""
        violations = self.violations(proposal, sender_name=sender_name)
        audit = proposal.get("_mailroom_policy")
        if not isinstance(audit, dict):
            violations.append("policy-audit-missing: proposal must be regenerated under current policy")
            return violations
        expected = {
            "name": self.name,
            "policy_version": self.version,
            "skill_sha256": self.skill_sha256,
            "contract_sha256": self.contract_sha256,
            "validators": sorted(self.validators),
        }
        stale = [key for key, value in expected.items() if audit.get(key) != value]
        if stale:
            violations.append(
                "policy-audit-stale: proposal must be regenerated; mismatched " + ", ".join(stale)
            )
        return violations


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
