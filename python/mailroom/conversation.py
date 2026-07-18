"""Read-only Outlook conversation gathering for Mailroom drafting."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .intake import MAIL_SELECT, MATON_GRAPH_ROOT
from .reply_guard import normalize_graph_url


FetchJson = Callable[[str, dict[str, str]], dict[str, Any]]


class ConversationReader(Protocol):
    def get_conversation(self, item: dict[str, Any]) -> list[dict[str, Any]]: ...


@dataclass
class MatonConversationReader:
    connection_id: str
    api_key: str
    fetch_json: FetchJson | None = None
    max_pages: int = 20
    max_messages: int = 100

    def get_conversation(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        conversation_id = item.get("conversation_id")
        if not conversation_id:
            return []
        escaped = str(conversation_id).replace("'", "''")
        params = {
            "$select": MAIL_SELECT,
            "$filter": f"conversationId eq '{escaped}'",
            "$top": "50",
        }
        url: str | None = f"{MATON_GRAPH_ROOT}/me/messages?{urllib.parse.urlencode(params)}"
        fetch = self.fetch_json or self._fetch_json
        messages: list[dict[str, Any]] = []
        pages = 0
        while url and pages < self.max_pages:
            data = fetch(url, self._headers())
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise ValueError("Conversation response is missing a message value array")
            for raw in data["value"]:
                if not isinstance(raw, dict):
                    raise ValueError("Conversation response contains a non-object message")
                if raw.get("conversationId") != conversation_id or raw.get("isDraft") is True:
                    continue
                messages.append(_normalize_message(raw, mailbox=str(item.get("mailbox") or "")))
                if len(messages) > self.max_messages:
                    raise RuntimeError(
                        f"Conversation lookup exceeded max_messages={self.max_messages}"
                    )
            next_link = data.get("@odata.nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise ValueError("Conversation response has an invalid @odata.nextLink")
            url = normalize_graph_url(next_link) if next_link else None
            pages += 1
        if url:
            raise RuntimeError(f"Conversation lookup exceeded max_pages={self.max_pages}")
        return sorted(messages, key=lambda message: message.get("timestamp") or "")

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


def _normalize_message(raw: dict[str, Any], *, mailbox: str) -> dict[str, Any]:
    sender = (raw.get("from") or {}).get("emailAddress") or {}
    body = raw.get("body") or {}
    return {
        "message_id": raw.get("id"),
        "internet_message_id": raw.get("internetMessageId"),
        "conversation_id": raw.get("conversationId"),
        "timestamp": raw.get("receivedDateTime") or raw.get("sentDateTime"),
        "direction": "sent" if str(sender.get("address") or "").lower() == mailbox.lower() else "received",
        "sender_name": sender.get("name"),
        "sender_email": (sender.get("address") or "").lower() or None,
        "subject": raw.get("subject"),
        "to_recipients": _recipients(raw.get("toRecipients")),
        "cc_recipients": _recipients(raw.get("ccRecipients")),
        "body_preview": raw.get("bodyPreview"),
        "body_content": body.get("content"),
        "has_attachments": raw.get("hasAttachments") is True,
    }


def _recipients(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    result = []
    for recipient in raw:
        address = (recipient or {}).get("emailAddress") or {}
        if address.get("address"):
            result.append({"name": address.get("name") or "", "address": address["address"]})
    return result
