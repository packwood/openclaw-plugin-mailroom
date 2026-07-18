"""Read-only Outlook attachment metadata for relevant Mailroom messages."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .reply_guard import normalize_graph_url


GRAPH_ROOT = "https://gateway.maton.ai/outlook/v1.0"
FetchJson = Callable[[str, dict[str, str]], dict[str, Any]]


class AttachmentReader(Protocol):
    def list_attachments(self, item: dict[str, Any]) -> list[dict[str, Any]]: ...


@dataclass
class MatonAttachmentReader:
    connection_id: str
    api_key: str
    fetch_json: FetchJson | None = None
    max_pages: int = 20

    def list_attachments(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        if not item.get("has_attachments"):
            return []
        message_id = item.get("provider_message_id")
        if not message_id:
            raise ValueError("Attachment lookup requires provider_message_id")
        url: str | None = (
            f"{GRAPH_ROOT}/me/messages/{urllib.parse.quote(str(message_id), safe='')}/attachments"
            "?$select=name,size,contentType,isInline"
        )
        fetch = self.fetch_json or self._fetch_json
        attachments: list[dict[str, Any]] = []
        pages = 0
        while url and pages < self.max_pages:
            data = fetch(url, self._headers())
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise ValueError("Attachment response is missing a value array")
            for raw in data["value"]:
                if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                    raise ValueError("Attachment response contains invalid metadata")
                attachments.append({
                    "name": raw["name"],
                    "size": raw.get("size") if isinstance(raw.get("size"), int) else None,
                    "content_type": raw.get("contentType"),
                    "is_inline": raw.get("isInline") is True,
                })
            next_link = data.get("@odata.nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise ValueError("Attachment response has an invalid @odata.nextLink")
            url = normalize_graph_url(next_link) if next_link else None
            pages += 1
        if url:
            raise RuntimeError(f"Attachment lookup exceeded max_pages={self.max_pages}")
        return attachments

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
