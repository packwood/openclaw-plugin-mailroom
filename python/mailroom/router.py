"""Deterministic deal router with explicit owner/watcher semantics."""

from __future__ import annotations

import json
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import Disposition, IncomingMessage, Priority, RouteDecision


DEFAULT_RULEPACK_DIR = Path(__file__).with_name("rulepacks")
AUTOMATED_SENDER_LOCAL_PARTS = {
    "mailer-daemon", "postmaster", "no-reply", "noreply", "do-not-reply", "donotreply",
}
AUTOMATED_SENDER_DOMAINS = {"fireflies.ai"}
AUTOMATED_SUBJECT_PREFIXES = (
    "automatic reply:", "undeliverable:", "out of office automatic reply:",
)
CALENDAR_RESPONSE_PREFIXES = ("accepted:", "declined:", "tentative:")
MEETING_RESPONSE_TYPES = {
    "meetingaccepted", "meetingtentativelyaccepted", "meetingdeclined",
}


@dataclass(frozen=True)
class RulePack:
    agent: str
    mailboxes: tuple[str, ...] = ()
    sender_emails: tuple[str, ...] = ()
    sender_domains: tuple[str, ...] = ()
    subject_terms: tuple[str, ...] = ()
    body_terms: tuple[str, ...] = ()
    critical_terms: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    threshold: int = 30
    watcher_threshold: int = 20
    base_priority: Priority = Priority.P2
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "RulePack":
        return cls(
            agent=data["agent"],
            mailboxes=tuple(x.lower() for x in data.get("mailboxes", [])),
            sender_emails=tuple(x.lower() for x in data.get("sender_emails", [])),
            sender_domains=tuple(x.lower().lstrip("@") for x in data.get("sender_domains", [])),
            subject_terms=tuple(x.lower() for x in data.get("subject_terms", [])),
            body_terms=tuple(x.lower() for x in data.get("body_terms", [])),
            critical_terms=tuple(x.lower() for x in data.get("critical_terms", [])),
            exclude_terms=tuple(x.lower() for x in data.get("exclude_terms", [])),
            threshold=int(data.get("threshold", 30)),
            watcher_threshold=int(data.get("watcher_threshold", 20)),
            base_priority=Priority(data.get("base_priority", "P2")),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class ScoredRule:
    pack: RulePack
    score: int
    reasons: tuple[str, ...]
    priority: Priority


class DeterministicRouter:
    def __init__(self, rulepacks: Iterable[RulePack]):
        self.rulepacks = tuple(rulepacks)

    @classmethod
    def from_directory(cls, directory: str | Path = DEFAULT_RULEPACK_DIR) -> "DeterministicRouter":
        packs = []
        for path in sorted(Path(directory).glob("*.json")):
            packs.append(RulePack.from_dict(json.loads(path.read_text())))
        return cls(packs)

    def route(self, message: IncomingMessage) -> RouteDecision:
        # Hard-noise rules use structured message type and exact sender/subject
        # patterns. Broad phrase matching here can silently discard human mail.
        noise = _global_noise_reasons(message)
        if noise:
            return RouteDecision(
                draft_owner=None, watchers=(), confidence=1.0,
                reasons=tuple(f"global-noise:{term}" for term in noise),
                priority=Priority.P3, disposition=Disposition.NOISE, outcome="DROPPED",
            )
        scored = sorted(
            (self._score(pack, message) for pack in self.rulepacks),
            key=lambda item: (-item.score, -_priority_rank(item.priority), item.pack.agent),
        )
        matched = [item for item in scored if item.score >= item.pack.threshold]
        if not matched:
            borderline = [item for item in scored if item.score >= item.pack.watcher_threshold]
            if not borderline:
                return RouteDecision(
                    draft_owner=None, watchers=(), confidence=0.0,
                    reasons=("NO_ROUTING_SIGNAL",), priority=Priority.P3,
                    disposition=Disposition.REVIEW_REQUIRED, outcome="UNMATCHED",
                )
            return RouteDecision(
                draft_owner=None,
                watchers=tuple(item.pack.agent for item in borderline),
                confidence=min(0.49, max((item.score for item in borderline), default=0) / 100),
                reasons=tuple(reason for item in borderline for reason in item.reasons),
                priority=max((item.priority for item in borderline), default=Priority.P3, key=_priority_rank),
                disposition=Disposition.REVIEW_REQUIRED,
                outcome="BORDERLINE",
            )

        owner = matched[0]
        watchers = tuple(
            item.pack.agent for item in scored[1:]
            if item.score >= item.pack.watcher_threshold and item.pack.agent != owner.pack.agent
        )
        confidence = min(0.99, max(0.5, owner.score / 100))
        return RouteDecision(
            draft_owner=owner.pack.agent,
            watchers=watchers,
            confidence=confidence,
            reasons=owner.reasons,
            priority=owner.priority,
            disposition=Disposition.REPLY_REQUIRED,
            outcome="ROUTED",
        )

    @staticmethod
    def _score(pack: RulePack, message: IncomingMessage) -> ScoredRule:
        mailbox = message.mailbox.lower()
        sender = (message.sender_email or "").lower()
        domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
        subject = (message.subject or "").lower()
        body = _current_message_text(message.body_content or message.body_preview or "").lower()
        combined = f"{subject}\n{body}\n{sender}"
        reasons: list[str] = []
        score = 0
        priority = pack.base_priority

        if pack.mailboxes and mailbox not in pack.mailboxes:
            return ScoredRule(pack, -1000, ("mailbox excluded",), Priority.P3)
        excluded = [term for term in pack.exclude_terms if _contains_term(combined, term)]
        if excluded:
            return ScoredRule(pack, -1000, tuple(f"excluded:{term}" for term in excluded), Priority.P3)
        if sender and sender in pack.sender_emails:
            score += 100
            reasons.append(f"sender:{sender}")
        if domain and domain in pack.sender_domains:
            score += 60
            reasons.append(f"domain:{domain}")
        subject_term = _best_term(subject, pack.subject_terms)
        if subject_term:
            score += 35
            reasons.append(f"subject:{subject_term}")
        body_term = _best_term(body, pack.body_terms)
        if body_term:
            score += 12
            reasons.append(f"body:{body_term}")
        critical_term = _best_term(combined, pack.critical_terms)
        if critical_term:
            score += 80
            priority = Priority.P0
            reasons.append(f"critical:{critical_term}")
        return ScoredRule(pack, score, tuple(reasons), priority)


def _priority_rank(priority: Priority) -> int:
    return {Priority.P3: 0, Priority.P2: 1, Priority.P1: 2, Priority.P0: 3}[priority]


def _contains_term(text: str, term: str) -> bool:
    """Match a phrase at word boundaries so short acronyms cannot hit substrings."""
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, flags=re.IGNORECASE) is not None


def _global_noise_reasons(message: IncomingMessage) -> list[str]:
    reasons: list[str] = []
    raw = message.raw if isinstance(message.raw, dict) else {}
    odata_type = str(raw.get("@odata.type") or "").lower()
    meeting_type = str(raw.get("meetingMessageType") or "").lower()
    if (
        odata_type.endswith("eventmessageresponse")
        or meeting_type in MEETING_RESPONSE_TYPES
    ):
        reasons.append(f"meeting-response:{meeting_type or 'event-message-response'}")

    subject = (message.subject or "").strip().lower()
    prefix = next((value for value in AUTOMATED_SUBJECT_PREFIXES if subject.startswith(value)), None)
    if prefix:
        reasons.append(f"automated-subject:{prefix}")
    calendar_prefix = next(
        (value for value in CALENDAR_RESPONSE_PREFIXES if subject.startswith(value)), None,
    )
    if calendar_prefix and _calendar_response_fallback(message):
        reasons.append(f"calendar-response-subject:{calendar_prefix}")

    sender = (message.sender_email or "").strip().lower()
    if "@" in sender:
        local_part, domain = sender.rsplit("@", 1)
        if local_part in AUTOMATED_SENDER_LOCAL_PARTS:
            reasons.append(f"automated-sender:{local_part}@")
        if any(domain == value or domain.endswith(f".{value}") for value in AUTOMATED_SENDER_DOMAINS):
            reasons.append(f"automated-domain:{domain}")
    return reasons


def _calendar_response_fallback(message: IncomingMessage) -> bool:
    """Recognize low-content responses when the provider erases Graph's derived type."""
    body = _current_message_text(message.body_content or message.body_preview or "").strip().lower()
    if not body:
        return True
    calendar_markers = ("when:", "where:", "organizer:", "attendees:")
    if any(marker in body for marker in calendar_markers):
        return True
    disclaimer_starts = (
        "this message contains privileged and confidential information",
        "this email contains privileged and confidential information",
        "this message and any attachments are confidential",
        "this email and any attachments are confidential",
    )
    return body.startswith(disclaimer_starts)


def _best_term(text: str, terms: tuple[str, ...]) -> str | None:
    matches = [term for term in terms if _contains_term(text, term)]
    return max(matches, key=len) if matches else None


def _current_message_text(content: str) -> str:
    """Remove common reply/forward history before deterministic classification."""
    markers = (
        r"<blockquote\b",
        r"<div[^>]+(?:id|class)=[\"'][^\"']*(?:divRplyFwdMsg|gmail_quote)[^\"']*[\"']",
        r"-----\s*Original Message\s*-----",
        r"(?:^|\n)From:\s.*\nSent:\s",
    )
    current = content
    positions = []
    for marker in markers:
        match = re.search(marker, current, flags=re.IGNORECASE | re.DOTALL)
        if match:
            positions.append(match.start())
    if positions:
        current = current[:min(positions)]
    current = re.sub(r"<(?:head|style|script)\b[^>]*>.*?</(?:head|style|script)>", " ", current, flags=re.IGNORECASE | re.DOTALL)
    current = re.sub(r"<[^>]+>", " ", current)
    return " ".join(html.unescape(current).split())
