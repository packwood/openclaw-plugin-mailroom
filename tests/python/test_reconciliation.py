from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.parse

from mailroom.ledger import MailroomLedger
from mailroom.models import IncomingMessage, MailState, RouteDecision
from mailroom.reconciliation import MatonSentVerifier, SendReconciler, _fingerprint


class FakeVerifier:
    def __init__(self, sent_id=None):
        self.sent_id = sent_id

    def find_verified_message(self, _item):
        return self.sent_id


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = MailroomLedger(Path(self.temp.name) / "mailroom.db")
        msg = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="message",
            conversation_id="conversation", received_at="2026-07-12T12:00:00Z",
            sender_email="person@example.com", sender_name="Person",
            subject="Project Redwood", body_preview="Hello",
        )
        item, _ = self.ledger.upsert_message(msg, run_mode="production")
        item = self.ledger.route(item["mail_item_id"], RouteDecision(
            draft_owner="primary", watchers=(), confidence=0.9, reasons=("subject",), outcome="ROUTED",
        ))
        item = self.ledger.request_draft(item["mail_item_id"])
        item = self.ledger.propose_draft(item["mail_item_id"], {"reply_text": "Thanks"})
        item = self.ledger.transition(item["mail_item_id"], MailState.OUTLOOK_DRAFTING, actor="test")
        item = self.ledger.transition(item["mail_item_id"], MailState.OUTLOOK_DRAFTED, actor="test")
        item = self.ledger.transition(item["mail_item_id"], MailState.SEND_APPROVAL_PENDING, actor="test")
        item = self.ledger.transition(item["mail_item_id"], MailState.SENDING, actor="test")
        self.item = self.ledger.transition(
            item["mail_item_id"], MailState.SEND_ACCEPTED, actor="test",
            patch={"approval_fingerprint": "fingerprint", "send_accepted_at": "2026-07-12T12:01:00Z"},
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_sent_match_transitions_to_verified(self):
        summary = SendReconciler(self.ledger, FakeVerifier("sent-id")).run()
        self.assertEqual((summary.checked, summary.verified, summary.pending), (1, 1, 0))
        item = self.ledger.get(self.item["mail_item_id"])
        self.assertEqual(item["state"], MailState.SENT_VERIFIED.value)
        self.assertEqual(item["sent_message_id"], "sent-id")

    def test_no_match_remains_send_accepted(self):
        recent = datetime.now(timezone.utc).isoformat()
        with self.ledger.transaction() as conn:
            conn.execute(
                "UPDATE mail_items SET send_accepted_at = ? WHERE mail_item_id = ?",
                (recent, self.item["mail_item_id"]),
            )
        summary = SendReconciler(self.ledger, FakeVerifier()).run()
        self.assertEqual((summary.checked, summary.verified, summary.pending), (1, 0, 1))
        self.assertEqual(self.ledger.get(self.item["mail_item_id"])["state"], MailState.SEND_ACCEPTED.value)

    def test_stale_unverified_send_becomes_outcome_unknown(self):
        stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with self.ledger.transaction() as conn:
            conn.execute(
                "UPDATE mail_items SET send_accepted_at = ? WHERE mail_item_id = ?",
                (stale, self.item["mail_item_id"]),
            )
        summary = SendReconciler(self.ledger, FakeVerifier()).run()
        self.assertEqual(summary.uncertain, 1)
        self.assertEqual(
            self.ledger.get(self.item["mail_item_id"])["state"],
            MailState.SEND_OUTCOME_UNKNOWN.value,
        )

    def test_outcome_unknown_is_rechecked_and_can_be_verified(self):
        unknown = self.ledger.transition(
            self.item["mail_item_id"], MailState.SEND_OUTCOME_UNKNOWN,
            actor="test", expected_states=[MailState.SEND_ACCEPTED],
        )
        summary = SendReconciler(self.ledger, FakeVerifier("sent-id")).run()
        self.assertEqual((summary.checked, summary.verified, summary.unresolved), (1, 1, 0))
        item = self.ledger.get(unknown["mail_item_id"])
        self.assertEqual(item["state"], MailState.SENT_VERIFIED.value)
        self.assertEqual(item["sent_message_id"], "sent-id")

    def test_unmatched_outcome_unknown_remains_fail_closed(self):
        unknown = self.ledger.transition(
            self.item["mail_item_id"], MailState.SEND_OUTCOME_UNKNOWN,
            actor="test", expected_states=[MailState.SEND_ACCEPTED],
        )
        summary = SendReconciler(self.ledger, FakeVerifier()).run()
        self.assertEqual((summary.checked, summary.unresolved), (1, 1))
        self.assertEqual(
            self.ledger.get(unknown["mail_item_id"])["state"],
            MailState.SEND_OUTCOME_UNKNOWN.value,
        )

    def test_fingerprint_matches_typescript_contract(self):
        message = {
            "toRecipients": [{"emailAddress": {"address": "B@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "a@example.com"}}],
            "bccRecipients": [], "subject": "Subject", "body": {"content": "<p>Hello</p>"},
        }
        self.assertEqual(len(_fingerprint("work", message, [])), 24)

    def test_maton_verifier_requires_exact_fingerprint_match(self):
        message = {
            "id": "sent-id", "conversationId": "conversation", "hasAttachments": False,
            "toRecipients": [{"emailAddress": {"address": "person@example.com"}}],
            "ccRecipients": [], "bccRecipients": [], "subject": "Project Redwood",
            "body": {"content": "<p>Thanks</p>"},
        }
        expected = _fingerprint("operator@example.com", message, [])
        urls = []

        def fetch(url, _headers):
            urls.append(url)
            return {"value": [message]}

        verifier = MatonSentVerifier("connection", "api-key", fetch_json=fetch)
        found = verifier.find_verified_message({
            "mailbox": "operator@example.com",
            "conversation_id": "conversation",
            "approval_fingerprint": expected,
            "send_accepted_at": datetime.now(timezone.utc).isoformat(),
        })
        self.assertEqual(found, "sent-id")
        self.assertIn("mailFolders/sentitems/messages", urls[0])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
        self.assertTrue(query["$filter"][0].startswith("sentDateTime ge "))


if __name__ == "__main__":
    unittest.main()
