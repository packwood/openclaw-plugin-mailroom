"""Versioned Agent Responsibility Profiles and atomic last-known-good storage."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, overload


PROFILE_SCHEMA_VERSION = 1
PROFILE_PROMPT_VERSION = "mailroom-agent-responsibility-profile-v1"
DEFAULT_PROFILE_DIR = Path.home() / ".openclaw" / "mailroom" / "responsibility-profiles"
DEFAULT_PROFILE_OVERRIDE_DIR = (
    Path.home() / ".openclaw" / "mailroom" / "responsibility-profile-overrides"
)


class ProfileValidationError(ValueError):
    """A generated or persisted profile does not satisfy the routing contract."""


class ProfileStoreError(RuntimeError):
    """A profile set could not be loaded or safely published."""


def _strict_keys(value: dict[str, Any], required: set[str], *, label: str) -> None:
    missing = required - set(value)
    extra = set(value) - required
    if missing or extra:
        details = []
        if missing:
            details.append("missing=" + ",".join(sorted(missing)))
        if extra:
            details.append("extra=" + ",".join(sorted(extra)))
        raise ProfileValidationError(
            f"{label} fields are invalid ({'; '.join(details)})"
        )


def _text(value: Any, *, label: str, max_chars: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileValidationError(f"{label} must be a non-empty string")
    cleaned = " ".join(value.split())
    if len(cleaned) > max_chars:
        raise ProfileValidationError(f"{label} exceeds {max_chars} characters")
    return cleaned


def _strings(value: Any, *, label: str, max_items: int = 100) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProfileValidationError(f"{label} must be an array")
    if len(value) > max_items:
        raise ProfileValidationError(f"{label} exceeds {max_items} items")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        cleaned = _text(item, label=f"{label}[{index}]", max_chars=500)
        key = cleaned.casefold()
        if key not in seen:
            result.append(cleaned)
            seen.add(key)
    return tuple(result)


def _agent_id(value: Any, *, label: str = "agent_id") -> str:
    agent_id = _text(value, label=label, max_chars=128).lower()
    if any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in agent_id
    ):
        raise ProfileValidationError("agent_id contains unsupported characters")
    return agent_id


@dataclass(frozen=True)
class SourceProvenance:
    agent_id: str
    relative_path: str
    category: str
    status: str
    size_bytes: int | None
    included_chars: int
    sha256: str | None
    truncated: bool
    detail: str | None = None

    FIELDS = {
        "agent_id",
        "relative_path",
        "category",
        "status",
        "size_bytes",
        "included_chars",
        "sha256",
        "truncated",
        "detail",
    }
    STATUSES = {"read", "missing", "unreadable", "binary", "unsafe", "truncated"}
    CATEGORIES = {"identity", "instructions", "memory", "context"}

    @classmethod
    def from_dict(cls, value: Any) -> "SourceProvenance":
        if not isinstance(value, dict):
            raise ProfileValidationError("source provenance must be an object")
        _strict_keys(value, cls.FIELDS, label="source provenance")
        agent_id = _text(
            value["agent_id"], label="source agent_id", max_chars=128
        ).lower()
        relative_path = _text(
            value["relative_path"], label="source relative_path", max_chars=1000
        )
        category = value["category"]
        status = value["status"]
        if category not in cls.CATEGORIES:
            raise ProfileValidationError("source category is invalid")
        if status not in cls.STATUSES:
            raise ProfileValidationError("source status is invalid")
        size_bytes = value["size_bytes"]
        if size_bytes is not None and (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            raise ProfileValidationError("source size_bytes is invalid")
        included_chars = value["included_chars"]
        if (
            isinstance(included_chars, bool)
            or not isinstance(included_chars, int)
            or included_chars < 0
        ):
            raise ProfileValidationError("source included_chars is invalid")
        sha256 = value["sha256"]
        if sha256 is not None and (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ProfileValidationError("source sha256 is invalid")
        if not isinstance(value["truncated"], bool):
            raise ProfileValidationError("source truncated must be boolean")
        detail = value["detail"]
        if detail is not None:
            detail = _text(detail, label="source detail", max_chars=500)
        return cls(
            agent_id=agent_id,
            relative_path=relative_path,
            category=category,
            status=status,
            size_bytes=size_bytes,
            included_chars=included_chars,
            sha256=sha256,
            truncated=value["truncated"],
            detail=detail,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "relative_path": self.relative_path,
            "category": self.category,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "included_chars": self.included_chars,
            "sha256": self.sha256,
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class NamedEntities:
    companies: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    counterparties: tuple[str, ...] = ()
    people: tuple[str, ...] = ()

    FIELDS = {"companies", "projects", "counterparties", "people"}

    @classmethod
    def from_dict(cls, value: Any) -> "NamedEntities":
        if not isinstance(value, dict):
            raise ProfileValidationError("named_entities must be an object")
        _strict_keys(value, cls.FIELDS, label="named_entities")
        return cls(
            **{
                field: _strings(value[field], label=f"named_entities.{field}")
                for field in sorted(cls.FIELDS)
            }
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "companies": list(self.companies),
            "projects": list(self.projects),
            "counterparties": list(self.counterparties),
            "people": list(self.people),
        }

    def values(self) -> tuple[str, ...]:
        return self.companies + self.projects + self.counterparties + self.people


@dataclass(frozen=True)
class AgentResponsibilityProfile:
    agent_id: str
    mission: str
    distinctive_specialties: tuple[str, ...]
    domains: tuple[str, ...]
    industries: tuple[str, ...]
    project_transaction_types: tuple[str, ...]
    functional_responsibilities: tuple[str, ...]
    named_entities: NamedEntities
    differentiating_signals: tuple[str, ...]
    shared_capabilities: tuple[str, ...]
    positive_routing_signals: tuple[str, ...]
    negative_routing_signals: tuple[str, ...]
    ambiguity_guidance: tuple[str, ...]
    sources: tuple[SourceProvenance, ...] = ()

    ROUTING_FIELDS = {
        "agent_id",
        "mission",
        "distinctive_specialties",
        "domains",
        "industries",
        "project_transaction_types",
        "functional_responsibilities",
        "named_entities",
        "differentiating_signals",
        "shared_capabilities",
        "positive_routing_signals",
        "negative_routing_signals",
        "ambiguity_guidance",
    }
    FIELDS = ROUTING_FIELDS | {"sources"}
    LIST_FIELDS = (
        "distinctive_specialties",
        "domains",
        "industries",
        "project_transaction_types",
        "functional_responsibilities",
        "differentiating_signals",
        "shared_capabilities",
        "positive_routing_signals",
        "negative_routing_signals",
        "ambiguity_guidance",
    )

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        sources: Iterable[SourceProvenance] | None = None,
        routing_only: bool = False,
    ) -> "AgentResponsibilityProfile":
        if not isinstance(value, dict):
            raise ProfileValidationError("agent profile must be an object")
        expected = cls.ROUTING_FIELDS if routing_only else cls.FIELDS
        _strict_keys(value, expected, label="agent profile")
        agent_id = _agent_id(value["agent_id"])
        fields = {
            field: _strings(value[field], label=field) for field in cls.LIST_FIELDS
        }
        if sources is None:
            raw_sources = value.get("sources", [])
            if not isinstance(raw_sources, list):
                raise ProfileValidationError("sources must be an array")
            parsed_sources = tuple(
                SourceProvenance.from_dict(item) for item in raw_sources
            )
        else:
            parsed_sources = tuple(sources)
        if any(source.agent_id != agent_id for source in parsed_sources):
            raise ProfileValidationError(
                f"profile {agent_id} contains provenance for another agent"
            )
        return cls(
            agent_id=agent_id,
            mission=_text(value["mission"], label="mission", max_chars=1000),
            named_entities=NamedEntities.from_dict(value["named_entities"]),
            sources=parsed_sources,
            **fields,
        )

    def to_dict(self, *, routing_only: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "agent_id": self.agent_id,
            "mission": self.mission,
            **{field: list(getattr(self, field)) for field in self.LIST_FIELDS},
            "named_entities": self.named_entities.to_dict(),
        }
        if not routing_only:
            value["sources"] = [source.to_dict() for source in self.sources]
        return value

    def specific_signals(self) -> frozenset[str]:
        values = (
            self.distinctive_specialties
            + self.domains
            + self.industries
            + self.project_transaction_types
            + self.functional_responsibilities
            + self.named_entities.values()
            + self.differentiating_signals
            + self.positive_routing_signals
        )
        return frozenset(item.casefold() for item in values)


@dataclass(frozen=True)
class GenerationMetadata:
    model: str
    generator_agent: str
    prompt_version: str
    corpus_provider: str
    run_id: str
    started_at: str
    completed_at: str
    fleet_refinement: bool

    FIELDS = {
        "model",
        "generator_agent",
        "prompt_version",
        "corpus_provider",
        "run_id",
        "started_at",
        "completed_at",
        "fleet_refinement",
    }

    @classmethod
    def from_dict(cls, value: Any) -> "GenerationMetadata":
        if not isinstance(value, dict):
            raise ProfileValidationError("generation metadata must be an object")
        _strict_keys(value, cls.FIELDS, label="generation metadata")
        if not isinstance(value["fleet_refinement"], bool):
            raise ProfileValidationError("fleet_refinement must be boolean")
        return cls(
            model=_text(value["model"], label="generation model", max_chars=200),
            generator_agent=_text(
                value["generator_agent"], label="generator_agent", max_chars=128
            ),
            prompt_version=_text(
                value["prompt_version"], label="prompt_version", max_chars=200
            ),
            corpus_provider=_text(
                value["corpus_provider"], label="corpus_provider", max_chars=200
            ),
            run_id=_text(value["run_id"], label="run_id", max_chars=200),
            started_at=_text(value["started_at"], label="started_at", max_chars=100),
            completed_at=_text(
                value["completed_at"], label="completed_at", max_chars=100
            ),
            fleet_refinement=value["fleet_refinement"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "generator_agent": self.generator_agent,
            "prompt_version": self.prompt_version,
            "corpus_provider": self.corpus_provider,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "fleet_refinement": self.fleet_refinement,
        }


@dataclass(frozen=True)
class FleetProfileSet:
    schema_version: int
    profile_set_id: str
    generated_at: str
    fleet_agent_ids: tuple[str, ...]
    profiles: tuple[AgentResponsibilityProfile, ...]
    generation: GenerationMetadata
    validation_status: str = "valid"

    FIELDS = {
        "schema_version",
        "profile_set_id",
        "generated_at",
        "fleet_agent_ids",
        "profiles",
        "generation",
        "validation_status",
    }

    @classmethod
    def build(
        cls,
        profiles: Iterable[AgentResponsibilityProfile],
        generation: GenerationMetadata,
        *,
        generated_at: str | None = None,
    ) -> "FleetProfileSet":
        ordered = tuple(sorted(profiles, key=lambda profile: profile.agent_id))
        if not ordered:
            raise ProfileValidationError("profile set must contain at least one agent")
        ids = tuple(profile.agent_id for profile in ordered)
        if len(ids) != len(set(ids)):
            raise ProfileValidationError("profile set contains duplicate agent IDs")
        canonical = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "fleet_agent_ids": list(ids),
            "profiles": [profile.to_dict() for profile in ordered],
            "generation_contract": {
                "model": generation.model,
                "prompt_version": generation.prompt_version,
                "corpus_provider": generation.corpus_provider,
            },
        }
        digest = hashlib.sha256(
            json.dumps(
                canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            schema_version=PROFILE_SCHEMA_VERSION,
            profile_set_id=f"arp-{digest[:24]}",
            generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
            fleet_agent_ids=ids,
            profiles=ordered,
            generation=generation,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "FleetProfileSet":
        if not isinstance(value, dict):
            raise ProfileValidationError("fleet profile set must be an object")
        _strict_keys(value, cls.FIELDS, label="fleet profile set")
        if value["schema_version"] != PROFILE_SCHEMA_VERSION:
            raise ProfileValidationError("unsupported profile schema version")
        if value["validation_status"] != "valid":
            raise ProfileValidationError("profile set is not marked valid")
        raw_profiles = value["profiles"]
        if not isinstance(raw_profiles, list):
            raise ProfileValidationError("profiles must be an array")
        generation = GenerationMetadata.from_dict(value["generation"])
        built = cls.build(
            (AgentResponsibilityProfile.from_dict(item) for item in raw_profiles),
            generation,
            generated_at=_text(
                value["generated_at"], label="generated_at", max_chars=100
            ),
        )
        raw_ids = value["fleet_agent_ids"]
        ids = _strings(raw_ids, label="fleet_agent_ids")
        if len(ids) != len(raw_ids):
            raise ProfileValidationError("fleet_agent_ids contain duplicates")
        if ids != built.fleet_agent_ids:
            raise ProfileValidationError(
                "fleet_agent_ids do not exactly match sorted profiles"
            )
        if value["profile_set_id"] != built.profile_set_id:
            raise ProfileValidationError(
                "profile_set_id does not match profile content"
            )
        return built

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_set_id": self.profile_set_id,
            "generated_at": self.generated_at,
            "fleet_agent_ids": list(self.fleet_agent_ids),
            "profiles": [profile.to_dict() for profile in self.profiles],
            "generation": self.generation.to_dict(),
            "validation_status": self.validation_status,
        }

    def profile(self, agent_id: str) -> AgentResponsibilityProfile:
        normalized = agent_id.casefold()
        for profile in self.profiles:
            if profile.agent_id.casefold() == normalized:
                return profile
        raise KeyError(agent_id)

    def routing_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_set_id": self.profile_set_id,
            "profiles": [
                profile.to_dict(routing_only=True) for profile in self.profiles
            ],
        }

    def shared_signal_values(self) -> frozenset[str]:
        explicitly_shared = {
            value.casefold()
            for profile in self.profiles
            for value in profile.shared_capabilities
        }
        counts: dict[str, int] = {}
        for profile in self.profiles:
            for value in profile.specific_signals():
                counts[value] = counts.get(value, 0) + 1
        # Repeated fleet-wide signals are non-discriminating even if a model
        # accidentally left them outside shared_capabilities.
        return frozenset(
            explicitly_shared | {value for value, count in counts.items() if count > 1}
        )


class ProfileOverrideStore:
    """User-authored partial profile overrides that survive regeneration."""

    OVERRIDABLE_FIELDS = (
        "mission",
        *AgentResponsibilityProfile.LIST_FIELDS,
        "named_entities",
    )
    FIELDS = {"agent_id", *OVERRIDABLE_FIELDS}

    def __init__(self, root: str | Path = DEFAULT_PROFILE_OVERRIDE_DIR):
        self.root = Path(root).expanduser()

    def path_for(self, agent_id: str) -> Path:
        return self.root / f"{_agent_id(agent_id)}.json"

    def list_agent_ids(self) -> tuple[str, ...]:
        if not self.root.exists():
            return ()
        return tuple(
            sorted(
                path.stem
                for path in self.root.glob("*.json")
                if path.is_file() and not path.name.startswith(".")
            )
        )

    def load(self, agent_id: str) -> dict[str, Any]:
        normalized = _agent_id(agent_id)
        path = self.path_for(normalized)
        if not path.exists():
            return {"agent_id": normalized}
        try:
            return self.validate_override(
                json.loads(path.read_text(encoding="utf-8")),
                agent_id=normalized,
            )
        except (OSError, json.JSONDecodeError, ProfileValidationError) as exc:
            raise ProfileStoreError(
                f"Profile override is unreadable or invalid: {normalized}"
            ) from exc

    def save(self, agent_id: str, override: dict[str, Any]) -> dict[str, Any]:
        normalized = _agent_id(agent_id)
        validated = self.validate_override(override, agent_id=normalized)
        if set(validated) == {"agent_id"}:
            self.delete(normalized)
            return validated
        payload = json.dumps(validated, indent=2, sort_keys=True) + "\n"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write(self.path_for(normalized), payload)
        return validated

    def delete(self, agent_id: str) -> None:
        path = self.path_for(agent_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def snapshot(self, agent_id: str) -> str | None:
        path = self.path_for(agent_id)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProfileStoreError(
                f"Profile override cannot be snapshotted: {_agent_id(agent_id)}"
            ) from exc

    def restore(self, agent_id: str, payload: str | None) -> None:
        normalized = _agent_id(agent_id)
        if payload is None:
            self.delete(normalized)
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write(self.path_for(normalized), payload)

    def validate_override(
        self, value: Any, *, agent_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProfileValidationError("profile override must be an object")
        extra = set(value) - self.FIELDS
        if extra:
            raise ProfileValidationError(
                "profile override fields are invalid (extra="
                + ",".join(sorted(extra))
                + ")"
            )
        if "agent_id" not in value and agent_id is None:
            raise ProfileValidationError("profile override is missing agent_id")
        normalized = _agent_id(value.get("agent_id", agent_id))
        if agent_id is not None and normalized != _agent_id(agent_id):
            raise ProfileValidationError("profile override agent_id does not match")
        cleaned: dict[str, Any] = {"agent_id": normalized}
        if "mission" in value:
            cleaned["mission"] = _text(value["mission"], label="mission", max_chars=1000)
        for field in AgentResponsibilityProfile.LIST_FIELDS:
            if field in value:
                cleaned[field] = list(_strings(value[field], label=field))
        if "named_entities" in value:
            named = value["named_entities"]
            if not isinstance(named, dict):
                raise ProfileValidationError("named_entities must be an object")
            extra_named = set(named) - NamedEntities.FIELDS
            if extra_named:
                raise ProfileValidationError(
                    "named_entities fields are invalid (extra="
                    + ",".join(sorted(extra_named))
                    + ")"
                )
            cleaned["named_entities"] = {
                field: list(_strings(named[field], label=f"named_entities.{field}"))
                for field in sorted(set(named) & NamedEntities.FIELDS)
            }
        return cleaned

    def apply(self, profile_set: FleetProfileSet, *, generated_at: str | None = None) -> FleetProfileSet:
        profiles_by_id = {profile.agent_id: profile for profile in profile_set.profiles}
        unknown = sorted(set(self.list_agent_ids()) - set(profiles_by_id))
        if unknown:
            raise ProfileValidationError(
                "profile override references unknown agent(s): " + ", ".join(unknown)
            )
        merged: list[AgentResponsibilityProfile] = []
        for profile in profile_set.profiles:
            override = self.load(profile.agent_id)
            value = profile.to_dict(routing_only=True)
            for field, replacement in override.items():
                if field == "agent_id":
                    continue
                if field == "named_entities":
                    current_named = dict(value["named_entities"])
                    current_named.update(replacement)
                    value["named_entities"] = current_named
                else:
                    value[field] = replacement
            merged.append(
                AgentResponsibilityProfile.from_dict(
                    value, routing_only=True, sources=profile.sources
                )
            )
        return FleetProfileSet.build(
            merged,
            profile_set.generation,
            generated_at=generated_at or profile_set.generated_at,
        )


@dataclass(frozen=True)
class ProfileDiff:
    previous_profile_set_id: str | None
    current_profile_set_id: str
    added_agents: tuple[str, ...]
    removed_agents: tuple[str, ...]
    changed_agents: tuple[str, ...]
    changes: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_profile_set_id": self.previous_profile_set_id,
            "current_profile_set_id": self.current_profile_set_id,
            "added_agents": list(self.added_agents),
            "removed_agents": list(self.removed_agents),
            "changed_agents": list(self.changed_agents),
            "changes": self.changes,
        }


def diff_profile_sets(
    previous: FleetProfileSet | None, current: FleetProfileSet
) -> ProfileDiff:
    before = (
        {
            profile.agent_id: profile.to_dict(routing_only=True)
            for profile in previous.profiles
        }
        if previous
        else {}
    )
    after = {
        profile.agent_id: profile.to_dict(routing_only=True)
        for profile in current.profiles
    }
    added = tuple(sorted(set(after) - set(before)))
    removed = tuple(sorted(set(before) - set(after)))
    changed = tuple(
        sorted(
            agent_id
            for agent_id in set(before) & set(after)
            if before[agent_id] != after[agent_id]
        )
    )
    changes: dict[str, dict[str, Any]] = {}
    for agent_id in (*added, *removed, *changed):
        changes[agent_id] = {
            "before": before.get(agent_id),
            "after": after.get(agent_id),
        }
    return ProfileDiff(
        previous_profile_set_id=previous.profile_set_id if previous else None,
        current_profile_set_id=current.profile_set_id,
        added_agents=added,
        removed_agents=removed,
        changed_agents=changed,
        changes=changes,
    )


@dataclass(frozen=True)
class PublishResult:
    profile_set_id: str
    changed: bool
    current_path: Path
    version_path: Path
    diff_path: Path | None
    diff: ProfileDiff


class ProfileStore:
    """Content-addressed profile versions with an atomically replaced current set."""

    def __init__(self, root: str | Path = DEFAULT_PROFILE_DIR):
        self.root = Path(root).expanduser()
        self.versions = self.root / "versions"
        self.diffs = self.root / "diffs"
        self.current_path = self.root / "current.json"
        self.generated_baseline_path = self.root / "generated-baseline.json"

    @overload
    def load_generated_baseline(
        self, *, required: Literal[True] = True
    ) -> FleetProfileSet: ...

    @overload
    def load_generated_baseline(
        self, *, required: Literal[False]
    ) -> FleetProfileSet | None: ...

    def load_generated_baseline(
        self, *, required: bool = True
    ) -> FleetProfileSet | None:
        if not self.generated_baseline_path.exists():
            if required:
                raise ProfileStoreError(
                    "No generated responsibility-profile baseline at "
                    f"{self.generated_baseline_path}"
                )
            return None
        try:
            return FleetProfileSet.from_dict(
                json.loads(self.generated_baseline_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ProfileValidationError) as exc:
            raise ProfileStoreError(
                "Generated responsibility-profile baseline is unreadable or invalid"
            ) from exc

    @overload
    def load_current(self, *, required: Literal[True] = True) -> FleetProfileSet: ...

    @overload
    def load_current(self, *, required: Literal[False]) -> FleetProfileSet | None: ...

    def load_current(self, *, required: bool = True) -> FleetProfileSet | None:
        if not self.current_path.exists():
            if required:
                raise ProfileStoreError(
                    f"No current responsibility profile set at {self.current_path}"
                )
            return None
        try:
            value = json.loads(self.current_path.read_text(encoding="utf-8"))
            return FleetProfileSet.from_dict(value)
        except (OSError, json.JSONDecodeError, ProfileValidationError) as exc:
            raise ProfileStoreError(
                "Current responsibility profile set is unreadable or invalid"
            ) from exc

    def load_version(self, profile_set_id: str) -> FleetProfileSet:
        if not profile_set_id.startswith("arp-") or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in profile_set_id
        ):
            raise ProfileStoreError("Invalid profile set identifier")
        path = self.versions / f"{profile_set_id}.json"
        try:
            return FleetProfileSet.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ProfileValidationError) as exc:
            raise ProfileStoreError(
                f"Profile version is unreadable or invalid: {profile_set_id}"
            ) from exc

    def publish(self, profile_set: FleetProfileSet) -> PublishResult:
        # Re-parse before any writes so only the exact serialized contract can publish.
        validated = FleetProfileSet.from_dict(profile_set.to_dict())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _exclusive_publish_lock(self.root / ".publish.lock"):
            return self._publish_locked(validated)

    def publish_generated(
        self,
        baseline: FleetProfileSet,
        effective: FleetProfileSet,
    ) -> PublishResult:
        """Publish a generated baseline and its override-merged effective set.

        The effective current file remains the runtime commit point. The baseline
        is written first under the same process lock so a successful return always
        leaves manual overrides reversible. A failed effective publication is
        detected by callers because current.json remains unchanged.
        """
        validated_baseline = FleetProfileSet.from_dict(baseline.to_dict())
        validated_effective = FleetProfileSet.from_dict(effective.to_dict())
        if validated_baseline.fleet_agent_ids != validated_effective.fleet_agent_ids:
            raise ProfileStoreError(
                "Generated baseline and effective profiles cover different fleets"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        baseline_payload = (
            json.dumps(
                validated_baseline.to_dict(),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        )
        with _exclusive_publish_lock(self.root / ".publish.lock"):
            _atomic_write(self.generated_baseline_path, baseline_payload)
            return self._publish_locked(validated_effective)

    def _publish_locked(self, validated: FleetProfileSet) -> PublishResult:
        previous = self.load_current(required=False)
        diff = diff_profile_sets(previous, validated)
        version_path = self.versions / f"{validated.profile_set_id}.json"
        if previous and previous.profile_set_id == validated.profile_set_id:
            return PublishResult(
                profile_set_id=validated.profile_set_id,
                changed=False,
                current_path=self.current_path,
                version_path=version_path,
                diff_path=None,
                diff=diff,
            )
        self.versions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.diffs.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = (
            json.dumps(
                validated.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
            )
            + "\n"
        )
        _atomic_write(version_path, payload)
        diff_name = f"{previous.profile_set_id if previous else 'none'}..{validated.profile_set_id}.json"
        diff_path = self.diffs / diff_name
        _atomic_write(
            diff_path,
            json.dumps(diff.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
        )
        # Current is the commit point. A failure above leaves the prior set active.
        _atomic_write(self.current_path, payload)
        return PublishResult(
            profile_set_id=validated.profile_set_id,
            changed=True,
            current_path=self.current_path,
            version_path=version_path,
            diff_path=diff_path,
            diff=diff,
        )


@contextmanager
def _exclusive_publish_lock(path: Path) -> Iterator[None]:
    """Serialize concurrent manual/scheduled publications on this host."""
    import fcntl

    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
