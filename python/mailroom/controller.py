"""Idempotent shadow-mode intake and deterministic routing controller."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .intake import MailIntake
from .ledger import MailroomLedger
from .models import Disposition, MailState, RouteDecision
from .router import DeterministicRouter
from .triage import (
    DEFAULT_MIN_OWNER_CONFIDENCE,
    DEFAULT_MIN_OWNER_SEPARATION,
    SemanticTriageAvailabilityError,
    SemanticTriageError,
    SemanticTriager,
    UnsafeSemanticOutputError,
    apply_semantic_triage,
)


@dataclass(frozen=True)
class ShadowRunSummary:
    events_seen: int = 0
    created: int = 0
    routed: int = 0
    review: int = 0
    dropped: int = 0
    triaged: int = 0
    triage_dropped: int = 0
    triage_repaired: int = 0
    triage_degraded: int = 0
    triage_errors: int = 0
    muted: int = 0
    removals_recorded: int = 0
    skipped: int = 0


class ShadowController:
    """Persist and route mail without drafting, notifying, or changing Outlook."""

    def __init__(
        self,
        ledger: MailroomLedger,
        intake: MailIntake,
        router: DeterministicRouter,
        *,
        run_mode: str = "shadow",
        triager: SemanticTriager | None = None,
        min_owner_confidence: float = DEFAULT_MIN_OWNER_CONFIDENCE,
        min_owner_separation: float = DEFAULT_MIN_OWNER_SEPARATION,
    ):
        if run_mode not in {"shadow", "production"}:
            raise ValueError(f"Unsupported run_mode: {run_mode}")
        self.ledger = ledger
        self.intake = intake
        self.router = router
        self.run_mode = run_mode
        self.triager = triager
        self.min_owner_confidence = min_owner_confidence
        self.min_owner_separation = min_owner_separation

    def run(self) -> ShadowRunSummary:
        counts = {
            "events_seen": 0,
            "created": 0,
            "routed": 0,
            "review": 0,
            "dropped": 0,
            "triaged": 0,
            "triage_dropped": 0,
            "triage_repaired": 0,
            "triage_degraded": 0,
            "triage_errors": 0,
            "muted": 0,
            "removals_recorded": 0,
            "skipped": 0,
        }
        batch = self.intake.poll()
        for event in batch.events:
            counts["events_seen"] += 1
            if event.event_type == "removed":
                if (
                    event.mailbox
                    and event.provider_message_id
                    and self.ledger.record_provider_removal(
                        event.mailbox,
                        event.provider_message_id,
                        reason=event.removal_reason,
                    )
                ):
                    counts["removals_recorded"] += 1
                else:
                    counts["skipped"] += 1
                continue
            if event.event_type != "upsert" or event.message is None:
                counts["skipped"] += 1
                continue

            row, created = self.ledger.upsert_message(
                event.message, run_mode=self.run_mode
            )
            counts["created"] += int(created)
            if row["state"] != MailState.INGESTED.value:
                counts["skipped"] += 1
                continue
            if event.message.intake_warnings:
                decision = RouteDecision(
                    draft_owner=None,
                    watchers=(),
                    confidence=0.0,
                    reasons=event.message.intake_warnings,
                    outcome="BORDERLINE",
                )
                self.ledger.route(
                    row["mail_item_id"], decision, actor="intake-quarantine"
                )
                counts["review"] += 1
                continue
            if self.ledger.is_thread_muted(
                event.message.mailbox, event.message.conversation_id
            ):
                decision = RouteDecision(
                    draft_owner=None,
                    watchers=(),
                    confidence=1.0,
                    reasons=("MUTED_BY_OPERATOR",),
                    outcome="DROPPED",
                )
                self.ledger.route(row["mail_item_id"], decision, actor="thread-policy")
                counts["muted"] += 1
                continue

            decision = self.router.route(event.message)
            # Calendar responses, bounces, and known automation remain noise
            # even when a prior human message established thread affinity.
            if decision.disposition == Disposition.NOISE:
                self.ledger.route(row["mail_item_id"], decision)
                counts["dropped"] += 1
                continue

            thread_owner = self.ledger.get_thread_route(
                event.message.mailbox,
                event.message.conversation_id,
            )
            if thread_owner:
                decision = RouteDecision(
                    draft_owner=thread_owner,
                    watchers=(),
                    confidence=1.0,
                    reasons=("THREAD_AFFINITY",),
                    priority=decision.priority,
                    disposition=Disposition.REPLY_REQUIRED,
                    outcome="ROUTED",
                )

            actor = "thread-policy" if thread_owner else "router"
            if self.triager is not None:
                try:
                    semantic = self.triager.triage(event.message, decision)
                    decision = apply_semantic_triage(
                        decision,
                        semantic,
                        min_owner_confidence=self.min_owner_confidence,
                        min_owner_separation=self.min_owner_separation,
                    )
                    counts["triaged"] += 1
                    if semantic.evidence_repaired:
                        counts["triage_repaired"] += 1
                    if decision.outcome == "DROPPED":
                        counts["triage_dropped"] += 1
                    actor = "semantic-triage"
                except UnsafeSemanticOutputError as exc:
                    # Unknown/duplicated owners are unsafe model output. Never
                    # fall back to a forced deterministic assignment.
                    counts["triage_degraded"] += 1
                    decision = RouteDecision(
                        draft_owner=None,
                        watchers=(),
                        confidence=0.0,
                        reasons=tuple(
                            dict.fromkeys(
                                (
                                    *decision.reasons,
                                    f"SEMANTIC_TRIAGE_UNSAFE:{type(exc).__name__}",
                                )
                            )
                        ),
                        priority=decision.priority,
                        disposition=Disposition.REVIEW_REQUIRED,
                        outcome="BORDERLINE",
                    )
                    actor = "semantic-triage-safety"
                except SemanticTriageAvailabilityError as exc:
                    # Provider/configuration availability is an operational
                    # failure even though deterministic routing remains live.
                    counts["triage_errors"] += 1
                    decision = replace(
                        decision,
                        reasons=tuple(
                            dict.fromkeys(
                                (
                                    *decision.reasons,
                                    f"SEMANTIC_TRIAGE_UNAVAILABLE:{type(exc).__name__}",
                                )
                            )
                        ),
                    )
                except SemanticTriageError as exc:
                    # A bounded repair was exhausted for this message. Preserve
                    # it safely without turning one malformed classification
                    # into a production-cycle outage.
                    counts["triage_degraded"] += 1
                    decision = replace(
                        decision,
                        reasons=tuple(
                            dict.fromkeys(
                                (
                                    *decision.reasons,
                                    f"SEMANTIC_TRIAGE_DEGRADED:{type(exc).__name__}",
                                )
                            )
                        ),
                    )
                except Exception as exc:
                    # Availability or malformed output must never cause a silent
                    # false negative. Preserve deterministic routing, or retain
                    # unmatched mail for human review, and expose the failure.
                    counts["triage_errors"] += 1
                    decision = replace(
                        decision,
                        reasons=tuple(
                            dict.fromkeys(
                                (
                                    *decision.reasons,
                                    f"SEMANTIC_TRIAGE_UNAVAILABLE:{type(exc).__name__}",
                                )
                            )
                        ),
                    )

            routed = self.ledger.route(row["mail_item_id"], decision, actor=actor)
            if routed["state"] == MailState.ROUTED.value:
                counts["routed"] += 1
            elif routed["state"] == MailState.DROPPED.value:
                counts["dropped"] += 1
            else:
                counts["review"] += 1
        if batch.checkpoint is not None:
            if not batch.adapter or not batch.mailbox or not batch.scope:
                raise ValueError("Checkpoint metadata is incomplete")
            self.ledger.save_checkpoint(
                batch.adapter,
                batch.mailbox,
                batch.scope,
                batch.checkpoint,
                expected_cursor=batch.previous_checkpoint,
            )
        return ShadowRunSummary(**counts)
