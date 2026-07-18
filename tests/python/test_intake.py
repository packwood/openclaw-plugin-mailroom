from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path

from mailroom.intake import MatonDeltaIntake
from mailroom.ledger import MailroomLedger


class IntakeTests(unittest.TestCase):
    def test_delta_parsing_and_checkpoint(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = MailroomLedger(Path(temp.name) / "mailroom.db")
        pages = [{
            "value": [
                {
                    "id": "immutable-1",
                    "internetMessageId": "<one@example.com>",
                    "conversationId": "conv-1",
                    "receivedDateTime": "2026-07-12T12:00:00Z",
                    "subject": "Project Redwood",
                    "from": {"emailAddress": {"name": "A", "address": "a@example.com"}},
                    "bodyPreview": "Hello",
                    "body": {"contentType": "HTML", "content": "<p>Hello</p>"},
                    "isRead": False,
                    "hasAttachments": False,
                },
                {"id": "removed-1", "@removed": {"reason": "deleted"}},
            ],
            "@odata.deltaLink": "https://gateway.maton.ai/outlook/v1.0/final",
        }]

        def fetch(_url, headers):
            self.assertEqual(headers["Prefer"], 'IdType="ImmutableId"')
            return pages.pop(0)

        intake = MatonDeltaIntake(
            ledger=ledger, mailbox="operator@example.com",
            connection_id="connection", api_key="key", fetch_json=fetch,
            bootstrap_lookback_hours=24 * 365,
        )
        batch = intake.poll()
        self.assertEqual([event.event_type for event in batch.events], ["upsert", "removed"])
        self.assertEqual(batch.events[0].message.immutable_id, "immutable-1")
        self.assertEqual(batch.checkpoint, "https://gateway.maton.ai/outlook/v1.0/final")
        self.assertIsNone(ledger.get_checkpoint("maton_delta", "operator@example.com", "inbox"))

    def test_maton_prefer_fallback_and_graph_continuation_rewrite(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = MailroomLedger(Path(temp.name) / "mailroom.db")
        calls = []

        def fetch(url, headers):
            calls.append((url, dict(headers)))
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 401, "unsupported preference", {}, None)
            if "messages/mutable-id?" in url:
                return {
                    "id": "mutable-id", "internetMessageId": "<stable@example.com>",
                    "conversationId": "conv", "subject": "Hello",
                    "receivedDateTime": "2026-07-12T12:00:00Z",
                    "from": {"emailAddress": {"name": "Sender", "address": "sender@example.com"}},
                    "body": {"contentType": "text", "content": "Hello"},
                }
            if len(calls) == 2:
                return {
                    "value": [{
                        "id": "mutable-id", "internetMessageId": "<stable@example.com>",
                        "conversationId": "conv", "subject": "Hello",
                        "receivedDateTime": "2026-07-12T12:00:00Z",
                    }],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages/delta?$skiptoken=abc",
                }
            return {
                "value": [],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=xyz",
            }

        intake = MatonDeltaIntake(
            ledger=ledger, mailbox="operator@example.com", connection_id="connection",
            api_key="key", fetch_json=fetch, bootstrap_lookback_hours=24 * 365,
        )
        batch = intake.poll()
        self.assertIn("Prefer", calls[0][1])
        self.assertNotIn("Prefer", calls[1][1])
        self.assertIn("messages/mutable-id?", calls[2][0])
        self.assertTrue(calls[3][0].startswith("https://gateway.maton.ai/outlook/v1.0/"))
        self.assertIsNone(batch.events[0].message.immutable_id)
        self.assertEqual(batch.events[0].message.internet_message_id, "<stable@example.com>")
        self.assertTrue(batch.checkpoint.startswith("https://gateway.maton.ai/outlook/v1.0/"))

    def test_bootstrap_cutoff_is_enforced_locally(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = MailroomLedger(Path(temp.name) / "mailroom.db")

        def fetch(_url, _headers):
            return {
                "value": [{
                    "id": "too-old", "receivedDateTime": "2000-01-01T00:00:00Z",
                    "internetMessageId": "<old@example.com>", "subject": "Old",
                }],
                "@odata.deltaLink": "https://gateway.maton.ai/outlook/v1.0/final",
            }

        intake = MatonDeltaIntake(
            ledger=ledger, mailbox="operator@example.com", connection_id="connection",
            api_key="key", fetch_json=fetch, bootstrap_lookback_hours=2,
        )
        batch = intake.poll()
        self.assertEqual(batch.events, ())
        self.assertEqual(ledger.list_items(), [])
        self.assertEqual(batch.checkpoint, "https://gateway.maton.ai/outlook/v1.0/final")

    def test_unknown_timestamp_is_retained_with_warning(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = MailroomLedger(Path(temp.name) / "mailroom.db")

        def fetch(_url, _headers):
            if "/me/messages/unknown-time?" in _url:
                return {
                    "id": "unknown-time", "conversationId": "unknown-conversation",
                    "subject": "Unknown", "from": {"emailAddress": {
                        "name": "Unknown", "address": "unknown@example.com",
                    }}, "body": {"contentType": "text", "content": "Unknown"},
                }
            return {
                "value": [{"id": "unknown-time", "subject": "Unknown"}],
                "@odata.deltaLink": "https://gateway.maton.ai/outlook/v1.0/final",
            }

        batch = MatonDeltaIntake(
            ledger=ledger, mailbox="operator@example.com", connection_id="connection",
            api_key="key", fetch_json=fetch,
        ).poll()
        self.assertEqual(
            batch.events[0].message.intake_warnings,
            ("UNKNOWN_RECEIVED_AT_DURING_BOOTSTRAP",),
        )

    def test_partial_delta_message_is_hydrated_before_mapping(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = MailroomLedger(Path(temp.name) / "mailroom.db")
        calls = []

        def fetch(url, _headers):
            calls.append(url)
            if "/me/messages/partial-1?" in url:
                return {
                    "id": "partial-1", "conversationId": "conversation-1",
                    "receivedDateTime": "2026-07-12T12:00:00Z", "subject": "Hydrated",
                    "from": {"emailAddress": {"name": "A", "address": "a@example.com"}},
                    "body": {"contentType": "HTML", "content": "<p>Complete</p>"},
                    "bodyPreview": "Complete", "isRead": False, "hasAttachments": False,
                }
            return {
                "value": [{"id": "partial-1", "changeKey": "new-change-key"}],
                "@odata.deltaLink": "https://gateway.maton.ai/outlook/v1.0/final",
            }

        batch = MatonDeltaIntake(
            ledger=ledger, mailbox="operator@example.com", connection_id="connection",
            api_key="key", fetch_json=fetch, bootstrap_lookback_hours=24 * 365,
        ).poll()
        message = batch.events[0].message
        self.assertEqual(message.sender_email, "a@example.com")
        self.assertEqual(message.body_content, "<p>Complete</p>")
        self.assertEqual(message.raw["changeKey"], "new-change-key")
        self.assertEqual(len(calls), 2)

    def test_disappearing_partial_message_does_not_pin_delta_checkpoint(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = MailroomLedger(Path(temp.name) / "mailroom.db")

        def fetch(url, _headers):
            if "/me/messages/gone?" in url:
                raise urllib.error.HTTPError(url, 404, "gone", {}, None)
            return {
                "value": [{"id": "gone", "changeKey": "deleted-between-requests"}],
                "@odata.deltaLink": "https://gateway.maton.ai/outlook/v1.0/after-gone",
            }

        batch = MatonDeltaIntake(
            ledger=ledger, mailbox="operator@example.com", connection_id="connection",
            api_key="key", fetch_json=fetch,
        ).poll()
        self.assertEqual(len(batch.events), 1)
        self.assertEqual(batch.events[0].event_type, "removed")
        self.assertEqual(batch.events[0].provider_message_id, "gone")
        self.assertEqual(batch.events[0].removal_reason, "hydration_http_404")
        self.assertEqual(
            batch.checkpoint, "https://gateway.maton.ai/outlook/v1.0/after-gone",
        )

    def test_untrusted_continuation_host_fails_closed(self):
        with self.assertRaises(ValueError):
            MatonDeltaIntake._gateway_url("https://evil.example/steal")

    def test_checkpoint_can_be_ignored_for_isolated_catchup(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = MailroomLedger(Path(temp.name) / "mailroom.db")
        ledger.save_checkpoint("maton_delta", "operator@example.com", "inbox", "https://gateway.maton.ai/outlook/v1.0/live")
        urls = []

        def fetch(url, _headers):
            urls.append(url)
            return {"value": [], "@odata.deltaLink": "https://gateway.maton.ai/outlook/v1.0/discard"}

        MatonDeltaIntake(
            ledger=ledger, mailbox="operator@example.com", connection_id="connection",
            api_key="key", fetch_json=fetch, bootstrap_lookback_hours=96,
            use_checkpoint=False,
        ).poll()
        self.assertIn("receivedDateTime", urls[0])
        self.assertEqual(
            ledger.get_checkpoint("maton_delta", "operator@example.com", "inbox"),
            "https://gateway.maton.ai/outlook/v1.0/live",
        )


if __name__ == "__main__":
    unittest.main()
