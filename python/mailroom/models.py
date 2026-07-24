"""Mailroom domain models with no provider or database dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MailState(str, Enum):
    INGESTED = "INGESTED"
    ROUTING_REVIEW = "ROUTING_REVIEW"
    ROUTED = "ROUTED"
    DROPPED = "DROPPED"
    DRAFT_REQUESTED = "DRAFT_REQUESTED"
    DRAFTING = "DRAFTING"
    DRAFT_PROPOSED = "DRAFT_PROPOSED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    DEFERRED = "DEFERRED"
    DENIED_MESSAGE = "DENIED_MESSAGE"
    REPLIED_ELSEWHERE = "REPLIED_ELSEWHERE"
    OUTLOOK_DRAFTING = "OUTLOOK_DRAFTING"
    OUTLOOK_DRAFTED = "OUTLOOK_DRAFTED"
    SEND_APPROVAL_PENDING = "SEND_APPROVAL_PENDING"
    SENDING = "SENDING"
    SEND_ACCEPTED = "SEND_ACCEPTED"
    SENT_VERIFIED = "SENT_VERIFIED"
    SEND_OUTCOME_UNKNOWN = "SEND_OUTCOME_UNKNOWN"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Disposition(str, Enum):
    REPLY_REQUIRED = "reply_required"
    REVIEW_REQUIRED = "review_required"
    FYI = "fyi"
    NOISE = "noise"
    UNCLASSIFIED = "unclassified"


class TriageLevel(str, Enum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class IncomingMessage:
    mailbox: str
    provider_message_id: str
    conversation_id: str | None
    received_at: str | None
    sender_email: str | None
    sender_name: str | None
    subject: str | None
    body_preview: str | None
    body_content: str | None = None
    immutable_id: str | None = None
    internet_message_id: str | None = None
    content_hash: str | None = None
    folder: str = "inbox"
    is_read: bool | None = None
    has_attachments: bool | None = None
    intake_warnings: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDecision:
    draft_owner: str | None
    watchers: tuple[str, ...]
    confidence: float
    reasons: tuple[str, ...]
    priority: Priority = Priority.P3
    disposition: Disposition = Disposition.UNCLASSIFIED
    outcome: str = "UNMATCHED"
    importance: TriageLevel = TriageLevel.UNKNOWN
    urgency: TriageLevel = TriageLevel.UNKNOWN
    triage_action: str | None = None
    triage_model: str | None = None
    triage_confidence: float | None = None
    triage_rationale: str | None = None


@dataclass(frozen=True)
class IntakeEvent:
    event_type: str
    message: IncomingMessage | None = None
    mailbox: str | None = None
    provider_message_id: str | None = None
    removal_reason: str | None = None


@dataclass(frozen=True)
class IntakeBatch:
    events: tuple[IntakeEvent, ...]
    checkpoint: str | None = None
    previous_checkpoint: str | None = None
    adapter: str | None = None
    mailbox: str | None = None
    scope: str | None = None
