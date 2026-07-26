from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mailroom.ledger import ConcurrentUpdate, InvalidTransition, MailroomLedger
from mailroom.models import Disposition, IncomingMessage, MailState, Priority, RouteDecision, TriageLevel


def message(**overrides):
    values = {
        "mailbox": "operator@example.com",
        "provider_message_id": "graph-1",
        "immutable_id": "immutable-1",
        "internet_message_id": "<one@example.com>",
        "conversation_id": "conversation-1",
        "received_at": "2026-07-12T12:00:00+00:00",
        "sender_email": "counterparty@example.com",
        "sender_name": "Counterparty",
        "subject": "Project Redwood",
        "body_preview": "Following up on Project Redwood",
    }
    values.update(overrides)
    return IncomingMessage(**values)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = MailroomLedger(Path(self.temp.name) / "mailroom.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_dedupes_by_immutable_id(self):
        first, created = self.ledger.upsert_message(message())
        second, created_again = self.ledger.upsert_message(message(provider_message_id="moved-id"))
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["mail_item_id"], second["mail_item_id"])
        self.assertEqual(second["provider_message_id"], "moved-id")

    def test_partial_delta_update_preserves_complete_message_fields_and_raw_context(self):
        first, _ = self.ledger.upsert_message(message(raw={
            "id": "graph-1",
            "from": {"emailAddress": {"name": "Counterparty", "address": "counterparty@example.com"}},
            "body": {"content": "Complete body"},
        }, content_hash="initial-provider-hash"))
        updated, created = self.ledger.upsert_message(message(
            sender_email=None, sender_name=None, subject=None, body_preview=None,
            body_content=None, received_at=None, conversation_id=None,
            is_read=True, raw={"id": "graph-1", "isRead": True},
            content_hash="partial-provider-hash",
        ))
        self.assertFalse(created)
        self.assertEqual(updated["sender_email"], first["sender_email"])
        self.assertEqual(updated["sender_name"], first["sender_name"])
        self.assertEqual(updated["subject"], first["subject"])
        self.assertEqual(updated["body_preview"], first["body_preview"])
        self.assertEqual(updated["received_at"], first["received_at"])
        raw = json.loads(updated["raw_json"])
        self.assertEqual(raw["from"]["emailAddress"]["address"], "counterparty@example.com")
        self.assertTrue(raw["isRead"])

    def test_adopted_newer_message_reuses_canonical_item_without_overwriting_original(self):
        original, _ = self.ledger.upsert_message(message())
        with self.ledger.transaction() as conn:
            conn.execute(
                """INSERT INTO adopted_provider_messages
                   (mailbox, provider_message_id, mail_item_id, adopted_at)
                   VALUES (?, ?, ?, ?)""",
                (original["mailbox"], "graph-new", original["mail_item_id"], "2026-07-12T13:00:00Z"),
            )
        adopted, created = self.ledger.upsert_message(message(
            provider_message_id="graph-new", immutable_id="immutable-new",
            internet_message_id="<new@example.com>", body_preview="New follow-up",
        ))
        self.assertFalse(created)
        self.assertEqual(adopted["mail_item_id"], original["mail_item_id"])
        self.assertEqual(adopted["provider_message_id"], original["provider_message_id"])
        self.assertEqual(len(self.ledger.list_items()), 1)

    def test_sensitive_ledger_permissions_are_private(self):
        self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ledger.path.parent.stat().st_mode & 0o777, 0o700)

    def test_denied_message_does_not_mute_new_message_in_thread(self):
        first, _ = self.ledger.upsert_message(message())
        routed = self.ledger.route(first["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.9, reasons=("subject",),
            priority=Priority.P1, disposition=Disposition.REPLY_REQUIRED, outcome="ROUTED",
        ))
        self.ledger.deny_message(routed["mail_item_id"])

        second, created = self.ledger.upsert_message(message(
            provider_message_id="graph-2", immutable_id="immutable-2",
            internet_message_id="<two@example.com>", body_preview="New request",
        ))
        self.assertTrue(created)
        self.assertEqual(second["state"], MailState.INGESTED.value)
        self.assertFalse(self.ledger.is_thread_muted(second["mailbox"], second["conversation_id"]))

    def test_thread_mute_is_explicit_and_persistent(self):
        item, _ = self.ledger.upsert_message(message())
        self.ledger.mute_thread(item["mail_item_id"], reason="operator requested")
        self.assertTrue(self.ledger.is_thread_muted(item["mailbox"], item["conversation_id"]))
        self.assertTrue(self.ledger.unmute_thread(item["mailbox"], item["conversation_id"]))
        self.assertFalse(self.ledger.is_thread_muted(item["mailbox"], item["conversation_id"]))

    def test_deferral_due_query(self):
        item, _ = self.ledger.upsert_message(message())
        self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.9, reasons=("subject",), outcome="ROUTED",
        ))
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.ledger.defer(item["mail_item_id"], past)
        due = self.ledger.due_deferred()
        self.assertEqual([row["mail_item_id"] for row in due], [item["mail_item_id"]])

    def test_due_deferral_returns_to_previous_state_and_clears_card(self):
        item, _ = self.ledger.upsert_message(message(), run_mode="production")
        item = self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.9, reasons=("subject",), outcome="ROUTED",
        ))
        item = self.ledger.request_draft(item["mail_item_id"])
        item = self.ledger.start_drafting(item["mail_item_id"])
        item = self.ledger.propose_draft(item["mail_item_id"], {"reply_text": "Thanks"})
        item = self.ledger.attach_card(
            item["mail_item_id"], channel="telegram", account_id="primary",
            chat_id="chat", message_id="card",
        )
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        deferred = self.ledger.defer(item["mail_item_id"], past)
        self.assertEqual(deferred["deferred_from_state"], MailState.DRAFT_PROPOSED.value)
        released = self.ledger.release_due_deferred()
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["state"], MailState.DRAFT_PROPOSED.value)
        self.assertIsNone(released[0]["card_message_id"])
        self.assertIsNone(released[0]["deferred_until"])

    def test_manual_review_assignment_persists_thread_owner(self):
        item, _ = self.ledger.upsert_message(message())
        item = self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner=None, watchers=(), confidence=0.0, reasons=(), outcome="UNMATCHED",
        ))
        routed = self.ledger.set_thread_route(item["mail_item_id"], "primary")
        self.assertEqual(routed["state"], MailState.ROUTED.value)
        self.assertEqual(routed["draft_owner"], "primary")
        self.assertEqual(self.ledger.get_thread_route(item["mailbox"], item["conversation_id"]), "primary")

    def test_shadow_auto_route_does_not_create_production_thread_affinity(self):
        item, _ = self.ledger.upsert_message(message(), run_mode="shadow")
        self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.9, reasons=("subject",), outcome="ROUTED",
        ))
        self.assertIsNone(self.ledger.get_thread_route(item["mailbox"], item["conversation_id"]))

    def test_invalid_and_compare_and_swap_transitions_fail(self):
        item, _ = self.ledger.upsert_message(message())
        with self.assertRaises(InvalidTransition):
            self.ledger.transition(item["mail_item_id"], MailState.SENDING, actor="test")
        with self.assertRaises(ConcurrentUpdate):
            self.ledger.transition(
                item["mail_item_id"], MailState.ROUTED, actor="test",
                expected_states=[MailState.DRAFT_PROPOSED],
            )

    def test_checkpoint_compare_and_swap_rejects_a_slower_cycle(self):
        self.ledger.save_checkpoint("delta", "mailbox", "inbox", "cursor-1")
        self.ledger.save_checkpoint(
            "delta", "mailbox", "inbox", "cursor-2", expected_cursor="cursor-1",
        )
        with self.assertRaises(ConcurrentUpdate):
            self.ledger.save_checkpoint(
                "delta", "mailbox", "inbox", "stale-result", expected_cursor="cursor-1",
            )
        self.assertEqual(
            self.ledger.get_checkpoint("delta", "mailbox", "inbox"), "cursor-2",
        )

    def test_pending_input_requires_exact_unexpired_sender_and_consumes_once(self):
        item, _ = self.ledger.upsert_message(message())
        self.ledger.create_pending_input(
            item["mail_item_id"], kind="revise", channel="telegram",
            chat_id="chat-1", card_message_id="card-1", expected_sender_id="operator",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        self.assertIsNone(self.ledger.resolve_pending_input(
            channel="telegram", chat_id="chat-1", card_message_id="card-1",
            sender_id="not-operator", account_id=None,
        ))
        resolved = self.ledger.resolve_pending_input(
            channel="telegram", chat_id="chat-1", card_message_id="card-1",
            sender_id="operator", account_id=None,
        )
        self.assertEqual(resolved["mail_item_id"], item["mail_item_id"])
        self.assertIsNone(self.ledger.resolve_pending_input(
            channel="telegram", chat_id="chat-1", card_message_id="card-1",
            sender_id="operator", account_id=None,
        ))

    def test_pending_input_isolated_by_channel_account(self):
        item, _ = self.ledger.upsert_message(message())
        self.ledger.create_pending_input(
            item["mail_item_id"], kind="revise", channel="telegram",
            account_id="bot-a", chat_id="chat-1", card_message_id="card-1",
            expected_sender_id="operator", expires_at="2099-01-01T00:00:00+00:00",
        )
        self.assertIsNone(self.ledger.resolve_pending_input(
            channel="telegram", account_id="bot-b", chat_id="chat-1",
            card_message_id="card-1", sender_id="operator",
        ))
        self.assertIsNotNone(self.ledger.resolve_pending_input(
            channel="telegram", account_id="bot-a", chat_id="chat-1",
            card_message_id="card-1", sender_id="operator",
        ))

    def test_explicit_unmatched_route_waits_for_review(self):
        item, _ = self.ledger.upsert_message(message())
        routed = self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner=None, watchers=(), confidence=0.0, reasons=(), outcome="UNMATCHED",
        ))
        self.assertEqual(routed["state"], MailState.ROUTING_REVIEW.value)

    def test_route_persists_semantic_triage_audit_fields(self):
        item, _ = self.ledger.upsert_message(message())
        routed = self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.96,
            reasons=("semantic:reply",), priority=Priority.P1,
            disposition=Disposition.REPLY_REQUIRED, outcome="ROUTED",
            importance=TriageLevel.HIGH, urgency=TriageLevel.HIGH,
            triage_action="reply", triage_model="gemini-3.1-flash-lite",
            triage_confidence=0.96, triage_rationale="Approval requested today.",
        ))
        self.assertEqual((routed["importance"], routed["urgency"]), ("high", "high"))
        self.assertEqual(routed["triage_action"], "reply")
        self.assertEqual(routed["triage_model"], "gemini-3.1-flash-lite")
        self.assertAlmostEqual(routed["triage_confidence"], 0.96)
        self.assertEqual(routed["triage_rationale"], "Approval requested today.")

    def test_priority_ordering_places_urgent_items_first(self):
        low, _ = self.ledger.upsert_message(message())
        self.ledger.route(low["mail_item_id"], RouteDecision(
            draft_owner=None, watchers=(), confidence=0.5, reasons=("low",),
            priority=Priority.P3, disposition=Disposition.REVIEW_REQUIRED, outcome="BORDERLINE",
        ))
        urgent_message = message(
            provider_message_id="graph-urgent", immutable_id="immutable-urgent",
            internet_message_id="<urgent@example.com>", conversation_id="urgent",
        )
        urgent, _ = self.ledger.upsert_message(urgent_message)
        self.ledger.route(urgent["mail_item_id"], RouteDecision(
            draft_owner=None, watchers=(), confidence=0.9, reasons=("urgent",),
            priority=Priority.P0, disposition=Disposition.REVIEW_REQUIRED, outcome="BORDERLINE",
        ))
        rows = self.ledger.list_items(
            state=MailState.ROUTING_REVIEW, order_by_priority=True,
        )
        self.assertEqual([row["priority"] for row in rows], ["P0", "P3"])

    def test_list_items_can_select_only_rows_without_cards_before_limit(self):
        first, _ = self.ledger.upsert_message(message(), run_mode="production")
        first = self.ledger.route(first["mail_item_id"], RouteDecision(
            draft_owner=None, watchers=(), confidence=0.5, reasons=("first",),
            priority=Priority.P0, disposition=Disposition.REVIEW_REQUIRED,
            outcome="BORDERLINE",
        ))
        self.ledger.attach_card(
            first["mail_item_id"], channel="telegram", account_id="default",
            chat_id="123456789", message_id="42",
        )
        second_message = message(
            provider_message_id="graph-unnotified",
            immutable_id="immutable-unnotified",
            internet_message_id="<unnotified@example.com>",
            conversation_id="unnotified",
        )
        second, _ = self.ledger.upsert_message(
            second_message, run_mode="production",
        )
        second = self.ledger.route(second["mail_item_id"], RouteDecision(
            draft_owner=None, watchers=(), confidence=0.5, reasons=("second",),
            priority=Priority.P3, disposition=Disposition.REVIEW_REQUIRED,
            outcome="BORDERLINE",
        ))
        rows = self.ledger.list_items(
            state=MailState.ROUTING_REVIEW,
            run_mode="production",
            card_attached=False,
            limit=1,
            order_by_priority=True,
        )
        self.assertEqual([row["mail_item_id"] for row in rows], [second["mail_item_id"]])

    def test_draft_proposal_and_card_are_restart_safe(self):
        item, _ = self.ledger.upsert_message(message(), run_mode="production")
        routed = self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.9,
            reasons=("subject:redwood",), outcome="ROUTED",
        ))
        requested = self.ledger.request_draft(routed["mail_item_id"])
        requested = self.ledger.start_drafting(requested["mail_item_id"])
        proposed = self.ledger.propose_draft(requested["mail_item_id"], {
            "reply_text": "Thank you.", "reply_all": "auto", "rationale": "Acknowledges receipt.",
        })
        self.assertEqual(proposed["state"], MailState.DRAFT_PROPOSED.value)
        card = self.ledger.attach_card(
            proposed["mail_item_id"], channel="telegram", account_id="primary",
            chat_id="123456789", message_id="42",
        )
        resolved = self.ledger.get_callback_item(
            card["callback_token"], channel="telegram", account_id="primary",
            chat_id="123456789", message_id="42",
        )
        self.assertEqual(resolved["mail_item_id"], item["mail_item_id"])
        self.assertIsNone(self.ledger.get_callback_item(
            card["callback_token"], channel="telegram", account_id="legal",
            chat_id="123456789", message_id="42",
        ))

    def test_expired_worker_cannot_complete_after_drafting_lease_is_reissued(self):
        item, _ = self.ledger.upsert_message(message(), run_mode="production")
        item = self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.9,
            reasons=("subject:redwood",), outcome="ROUTED",
        ))
        item = self.ledger.request_draft(item["mail_item_id"])
        expired_worker = self.ledger.start_drafting(item["mail_item_id"])
        recovered = self.ledger.transition(
            item["mail_item_id"], MailState.DRAFT_REQUESTED,
            actor="test:lease-recovery",
            expected_states=[MailState.DRAFTING],
            expected_version=expired_worker["version"],
        )
        current_worker = self.ledger.start_drafting(recovered["mail_item_id"])
        with self.assertRaises(ConcurrentUpdate):
            self.ledger.propose_draft(
                item["mail_item_id"], {"reply_text": "Stale result"},
                expected_version=expired_worker["version"],
            )
        proposed = self.ledger.propose_draft(
            item["mail_item_id"], {"reply_text": "Current result"},
            expected_version=current_worker["version"],
        )
        self.assertIn("Current result", proposed["proposal_json"])

    def test_draft_provenance_is_copied_to_the_event_ledger(self):
        item, _ = self.ledger.upsert_message(message(), run_mode="production")
        item = self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.9,
            reasons=("subject:redwood",), outcome="ROUTED",
        ))
        item = self.ledger.request_draft(item["mail_item_id"])
        item = self.ledger.start_drafting(item["mail_item_id"])
        provenance = {
            "run_id": "run-123", "session_id": "session-123",
            "tool_names": ["Bash"], "audit_status": "complete",
        }
        self.ledger.propose_draft(
            item["mail_item_id"],
            {"reply_text": "Thanks", "_mailroom_provenance": provenance},
            expected_version=item["version"],
        )
        with self.ledger.connect() as conn:
            row = conn.execute(
                """
                SELECT metadata_json FROM mail_events
                WHERE mail_item_id = ? AND to_state = 'DRAFT_PROPOSED'
                ORDER BY rowid DESC LIMIT 1
                """,
                (item["mail_item_id"],),
            ).fetchone()
        self.assertEqual(
            json.loads(row["metadata_json"])["draft_provenance"], provenance,
        )


if __name__ == "__main__":
    unittest.main()
