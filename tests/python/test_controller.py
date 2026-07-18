from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mailroom.controller import ShadowController
from mailroom.ledger import MailroomLedger
from mailroom.models import (
    IncomingMessage,
    IntakeBatch,
    IntakeEvent,
    MailState,
    RouteDecision,
    TriageLevel,
)
from mailroom.router import DeterministicRouter, RulePack
from mailroom.triage import (
    OwnerCandidate,
    SemanticTriage,
    SemanticTriageAvailabilityError,
    SemanticTriageError,
    UnsafeSemanticOutputError,
)


class StaticIntake:
    mailbox = "operator@example.com"

    def __init__(self, events):
        self.events = events

    def poll(self):
        return IntakeBatch(events=tuple(self.events))


class FakeTriager:
    def __init__(self, decision=None, error=None):
        self.decision = decision
        self.error = error
        self.calls = []

    def triage(self, message, deterministic):
        self.calls.append((message, deterministic))
        if self.error:
            raise self.error
        return self.decision


def semantic(
    *,
    action="reply",
    owner="primary",
    importance=TriageLevel.MEDIUM,
    urgency=TriageLevel.MEDIUM,
    confidence=0.95,
    evidence_repaired=False,
):
    candidates = (
        ()
        if owner is None
        else (
            OwnerCandidate(
                owner=owner,
                confidence=confidence,
                reasons=("Profile and email match.",),
                specific_signals=("Project Redwood",),
                shared_signals=(),
            ),
        )
    )
    return SemanticTriage(
        action=action,
        candidates=candidates,
        importance=importance,
        urgency=urgency,
        classification_confidence=confidence,
        rationale="Evidence-based triage.",
        model="gemini-3.1-flash-lite",
        profile_set_id="arp-test",
        evidence_repaired=evidence_repaired,
    )


def message(provider_id="one", conversation_id="conversation-1"):
    return IncomingMessage(
        mailbox=StaticIntake.mailbox,
        provider_message_id=provider_id,
        immutable_id=provider_id,
        internet_message_id=f"<{provider_id}@example.com>",
        conversation_id=conversation_id,
        received_at="2026-07-12T12:00:00Z",
        sender_email="person@example.com",
        sender_name="Person",
        subject="Project Redwood",
        body_preview="Customer operations follow-up",
    )


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = MailroomLedger(Path(self.temp.name) / "mailroom.db")
        self.router = DeterministicRouter(
            [
                RulePack(
                    agent="primary",
                    subject_terms=("redwood",),
                    metadata={"deal": "Project Redwood"},
                ),
            ]
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_routes_once_and_replay_is_idempotent(self):
        event = IntakeEvent(event_type="upsert", message=message())
        first = ShadowController(self.ledger, StaticIntake([event]), self.router).run()
        second = ShadowController(self.ledger, StaticIntake([event]), self.router).run()
        self.assertEqual((first.created, first.routed), (1, 1))
        self.assertEqual((second.created, second.skipped), (0, 1))
        self.assertEqual(self.ledger.list_items()[0]["state"], MailState.ROUTED.value)

    def test_thread_mute_drops_only_new_message(self):
        first, _ = self.ledger.upsert_message(message())
        self.ledger.mute_thread(first["mail_item_id"])
        event = IntakeEvent(event_type="upsert", message=message("two"))
        summary = ShadowController(
            self.ledger, StaticIntake([event]), self.router
        ).run()
        self.assertEqual(summary.muted, 1)
        rows = self.ledger.list_items()
        dropped = next(row for row in rows if row["provider_message_id"] == "two")
        self.assertEqual(dropped["state"], MailState.DROPPED.value)

    def test_global_noise_overrides_existing_thread_affinity(self):
        first, _ = self.ledger.upsert_message(message("first"), run_mode="production")
        self.ledger.route(
            first["mail_item_id"],
            RouteDecision(
                draft_owner="primary",
                watchers=(),
                confidence=1.0,
                reasons=("subject",),
                outcome="ROUTED",
            ),
        )
        declined = message("declined")
        declined = IncomingMessage(
            **{
                **declined.__dict__,
                "subject": "Declined: Project Redwood call",
                "raw": {"meetingMessageType": "meetingDeclined"},
            }
        )
        summary = ShadowController(
            self.ledger,
            StaticIntake([IntakeEvent(event_type="upsert", message=declined)]),
            self.router,
            run_mode="production",
        ).run()
        self.assertEqual(summary.dropped, 1)
        row = next(
            r
            for r in self.ledger.list_items()
            if r["provider_message_id"] == "declined"
        )
        self.assertEqual(row["state"], MailState.DROPPED.value)

    def test_unknown_message_is_retained_for_routing_review(self):
        unknown = IncomingMessage(
            **{
                **message("unknown").__dict__,
                "subject": "Approval needed by 3 PM",
                "body_preview": "Please approve the revised terms.",
            }
        )
        summary = ShadowController(
            self.ledger,
            StaticIntake([IntakeEvent(event_type="upsert", message=unknown)]),
            self.router,
        ).run()
        self.assertEqual((summary.review, summary.dropped), (1, 0))
        self.assertEqual(
            self.ledger.list_items()[0]["state"], MailState.ROUTING_REVIEW.value
        )

    def test_semantic_triage_routes_novel_important_mail(self):
        unknown = IncomingMessage(
            **{
                **message("semantic-route").__dict__,
                "subject": "Approval needed by 3 PM",
                "body_preview": "Please approve the revised terms.",
            }
        )
        triager = FakeTriager(
            semantic(
                importance=TriageLevel.HIGH,
                urgency=TriageLevel.HIGH,
            )
        )
        summary = ShadowController(
            self.ledger,
            StaticIntake([IntakeEvent(event_type="upsert", message=unknown)]),
            self.router,
            run_mode="production",
            triager=triager,
        ).run()
        self.assertEqual(
            (summary.triaged, summary.routed, summary.triage_errors), (1, 1, 0)
        )
        row = self.ledger.list_items()[0]
        self.assertEqual(row["draft_owner"], "primary")
        self.assertEqual(
            (row["importance"], row["urgency"], row["priority"]), ("high", "high", "P1")
        )
        self.assertEqual(row["triage_model"], "gemini-3.1-flash-lite")
        self.assertIsNone(triager.calls[0][1].draft_owner)
        self.assertEqual(triager.calls[0][0].subject, "Approval needed by 3 PM")

    def test_semantic_high_confidence_low_no_reply_drops_without_notification_state(
        self,
    ):
        triager = FakeTriager(
            semantic(
                action="no_reply",
                owner=None,
                importance=TriageLevel.LOW,
                urgency=TriageLevel.LOW,
                confidence=0.97,
            )
        )
        summary = ShadowController(
            self.ledger,
            StaticIntake(
                [IntakeEvent(event_type="upsert", message=message("no-reply"))]
            ),
            self.router,
            run_mode="production",
            triager=triager,
        ).run()
        self.assertEqual(
            (summary.triaged, summary.triage_dropped, summary.dropped), (1, 1, 1)
        )
        row = self.ledger.list_items()[0]
        self.assertEqual((row["state"], row["disposition"]), ("DROPPED", "fyi"))
        self.assertEqual(row["triage_action"], "no_reply")

    def test_counts_repaired_semantic_evidence_separately(self):
        summary = ShadowController(
            self.ledger,
            StaticIntake(
                [IntakeEvent(event_type="upsert", message=message("repaired"))]
            ),
            self.router,
            run_mode="production",
            triager=FakeTriager(semantic(evidence_repaired=True)),
        ).run()
        self.assertEqual(
            (summary.triaged, summary.triage_repaired, summary.triage_errors),
            (1, 1, 0),
        )
        row = self.ledger.list_items()[0]
        self.assertIn("semantic:evidence-repaired", row["route_reasons_json"])

    def test_semantic_failure_fails_open_to_deterministic_route(self):
        triager = FakeTriager(error=RuntimeError("provider unavailable"))
        summary = ShadowController(
            self.ledger,
            StaticIntake(
                [IntakeEvent(event_type="upsert", message=message("fallback"))]
            ),
            self.router,
            run_mode="production",
            triager=triager,
        ).run()
        self.assertEqual((summary.routed, summary.triage_errors), (1, 1))
        row = self.ledger.list_items()[0]
        self.assertEqual(row["state"], MailState.ROUTED.value)
        self.assertIn(
            "SEMANTIC_TRIAGE_UNAVAILABLE:RuntimeError", row["route_reasons_json"]
        )

    def test_provider_unavailability_remains_an_operational_error(self):
        triager = FakeTriager(
            error=SemanticTriageAvailabilityError("provider unavailable")
        )
        summary = ShadowController(
            self.ledger,
            StaticIntake(
                [IntakeEvent(event_type="upsert", message=message("unavailable"))]
            ),
            self.router,
            run_mode="production",
            triager=triager,
        ).run()
        self.assertEqual((summary.routed, summary.triage_errors), (1, 1))
        row = self.ledger.list_items()[0]
        self.assertIn(
            "SEMANTIC_TRIAGE_UNAVAILABLE:SemanticTriageAvailabilityError",
            row["route_reasons_json"],
        )

    def test_exhausted_validation_is_degraded_without_cycle_error(self):
        triager = FakeTriager(error=SemanticTriageError("invalid evidence"))
        summary = ShadowController(
            self.ledger,
            StaticIntake(
                [IntakeEvent(event_type="upsert", message=message("degraded"))]
            ),
            self.router,
            run_mode="production",
            triager=triager,
        ).run()
        self.assertEqual(
            (summary.routed, summary.triage_degraded, summary.triage_errors),
            (1, 1, 0),
        )
        row = self.ledger.list_items()[0]
        self.assertIn(
            "SEMANTIC_TRIAGE_DEGRADED:SemanticTriageError",
            row["route_reasons_json"],
        )

    def test_unknown_semantic_owner_fails_closed_to_routing_review(self):
        triager = FakeTriager(error=UnsafeSemanticOutputError("unknown owner"))
        summary = ShadowController(
            self.ledger,
            StaticIntake([IntakeEvent(event_type="upsert", message=message("unsafe"))]),
            self.router,
            run_mode="production",
            triager=triager,
        ).run()
        self.assertEqual(
            (summary.review, summary.triage_degraded, summary.triage_errors),
            (1, 1, 0),
        )
        row = self.ledger.list_items()[0]
        self.assertEqual(row["state"], MailState.ROUTING_REVIEW.value)
        self.assertIsNone(row["draft_owner"])
        self.assertIn("SEMANTIC_TRIAGE_UNSAFE", row["route_reasons_json"])

    def test_deterministic_hard_noise_bypasses_semantic_triage(self):
        triager = FakeTriager(semantic())
        automated = IncomingMessage(
            **{
                **message("automated").__dict__,
                "sender_email": "no-reply@example.com",
            }
        )
        summary = ShadowController(
            self.ledger,
            StaticIntake([IntakeEvent(event_type="upsert", message=automated)]),
            self.router,
            run_mode="production",
            triager=triager,
        ).run()
        self.assertEqual((summary.dropped, summary.triaged), (1, 0))
        self.assertEqual(triager.calls, [])

    def test_manual_thread_route_is_inherited_by_new_messages(self):
        first = message("review")
        first = IncomingMessage(**{**first.__dict__, "subject": "Ambiguous follow-up"})
        first_row, _ = self.ledger.upsert_message(first)
        self.ledger.route(
            first_row["mail_item_id"],
            RouteDecision(
                draft_owner=None,
                watchers=(),
                confidence=0.0,
                reasons=(),
                outcome="UNMATCHED",
            ),
        )
        self.ledger.set_thread_route(first_row["mail_item_id"], "primary")

        follow_up = message("follow-up")
        follow_up = IncomingMessage(
            **{**follow_up.__dict__, "subject": "Can Thursday work?"}
        )
        summary = ShadowController(
            self.ledger,
            StaticIntake([IntakeEvent(event_type="upsert", message=follow_up)]),
            self.router,
        ).run()
        self.assertEqual(summary.routed, 1)
        routed = next(
            row
            for row in self.ledger.list_items()
            if row["provider_message_id"] == "follow-up"
        )
        self.assertEqual(routed["draft_owner"], "primary")
        self.assertEqual(routed["route_reasons_json"], '["THREAD_AFFINITY"]')

    def test_checkpoint_advances_only_after_processing(self):
        class FailingRouter:
            def route(self, _message):
                raise RuntimeError("routing failed")

        event = IntakeEvent(event_type="upsert", message=message())
        intake = StaticIntake([event])
        intake.poll = lambda: IntakeBatch(
            events=(event,),
            checkpoint="cursor",
            adapter="test",
            mailbox=StaticIntake.mailbox,
            scope="inbox",
        )
        with self.assertRaises(RuntimeError):
            ShadowController(self.ledger, intake, FailingRouter()).run()
        self.assertIsNone(
            self.ledger.get_checkpoint("test", StaticIntake.mailbox, "inbox")
        )

        summary = ShadowController(self.ledger, intake, self.router).run()
        self.assertEqual(summary.routed, 1)
        self.assertEqual(
            self.ledger.get_checkpoint("test", StaticIntake.mailbox, "inbox"),
            "cursor",
        )

    def test_unknown_bootstrap_timestamp_is_quarantined(self):
        warned = message()
        warned = IncomingMessage(
            **{
                **warned.__dict__,
                "intake_warnings": ("UNKNOWN_RECEIVED_AT_DURING_BOOTSTRAP",),
            }
        )
        summary = ShadowController(
            self.ledger,
            StaticIntake([IntakeEvent(event_type="upsert", message=warned)]),
            self.router,
        ).run()
        self.assertEqual(summary.review, 1)
        self.assertEqual(
            self.ledger.list_items()[0]["state"], MailState.ROUTING_REVIEW.value
        )
        self.assertEqual(self.ledger.list_items()[0]["run_mode"], "shadow")


if __name__ == "__main__":
    unittest.main()
