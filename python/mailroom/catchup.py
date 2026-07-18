"""Checkpoint-isolated historical Mailroom candidate scanning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .intake import MailIntake
from .ledger import MailroomLedger
from .models import IncomingMessage
from .reply_guard import ReplyChecker
from .router import DeterministicRouter


@dataclass(frozen=True)
class CatchupCandidate:
    owner: str
    mailbox: str
    provider_message_id: str
    conversation_id: str | None
    received_at: str | None
    sender_email: str | None
    sender_name: str | None
    subject: str | None
    priority: str
    reasons: tuple[str, ...]
    ledger_status: str
    existing_state: str | None
    existing_run_mode: str | None


class CatchupScanner:
    """Scan historical mail without saving a delta checkpoint or mutating the ledger."""

    def __init__(
        self,
        ledger: MailroomLedger,
        intake: MailIntake,
        router: DeterministicRouter,
        reply_checker: ReplyChecker,
        *,
        owners: Iterable[str],
    ):
        self.ledger = ledger
        self.intake = intake
        self.router = router
        self.reply_checker = reply_checker
        self.owners = frozenset(owners)

    def run(
        self,
        *,
        apply_senders: Iterable[str] = (),
        confirm_count: int | None = None,
    ) -> dict[str, Any]:
        requested_senders = frozenset(sender.lower() for sender in apply_senders)
        if requested_senders and confirm_count is None:
            raise ValueError("Catch-up apply requires confirm_count")
        if confirm_count is not None and not requested_senders:
            raise ValueError("confirm_count is only valid with apply_senders")
        batch = self.intake.poll()
        routed: list[tuple[IncomingMessage, Any]] = []
        scanned = removed = timestamp_quarantined = 0
        for event in batch.events:
            if event.event_type == "removed":
                removed += 1
                continue
            if event.event_type != "upsert" or event.message is None:
                continue
            scanned += 1
            if event.message.intake_warnings:
                timestamp_quarantined += 1
                continue
            decision = self.router.route(event.message)
            if decision.outcome == "ROUTED" and decision.draft_owner in self.owners:
                routed.append((event.message, decision))

        latest: dict[tuple[str, str], tuple[IncomingMessage, Any]] = {}
        for message, decision in routed:
            key = (
                message.mailbox.lower(),
                message.conversation_id or message.provider_message_id,
            )
            current = latest.get(key)
            if current is None or _timestamp(message.received_at) > _timestamp(current[0].received_at):
                latest[key] = (message, decision)

        candidates: list[CatchupCandidate] = []
        promotable: list[tuple[CatchupCandidate, IncomingMessage, Any]] = []
        suppressed_replied: list[dict[str, Any]] = []
        already_production: list[dict[str, Any]] = []
        for message, decision in sorted(
            latest.values(), key=lambda pair: _timestamp(pair[0].received_at), reverse=True,
        ):
            existing = self.ledger.find_existing_message(message)
            if existing and existing.get("run_mode") == "production":
                already_production.append(_brief(message, decision, existing))
                continue
            reply = self.reply_checker.find_reply_after({
                "conversation_id": message.conversation_id,
                "received_at": message.received_at,
                "sender_email": message.sender_email,
            })
            if reply is not None:
                item = _brief(message, decision, existing)
                item.update({"sent_message_id": reply.message_id, "sent_at": reply.sent_at})
                suppressed_replied.append(item)
                continue
            candidate = CatchupCandidate(
                owner=decision.draft_owner,
                mailbox=message.mailbox,
                provider_message_id=message.provider_message_id,
                conversation_id=message.conversation_id,
                received_at=message.received_at,
                sender_email=message.sender_email,
                sender_name=message.sender_name,
                subject=message.subject,
                priority=decision.priority.value,
                reasons=decision.reasons,
                ledger_status="shadow_existing" if existing else "new",
                existing_state=existing.get("state") if existing else None,
                existing_run_mode=existing.get("run_mode") if existing else None,
            )
            candidates.append(candidate)
            promotable.append((candidate, message, decision))

        promoted: list[dict[str, Any]] = []
        if requested_senders:
            selected = [
                record for record in promotable
                if (record[0].sender_email or "").lower() in requested_senders
            ]
            selected_senders = {(record[0].sender_email or "").lower() for record in selected}
            missing = sorted(requested_senders - selected_senders)
            if missing:
                raise ValueError(f"Approved sender(s) are no longer eligible: {', '.join(missing)}")
            if len(selected) != confirm_count:
                raise ValueError(
                    f"Catch-up confirmation mismatch: expected {confirm_count}, found {len(selected)}"
                )
            for candidate, message, decision in selected:
                row, changed = self.ledger.promote_catchup_message(message, decision)
                if not changed:
                    raise RuntimeError(
                        f"Catch-up candidate became non-promotable: {candidate.provider_message_id}"
                    )
                promoted.append({
                    "mail_item_id": row["mail_item_id"], "owner": row["draft_owner"],
                    "sender_email": row["sender_email"], "subject": row["subject"],
                    "state": row["state"],
                })

        return {
            "dry_run": not requested_senders,
            "checkpoint_preserved": True,
            "summary": {
                "events_scanned": scanned,
                "removed_events_ignored": removed,
                "timestamp_quarantined": timestamp_quarantined,
                "owner_matches_before_thread_collapse": len(routed),
                "owner_threads_after_collapse": len(latest),
                "already_production": len(already_production),
                "suppressed_already_replied": len(suppressed_replied),
                "candidates": len(candidates),
            },
            "candidates": [asdict(candidate) for candidate in candidates],
            "suppressed_already_replied": suppressed_replied,
            "already_production": already_production,
            "scan_checkpoint_discarded": batch.checkpoint is not None,
            "promoted": promoted,
        }


def _timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def _brief(message: IncomingMessage, decision: Any, existing: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "owner": decision.draft_owner,
        "provider_message_id": message.provider_message_id,
        "conversation_id": message.conversation_id,
        "received_at": message.received_at,
        "sender_email": message.sender_email,
        "sender_name": message.sender_name,
        "subject": message.subject,
        "existing_state": existing.get("state") if existing else None,
        "existing_run_mode": existing.get("run_mode") if existing else None,
    }
