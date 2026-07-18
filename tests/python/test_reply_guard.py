from __future__ import annotations

import unittest

from mailroom.reply_guard import MatonSentReplyChecker


class ReplyGuardTests(unittest.TestCase):
    def test_finds_only_sent_message_after_inbound_in_same_conversation(self):
        def fetch(_url, _headers):
            return {"value": [
                {"id": "wrong-thread", "conversationId": "other", "sentDateTime": "2026-07-12T13:00:00Z", "isDraft": False},
                {"id": "too-old", "conversationId": "conversation", "sentDateTime": "2026-07-12T11:59:00Z", "isDraft": False},
                {"id": "draft", "conversationId": "conversation", "sentDateTime": "2026-07-12T13:00:00Z", "isDraft": True},
                {"id": "forward", "conversationId": "conversation", "sentDateTime": "2026-07-12T13:30:00Z", "subject": "Fwd: Deal", "isDraft": False, "toRecipients": [{"emailAddress": {"address": "other@example.com"}}]},
                {"id": "manual-reply", "conversationId": "conversation", "sentDateTime": "2026-07-12T12:30:00Z", "subject": "Re: Deal", "isDraft": False, "toRecipients": [{"emailAddress": {"address": "sender@example.com"}}]},
            ]}

        checker = MatonSentReplyChecker("connection", "api-key", fetch_json=fetch)
        reply = checker.find_reply_after({
            "conversation_id": "conversation", "received_at": "2026-07-12T12:00:00Z",
            "sender_email": "sender@example.com",
        })
        self.assertEqual(reply.message_id, "manual-reply")
        self.assertEqual(reply.sent_at, "2026-07-12T12:30:00Z")

    def test_missing_correlation_fields_fails_closed(self):
        checker = MatonSentReplyChecker("connection", "api-key", fetch_json=lambda *_: {"value": []})
        with self.assertRaises(ValueError):
            checker.find_reply_after({"conversation_id": "conversation"})

    def test_checked_reply_target_fields_override_original_message_fields(self):
        urls = []

        def fetch(url, _headers):
            urls.append(url)
            return {"value": [{
                "id": "reply", "conversationId": "conversation",
                "sentDateTime": "2026-07-13T12:30:00Z", "isDraft": False,
                "toRecipients": [{"emailAddress": {"address": "latest@example.com"}}],
            }]}

        reply = MatonSentReplyChecker(
            "connection", "api-key", fetch_json=fetch,
        ).find_reply_after({
            "conversation_id": "conversation", "received_at": "2026-07-12T12:00:00Z",
            "sender_email": "original@example.com",
            "reply_target_received_at": "2026-07-13T12:00:00Z",
            "reply_target_sender_email": "latest@example.com",
        })
        self.assertEqual(reply.message_id, "reply")
        self.assertIn("2026-07-13T12%3A00%3A00Z", urls[0])

    def test_follows_trusted_next_link_until_matching_reply(self):
        calls = []

        def fetch(url, _headers):
            calls.append(url)
            if len(calls) == 1:
                return {
                    "value": [],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages?$skip=10",
                }
            return {"value": [{
                "id": "reply", "conversationId": "conversation",
                "sentDateTime": "2026-07-12T12:30:00Z", "isDraft": False,
                "toRecipients": [{"emailAddress": {"address": "sender@example.com"}}],
            }]}

        checker = MatonSentReplyChecker("connection", "api-key", fetch_json=fetch)
        reply = checker.find_reply_after({
            "conversation_id": "conversation", "received_at": "2026-07-12T12:00:00Z",
            "sender_email": "sender@example.com",
        })
        self.assertEqual(reply.message_id, "reply")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith("https://gateway.maton.ai/outlook/v1.0/"))

    def test_malformed_success_response_fails_closed(self):
        checker = MatonSentReplyChecker(
            "connection", "api-key", fetch_json=lambda *_: {"unexpected": []},
        )
        with self.assertRaisesRegex(ValueError, "value array"):
            checker.find_reply_after({
                "conversation_id": "conversation", "received_at": "2026-07-12T12:00:00Z",
                "sender_email": "sender@example.com",
            })


if __name__ == "__main__":
    unittest.main()
