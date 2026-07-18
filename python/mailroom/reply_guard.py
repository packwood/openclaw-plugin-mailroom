"""Deterministic Sent Items guard that suppresses drafts after a manual reply."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


GRAPH_ROOT = "https://gateway.maton.ai/outlook/v1.0"
FetchJson = Callable[[str, dict[str, str]], dict[str, Any]]


@dataclass(frozen=True)
class SentReply:
    message_id: str
    sent_at: str
    subject: str | None = None


class ReplyChecker(Protocol):
    def find_reply_after(self, item: dict[str, Any]) -> SentReply | None: ...


@dataclass
class MatonSentReplyChecker:
    connection_id: str
    api_key: str
    fetch_json: FetchJson | None = None
    max_pages: int = 20

    def find_reply_after(self, item: dict[str, Any]) -> SentReply | None:
        conversation_id = item.get("conversation_id")
        received_at = item.get("reply_target_received_at") or item.get("received_at")
        sender_email = (
            item.get("reply_target_sender_email") or item.get("sender_email") or ""
        ).lower()
        if not conversation_id or not received_at or not sender_email:
            raise ValueError(
                "Cannot verify Sent Items without conversation_id, received_at, and sender_email"
            )
        received = _parse_time(received_at)
        escaped_conversation = conversation_id.replace("'", "''")
        params = {
            "$select": "id,conversationId,sentDateTime,subject,isDraft,toRecipients,ccRecipients",
            "$filter": (
                f"sentDateTime gt {received.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} and "
                f"conversationId eq '{escaped_conversation}'"
            ),
            "$orderby": "sentDateTime desc",
            "$top": "10",
        }
        url: str | None = f"{GRAPH_ROOT}/me/mailFolders/sentitems/messages?{urllib.parse.urlencode(params)}"
        fetch = self.fetch_json or self._fetch_json
        pages = 0
        while url and pages < self.max_pages:
            data = fetch(url, self._headers())
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise ValueError("Sent Items response is missing a message value array")
            for message in data["value"]:
                if not isinstance(message, dict):
                    raise ValueError("Sent Items response contains a non-object message")
                if message.get("isDraft") is True or message.get("conversationId") != conversation_id:
                    continue
                recipients = {
                    str(recipient.get("emailAddress", {}).get("address") or "").lower()
                    for field in ("toRecipients", "ccRecipients")
                    for recipient in message.get(field, [])
                }
                if sender_email not in recipients:
                    continue
                sent_at = message.get("sentDateTime")
                message_id = message.get("id")
                if not sent_at or not message_id or _parse_time(sent_at) <= received:
                    continue
                return SentReply(str(message_id), str(sent_at), message.get("subject"))
            next_link = data.get("@odata.nextLink")
            url = normalize_graph_url(next_link) if next_link else None
            pages += 1
        if url:
            raise RuntimeError(f"Sent Items guard exceeded max_pages={self.max_pages}")
        return None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Maton-Connection": self.connection_id,
            "Accept": "application/json",
        }

    @staticmethod
    def _fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def normalize_graph_url(url: str) -> str:
    """Convert trusted Microsoft Graph continuation URLs to the Maton gateway."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ValueError("Unsafe Sent Items continuation URL")
    if parsed.hostname == "graph.microsoft.com":
        path = parsed.path
        if path.startswith("/v1.0"):
            path = path[len("/v1.0"):]
        return urllib.parse.urlunparse((
            "https", "gateway.maton.ai", f"/outlook/v1.0{path}", "", parsed.query, "",
        ))
    if parsed.hostname == "gateway.maton.ai" and parsed.path.startswith("/outlook/v1.0/"):
        return url
    raise ValueError(f"Untrusted Sent Items continuation host: {parsed.hostname or 'missing'}")
