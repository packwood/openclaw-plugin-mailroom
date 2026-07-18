from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mailroom.catchup import CatchupScanner
from mailroom.ledger import MailroomLedger
from mailroom.models import IncomingMessage, IntakeBatch, IntakeEvent
from mailroom.reply_guard import SentReply
from mailroom.router import DeterministicRouter, RulePack


class StaticIntake:
    def __init__(self, messages):
        self.messages = messages

    def poll(self):
        return IntakeBatch(
            events=tuple(IntakeEvent(event_type="upsert", message=m) for m in self.messages),
            checkpoint="discard-me", adapter="test", mailbox="operator@example.com", scope="inbox",
        )


class FakeReplyChecker:
    def find_reply_after(self, item):
        if item["conversation_id"] == "replied":
            return SentReply("sent", "2026-07-12T15:00:00Z", "Re: Deal")
        return None


def message(identifier, conversation, received, subject="Project Redwood"):
    return IncomingMessage(
        mailbox="operator@example.com", provider_message_id=identifier,
        conversation_id=conversation, received_at=received,
        sender_email="person@example.com", sender_name="Person",
        subject=subject, body_preview="Please respond",
    )


class CatchupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = MailroomLedger(Path(self.temp.name) / "mailroom.db")
        self.router = DeterministicRouter([
            RulePack(agent="primary", mailboxes=("operator@example.com",), subject_terms=("redwood",)),
            RulePack(agent="research", mailboxes=("operator@example.com",), subject_terms=("example research",)),
        ])

    def test_dry_run_collapses_threads_and_suppresses_replies(self):
        messages = [
            message("old", "same", "2026-07-12T10:00:00Z"),
            message("new", "same", "2026-07-12T11:00:00Z"),
            message("answered", "replied", "2026-07-12T12:00:00Z"),
            message("research", "research", "2026-07-12T13:00:00Z", "Example Research"),
            message("noise", "noise", "2026-07-12T14:00:00Z", "Lunch"),
        ]
        result = CatchupScanner(
            self.ledger, StaticIntake(messages), self.router, FakeReplyChecker(),
            owners=("primary", "research"),
        ).run()
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["checkpoint_preserved"])
        self.assertEqual(result["summary"]["owner_matches_before_thread_collapse"], 4)
        self.assertEqual(result["summary"]["owner_threads_after_collapse"], 3)
        self.assertEqual(result["summary"]["suppressed_already_replied"], 1)
        self.assertEqual({x["provider_message_id"] for x in result["candidates"]}, {"new", "research"})
        self.assertEqual(self.ledger.list_items(), [])

    def test_existing_production_item_is_not_a_candidate(self):
        existing = message("existing", "existing", "2026-07-12T10:00:00Z")
        self.ledger.upsert_message(existing, run_mode="production")
        result = CatchupScanner(
            self.ledger, StaticIntake([existing]), self.router, FakeReplyChecker(),
            owners=("primary",),
        ).run()
        self.assertEqual(result["summary"]["already_production"], 1)
        self.assertEqual(result["candidates"], [])

    def test_selective_apply_promotes_only_confirmed_sender(self):
        primary = message("primary", "primary", "2026-07-12T10:00:00Z")
        research = message("research", "research", "2026-07-12T11:00:00Z", "Example Research")
        self.ledger.upsert_message(primary, run_mode="shadow")
        result = CatchupScanner(
            self.ledger, StaticIntake([primary, research]), self.router, FakeReplyChecker(),
            owners=("primary", "research"),
        ).run(apply_senders=("person@example.com",), confirm_count=2)
        self.assertFalse(result["dry_run"])
        self.assertEqual(len(result["promoted"]), 2)
        rows = self.ledger.list_items(run_mode="production")
        self.assertEqual({row["draft_owner"] for row in rows}, {"primary", "research"})
        self.assertEqual({row["state"] for row in rows}, {"ROUTED"})

    def test_selective_apply_fails_closed_on_count_mismatch(self):
        primary = message("primary", "primary", "2026-07-12T10:00:00Z")
        with self.assertRaisesRegex(ValueError, "confirmation mismatch"):
            CatchupScanner(
                self.ledger, StaticIntake([primary]), self.router, FakeReplyChecker(),
                owners=("primary",),
            ).run(apply_senders=("person@example.com",), confirm_count=2)
        self.assertEqual(self.ledger.list_items(), [])


if __name__ == "__main__":
    unittest.main()
