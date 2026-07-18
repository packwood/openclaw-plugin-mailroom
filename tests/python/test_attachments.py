from __future__ import annotations

import unittest

from mailroom.attachments import MatonAttachmentReader


class AttachmentReaderTests(unittest.TestCase):
    def test_lists_full_names_and_follows_trusted_pagination(self):
        calls = []

        def fetch(url, _headers):
            calls.append(url)
            if len(calls) == 1:
                return {
                    "value": [{
                        "name": "Audax NDA revised clean version.docx", "size": 2048,
                        "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "isInline": False,
                    }],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages/id/attachments?$skip=1",
                }
            return {"value": [{"name": "logo.png", "size": 100, "isInline": True}]}

        result = MatonAttachmentReader("connection", "key", fetch_json=fetch).list_attachments({
            "has_attachments": 1, "provider_message_id": "message/id",
        })
        self.assertEqual([item["name"] for item in result], [
            "Audax NDA revised clean version.docx", "logo.png",
        ])
        self.assertTrue(calls[1].startswith("https://gateway.maton.ai/outlook/v1.0/"))

    def test_malformed_success_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "value array"):
            MatonAttachmentReader(
                "connection", "key", fetch_json=lambda *_: {},
            ).list_attachments({"has_attachments": 1, "provider_message_id": "message"})


if __name__ == "__main__":
    unittest.main()
