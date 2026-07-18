"""Fail-closed reconciliation of accepted sends against Outlook Sent Items."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .ledger import MailroomLedger
from .models import MailState
from .reply_guard import normalize_graph_url


GRAPH_ROOT = "https://gateway.maton.ai/outlook/v1.0"
FetchJson = Callable[[str, dict[str, str]], dict[str, Any]]


class SentVerifier(Protocol):
    def find_verified_message(self, item: dict[str, Any]) -> str | None: ...


def account_for_mailbox(mailbox: str) -> str:
    """Use the mailbox as the stable public fingerprint namespace.

    Friendly account aliases are a UI concern and are configured by the
    OpenClaw plugin. The reconciliation engine must not embed deployment-specific
    mailbox mappings.
    """
    normalized = mailbox.strip().lower()
    if "@" not in normalized:
        raise ValueError(f"Invalid mailbox address: {mailbox}")
    return normalized


@dataclass
class MatonSentVerifier:
    connection_id: str
    api_key: str
    fetch_json: FetchJson | None = None
    max_pages: int = 20

    def find_verified_message(self, item: dict[str, Any]) -> str | None:
        conversation_id = item.get("conversation_id")
        expected = item.get("approval_fingerprint")
        accepted_at = item.get("send_accepted_at")
        if not conversation_id or not expected or not accepted_at:
            return None
        since = datetime.fromisoformat(accepted_at.replace("Z", "+00:00")) - timedelta(minutes=5)
        params = {
            "$select": "id,conversationId,sentDateTime,toRecipients,ccRecipients,bccRecipients,subject,body,hasAttachments",
            "$filter": (
                f"sentDateTime ge {since.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} and "
                f"conversationId eq '{conversation_id.replace(chr(39), chr(39) * 2)}'"
            ),
            "$orderby": "sentDateTime desc",
            "$top": "10",
        }
        url: str | None = f"{GRAPH_ROOT}/me/mailFolders/sentitems/messages?{urllib.parse.urlencode(params)}"
        fetch = self.fetch_json or self._fetch_json
        account = account_for_mailbox(item["mailbox"])
        pages = 0
        while url and pages < self.max_pages:
            data = fetch(url, self._headers())
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise ValueError("Sent Items reconciliation response is missing a message value array")
            for message in data["value"]:
                if not isinstance(message, dict) or not message.get("id"):
                    raise ValueError("Sent Items reconciliation contains an invalid message")
                attachments: list[dict[str, Any]] = []
                if message.get("hasAttachments"):
                    attachment_url = (
                        f"{GRAPH_ROOT}/me/messages/{urllib.parse.quote(message['id'], safe='')}/attachments"
                        "?$select=name,size,contentType"
                    )
                    attachment_data = fetch(attachment_url, self._headers())
                    if not isinstance(attachment_data, dict) or not isinstance(attachment_data.get("value"), list):
                        raise ValueError("Sent attachment response is missing a value array")
                    attachments = attachment_data["value"]
                if _fingerprint(account, message, attachments) == expected:
                    return str(message["id"])
            next_link = data.get("@odata.nextLink")
            url = normalize_graph_url(next_link) if next_link else None
            pages += 1
        if url:
            raise RuntimeError(f"Sent Items reconciliation exceeded max_pages={self.max_pages}")
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


def _fingerprint(account: str, message: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
    addresses = sorted(
        str(recipient.get("emailAddress", {}).get("address") or "").lower()
        for field in ("toRecipients", "ccRecipients", "bccRecipients")
        for recipient in message.get(field, [])
        if recipient.get("emailAddress", {}).get("address")
    )
    attachment_rows = sorted(
        f"{attachment.get('name', '')}|{attachment.get('size', '')}|{attachment.get('contentType', '')}"
        for attachment in attachments
    )
    material = (
        f"{account}\n{','.join(addresses)}\n{message.get('subject') or ''}\n"
        f"{(message.get('body') or {}).get('content') or ''}\n{';'.join(attachment_rows)}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class ReconciliationSummary:
    checked: int = 0
    verified: int = 0
    pending: int = 0
    uncertain: int = 0
    unresolved: int = 0
    errors: int = 0


class SendReconciler:
    def __init__(
        self, ledger: MailroomLedger, verifier: SentVerifier, *,
        mailbox: str | None = None, accepted_timeout_hours: int = 2,
    ):
        self.ledger = ledger
        self.verifier = verifier
        self.mailbox = mailbox.lower() if mailbox else None
        self.accepted_timeout = timedelta(hours=accepted_timeout_hours)

    def run(self, limit: int = 20) -> ReconciliationSummary:
        checked = verified = pending = uncertain = unresolved = errors = 0
        items = [
            *self.ledger.list_items(
                state=MailState.SEND_ACCEPTED, run_mode="production", limit=limit,
            ),
            *self.ledger.list_items(
                state=MailState.SEND_OUTCOME_UNKNOWN,
                run_mode="production",
                limit=limit,
            ),
        ][:limit]
        for item in items:
            if self.mailbox and item["mailbox"].lower() != self.mailbox:
                continue
            checked += 1
            try:
                sent_id = self.verifier.find_verified_message(item)
                if sent_id is None:
                    if item["state"] == MailState.SEND_OUTCOME_UNKNOWN.value:
                        unresolved += 1
                        continue
                    accepted_at = datetime.fromisoformat(
                        str(item.get("send_accepted_at") or "").replace("Z", "+00:00")
                    )
                    if accepted_at.tzinfo is None:
                        accepted_at = accepted_at.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - accepted_at >= self.accepted_timeout:
                        self.ledger.transition(
                            item["mail_item_id"], MailState.SEND_OUTCOME_UNKNOWN,
                            actor="sent-reconciler", expected_states=[MailState.SEND_ACCEPTED],
                            patch={"last_error": "Outlook accepted send but exact message was not found in Sent Items before reconciliation deadline"},
                            metadata={"verification": "timed_out_without_fingerprint_match"},
                        )
                        uncertain += 1
                    else:
                        pending += 1
                    continue
                self.ledger.transition(
                    item["mail_item_id"], MailState.SENT_VERIFIED,
                    actor="sent-reconciler",
                    expected_states=[MailState(item["state"])],
                    patch={"sent_message_id": sent_id, "last_error": None},
                    metadata={"sent_message_id": sent_id, "verification": "content_fingerprint"},
                )
                verified += 1
            except Exception as exc:
                errors += 1
                with self.ledger.transaction() as conn:
                    conn.execute(
                        "UPDATE mail_items SET last_error = ?, updated_at = ? WHERE mail_item_id = ?",
                        (f"send reconciliation: {str(exc)[:1900]}", datetime.now(timezone.utc).isoformat(), item["mail_item_id"]),
                    )
        return ReconciliationSummary(
            checked=checked, verified=verified, pending=pending, uncertain=uncertain,
            unresolved=unresolved, errors=errors,
        )
