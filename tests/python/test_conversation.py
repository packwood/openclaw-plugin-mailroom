from __future__ import annotations

import unittest

from mailroom.conversation import MatonConversationReader


class ConversationReaderTests(unittest.TestCase):
    def test_fetches_complete_conversation_sorts_and_marks_direction(self):
        calls = []

        def fetch(url, _headers):
            calls.append(url)
            if len(calls) == 1:
                return {
                    "value": [{
                        "id": "later", "conversationId": "thread",
                        "receivedDateTime": "2026-07-12T14:00:00Z",
                        "from": {"emailAddress": {"address": "other@example.com"}},
                        "body": {"content": "Later"},
                    }],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=1",
                }
            return {"value": [{
                "id": "earlier", "conversationId": "thread",
                "sentDateTime": "2026-07-12T12:00:00Z",
                "from": {"emailAddress": {"address": "operator@example.com"}},
                "body": {"content": "Earlier"},
            }]}

        messages = MatonConversationReader(
            "connection", "key", fetch_json=fetch,
        ).get_conversation({"conversation_id": "thread", "mailbox": "operator@example.com"})
        self.assertEqual([m["message_id"] for m in messages], ["earlier", "later"])
        self.assertEqual([m["direction"] for m in messages], ["sent", "received"])
        self.assertIn("conversationId+eq", calls[0])

    def test_malformed_success_fails_closed(self):
        reader = MatonConversationReader(
            "connection", "key", fetch_json=lambda *_: {"unexpected": []},
        )
        with self.assertRaisesRegex(ValueError, "value array"):
            reader.get_conversation({"conversation_id": "thread", "mailbox": "operator@example.com"})


if __name__ == "__main__":
    unittest.main()
